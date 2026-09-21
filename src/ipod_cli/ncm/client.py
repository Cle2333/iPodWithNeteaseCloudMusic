"""网易云 API 客户端。

调的是本地跑的 `api-enhanced` 服务（默认 4000 端口），不是直连网易云。

## 这个模块的第一职责是"别把账号搞封"

所以有几条**刻意的设计**：

1. **绝不并发**。所有请求串行，请求之间强制间隔（默认 0.35s）。
   前端是 Web 应用，天然容易写成并发——但这里没有 async、没有线程池，
   一个客户端实例同一时刻只有一个请求在飞。
2. **限速退避**。网易云限速的表现很阴——**HTTP 200 且 code=200，
   但数据体是空的**。所以"空结果"按可疑处理，退避重试，
   而不是当成"这歌单就是空的"（P0 时我因此漏了 2184 条数据）。
3. **请求计数**。每次调用都记数，调用方能随时问"这一趟发了多少次请求"，
   前端也能显示出来。看不到的量管不住。
4. **档位阶梯只能往下走**。请求 `lossless` 拿不到可以退到 `exhigh`，
   但绝不"往上够"——`hires`/`jymaster`/`jyeffect` 是 24bit+ 或空间音频，
   iPod Classic 播不了，拿到了也是废文件（P0 实测踩过）。

## 下载不走这里

音频文件是从 `music.126.net` 的 CDN 直接拉的，**不经过 API**，
所以不计入风控压力。真正需要克制的是 API 调用次数。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ipod_cli.ncm.netutil import read_with_deadline
from ipod_cli.ncm.state import Account

#: 本地 api-enhanced 服务地址
DEFAULT_BASE_URL = "http://127.0.0.1:4000"

#: 两次请求之间的最小间隔（秒）。
#: 0.35s ≈ 每秒不到 3 次请求，对个人账号来说非常保守。
DEFAULT_MIN_INTERVAL = 0.35

#: 音质档位，从高到低。**只列 iPod Classic 能播的**。
QUALITY_LADDER = ("lossless", "exhigh", "standard")

#: 中文说明，给界面用
QUALITY_LABEL = {
    "standard": "标准 (128k)",
    "exhigh": "极高 (320k)",
    "lossless": "无损 (FLAC)",
}

#: 默认档位。用户定的：前端可选，默认 exhigh。
DEFAULT_QUALITY = "exhigh"

#: 一次请求最多重试几次（含首次）
MAX_ATTEMPTS = 4


class NcmError(RuntimeError):
    """调网易云接口出的问题。"""


class NotLoggedInError(NcmError):
    """没有可用 cookie，或 cookie 已失效。"""


@dataclass
class Song:
    """一首网易云的歌。字段名照抄接口，少转换少出错。"""

    id: int
    name: str
    artists: list[str] = field(default_factory=list)
    album: str = ""
    album_id: int = 0
    cover_url: str = ""
    duration_ms: int = 0
    track_no: int = 0
    #: 0 免费 / 1 VIP / 4 付费专辑 / 8 低码率免费……接口原值
    fee: int = 0

    @property
    def artist_text(self) -> str:
        """艺人拼成一行。**艺人名可能是 null**，不能直接 join。"""
        return "/".join(self.artists)

    @property
    def label(self) -> str:
        return f"{self.name} - {self.artist_text}" if self.artist_text else self.name

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Song:
        al = raw.get("al") or {}
        # 艺人名可能是显式 null（P0 实测踩到过），过滤掉空的——
        # 否则 artist_text 会拼出 "/某某" 这种前导斜杠，直接写进 ID3 就是脏数据
        artists = [
            name
            for name in ((a.get("name") or "").strip() for a in (raw.get("ar") or []))
            if name
        ]
        return cls(
            id=raw.get("id") or 0,
            name=raw.get("name") or "",
            artists=artists,
            album=al.get("name") or "",
            album_id=al.get("id") or 0,
            cover_url=al.get("picUrl") or "",
            duration_ms=raw.get("dt") or 0,
            track_no=raw.get("no") or 0,
            fee=raw.get("fee") if raw.get("fee") is not None else 0,
        )


@dataclass
class Playlist:
    id: int
    name: str
    track_count: int
    creator: str = ""
    subscribed: bool = False
    #: 5 = "我喜欢的音乐"（用户可能改过名字）
    special_type: int = 0

    @property
    def is_liked(self) -> bool:
        return self.special_type == 5


@dataclass
class SongUrl:
    """一首歌拿到的下载链接。"""

    song_id: int
    url: str = ""
    level: str = ""
    bitrate: int = 0
    size: int = 0
    #: 是不是**试听片段**（通常 30 秒）。
    #:
    #: 网易云在"没带 cookie"或"这首歌你只能试听"时会返回试听链接：
    #: 码率降到 128k、体积很小、`freeTrialInfo` 里有 `end: 30`。
    #: **绝不能把它当成完整歌曲下下来**——文件能播、标签正常、
    #: 所有校验都过，唯一的症状是"时长不对"，极难发现。
    trial: bool = False

    @property
    def available(self) -> bool:
        """有 URL 且**是完整歌曲**才算能用。"""
        return bool(self.url) and not self.trial


def http_transport(url: str, timeout: int) -> dict[str, Any]:
    """真实的 HTTP 传输。测试里会被替换掉，见 tests/test_ncm_client.py。"""
    request = urllib.request.Request(url, headers={"User-Agent": "ipod-cli/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # ★ 本地 API 服务也可能卡住不返回（node 那边在等网易云）。
            #   socket 超时管不住总时长，所以自己掐（见 netutil 的说明）。
            body = read_with_deadline(response, seconds=60.0).decode(
                "utf-8", "replace"
            )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise NcmError(f"接口返回 HTTP {exc.code}：{detail}") from exc
    except Exception as exc:  # 网络层什么都可能抛
        raise NcmError(f"连不上网易云 API 服务：{type(exc).__name__}: {exc}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise NcmError(f"接口返回的不是 JSON：{body[:200]}") from exc


class NcmClient:
    """网易云 API 客户端。

    ``transport`` 是给测试用的注入口——测试注入假传输层，
    **一次真网络请求都不发**（保护账号，也保证测试可重复）。
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        cookie: str = "",
        min_interval: float = DEFAULT_MIN_INTERVAL,
        timeout: int = 30,
        transport: Callable[[str, int], dict[str, Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookie = cookie
        self.min_interval = min_interval
        self.timeout = timeout
        self._transport = transport or http_transport
        self._sleep = sleep
        self._clock = clock
        #: 上一次请求的时刻。None = 还没发过请求——**首次不该等**，
        #: 用 0.0 当初始值会让第一次请求白等一个间隔（还得靠 monotonic
        #: 恰好是个大数才不出问题，属于碰巧不炸）。
        self._last_request_at: float | None = None
        #: 累计发出的请求数。调用方可以据此控制节奏/展示给用户。
        self.request_count = 0

    # ── 底层：限速 + 重试 ────────────────────────────────────────────

    def _throttle(self) -> None:
        """保证两次请求之间至少隔 min_interval 秒。

        这是"保护账号"的第一道闸——前端是 Web 应用，很容易不小心写成
        并发/循环狂发，这里用串行 + 强制间隔把最容易出事的路径堵死。
        """
        if self.min_interval <= 0 or self._last_request_at is None:
            return
        elapsed = self._clock() - self._last_request_at
        remaining = self.min_interval - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def _mark_request(self, *, count: bool = True) -> None:
        """标记"刚发过一次请求"。

        所有地方都必须走这里，否则会混进第二个时钟源（time.monotonic），
        限速就形同虚设。

        ``count=False`` 用于封面这类 CDN 请求：**它照样占用限速窗口**
        （避免瞬时并发），但不算进"API 请求数"——那个数字是给用户看
        账号风控压力的，混进 CDN 请求就不准了。
        """
        self._last_request_at = self._clock()
        if count:
            self.request_count += 1

    def _request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发一次请求（串行、限速）。

        这里**不做重试** —— 重试策略由调用方按"什么算异常"决定，
        因为不同接口的可疑信号不一样（歌单接口看 songs 是否为空，
        歌曲详情接口看 songs 长度对不对）。
        """
        query = {k: v for k, v in (params or {}).items() if v is not None}
        query["timestamp"] = int(time.time() * 1000)
        if self.cookie and "cookie" not in query:
            query["cookie"] = self.cookie

        url = f"{self.base_url}{path}?{urllib.parse.urlencode(query)}"
        self._throttle()
        try:
            return self._transport(url, self.timeout)
        finally:
            self._mark_request()

    def _request_with_retry(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        expect: Callable[[dict[str, Any]], bool] | None = None,
        what: str = "数据",
    ) -> dict[str, Any]:
        """带退避重试的请求。

        ``expect`` 判断响应"看起来正常吗"。不满足就退避重试——
        网易云限速时返回 200 + 空数据，只有靠这个判据才抓得住。
        """
        last: dict[str, Any] = {}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                last = self._request(path, params)
            except NcmError:
                if attempt == MAX_ATTEMPTS:
                    raise
                self._sleep(min(0.5 * 2 ** (attempt - 1), 4.0))
                continue

            if expect is None or expect(last):
                return last

            if attempt < MAX_ATTEMPTS:
                # 退避：0.8 / 1.6 / 3.2 秒。限速是"别急"，不是"重试就能过"。
                self._sleep(min(0.8 * 2 ** (attempt - 1), 4.0))

        raise NcmError(f"连续 {MAX_ATTEMPTS} 次都没拿到{what}（可能被限速了，稍后再试）")

    # ── 服务健康 ────────────────────────────────────────────────────

    def ping(self) -> bool:
        """本地 API 服务活着吗。不发业务请求。"""
        try:
            self._request("/login/qr/key", {"timestamp": int(time.time() * 1000)})
            return True
        except NcmError:
            return False

    # ── 登录（扫码 + 切换账号） ──────────────────────────────────────

    def qr_login_start(self) -> tuple[str, bytes]:
        """开始扫码登录。返回 (unikey, 二维码 PNG 字节)。"""
        key_resp = self._request("/login/qr/key")
        unikey = ((key_resp.get("data") or {}) or {}).get("unikey")
        if not unikey:
            raise NcmError("拿不到登录二维码的 key，接口返回异常")

        create = self._request("/login/qr/create", {"key": unikey, "qrimg": "true"})
        qrimg = ((create.get("data") or {}) or {}).get("qrimg") or ""
        if not qrimg.startswith("data:image"):
            raise NcmError("拿不到二维码图，接口返回异常")
        _, _, payload = qrimg.partition(",")
        return unikey, base64.b64decode(payload)

    def qr_login_poll(self, unikey: str) -> tuple[int, str]:
        """轮询扫码状态。

        返回 (状态码, cookie)。状态码含义：

        ===== ==========================
        800   二维码已过期
        801   等待扫码
        802   已扫描，等手机上确认
        803   成功（此时的 cookie 可用）
        ===== ==========================
        """
        resp = self._request("/login/qr/check", {"key": unikey})
        code = resp.get("code") or 0
        return code, (resp.get("cookie") or "")

    def login_status(self, cookie: str = "") -> Account:
        """查 cookie 对应哪个账号（也用来验证 cookie 还有效）。"""
        use = cookie or self.cookie
        if not use:
            raise NotLoggedInError("没有 cookie，先登录")
        resp = self._request("/login/status", {"cookie": use})
        data = resp.get("data") or {}
        account = data.get("account") or {}
        if not account.get("id"):
            raise NotLoggedInError("cookie 已失效，需要重新扫码登录")
        return Account(
            uid=account["id"],
            nickname=account.get("userName") or "",
            cookie=use,
            vip_type=account.get("vipType") or 0,
        )

    # ── 歌单 ────────────────────────────────────────────────────────

    def user_playlists(self, uid: int, cookie: str = "") -> list[Playlist]:
        """某账号的歌单（含收藏）。"""
        resp = self._request_with_retry(
            "/user/playlist",
            {"uid": uid, "cookie": cookie or self.cookie, "limit": 100},
            expect=lambda r: bool(r.get("playlist")),
            what="歌单列表",
        )
        out: list[Playlist] = []
        for raw in resp.get("playlist") or []:
            out.append(
                Playlist(
                    id=raw.get("id") or 0,
                    name=raw.get("name") or "",
                    track_count=raw.get("trackCount") or 0,
                    creator=((raw.get("creator") or {}).get("nickname")) or "",
                    subscribed=bool(raw.get("subscribed")),
                    special_type=raw.get("specialType") or 0,
                )
            )
        return out

    def liked_playlist_id(self, uid: int, cookie: str = "") -> int:
        """「我喜欢的音乐」在网易云那边的**歌单 id**（specialType=5）。拿不到返回 0。

        为什么要它：`/likelist` 只给"有哪些歌"，**顺序是网易云内部的、
        对外不可解释**——实测 246 首，跟 App 显示的顺序同位置零匹配。
        拿到歌单 id 就能走 `/playlist/track/all`，那是用户在 App 里
        看到的顺序（而且顺带带回元数据，更省请求）。

        注意名字可能被用户改过（实测改成了"被改过名的喜欢的音乐"），
        所以只能认 ``specialType``，**不能按名字找**。
        """
        for playlist in self.user_playlists(uid, cookie=cookie):
            if playlist.is_liked:
                return playlist.id
        return 0

    def liked_song_ids(self, uid: int, cookie: str = "") -> list[int]:
        """「我喜欢的音乐」全部歌曲 ID。

        ★ **顺序不可信**：这份列表只保证"有哪些歌"，顺序是网易云内部存储的，
        跟 App 里显示的顺序对不上（实测 246 首同位置零匹配）。
        要按 App 的顺序取歌，用 :meth:`liked_playlist_id` + :meth:`playlist_tracks`。

        这里保留它做兜底（拿不到歌单 id 时至少还有歌）和历史调用方的兼容。
        """
        resp = self._request_with_retry(
            "/likelist",
            {"uid": uid, "cookie": cookie or self.cookie},
            expect=lambda r: "ids" in r,
            what="我喜欢的音乐",
        )
        return [int(i) for i in (resp.get("ids") or [])]

    def playlist_tracks(
        self, playlist_id: int, expected: int = 0, cookie: str = ""
    ) -> list[Song]:
        """取歌单全部曲目（自动翻页）。

        接口单次上限 1000，且**被限速时会返回空数组**——
        这里按可疑处理并重试，不把空结果当成"歌单是空的"。
        """
        use = cookie or self.cookie
        songs: list[Song] = []
        offset = 0
        while True:
            collected = len(songs)

            def looks_ok(resp: dict[str, Any], got: int = collected) -> bool:
                # 空响应只有在"已经收够了"时才算正常；否则按限速处理去重试
                if resp.get("songs"):
                    return True
                return bool(expected) and got >= expected

            resp = self._request_with_retry(
                "/playlist/track/all",
                {"id": playlist_id, "cookie": use, "limit": 1000, "offset": offset},
                expect=looks_ok,
                what=f"歌单 {playlist_id} 的曲目",
            )
            batch = resp.get("songs") or []
            if not batch:
                break
            songs.extend(Song.from_api(s) for s in batch)
            offset += len(batch)
            if len(batch) < 1000 or (expected and offset >= expected):
                break
        return songs

    # ── 歌曲 ────────────────────────────────────────────────────────

    def song_details(self, song_ids: list[int], cookie: str = "") -> list[Song]:
        """批量补歌曲元数据。一次问 100 首。

        判据只要求"非空"：**不可以要求数量完全对上**——下架/无版权的歌
        本来就不会返回，那会让重试逻辑误判成限速，白等 4 轮还报错。
        拿不到的那些由调用方按 ID 比对发现。
        """
        if not song_ids:
            return []
        use = cookie or self.cookie
        out: list[Song] = []
        for start in range(0, len(song_ids), 100):
            batch = song_ids[start:start + 100]
            resp = self._request_with_retry(
                "/song/detail",
                {"ids": ",".join(str(i) for i in batch), "cookie": use},
                expect=lambda r: bool(r.get("songs")),
                what=f"{len(batch)} 首歌的详情",
            )
            out.extend(Song.from_api(s) for s in resp.get("songs") or [])
        return out

    def song_url(self, song_id: int, level: str = DEFAULT_QUALITY,
                 cookie: str = "") -> SongUrl:
        """取下载链接。

        从 ``level`` 开始**逐级往下**试（拿不到无损就用 320k，
        再不行就 128k）。**不会往上够**——`hires`/`jymaster`/`jyeffect`
        是 24bit+ 或空间音频，iPod Classic 播不了。
        """
        if level not in QUALITY_LADDER:
            raise ValueError(
                f"音质只能是 {QUALITY_LADDER} 之一，收到 {level!r}"
                "（对 iPod 无意义的档位不在候选里）"
            )
        ladder = QUALITY_LADDER[QUALITY_LADDER.index(level):]
        use = cookie or self.cookie
        trial_seen: SongUrl | None = None

        for candidate in ladder:
            # **必须走重试路径**：接口偶尔会返回"code=200 但 data 为空"。
            # 不重试的话会被当成"这个档位拿不到"，于是静默降级到低档——
            # 用户拿到的就是 128k 而不是 320k，而唯一的症状只是"文件偏小"，
            # 根本查不出来。实测在批量下载时就命中过这种情况。
            #
            # 注意判据是"data 为空"而不是"url 为空"：
            # data 里有条目但 url 是 null，是**合法的"这首歌没有"**，
            # 那种情况重试多少次都一样，交给下一档即可。
            resp = self._request_with_retry(
                "/song/url/v1",
                {"id": song_id, "level": candidate, "cookie": use,
                 "unblock": "false"},
                expect=lambda r: bool(r.get("data")),
                what=f"歌曲 {song_id} 的 {candidate} 链接",
            )
            items = resp.get("data") or []
            if not items:
                continue
            item = items[0]
            if not item.get("url"):
                continue

            result = SongUrl(
                song_id=song_id,
                url=item["url"],
                level=item.get("level") or candidate,
                bitrate=item.get("br") or 0,
                size=item.get("size") or 0,
                trial=bool(item.get("freeTrialInfo")),
            )
            if not result.trial:
                return result
            # 试听片段先记着，继续往下试——万一低档位给的是完整版呢
            # （不常见，但试一下不花什么代价）。全都只有试听的话，
            # 把这个试听结果返回出去，让调用方去拒绝。
            trial_seen = trial_seen or result

        return trial_seen or SongUrl(song_id=song_id)

    # ── 封面 ────────────────────────────────────────────────────────

    def cover_bytes(self, cover_url: str, size: int = 600) -> bytes:
        """下封面。``size`` 是边长，网易云用 ``?param=NxN`` 控制。

        600×600 是实测过的——476KB 左右，塞进 ID3/FLAC 都不算负担，
        在 iPod 的屏幕上足够清晰。
        """
        if not cover_url:
            return b""
        url = f"{cover_url}?param={size}y{size}"
        # 封面在 CDN 上，不算 API 请求，但仍然串行，避免瞬时并发
        self._throttle()
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "ipod-cli/0.1"}
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                # ★ 同样要掐总时长（同 netutil 的说明）。封面拿不到不该
                #   阻断整首歌，所以这里超时就返回空 —— 但**必须真的超时**，
                #   不能永远等下去把整个同步拖死。
                return read_with_deadline(response, seconds=60.0)
        except Exception:
            # 封面拿不到不该阻断整首歌的下载——没有封面照样能播
            return b""
        finally:
            self._mark_request(count=False)
