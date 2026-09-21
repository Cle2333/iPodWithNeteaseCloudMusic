"""下载歌曲：音频 + 写标签 + 内嵌封面。

## 产出的文件是"半成品"，必须补两样东西

P0 实测：网易云给的 FLAC **一个标签都没有**（MP3 倒是完整）。
两者都**没有内嵌封面**。所以：

1. **标签**：一律按接口元数据重写（不依赖文件自带的），
   这样无论拿到的是哪种格式、哪个档位，进 iPod 后显示的字段都一致。
2. **封面**：从 `al.picUrl` 下 600×600 的图，内嵌进文件。
   项目现有的提取器读的就是 ID3 `APIC`（MP3）和 FLAC 的 `pictures[0]`，
   所以内嵌之后导入管线能直接取到封面。

## 格式判定看"实际给的"，不看"请求的"

请求 `lossless` 有可能被降级成 320k MP3 回来。如果按请求的档位决定扩展名，
就会把 MP3 字节存成 `.flac`——**这正是本项目真实踩过的坑**
（当年是把 FLAC 字节拷成 `.m4a`，文件能播、元数据全错、极难定位）。
所以：扩展名看响应里的 `level`，并且下完**核对文件魔数**。
"""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ipod_cli.mediafile import probe_media_file
from ipod_cli.ncm.client import NcmClient, Song, SongUrl
from ipod_cli.ncm.netutil import ReadTimeoutError, read_with_deadline

#: 这些档位给的是 FLAC；其余是 MP3
LOSSLESS_LEVELS = frozenset({"lossless", "hires", "jymaster"})

#: 下载一首歌的**总时长**上限（秒）。
#:
#: 为什么需要它：`urlopen(timeout=N)` 只是**每次 recv** 的超时。服务器只要
#: 慢慢吐数据，每次 recv 都算没超时，于是永远不触发——实测同步就是这么卡死的
#: （应用停在等响应上，界面跟着无响应）。所以自己掐墙钟。
#: 50 MB 的无损在正常网速下也就几十秒，5 分钟已经很宽容。
TRANSFER_DEADLINE_SECONDS = 300.0

#: 单曲下载上限，防止接口返回离谱的 URL 把磁盘写满
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024

_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DownloadError(RuntimeError):
    """下载失败。"""


@dataclass
class DownloadResult:
    song_id: int
    path: Path
    level: str
    size: int
    has_cover: bool
    duration_ms: int = 0

    @property
    def megabytes(self) -> float:
        return self.size / 1048576


def safe_filename(text: str, *, limit: int = 80) -> str:
    """把标题艺人变成 Windows 上安全的文件名。

    先归并空白再替换非法字符——反过来的话制表符会被替换成下划线，
    得到 "a _ b" 这种带孤立下划线的脏名字。
    只做必要的处理，不动中文、不动词间空格——
    缓存目录是给人看的，不是给机器解析的。
    """
    # 1. 空白（含制表/换行）先归并成一个空格
    cleaned = re.sub(r"\s+", " ", text).strip()
    # 2. 再替换文件系统不认的字符
    cleaned = _ILLEGAL_CHARS.sub("_", cleaned).strip(" .")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip()
    # 3. 全是分隔符（比如输入是 "///"）等于没名字
    if not re.sub(r"[_\s.]", "", cleaned):
        return "未命名"
    return cleaned


def expected_extension(level: str) -> str:
    return ".flac" if level in LOSSLESS_LEVELS else ".mp3"


def detect_kind(raw: bytes) -> str:
    """按魔数认格式。认不出返回 'unknown'。"""
    if raw[:4] == b"fLaC":
        return "flac"
    if raw[:3] == b"ID3":
        return "mp3"
    # MP3 不带 ID3 时以帧同步字开头
    if len(raw) >= 2 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0:
        return "mp3"
    if raw[4:8] == b"ftyp":
        return "m4a"
    return "unknown"


def target_path(dest_dir: Path, song: Song, level: str) -> Path:
    """缓存文件路径：``<id> <艺人> - <标题>.<ext>``。

    带 ID 前缀是为了**唯一**（同名歌曲很常见），带标题是为了**人能看懂**
    （出问题时可以直接去缓存目录里翻）。
    """
    dest_dir = Path(dest_dir)
    stem = safe_filename(f"{song.id} {song.label}")
    return dest_dir / f"{stem}{expected_extension(level)}"


