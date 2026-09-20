"""把 iPod 上的歌导出回 PC。

导出的两件事：把音频文件拷出来，以及可以顺手导一份清单（CSV/JSON）。
按艺人/专辑分目录是默认行为，因为直接倒成一堆 4 位随机文件名基本没法用。
"""

from __future__ import annotations

import csv
import json
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .discovery import IpodDevice, human_size
from .library import LibraryData, Track

ProgressCallback = Callable[[str], None]

# 文件名里不能出现的字符（Windows 最严，统一处理）
_INVALID_FILENAME_CHARS = '<>:"/\\|?*'
_ILLEGAL_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass
class ExportItem:
    track: Track
    source: Path
    dest: Path
    exists: bool = True
    copied: bool = False
    error: str = ""


@dataclass
class ExportResult:
    items: list[ExportItem] = field(default_factory=list)
    bytes_copied: int = 0
    manifest_path: Path | None = None

    @property
    def copied(self) -> int:
        return sum(1 for i in self.items if i.copied)

    @property
    def missing(self) -> list[ExportItem]:
        return [i for i in self.items if not i.exists]

    @property
    def failed(self) -> list[ExportItem]:
        return [i for i in self.items if i.error]


def sanitize_filename(name: str, fallback: str = "未命名") -> str:
    """把任意文本变成安全的文件名。"""
    cleaned = "".join(
        "_" if ch in _INVALID_FILENAME_CHARS or ord(ch) < 32 else ch
        for ch in str(name or "")
    ).strip().strip(".")

    if not cleaned:
        cleaned = fallback
    # Windows 保留名（CON/PRN/...）加下划线避开
    if cleaned.split(".")[0].upper() in _ILLEGAL_WINDOWS_NAMES:
        cleaned = f"_{cleaned}"
    # 文件名过长会让很多工具炸掉，按字节谨慎截断
    if len(cleaned) > 120:
        cleaned = cleaned[:120].rstrip()
    return cleaned


def _track_filename(track: Track, index: int, *, flat: bool, keep_original: bool) -> str:
    if keep_original:
        return sanitize_filename(Path(track.relative_path).name, f"track{index:03d}")
    extension = Path(track.relative_path).suffix or ".mp3"
    # 音轨号优先；没有音轨号就用列表序号，两者都补两位保持视觉一致
    number = track.track_number if track.track_number else index
    prefix = f"{number:02d} "
    stem = f"{prefix}{track.display_name}"
    if track.artist and not flat:
        stem = f"{prefix}{track.title or track.display_name}"
    return sanitize_filename(stem, f"track{index:02d}") + extension


def _track_relative_dir(track: Track, *, flat: bool, organize_by: str) -> Path:
    if flat or organize_by == "none":
        return Path()
    if organize_by == "album":
        album = sanitize_filename(track.album or "未知专辑")
        artist = sanitize_filename(track.album_artist or track.artist or "未知艺人")
        return Path(artist) / album
    # 默认按艺人
    return Path(sanitize_filename(track.artist or "未知艺人"))


def build_export_items(
    device: IpodDevice,
    tracks: Iterable[Track],
    dest_dir: Path,
    *,
    flat: bool = False,
    organize_by: str = "artist",
    keep_original: bool = False,
) -> list[ExportItem]:
    """生成导出清单（不拷贝任何东西）。"""
    dest_dir = Path(dest_dir)
    items: list[ExportItem] = []
    used: set[Path] = set()

    for index, track in enumerate(tracks, start=1):
        source = device.root / track.relative_path
        subdir = _track_relative_dir(track, flat=flat, organize_by=organize_by)
        filename = _track_filename(
            track, index, flat=flat, keep_original=keep_original
        )
        dest = dest_dir / subdir / filename

        # 同目录重名（不同版本/重复曲目）加序号
        counter = 2
        while dest in used:
            stem, dot, suffix = filename.rpartition(".")
            dest = dest_dir / subdir / f"{stem} ({counter}){dot}{suffix}"
            counter += 1
        used.add(dest)

        items.append(
            ExportItem(track=track, source=source, dest=dest, exists=source.is_file())
        )
    return items


def execute_export(
    items: list[ExportItem],
    *,
    progress: ProgressCallback | None = None,
    overwrite: bool = False,
) -> ExportResult:
    """执行拷贝。"""
    result = ExportResult(items=items)
    total = len(items)

    for index, item in enumerate(items, start=1):
        if not item.exists:
            item.error = "iPod 上找不到这个文件"
            continue
        if progress is not None:
            progress(f"正在导出 {index}/{total}：{item.track.display_name}")
        try:
            if item.dest.exists() and not overwrite:
                item.error = "目标文件已存在（用 --overwrite 覆盖）"
                continue
            item.dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.source, item.dest)
            item.copied = True
            result.bytes_copied += item.dest.stat().st_size
        except OSError as exc:
            item.error = str(exc)

    return result


