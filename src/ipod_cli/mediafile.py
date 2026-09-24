"""读取 PC 上音乐文件的标签信息。

用 mutagen 解析各类容器的标签，统一成一句话：不管源文件是 MP3 还是 FLAC，
都能拿到标题/艺人/专辑/时长/码率这些写进 iTunesDB 需要的东西。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis

# iPod Classic 原生支持的音频格式（其余需要转码）
NATIVE_AUDIO_EXTENSIONS = {
    ".mp3": "mp3",
    ".m4a": "m4a",
    ".m4b": "m4b",     # 有声书
    ".aac": "aac",
    ".wav": "wav",
    ".aif": "aif",
    ".aiff": "aiff",
    ".aa": "aa",       # Audible
    ".m4p": "m4p",     # iTunes 加密 AAC
}

# 需要转码才能上 iPod 的格式
TRANSCODE_AUDIO_EXTENSIONS = {
    ".flac": "flac",
    ".ogg": "ogg",
    ".oga": "ogg",
    ".opus": "opus",
    ".ape": "ape",
    ".wv": "wv",
    ".wma": "wma",
    ".mpc": "mpc",
    ".wavpack": "wv",
}

#: 所有可能出现在设备上的音频扩展名（原生 + 需转码）。
#:
#: 设备侧判断"这个文件是不是音频"用它。**唯一用处是"别把非音频文件当音乐"**：
#: 修复功能扫孤儿时，Music/ 下的 desktop.ini / Thumbs.db 这类系统文件不是同步
#: 残留，按音频扩展名过滤掉它们才不会被当成孤儿删掉。
SUPPORTED_AUDIO_EXTENSIONS = frozenset(NATIVE_AUDIO_EXTENSIONS) | frozenset(
    TRANSCODE_AUDIO_EXTENSIONS
)

# 常见的专辑封面文件（没有内嵌封面时去找）
COVER_FILENAMES = (
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.jpeg", "front.png",
    "album.jpg", "album.jpeg", "album.png",
    "AlbumArtSmall.jpg", "AlbumArt.jpg",
)


class UnreadableMediaError(RuntimeError):
    """文件不是可识别的音频，或标签损坏。"""


@dataclass
class PcTrack:
    """一个 PC 侧音频文件读出来的信息。"""

    source_path: Path
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    composer: str = ""
    comment: str = ""
    grouping: str = ""

    track_number: int = 0
    total_tracks: int = 0
    disc_number: int = 1
    total_discs: int = 1
    year: int = 0

    duration_ms: int = 0
    bitrate: int = 0            # kbps
    sample_rate: int = 44100
    channels: int = 2
    vbr: bool = False

    filetype: str = ""          # 写进 iTunesDB 的扩展名（不含点）
    size: int = 0
    needs_transcode: bool = False

    has_embedded_artwork: bool = False
    cover_file: Path | None = None

    read_warnings: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.title or self.source_path.stem

    @property
    def duration_text(self) -> str:
        if self.duration_ms <= 0:
            return "--:--"
        total = self.duration_ms // 1000
        return f"{total // 60:d}:{total % 60:02d}"


def _first(value) -> str:
    """标签值可能是列表，取第一个并转成字符串。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        value = value[0]
    return str(value).strip()


def _as_int(value, default: int = 0) -> int:
    """尽力把标签值转成整数，支持 "3/12" 这种形式。"""
    text = _first(value)
    if not text:
        return default
    # "3/12" → 3
    head = text.split("/")[0].strip()
    digits = "".join(ch for ch in head if ch.isdigit())
    if not digits:
        return default
    try:
        return int(digits)
    except ValueError:
        return default


def _parse_year(value) -> int:
    text = _first(value)
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits[:4]) if len(digits) >= 4 else 0


