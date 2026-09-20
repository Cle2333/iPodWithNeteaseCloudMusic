"""读取 iPod 上的 iTunesDB，得到曲目清单。

这是"列出"和"导出"两个功能的地基。用 vendored 的 ``load_ipod_library``
拿到扁平字典——它同时给了两样东西：

* 人看的：``Track`` 对象
* 重写时要保真的：原始扁平字典（``track_dicts``），交给
  ``track_dict_to_info`` 就能无损转回写模型，这样导入新歌时不会
  把 iPod 上已有的歌弄丢。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from iopenpod.device import resolve_itdb_path
from iopenpod.itunesdb_parser.ipod_library import load_ipod_library

# 扁平字典的字符串键 → Track 属性名
_STRING_FIELD_BY_DICT_KEY = {
    "Title": "title",
    "Location": "location",
    "Artist": "artist",
    "Album": "album",
    "Album Artist": "album_artist",
    "Genre": "genre",
    "Composer": "composer",
    "Comment": "comment",
    "Category": "category",
    "Grouping": "grouping",
    "filetype": "filetype",
}

# 扁平字典的数值键 → Track 属性名
_NUMERIC_FIELD_BY_DICT_KEY = {
    "db_track_id": "db_track_id",
    "track_id": "track_id",
    "size": "size",
    "length": "length",
    "bitrate": "bitrate",
    "sample_rate_1": "sample_rate",
    "track_number": "track_number",
    "total_tracks": "total_tracks",
    "disc_number": "disc_number",
    "total_discs": "total_discs",
    "year": "year",
    "play_count_1": "play_count",
    "skip_count": "skip_count",
    "rating": "rating",
    "media_type": "media_type",
    "date_added": "date_added",
    "last_played": "last_played",
    "compilation_flag": "compilation_flag",
}


class LibraryError(RuntimeError):
    """iTunesDB 无法读取时抛出。"""


@dataclass
class Track:
    """一首歌在 iPod 数据库里的样子（只读视图）。"""

    db_track_id: int = 0
    track_id: int = 0
    title: str = ""
    location: str = ""          # ":iPod_Control:Music:F00:XXXX.mp3"
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    composer: str = ""
    comment: str = ""
    category: str = ""
    grouping: str = ""
    filetype: str = ""

    size: int = 0               # 字节
    length: int = 0             # 毫秒
    bitrate: int = 0            # kbps
    sample_rate: int = 0
    track_number: int = 0
    total_tracks: int = 0
    disc_number: int = 1
    total_discs: int = 1
    year: int = 0

    play_count: int = 0
    skip_count: int = 0
    rating: int = 0
    media_type: int = 0
    date_added: int = 0
    last_played: int = 0
    compilation_flag: int = 0

    # 解析时的原始扁平字典。播放列表成员匹配、重写保真等场景需要它
    # （例如 track_persistent_id 只存在于原始字典里）。
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def duration_text(self) -> str:
        """把毫秒时长格式化成 m:ss。"""
        if self.length <= 0:
            return "--:--"
        total_seconds = self.length // 1000
        return f"{total_seconds // 60:d}:{total_seconds % 60:02d}"

    @property
    def relative_path(self) -> str:
        """iPod 数据库里的冒号路径 → 相对 iPod 根目录的路径。

        ":iPod_Control:Music:F00:ABC.mp3" → "iPod_Control/Music/F00/ABC.mp3"
        """
        return self.location.strip(":").replace(":", "/")

    @property
    def display_name(self) -> str:
        return self.title or Path(self.relative_path).name or "（无标题）"

    @property
    def stars(self) -> int:
        """评分（0-5 星），iPod 里是 0-100。"""
        return int(round(self.rating / 20)) if self.rating else 0


@dataclass
class LibraryData:
    """一次读库的完整结果。"""

    root: Path
    db_path: Path
    tracks: list[Track] = field(default_factory=list)
    track_dicts: list[dict] = field(default_factory=list, repr=False)
    playlists: list[dict] = field(default_factory=list, repr=False)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def ipod_name(self) -> str:
        """从主播放列表里取 iPod 的名字。"""
        for playlist in self.playlists:
            name = playlist.get("Title") or playlist.get("Playlist Title")
            if name:
                return str(name)
        return "iPod"

    def summary(self) -> dict:
        return {
            "track_count": len(self.tracks),
            "album_count": len({(t.album or "", t.album_artist or t.artist) for t in self.tracks if t.album}),
            "artist_count": len({t.artist for t in self.tracks if t.artist}),
            "total_bytes": sum(t.size for t in self.tracks),
            "total_ms": sum(t.length for t in self.tracks),
        }


def locate_database(ipod_root: str | Path) -> Path:
    """找到 iPod 上实际的数据库文件（iTunesDB 或 iTunesCDB）。"""
    resolved = resolve_itdb_path(str(ipod_root))
    if not resolved:
        raise LibraryError(
            "这台 iPod 上找不到 iTunesDB。请确认：\n"
            "  1. 路径指向 iPod 的根目录（里面有 iPod_Control 文件夹）\n"
            "  2. iPod 已正确挂载/连接"
        )
    return Path(resolved)


def read_library(ipod_root: str | Path) -> LibraryData:
    """读取 iPod 上的完整库（曲目 + 播放列表 + 原始字典）。"""
    root = Path(ipod_root)
    db_path = locate_database(root)

    parsed = load_ipod_library(str(db_path))
    if parsed is None:
        raise LibraryError(
            f"解析 iTunesDB 失败：{db_path}\n"
            f"  文件可能已损坏。建议先用备份还原，或交给 iTunes 重建。"
        )

    track_dicts = list(parsed.get("mhlt", []) or [])
    tracks = [track_from_dict(d) for d in track_dicts]
    # 去掉解析不出来源文件的记录（否则导出/统计会带上幽灵曲目）
    tracks = [t for t in tracks if t.location]

    playlists = list(parsed.get("mhlp", []) or [])

    return LibraryData(
        root=root,
        db_path=db_path,
        tracks=tracks,
        track_dicts=track_dicts,
        playlists=playlists,
        raw=parsed,
    )


def track_from_dict(data: dict) -> Track:
    """把扁平字典转成 Track。"""
    track = Track()
    track.raw = data

    for key, attr in _STRING_FIELD_BY_DICT_KEY.items():
        value = data.get(key)
        if isinstance(value, str) and value:
            setattr(track, attr, value)

    for key, attr in _NUMERIC_FIELD_BY_DICT_KEY.items():
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            setattr(track, attr, int(value))

    return track
