"""共享测试夹具。

两件重要的事：

1. **所有测试都跑在 tmp_path 下的虚拟 iPod 上**，绝不碰真实设备。
   虚拟 iPod 由 vendored 的 ``create_virtual_ipod()`` 生成，带完整的
   SysInfo / HashInfo / 序列号，和真机的文件布局一致。
2. 测试音频用 ffmpeg 现场生成（含中文标签），不依赖任何预置素材文件。
   ffmpeg 不在时相关测试自动跳过，而不是假通过。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from iopenpod.device import create_virtual_ipod

# MB147 = iPod Classic 6th Gen 80GB Black（零售型号 A1238）
CLASSIC_MODEL = "MB147"


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


ffmpeg_required = pytest.mark.skipif(
    _ffmpeg() is None, reason="需要 ffmpeg 才能生成测试音频"
)


def _run_ffmpeg(args: list[str]) -> None:
    completed = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败：{completed.stderr.strip()}")


def make_cover_jpg(path: Path) -> Path:
    """生成一张纯色方图当封面。"""
    _run_ffmpeg([
        "-f", "lavfi", "-i", "color=c=0x2E5C8A:s=600x600:d=1",
        "-frames:v", "1", str(path),
    ])
    return path


def make_mp3(path: Path, *, title: str, artist: str = "", album: str = "",
             genre: str = "", year: str = "", track: str = "",
             duration: float = 1.0, bitrate: str = "192k",
             with_cover: bool = False) -> Path:
    """生成一个带指定标签的 MP3。"""
    args = [
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
    ]
    if with_cover:
        cover = path.parent / f"{path.stem}-cover.jpg"
        make_cover_jpg(cover)
        args += ["-i", str(cover), "-map", "0:a", "-map", "1:v"]
        args += ["-c:v", "mjpeg", "-id3v2_version", "3"]
        args += ["-metadata:s:v", "title=Album cover",
                 "-metadata:s:v", "comment=Cover (front)"]
    args += ["-codec:a", "libmp3lame", "-b:a", bitrate]
    if title:
        args += ["-metadata", f"title={title}"]
    if artist:
        args += ["-metadata", f"artist={artist}"]
    if album:
        args += ["-metadata", f"album={album}"]
    if genre:
        args += ["-metadata", f"genre={genre}"]
    if year:
        args += ["-metadata", f"date={year}"]
    if track:
        args += ["-metadata", f"track={track}"]
    args.append(str(path))
    _run_ffmpeg(args)
    return path


def make_m4a(path: Path, *, title: str, artist: str = "", album: str = "",
             extension_args: list[str] | None = None) -> Path:
    """生成一个带指定标签的 M4A（AAC）。"""
    args = [
        "-f", "lavfi", "-i", "sine=frequency=523:duration=1.0",
        "-codec:a", "aac", "-b:a", "256k",
        "-metadata", f"title={title}",
    ]
    if artist:
        args += ["-metadata", f"artist={artist}"]
    if album:
        args += ["-metadata", f"album={album}"]
    args += extension_args or []
    args.append(str(path))
    _run_ffmpeg(args)
    return path


def make_flac(path: Path, *, title: str, artist: str = "",
              bits: int = 16, sample_rate: int = 44100) -> Path:
    """生成一个 FLAC（iPod 不能直接播，必须转码）。

    ``bits`` / ``sample_rate`` 用来造高解析样本——网易云的 `lossless` 里
    有相当一部分是 24bit，而 iPod Classic 只支持 16bit ALAC，
    这类样本是那组回归测试的对象。
    """
    args = [
        "-f", "lavfi", "-i",
        f"sine=frequency=349:duration=1.0:sample_rate={sample_rate}",
    ]
    if bits > 16:
        # ffmpeg 的 flac 编码器：s32 输入会落成 24bit FLAC
        # （aformat 不接受 s24——24bit 在 ffmpeg 里是用 s32 承载的）
        args += ["-sample_fmt", "s32"]
    else:
        args += ["-sample_fmt", "s16"]
    args += ["-codec:a", "flac"]
    if title:
        args += ["-metadata", f"title={title}"]
    if artist:
        args += ["-metadata", f"artist={artist}"]
    args.append(str(path))
    _run_ffmpeg(args)
    return path


@pytest.fixture()
def ipod_root(tmp_path: Path) -> Path:
    """一个空的虚拟 iPod 根目录（Classic 6th Gen）。"""
    root = tmp_path / "IPOD"
    create_virtual_ipod(root, CLASSIC_MODEL, ipod_name="测试用 iPod")
    return root


@pytest.fixture()
def device(ipod_root: Path):
    """已识别的设备对象（未注册到内核）。"""
    from ipod_cli.discovery import probe_mount

    found = probe_mount(ipod_root)
    assert found is not None, "虚拟 iPod 应该能被识别"
    return found


@pytest.fixture()
def active_device(device):
    """已注册到内核的设备（写封面需要）。"""
    device.activate()
    return device


@pytest.fixture()
def music_dir(tmp_path: Path) -> Path:
    """专门放源音频的目录，和 iPod 分开，避免互相污染。"""
    path = tmp_path / "music"
    path.mkdir()
    return path


# ──────────────────────────────────────────────────────────────────────
# ipod_web（Flutter 桌面端用的本地后端）测试夹具
#
# **测试模块不要 `from tests.conftest import ...`**：项目里的 `tests/` 不是
# 包，`tests` 这个名字会撞上环境里别的同名包（本机就撞过 Hermes 自己的
# tests），import 会静默拿到别人的模块。所有东西都通过 fixture 暴露。
# ──────────────────────────────────────────────────────────────────────

from types import SimpleNamespace  # noqa: E402
from typing import Any  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from ipod_cli.ncm.state import StateStore  # noqa: E402
from ipod_web.app import create_app  # noqa: E402
from ipod_web.context import WebContext  # noqa: E402
from ipod_web.jobs import JobManager  # noqa: E402

#: 一张最小的合法 PNG（1x1 透明）。测试只关心"是不是有图传回来"，
#: 不关心图长什么样，所以不用真的二维码。
FAKE_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


class FakeNcmClient:
    """假的网易云客户端。

    按脚本返回扫码状态码，并记录请求次数——用来断言"轮询真的在轮询"、
    "请求数没有暴涨"，以及"请求跑在作业线程上"。
    """

    def __init__(
        self,
        codes: list[int] | None = None,
        *,
        cookie: str = "fake-cookie",
        uid: int = 123456,
        nickname: str = "测试账号",
        vip_type: int = 11,
    ) -> None:
        self.codes = list(codes or [])
        self.cookie = cookie
        self.uid = uid
        self.nickname = nickname
        self.vip_type = vip_type
        self.request_count = 0
        self.polled: list[str] = []
        self.login_status_calls = 0
        self.poll_error: Exception | None = None
        #: 每次调用发生在哪个线程。用来断言网络请求真的跑在**作业线程**上，
        #: 而不是路由的请求线程上（后者等于绕过了队列，见 T5）。
        self.threads: list[str] = []
        #: 单独数"取了几次二维码"。request_count 会被轮询推高，
        #: 用它断言"只起了一个登录"是数不准的。
        self.start_calls = 0
        #: 这张假二维码。测试直接比对它，不用另外 import 常量。
        self.png = FAKE_PNG

        #: 歌单列表（真实 Playlist 对象）。用 `make_playlist` 造。
        self.playlists: list[Any] = []
        #: 歌单 ID → 曲目（真实 Song 对象）。
        self.tracks: dict[int, list[Any]] = {}
        #: "我喜欢的音乐"的曲目。
        self.liked_songs: list[Any] = []
        self.liked_ids: list[int] = []
        #: 单独数"读了几次歌单列表"，用来断言缓存真的生效了。
        self.playlist_calls = 0

    def _note_thread(self) -> None:
        import threading

        self.threads.append(threading.current_thread().name)

    def qr_login_start(self) -> tuple[str, bytes]:
        self.request_count += 1
        self.start_calls += 1
        self._note_thread()
        return "fake-unikey", FAKE_PNG

    def qr_login_poll(self, unikey: str) -> tuple[int, str]:
        self.request_count += 1
        self._note_thread()
        self.polled.append(unikey)
        if self.poll_error is not None:
            raise self.poll_error
        code = self.codes.pop(0) if self.codes else 801
        return code, (self.cookie if code == 803 else "")

    def login_status(self) -> Any:
        self.request_count += 1
        self._note_thread()
        self.login_status_calls += 1
        return SimpleNamespace(
            uid=self.uid,
            nickname=self.nickname,
            cookie=self.cookie,
            vip_type=self.vip_type,
        )

    def ping(self) -> bool:
        self.request_count += 1
        self._note_thread()
        return True

    # ── 歌单 / 曲目 ───────────────────────────────────────────────────
    #
    # 用真实的 Song / Playlist 数据类，不用 SimpleNamespace：字段名或形状
    # 变了的时候测试要能跟着红，拿字典糊的话接口改了测试照样绿。

    def user_playlists(self, uid: int, cookie: str = "") -> list[Any]:
        self.request_count += 1
        self._note_thread()
        self.playlist_calls += 1
        return list(self.playlists)

    def liked_song_ids(self, uid: int, cookie: str = "") -> list[int]:
        self.request_count += 1
        self._note_thread()
        return list(self.liked_ids)

    def liked_playlist_id(self, uid: int, cookie: str = "") -> int:
        """「我喜欢的音乐」的歌单 id（specialType=5）。找不到返回 0。

        真实现要打一次 /user/playlist；这里从已设好的 playlists 里找，
        顺便记一次请求数——**测试要能断言"界面没白白多发请求"**。
        """
        self.request_count += 1
        self._note_thread()
        for playlist in self.playlists:
            if playlist.is_liked:
                return playlist.id
        return 0

    def playlist_tracks(
        self, playlist_id: int, expected: int = 0, cookie: str = ""
    ) -> list[Any]:
        self.request_count += 1
        self._note_thread()
        return list(self.tracks.get(playlist_id, []))

    def song_details(self, song_ids: list[int], cookie: str = "") -> list[Any]:
        self.request_count += 1
        self._note_thread()
        by_id = {s.id: s for group in self.tracks.values() for s in group}
        by_id.update({s.id: s for s in self.liked_songs})
        return [by_id[i] for i in song_ids if i in by_id]


@pytest.fixture
def web_store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "web-state.db")


@pytest.fixture
def web_ctx(tmp_path: Path, web_store: StateStore) -> WebContext:
    return WebContext(
        store=web_store,
        jobs=JobManager(),
        cache_dir=tmp_path / "cache",
    )


@pytest.fixture
def web_client(web_ctx: WebContext):
    """FastAPI 测试客户端。用 with 进入才会跑 lifespan（后端收尾逻辑）。"""
    app = create_app(web_ctx)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fast_poll(monkeypatch) -> None:
    """把登录轮询间隔压到 10 毫秒。

    不压的话每个登录用例都要真等 2 秒一轮——测试会慢到没人愿意跑，
    然后就没人跑了，等于没有测试。
    """
    import ipod_web.login as login_mod

    monkeypatch.setattr(login_mod, "POLL_INTERVAL", 0.01)


@pytest.fixture
def info_logging():
    """把 root logger 调到 INFO 并在用例结束后还原。

    真实后端由 ``app._setup_logging`` 做这件事；测试直接 ``create_app``
    绕过了那个入口，root 还是默认的 WARNING，于是 INFO 日志压根不会
    产生"记录"，挂在 root 上的缓冲自然也收不到。

    这不是测试在将就实现——是测试必须复现真实进程的日志配置，
    否则测出来的行为和线上不一样。
    """
    import logging

    root = logging.getLogger()
    original = root.level
    root.setLevel(logging.INFO)
    try:
        yield
    finally:
        root.setLevel(original)


@pytest.fixture
def make_fake_ncm():
    """工厂 fixture：``make_fake_ncm(codes=[801, 802, 803])``。"""

    def _make(codes: list[int] | None = None, **kwargs: Any) -> FakeNcmClient:
        return FakeNcmClient(codes, **kwargs)

    return _make


@pytest.fixture
def make_song():
    """造一首真实的 Song。字段名照抄接口，测试里不带字典。"""
    from ipod_cli.ncm.client import Song

    def _make(song_id: int, name: str = "", artist: str = "测试歌手") -> Any:
        return Song(
            id=song_id,
            name=name or f"歌曲{song_id}",
            artists=[artist] if artist else [],
            album="测试专辑",
            duration_ms=200_000,
        )

    return _make


@pytest.fixture
def make_playlist():
    """造一个真实的 Playlist。``liked=True`` 时是"我喜欢的音乐"。"""
    from ipod_cli.ncm.client import Playlist

    def _make(
        playlist_id: int,
        name: str = "",
        *,
        track_count: int = 0,
        liked: bool = False,
    ) -> Any:
        return Playlist(
            id=playlist_id,
            name=name or f"歌单{playlist_id}",
            track_count=track_count,
            special_type=5 if liked else 0,
        )

    return _make


@pytest.fixture
def patch_clients(monkeypatch):
    """把所有建客户端的路都换成假的。

    `run_login` 里建客户端有**两个地方**：轮询用的 `ctx.client()`，
    和拿账号信息用的 `ipod_cli.ncm.client.NcmClient(...)`。只换一个的话
    另一半会去打真网络——测试就变成"有时候能过"，而且会真消耗配额。
    """

    def _patch(ctx: WebContext, fake: FakeNcmClient) -> FakeNcmClient:
        monkeypatch.setattr(ctx, "client", lambda: fake)
        monkeypatch.setattr("ipod_cli.ncm.client.NcmClient", lambda *a, **k: fake)
        return fake

    return _patch

# ──────────────────────────────────────────────────────────────────────
# 真实数据保护
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def _protect_real_data():
    """跑测试前后对一遍**真实的** `.ncm/cache`，**文件被删掉**就报错。

    为什么需要：`WebContext.cache_dir` 的默认值是**相对路径**
    ``.ncm/cache``，而 pytest 的 cwd 就是仓库根目录。哪个测试忘了传
    `cache_dir` 又恰好打了 `/api/cache/clear`，删掉的就是用户真实的
    下载缓存——而且静默无声，没人会知道。

    ★ 判据只看"**东西少了**"，不管新增和改动。
    第一版比对整个 `.ncm/` 的内容，结果应用正跑着的时候自己会写
    `.ncm/logs/`，被当成"测试动了数据"报了个假警，白白查了半天。
    保护网只该管**不可逆的损失**（文件没了），别的都可能是应用在正常干活。
    """
    repo = Path(__file__).resolve().parent.parent
    cache = repo / ".ncm" / "cache"

    def snapshot() -> dict[str, int]:
        if not cache.is_dir():
            return {}
        return {
            str(item.relative_to(cache)): item.stat().st_size
            for item in cache.rglob("*")
            if item.is_file()
        }

    before = snapshot()
    yield
    after = snapshot()

    gone = sorted(set(before) - set(after))
    if gone:
        raise AssertionError(
            "测试删掉了真实的 .ncm/cache 里的文件——那是用户的数据，测试不该碰。\n"
            f"  消失的文件：{gone}\n"
            "  查一下是不是哪个测试的 WebContext 漏传了 cache_dir。"
        )

    shrunk = sorted(
        name for name in set(before) & set(after) if after[name] < before[name]
    )
    if shrunk:
        raise AssertionError(f"测试改小了真实的缓存文件：{shrunk}")