def read_pc_track(path: Path) -> PcTrack:
    """读一个 PC 音频文件。读不出来就抛 UnreadableMediaError。"""
    path = Path(path)
    extension = path.suffix.lower()
    track = PcTrack(source_path=path)

    try:
        track.size = path.stat().st_size
    except OSError as exc:
        raise UnreadableMediaError(f"读不到文件：{path}（{exc}）") from exc

    # ── 扩展名 → 格式 ────────────────────────────────────────────────
    if extension in NATIVE_AUDIO_EXTENSIONS:
        track.filetype = NATIVE_AUDIO_EXTENSIONS[extension]
    elif extension in TRANSCODE_AUDIO_EXTENSIONS:
        track.filetype = TRANSCODE_AUDIO_EXTENSIONS[extension]
        track.needs_transcode = True
    else:
        track.filetype = extension.lstrip(".")
        track.needs_transcode = True
        track.read_warnings.append(f"未知扩展名 {extension}，需要转码")

    # ── 时长/码率 ────────────────────────────────────────────────────
    try:
        audio = mutagen.File(str(path))
    except Exception as exc:
        raise UnreadableMediaError(f"mutagen 无法解析：{path}（{exc}）") from exc

    if audio is None:
        raise UnreadableMediaError(f"不是可识别的音频文件：{path}")

    info = getattr(audio, "info", None)
    if info is not None:
        length = getattr(info, "length", 0) or 0
        track.duration_ms = int(round(length * 1000))
        bitrate = getattr(info, "bitrate", 0) or 0
        track.bitrate = int(round(bitrate / 1000)) if bitrate else 0
        track.sample_rate = int(getattr(info, "sample_rate", 44100) or 44100)
        track.channels = int(getattr(info, "channels", 2) or 2)

    # ── 标签 ─────────────────────────────────────────────────────────
    tags = getattr(audio, "tags", None)
    if tags is not None:
        _read_tags(track, audio, tags, extension)

    # ── 兜底：文件名当标题 ────────────────────────────────────────────
    if not track.title:
        track.title = path.stem

    # ── 封面来源 ─────────────────────────────────────────────────────
    track.has_embedded_artwork = _has_embedded_artwork(audio, extension)
    if not track.has_embedded_artwork:
        track.cover_file = find_cover_file(path.parent)

    return track


def _read_tags(track: PcTrack, audio, tags, extension: str) -> None:
    """按容器类型取标签。mutagen 的 Easy* 接口字段名不统一，这里手工映射。"""
    def get(*keys):
        for key in keys:
            try:
                value = tags.get(key)
            except Exception:
                value = None
            if value:
                return value
        return None

    if isinstance(audio, MP4):
        # MP4/M4A 用 iTunes 风格的四字符 atom
        track.title = _first(get("\xa9nam"))
        track.artist = _first(get("\xa9ART"))
        track.album = _first(get("\xa9alb"))
        track.album_artist = _first(get("aART"))
        track.genre = _first(get("\xa9gen"))
        track.composer = _first(get("\xa9wrt"))
        track.comment = _first(get("\xa9cmt"))
        track.grouping = _first(get("\xa9grp"))
        track.track_number = _as_int(get("trkn"))
        track.total_tracks = _as_int(get("trkn"))
        track.disc_number = _as_int(get("disk"), 1)
        track.total_discs = _as_int(get("disk"), 1)
        track.year = _parse_year(get("\xa9day"))
        # trkn 是 [(num, total)] 这样的结构，上面 _as_int 拿的是第一个数
        raw_trkn = get("trkn")
        if isinstance(raw_trkn, list) and raw_trkn and isinstance(raw_trkn[0], tuple):
            numbers = raw_trkn[0]
            track.track_number = int(numbers[0]) if len(numbers) > 0 else 0
            track.total_tracks = int(numbers[1]) if len(numbers) > 1 else 0
        raw_disk = get("disk")
        if isinstance(raw_disk, list) and raw_disk and isinstance(raw_disk[0], tuple):
            numbers = raw_disk[0]
            track.disc_number = int(numbers[0]) if len(numbers) > 0 else 1
            track.total_discs = int(numbers[1]) if len(numbers) > 1 else 1
        return

    if isinstance(audio, FLAC):
        track.title = _first(get("title"))
        track.artist = _first(get("artist"))
        track.album = _first(get("album"))
        track.album_artist = _first(get("albumartist", "album artist"))
        track.genre = _first(get("genre"))
        track.composer = _first(get("composer"))
        track.comment = _first(get("comment", "description"))
        track.grouping = _first(get("grouping"))
        track.track_number = _as_int(get("tracknumber"))
        track.total_tracks = _as_int(get("tracktotal", "totaltracks"))
        track.disc_number = _as_int(get("discnumber"), 1)
        track.total_discs = _as_int(get("disctotal", "totaldiscs"), 1)
        track.year = _parse_year(get("date", "year"))
        return

    if isinstance(audio, OggVorbis):
        track.title = _first(get("title"))
        track.artist = _first(get("artist"))
        track.album = _first(get("album"))
        track.album_artist = _first(get("albumartist"))
        track.genre = _first(get("genre"))
        track.composer = _first(get("composer"))
        track.track_number = _as_int(get("tracknumber"))
        track.year = _parse_year(get("date"))
        return

    # 默认走 ID3（MP3/WAV/AIFF 等）
    track.title = _first(get("TIT2", "title"))
    track.artist = _first(get("TPE1", "artist"))
    track.album = _first(get("TALB", "album"))
    track.album_artist = _first(get("TPE2", "albumartist"))
    track.genre = _first(get("TCON", "genre"))
    track.composer = _first(get("TCOM", "composer"))
    track.comment = _first(get("COMM::eng", "COMM", "comment"))
    track.grouping = _first(get("TIT1", "grouping"))
    track.track_number = _as_int(get("TRCK", "tracknumber"))
    raw_trck = get("TRCK", "tracknumber")
    text = _first(raw_trck)
    if "/" in text:
        left, _, right = text.partition("/")
        track.track_number = _as_int(left)
        track.total_tracks = _as_int(right)
    raw_tpos = get("TPOS")
    text = _first(raw_tpos)
    if "/" in text:
        left, _, right = text.partition("/")
        track.disc_number = _as_int(left, 1)
        track.total_discs = _as_int(right, 1)
    track.year = _parse_year(get("TDRC", "TYER", "date"))