def write_csv_manifest(items: list[ExportItem], path: Path) -> Path:
    """导出曲目清单为 CSV（Excel 能直接打开，带 BOM 防中文乱码）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        ("标题", lambda i: i.track.title),
        ("艺人", lambda i: i.track.artist),
        ("专辑", lambda i: i.track.album),
        ("专辑艺人", lambda i: i.track.album_artist),
        ("流派", lambda i: i.track.genre),
        ("年份", lambda i: i.track.year or ""),
        ("音轨号", lambda i: i.track.track_number or ""),
        ("时长", lambda i: i.track.duration_text),
        ("码率kbps", lambda i: i.track.bitrate or ""),
        ("格式", lambda i: i.track.filetype),
        ("评分", lambda i: i.track.stars),
        ("播放次数", lambda i: i.track.play_count),
        ("文件大小", lambda i: i.track.size),
        ("iPod 路径", lambda i: i.track.relative_path),
        ("导出到", lambda i: str(i.dest) if i.copied else ""),
        ("状态", lambda i: "已导出" if i.copied else (i.error or "未导出")),
    ]

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow([name for name, _ in columns])
        for item in items:
            writer.writerow([getter(item) for _, getter in columns])
    return path


def write_json_manifest(items: list[ExportItem], path: Path) -> Path:
    """导出曲目清单为 JSON。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "title": i.track.title,
            "artist": i.track.artist,
            "album": i.track.album,
            "album_artist": i.track.album_artist,
            "genre": i.track.genre,
            "year": i.track.year,
            "track_number": i.track.track_number,
            "duration_ms": i.track.length,
            "bitrate_kbps": i.track.bitrate,
            "filetype": i.track.filetype,
            "rating_stars": i.track.stars,
            "play_count": i.track.play_count,
            "size_bytes": i.track.size,
            "ipod_path": i.track.relative_path,
            "exported_to": str(i.dest) if i.copied else None,
            "status": "exported" if i.copied else (i.error or "not_exported"),
        }
        for i in items
    ]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def select_tracks(
    library: LibraryData,
    *,
    artist: str | None = None,
    album: str | None = None,
    genre: str | None = None,
    search: str | None = None,
    playlist: str | None = None,
    keep_playlists: Sequence[str] = (),
) -> list[Track]:
    """按条件挑曲目。不传条件就是全部。

    ``keep_playlists`` 是**排除**列表：这些播放列表里的曲目会被剔出结果。
    所以 `select_tracks(library, keep_playlists=["我喜欢的音乐"])` 得到的是
    "除了那个歌单以外的所有歌"——配合删除就是"只留这个歌单"。
    实现成排除而不是"选中被保留的"，是为了让调用方拿到的始终是**要处理的那批**，
    跟其它参数保持同一语义，避免出现"参数名一样、含义相反"的坑。
    """
    tracks = list(library.tracks)

    if artist:
        needle = artist.casefold()
        tracks = [
            t for t in tracks
            if needle in t.artist.casefold() or needle in t.album_artist.casefold()
        ]
    if album:
        needle = album.casefold()
        tracks = [t for t in tracks if needle in t.album.casefold()]
    if genre:
        needle = genre.casefold()
        tracks = [t for t in tracks if needle in t.genre.casefold()]
    if search:
        needle = search.casefold()
        tracks = [
            t for t in tracks
            if needle in t.title.casefold()
            or needle in t.artist.casefold()
            or needle in t.album.casefold()
        ]
    if playlist:
        tracks = _filter_by_playlist(library, tracks, playlist)

    for name in keep_playlists:
        protected = _filter_by_playlist(library, tracks, name)
        protected_ids = {t.db_track_id for t in protected}
        tracks = [t for t in tracks if t.db_track_id not in protected_ids]

    return tracks


def _filter_by_playlist(library: LibraryData, tracks: list[Track], name: str) -> list[Track]:
    """按播放列表名筛选。找不到该列表时抛 ValueError。

    注意：播放列表条目用 ``track_id``（数据库内的位置序号）引用曲目，
    不是 ``db_track_id``。有 ``track_persistent_id`` 时优先用它（更稳，
    不受顺序变动影响），否则退回位置序号。
    """
    needle = name.casefold()
    target = None
    for playlist in library.playlists:
        title = str(playlist.get("Title") or "")
        if title.casefold() == needle:
            target = playlist
            break
    if target is None:
        available = sorted(
            {str(p.get("Title") or "") for p in library.playlists} - {""}
        )
        listing = "、".join(available[:20]) if available else "（没有任何播放列表）"
        raise ValueError(f"找不到播放列表「{name}」。已有的：{listing}")

    items = [i for i in (target.get("items") or []) if isinstance(i, dict)]
    if not items:
        return []

    wanted_track_ids = {
        item.get("track_id") for item in items if item.get("track_id")
    }
    wanted_persistent = {
        item.get("track_persistent_id")
        for item in items
        if item.get("track_persistent_id")
    }

    if wanted_persistent:
        selected = [
            t for t in tracks
            if t.raw.get("track_persistent_id") in wanted_persistent
            or t.raw.get("mhip_persistent_id") in wanted_persistent
        ]
        if selected:
            return selected

    return [t for t in tracks if t.track_id in wanted_track_ids]


def summarize(items: list[ExportItem]) -> str:
    copied = sum(1 for i in items if i.copied)
    missing = sum(1 for i in items if not i.exists)
    failed = sum(1 for i in items if i.error and i.exists)
    size = sum(i.dest.stat().st_size for i in items if i.copied and i.dest.exists())
    lines = [f"已导出 {copied} / {len(items)} 首，共 {human_size(size)}"]
    if missing:
        lines.append(f"  iPod 上缺失 {missing} 个文件（数据库有记录但文件不在）")
    if failed:
        lines.append(f"  失败 {failed} 个")
    return "\n".join(lines)