def download_audio(url: str, timeout: int = 60) -> bytes:
    """从 CDN 拉音频字节。

    注意：CDN **不是** API 接口，这一趟不增加账号的风控压力。
    真正要克制的是 ``client`` 那边的接口调用。
    """
    request = urllib.request.Request(url, headers={"User-Agent": "ipod-cli/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # ★ 必须自己掐总时长：`timeout` 只管单次 recv，服务器慢慢吐数据时
            #   它永远不触发（实测就是这么卡死的——应用停在等响应上，
            #   界面跟着无响应）。
            try:
                body = read_with_deadline(
                    response,
                    seconds=TRANSFER_DEADLINE_SECONDS,
                    max_bytes=MAX_DOWNLOAD_BYTES,
                )
            except ValueError as exc:
                raise DownloadError(str(exc)) from exc
            except ReadTimeoutError as exc:
                raise DownloadError(str(exc)) from exc
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(f"下载失败：{type(exc).__name__}: {exc}") from exc
    if not body:
        raise DownloadError("下载到 0 字节")
    return body


def write_tags(path: Path, song: Song, cover: bytes = b"") -> bool:
    """按接口元数据写标签 + 内嵌封面。返回是否嵌入了封面。

    两种容器分开处理，因为 mutagen 的接口完全不一样：
    FLAC 用 Vorbis comment + PICTURE 块，MP3 用 ID3v2.3 + APIC 帧。
    """
    suffix = path.suffix.lower()
    has_cover = False

    if suffix == ".flac":
        from mutagen.flac import FLAC, Picture

        audio = FLAC(str(path))
        audio["TITLE"] = song.name
        if song.artist_text:
            audio["ARTIST"] = song.artist_text
        if song.album:
            audio["ALBUM"] = song.album
        if song.track_no:
            audio["TRACKNUMBER"] = str(song.track_no)
        if cover:
            picture = Picture()
            picture.type = 3               # Cover (front)
            picture.mime = "image/jpeg"
            picture.data = cover
            audio.clear_pictures()
            audio.add_picture(picture)
            has_cover = True
        audio.save()

    elif suffix == ".mp3":
        from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TRCK

        try:
            tags = ID3(str(path))
        except Exception:
            # 文件没有 ID3 标签块，或者块坏了——新建一个
            tags = ID3()
        tags.delall("TIT2")
        tags.add(TIT2(encoding=3, text=song.name))
        if song.artist_text:
            tags.delall("TPE1")
            tags.add(TPE1(encoding=3, text=song.artist_text))
        if song.album:
            tags.delall("TALB")
            tags.add(TALB(encoding=3, text=song.album))
        if song.track_no:
            tags.delall("TRCK")
            tags.add(TRCK(encoding=3, text=str(song.track_no)))
        if cover:
            tags.delall("APIC")
            tags.add(
                APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover)
            )
            has_cover = True
        tags.save(str(path), v2_version=3)   # v2.3：iPod 的兼容性最好

    return has_cover


#: 下载到的时长比元数据短多少就认为不对。
#: 试听片段是 30 秒、完整歌曲通常 2~6 分钟，用比例判比绝对值稳。
#: 元数据本身也可能偏一点（不同的计长方式），所以留 20% 余量。
MIN_DURATION_RATIO = 0.8


def download_song(
    client: NcmClient,
    song: Song,
    dest_dir: Path,
    *,
    level: str,
    cookie: str = "",
    with_cover: bool = True,
    progress=None,
) -> DownloadResult:
    """把一首歌下到 ``dest_dir``，补好标签和封面。

    返回的文件**可以直接喂给 ``ipod import``**——标签齐全、封面内嵌，
    剩下的事（转码、写库、封面转 ithmb）由现有管线负责。

    ``cookie`` 必须传：**不带 cookie 请求下载链接，网易云会返回试听片段**
    （30 秒、128k、体积很小）。实测踩过这个坑——文件能播、标签正常、
    校验全过，只有"时长不对"一个症状，极难发现。
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    url = client.song_url(song.id, level, cookie=cookie)

    if url.trial:
        raise DownloadError(
            f"只能拿到试听片段，不是完整歌曲：{song.label}\n"
            f"      （通常是会员到期、或这首歌需要单独购买）"
        )
    if not url.available:
        raise DownloadError(f"拿不到下载链接（可能无版权）：{song.label}")

    # 按**实际拿到的**档位定扩展名，不按请求的档位
    ext = expected_extension(url.level)
    path = target_path(dest_dir, song, url.level)

    if progress:
        progress(f"下载 {song.label}（{url.level}）")

    raw = download_audio(url.url, timeout=client.timeout)

    # 核对魔数：请求无损却拿回 MP3（或反过来）都不能默默存下去
    kind = detect_kind(raw)
    want = "flac" if ext == ".flac" else "mp3"
    if kind != want:
        if kind == "unknown":
            raise DownloadError(
                f"下到的内容不是音频（{song.label}，前 8 字节 "
                f"{raw[:8].hex()}）——链接可能已过期"
            )
        raise DownloadError(
            f"格式不符：按 {url.level} 期望 {want}，实际拿到 {kind}"
            f"（{song.label}）"
        )

    path.write_bytes(raw)

    # 封面在 CDN 上，公开资源，不需要 cookie
    cover = client.cover_bytes(song.cover_url) if with_cover else b""
    try:
        has_cover = write_tags(path, song, cover)
    except Exception as exc:
        # 标签写不进去不该让整首歌白下——文件本体是好的，能播
        has_cover = False
        if progress:
            progress(f"⚠ 标签写入失败（{song.label}）：{type(exc).__name__}: {exc}")

    duration_ms, _, _ = probe_media_file(path)

    # ★ 最后一道闸：时长核对。
    # 就算 freeTrialInfo 没标出来（接口改字段、走了别的返回路径），
    # "元数据说 4 分钟、文件只有 30 秒"这种事也该被拦住。
    if duration_ms and song.duration_ms:
        ratio = duration_ms / song.duration_ms
        if ratio < MIN_DURATION_RATIO:
            path.unlink(missing_ok=True)
            raise DownloadError(
                f"下载到的文件明显偏短（{duration_ms / 1000:.0f} 秒，"
                f"应该是 {song.duration_ms / 1000:.0f} 秒）——"
                f"大概率是试听片段，已丢弃：{song.label}"
            )

    return DownloadResult(
        song_id=song.id,
        path=path,
        level=url.level,
        size=path.stat().st_size,
        has_cover=has_cover,
        duration_ms=duration_ms or song.duration_ms,
    )


def cleanup_partial(path: Path) -> None:
    """下载失败时清掉半成品。"""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "DownloadError",
    "DownloadResult",
    "SongUrl",
    "cleanup_partial",
    "detect_kind",
    "download_audio",
    "download_song",
    "expected_extension",
    "safe_filename",
    "target_path",
    "write_tags",
]