def _has_embedded_artwork(audio, extension: str) -> bool:
    """判断文件里有没有内嵌封面。"""
    try:
        if isinstance(audio, MP4):
            return bool(audio.tags and audio.tags.get("covr"))
        if isinstance(audio, FLAC):
            return bool(audio.pictures)
        if isinstance(audio, OggVorbis):
            return bool(audio.get("metadata_block_picture"))
        # ID3
        tags = getattr(audio, "tags", None)
        if isinstance(tags, ID3):
            return any(key.startswith("APIC") for key in tags.keys())
        if isinstance(tags, dict):
            return any(str(key).startswith("APIC") for key in tags.keys())
    except Exception:
        return False
    return False


def probe_media_file(path: Path) -> tuple[int, int, int]:
    """探测一个媒体文件的 (时长ms, 码率kbps, 采样率)。

    转码产物的真实属性必须重新探测——源文件是 FLAC 时，源码率跟转出来的
    ALAC 码率完全不是一回事，直接把源值写进 iTunesDB 会让库和文件对不上。
    """
    try:
        audio = mutagen.File(str(path))
    except Exception:
        return 0, 0, 44100
    if audio is None:
        return 0, 0, 44100
    info = getattr(audio, "info", None)
    if info is None:
        return 0, 0, 44100

    length = getattr(info, "length", 0) or 0
    bitrate = getattr(info, "bitrate", 0) or 0
    sample_rate = getattr(info, "sample_rate", 44100) or 44100
    return (
        int(round(length * 1000)),
        int(round(bitrate / 1000)) if bitrate else 0,
        int(sample_rate),
    )


def find_cover_file(directory: Path) -> Path | None:
    """在目录里找封面图文件（大小写不敏感）。"""
    try:
        entries = {entry.name.lower(): entry for entry in directory.iterdir() if entry.is_file()}
    except OSError:
        return None
    for name in COVER_FILENAMES:
        hit = entries.get(name.lower())
        if hit is not None:
            return hit
    return None


def collect_audio_files(source: Path, recursive: bool = True) -> list[Path]:
    """收集待导入的音频文件，按路径排序保证可复现。"""
    source = Path(source)
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise UnreadableMediaError(f"路径既不是文件也不是目录：{source}")

    iterator = source.rglob("*") if recursive else source.glob("*")
    files = [
        entry
        for entry in iterator
        if entry.is_file()
        and entry.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
        and not _is_hidden(entry)
    ]
    return sorted(files, key=lambda p: str(p).lower())


def _is_hidden(path: Path) -> bool:
    """跳过隐藏文件和 macOS 的 AppleDouble 残留。"""
    name = path.name
    if name.startswith("._"):
        return True
    if os.name == "nt":
        try:
            import stat as _stat

            return bool(path.stat().st_file_attributes & _stat.FILE_ATTRIBUTE_HIDDEN)
        except Exception:
            return False
    return name.startswith(".")
