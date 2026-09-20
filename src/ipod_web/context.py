"""后端的共享上下文：作业队列、状态库、设备缓存的持有者。

路由函数拿到的是这个对象，而不是各自去 new 一个——**整个进程只能有
一个 JobManager**，多一个就等于多一条并发路径，「绝不并发」当场失效。
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ipod_cli.discovery import DeviceNotFoundError, require_ipod
from ipod_cli.library import LibraryData, read_library
from ipod_cli.ncm.client import DEFAULT_BASE_URL, QUALITY_LABEL, NcmClient
from ipod_cli.ncm.state import StateStore
from ipod_web.jobs import JobManager
from ipod_web.logbuf import LogBuffer

if TYPE_CHECKING:
    from ipod_web.login import LoginSession

#: 设备库缓存的默认有效期（秒）
#:
#: 状态条每几秒轮询一次，每次都重读 iTunesDB 并解析全部曲目太浪费——
#: 6000 首的库每次解析要几百毫秒，白烧 CPU。5 秒内的重复读走缓存。
LIBRARY_CACHE_SECONDS = 5.0

#: 设置项的键名与默认值。
#:
#: 默认值是 P0 实测拍出来的：全库无损装不下（144GB > 79.6GB），所以默认
#: `exhigh`（320k，全库 40GB 装得下）；间隔 0.35s 是"够慢不会被风控"的
#: 下限，**界面上不允许调到比它更低**。
DEFAULT_SETTINGS: dict[str, str] = {
    "quality": "exhigh",
    "min_interval": "0.35",
}

#: 请求间隔的硬下限。界面里输入框的下界、后端的兜底都用它。
#:
#: 这是安全阀，不是偏好项：间隔调太小 = 高频请求 = 网易云风控封号。
MIN_INTERVAL_FLOOR = 0.2

#: 歌单元数据缓存时长（秒）。
#:
#: 10 分钟：够覆盖"来回翻几个歌单"的正常浏览，又不至于让新加的歌
#: 半天不出现。界面另有「刷新」按钮显式绕过它。
PLAYLIST_CACHE_SECONDS = 600.0


@dataclass
class NeteaseServiceStatus:
    """本地网易云 API 服务（Node）的连通性。"""

    reachable: bool
    base_url: str
    detail: str = ""


class WebContext:
    """后端运行时状态。"""

    def __init__(
        self,
        *,
        store: StateStore | None = None,
        jobs: JobManager | None = None,
        base_url: str = DEFAULT_BASE_URL,
        ipod_path: str | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.store = store or StateStore()
        # 作业日程放在状态库**旁边**（同一个 .ncm/logs/）——它俩是一对，
        # 分开配置迟早会出现"库在这边、日程在那边"的错位。
        # 传了 jobs 就用传的（测试都走这条，不落盘）。
        self.jobs = jobs or JobManager(
            history_path=self.store.path.parent / "logs" / "jobs.jsonl"
        )
        self.log_buffer = LogBuffer()
        self.base_url = base_url
        self.ipod_path = ipod_path
        self.cache_dir = Path(cache_dir) if cache_dir else Path(".ncm") / "cache"
        self.started_at = time.time()

        #: 正在进行的扫码登录。同一时刻只允许有一个。
        self.login: LoginSession | None = None

        self._library: LibraryData | None = None
        self._library_at = 0.0

        #: 网易云歌单列表缓存 (时间, 数据)。
        #:
        #: 浏览歌单不该反复打接口——那是在消耗有风控的配额。10 分钟内
        #: 重复进歌单页直接走缓存，界面给显式「刷新」按钮绕过。
        self._playlists: tuple[float, list[dict[str, Any]]] | None = None

        #: 每个歌单的曲目缓存 (时间, 曲目)。**只缓存元数据，不缓存同步状态**——
        #: 状态会因为下载/同步而变，缓存了就会显示"未处理"而其实已经下好了。
        self._tracks: dict[int, tuple[float, list[Any]]] = {}

    # ── 网易云歌单缓存 ────────────────────────────────────────────────

    def cached_playlists(self) -> list[dict[str, Any]] | None:
        if self._playlists is None:
            return None
        at, data = self._playlists
        if (time.monotonic() - at) > PLAYLIST_CACHE_SECONDS:
            return None
        return data

    def set_playlists(self, data: list[dict[str, Any]]) -> None:
        self._playlists = (time.monotonic(), data)

    def cached_tracks(self, playlist_id: int) -> list[Any] | None:
        entry = self._tracks.get(playlist_id)
        if entry is None:
            return None
        at, data = entry
        if (time.monotonic() - at) > PLAYLIST_CACHE_SECONDS:
            return None
        return data

    def set_tracks(self, playlist_id: int, tracks: list[Any]) -> None:
        self._tracks[playlist_id] = (time.monotonic(), tracks)

    def invalidate_playlists(self) -> None:
        self._playlists = None

    # ── 设置 ──────────────────────────────────────────────────────────

    def get_setting(self, key: str) -> str:
        return self.store.get_setting(key, DEFAULT_SETTINGS.get(key, ""))

    def all_settings(self) -> dict[str, Any]:
        """给界面的设置快照。**带上取值范围**，免得 Dart 侧再抄一份常量。"""
        quality = self.get_setting("quality")
        interval = self.min_interval()
        return {
            "quality": quality,
            "quality_label": QUALITY_LABEL.get(quality, quality),
            "quality_options": [
                {"value": value, "label": label}
                for value, label in QUALITY_LABEL.items()
            ],
            "min_interval": interval,
            "min_interval_floor": MIN_INTERVAL_FLOOR,
        }

    def min_interval(self) -> float:
        """请求间隔（秒）。**兜住下界**——设置库里被写歪了也不能真放出去。"""
        raw = self.get_setting("min_interval")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(DEFAULT_SETTINGS["min_interval"])
        return max(value, MIN_INTERVAL_FLOOR)

    def set_setting(self, key: str, value: str) -> None:
        self.store.set_setting(key, value)

    # ── 网易云客户端 ──────────────────────────────────────────────────

    def client(self) -> NcmClient:
        """按**当前账号和设置**建一个客户端。

        每次新建而不是复用同一个实例：cookie 会随切账号变化，
        共用一个实例很容易出现"界面切了账号、请求还在用旧 cookie"
        这种极难查的 bug。
        """
        account = self.store.active_account()
        return NcmClient(
            self.base_url,
            cookie=account.cookie if account else "",
            min_interval=self.min_interval(),
        )

    def active_cookie(self) -> str:
        account = self.store.active_account()
        return account.cookie if account else ""

    # ── 缓存 ──────────────────────────────────────────────────────────

    def cache_info(self) -> dict[str, Any]:
        """本地下载缓存的大小与数量。

        顺带报出**状态库里的记录数**：两者不一致说明缓存被外部动过
        （手删了文件、或者从别处拷来的），界面应该提示而不是假装没事。
        """
        files = 0
        total = 0
        if self.cache_dir.is_dir():
            for item in self.cache_dir.iterdir():
                if item.is_file():
                    files += 1
                    try:
                        total += item.stat().st_size
                    except OSError:
                        pass
        from ipod_cli.discovery import human_size

        return {
            "dir": str(self.cache_dir),
            "files": files,
            "bytes": total,
            "size_text": human_size(total) if total else "0 B",
        }

    # ── 设备 ──────────────────────────────────────────────────────────

    def device(self):
        """找 iPod。找不到抛 ``DeviceNotFoundError``。

        ★ 找到之后**顺手注册给内核**（``device.activate()``）。

        不注册的后果实测踩到了：写库时内核认为"设备未知"，于是拒绝写
        封面库并抛

          ArtworkDB write failed: No artwork format definitions are
          available for this iPod; cannot write ArtworkDB safely

        整个同步就失败了。内核"宁可不写也不猜错格式"是对的
        （猜错会污染封面库），所以责任在我们：必须告诉它是哪台设备。

        CLI 的同步路径自己调了 ``activate()``（``sync_cli.py``），
        而**界面这条路一次都没调**——同一个坑在两处，界面是新的所以就中招了。
        统一放这儿，省得以后再漏。

        只读操作（看设备信息、列曲目）也一起注册了：代价是一次全局赋值，
        换来的是"任何拿到设备的地方内核都知道是谁"，不会因为少调一次就炸。
        """
        device = require_ipod(self.ipod_path)
        device.activate()
        return device

    def library(self, *, max_age: float = LIBRARY_CACHE_SECONDS,
                force: bool = False) -> LibraryData:
        """读设备库（带缓存）。

        **写设备之后必须 force=True**，否则会拿着脏缓存去算删除计划——
        那个后果是真的删错歌。
        """
        now = time.monotonic()
        if (
            not force
            and self._library is not None
            and (now - self._library_at) < max_age
        ):
            return self._library

        device = self.device()
        library = read_library(device.root)
        self._library = library
        self._library_at = now
        return library

    def invalidate_library(self) -> None:
        """让设备缓存失效。任何写设备的操作之后都要调。"""
        self._library = None
        self._library_at = 0.0

    # ── 网易云服务 ────────────────────────────────────────────────────

    def netease_service(self, timeout: float = 0.3) -> NeteaseServiceStatus:
        """探测本地 Node API 服务在不在。

        **只做 TCP 连接，不发任何 API 请求**——既省时间，也绝不会
        因为"状态条轮询"而白白消耗网易云的请求配额（那条配额是有风控的）。
        """
        host, port = _split_host_port(self.base_url)
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return NeteaseServiceStatus(True, self.base_url, "服务正常")
        except OSError as exc:
            return NeteaseServiceStatus(
                False, self.base_url,
                f"连不上 {host}:{port}（{exc.__class__.__name__}）",
            )

    # ── 账号 ──────────────────────────────────────────────────────────

    def account_summary(self) -> dict[str, Any]:
        """当前登录账号的摘要。**不碰网络**，只读状态库。"""
        account = self.store.active_account()
        if account is None:
            return {
                "logged_in": False,
                "nickname": "",
                "uid": 0,
                "vip": False,
                "account_count": len(self.store.list_accounts()),
            }
        return {
            "logged_in": True,
            "nickname": account.nickname,
            "uid": account.uid,
            "vip": bool(account.vip_type),
            "account_count": len(self.store.list_accounts()),
        }

    # ── 收尾 ──────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        self.jobs.shutdown(wait=False)


def _split_host_port(url: str) -> tuple[str, int]:
    """从 base_url 里抠出 host 和 port。"""
    rest = url.split("://", 1)[-1]
    host_port = rest.split("/", 1)[0]
    if ":" in host_port:
        host, _, port_text = host_port.partition(":")
        try:
            return host, int(port_text)
        except ValueError:
            return host, 80
    return host_port, 80


__all__ = [
    "DeviceNotFoundError",
    "NeteaseServiceStatus",
    "WebContext",
]
