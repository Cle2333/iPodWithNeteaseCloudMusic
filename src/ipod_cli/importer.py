"""把 PC 上的歌导入 iPod。

流程刻意保守，顺序是：

  1. 先读 iPod 上已有的库（不然重写数据库会把已有的歌全抹掉）
  2. 读 PC 文件标签、判重、查剩余空间
  3. 分配 iPod 上的目标路径（苹果惯例：F00-F49 轮转 + 4 位随机名）
  4. 拷文件
  5. 已有曲目 + 新曲目合并成一份完整清单，重写 iTunesDB 并签名
  6. 读回校验

每一步都能单独预览（``build_import_plan``）而不动 iPod，方便先给用户看计划。
"""

from __future__ import annotations

import random
import shutil
import string
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from iopenpod.device import capabilities_for_family_gen
from iopenpod.itunesdb_writer import TrackInfo
from iopenpod.itunesdb_writer.mhit_writer import generate_db_track_id
from iopenpod.sync import ipod_filetype_for_extension

from .dbwrite import (
    DatabaseWriteError,
    build_track_infos,
    cleanup_stray_temp_files,
    write_library,
)
from .discovery import IpodDevice, human_size
from .library import LibraryData
from .mediafile import PcTrack, UnreadableMediaError, read_pc_track

__all__ = [
    "DatabaseWriteError",
    "ImportError_",
    "ImportPlan",
    "ImportResult",
    "PathAllocator",
    "PlannedTrack",
    "build_import_plan",
    "build_track_infos",
    "cleanup_stray_temp_files",
    "execute_import",
    "write_library",
]

MUSIC_SUBDIR = Path("iPod_Control") / "Music"
DEFAULT_MUSIC_DIRS = 50          # Classic 是 50 个（F00-F49）
_FILENAME_CHARS = string.ascii_uppercase + string.digits
_FILENAME_ATTEMPTS = 100

ProgressCallback = Callable[[str], None]


class ImportError_(RuntimeError):
    """导入无法进行时抛出。"""


@dataclass
class PlannedTrack:
    """一首待导入的歌 + 它将要占用的 iPod 位置。"""

    pc: PcTrack
    ipod_location: str
    dest_path: Path
    db_track_id: int
    action: str = "add"          # add / skip / transcode / error
    reason: str = ""

    @property
    def will_copy(self) -> bool:
        return self.action in {"add", "transcode"}


@dataclass
class ImportPlan:
    """一次导入的完整计划（不产生任何副作用）。"""

    device: IpodDevice
    library: LibraryData
    items: list[PlannedTrack] = field(default_factory=list)
    existing_dicts: list[dict] = field(default_factory=list, repr=False)

    @property
    def to_add(self) -> list[PlannedTrack]:
        return [i for i in self.items if i.will_copy]

    @property
    def skipped(self) -> list[PlannedTrack]:
        return [i for i in self.items if i.action == "skip"]

    @property
    def errored(self) -> list[PlannedTrack]:
        return [i for i in self.items if i.action == "error"]

    @property
    def bytes_to_copy(self) -> int:
        return sum(i.pc.size for i in self.to_add)

    @property
    def fits(self) -> bool:
        return self.bytes_to_copy <= self.device.free_bytes


@dataclass
class ImportResult:
    plan: ImportPlan
    added: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    bytes_copied: int = 0
    database_written: bool = False
    verified: bool = False
    verification_note: str = ""


# ──────────────────────────────────────────────────────────────────────
# 路径分配
# ──────────────────────────────────────────────────────────────────────

class PathAllocator:
    """按苹果惯例给新文件分配 iPod 上的位置。

    目录 F00..F{music_dirs-1} 轮转；文件名是 4 位随机大写字母数字 + 扩展名，
    带冲突检测重试。iPod 固件不关心文件名，但目录数量和分布要正常。
    """

    def __init__(
        self,
        ipod_root: Path,
        music_dirs: int = DEFAULT_MUSIC_DIRS,
        taken: set[str] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._root = Path(ipod_root)
        self._music_dirs = max(1, music_dirs)
        self._taken = {t.lower() for t in (taken or set())}
        self._counter = 0
        self._rng = rng or random.Random()
        self._used_names: set[tuple[str, str]] = set()

    def allocate(self, extension: str) -> tuple[str, Path]:
        """返回 (iPod 冒号路径, 目标文件绝对路径)。"""
        suffix = extension if extension.startswith(".") else f".{extension}"
        for _ in range(200):
            folder_name = f"F{self._counter:02d}"
            self._counter = (self._counter + 1) % self._music_dirs
            filename = self._random_filename(suffix)
            location = f":iPod_Control:Music:{folder_name}:{filename}"
            if location.lower() in self._taken:
                continue
            if (folder_name, filename.lower()) in self._used_names:
                continue
            dest = self._root / MUSIC_SUBDIR / folder_name / filename
            if dest.exists():
                continue
            self._taken.add(location.lower())
            self._used_names.add((folder_name, filename.lower()))
            return location, dest
        raise ImportError_("无法分配 iPod 上的文件名（冲突太多），请重试。")

    def _random_filename(self, suffix: str) -> str:
        return "".join(self._rng.choices(_FILENAME_CHARS, k=4)) + suffix


def _music_dir_count(device: IpodDevice) -> int:
    """查设备能力表得知该有多少个音乐目录，查不到就用默认值。"""
    if not device.family:
        return DEFAULT_MUSIC_DIRS
    try:
        caps = capabilities_for_family_gen(device.family, device.generation or "")
    except Exception:
        caps = None
    if caps is None:
        return DEFAULT_MUSIC_DIRS
    return int(getattr(caps, "music_dirs", DEFAULT_MUSIC_DIRS) or DEFAULT_MUSIC_DIRS)


# ──────────────────────────────────────────────────────────────────────
# 计划
# ──────────────────────────────────────────────────────────────────────

def _duplicate_key(title: str, artist: str, album: str, size: int) -> tuple:
    return (title.strip().casefold(), artist.strip().casefold(), album.strip().casefold(), size)


def build_import_plan(
    device: IpodDevice,
    library: LibraryData,
    files: list[Path],
    *,
    force: bool = False,
    allow_transcode: bool = True,
    progress: ProgressCallback | None = None,
) -> ImportPlan:
    """生成导入计划，不触碰 iPod。"""
    plan = ImportPlan(device=device, library=library, existing_dicts=library.track_dicts)

    existing_keys = {
        _duplicate_key(t.title, t.artist, t.album, t.size)
        for t in library.tracks
    }

    taken = {t.location for t in library.tracks}
    allocator = PathAllocator(
        device.root,
        music_dirs=_music_dir_count(device),
        taken=taken,
    )

    seen_in_batch: set[tuple] = set()

    for index, path in enumerate(files, start=1):
        if progress is not None:
            progress(f"正在读取标签 {index}/{len(files)}：{path.name}")
        try:
            pc = read_pc_track(path)
        except UnreadableMediaError as exc:
            plan.items.append(
                PlannedTrack(
                    pc=PcTrack(source_path=path, title=path.stem),
                    ipod_location="",
                    dest_path=path,
                    db_track_id=0,
                    action="error",
                    reason=str(exc),
                )
            )
            continue

        # 批内去重（同一个文件被目录扫两次的情况）
        key = _duplicate_key(pc.title, pc.artist, pc.album, pc.size)
        if key in seen_in_batch and not force:
            seen_in_batch.add(key)
            plan.items.append(
                PlannedTrack(
                    pc=pc, ipod_location="", dest_path=path, db_track_id=0,
                    action="skip", reason="本次导入中有重复文件",
                )
            )
            continue
        seen_in_batch.add(key)

        # 与 iPod 已有曲目重复
        if key in existing_keys and not force:
            plan.items.append(
                PlannedTrack(
                    pc=pc, ipod_location="", dest_path=path, db_track_id=0,
                    action="skip", reason="iPod 上已有同一首歌（标题/艺人/专辑/大小一致）",
                )
            )
            continue

        if pc.needs_transcode and not allow_transcode:
            plan.items.append(
                PlannedTrack(
                    pc=pc, ipod_location="", dest_path=path, db_track_id=0,
                    action="error",
                    reason=f"{pc.filetype.upper()} 格式 iPod 不能直接播放，需要转码",
                )
            )
            continue

        # 转码后的目标格式是 m4a；直接拷贝的沿用原扩展名
        target_extension = ".m4a" if pc.needs_transcode else path.suffix.lower()
        location, dest = allocator.allocate(target_extension)

        plan.items.append(
            PlannedTrack(
                pc=pc,
                ipod_location=location,
                dest_path=dest,
                db_track_id=generate_db_track_id(),
                action="transcode" if pc.needs_transcode else "add",
                reason="需要转码" if pc.needs_transcode else "",
            )
        )

    return plan


# ──────────────────────────────────────────────────────────────────────
# 执行
# ──────────────────────────────────────────────────────────────────────

def execute_import(
    plan: ImportPlan,
    *,
    progress: ProgressCallback | None = None,
    transcode: Callable[[PcTrack, Path], Path] | None = None,
    write_artwork: bool = True,
    extra_playlists: list | None = None,
) -> ImportResult:
    """执行导入：拷文件 → 重写并签名数据库 → 读回校验。

    ``extra_playlists`` 让调用方在**同一次写入**里顺带建好播放列表
    （``PlaylistInfo`` 列表，同名覆盖）。曲目和列表一次写完，
    不用写两遍——两遍意味着多一次整库重写的风险窗口。
    """
    result = ImportResult(plan=plan)
    device = plan.device

    if not plan.to_add:
        if progress is not None:
            progress("没有需要导入的新曲目。")
        return result

    # 需要转码却没有转码器时，绝不能按原样拷贝——那会把 FLAC 字节写进
    # .m4a 文件，得到"文件名像 AAC、内容是 FLAC"的坏文件，iPod 上直接
    # 播不出来，而且报错信息完全对不上。
    needing_transcode = [i for i in plan.to_add if i.action == "transcode"]
    if needing_transcode and transcode is None:
        names = "、".join(i.pc.display_name for i in needing_transcode[:5])
        more = f" 等 {len(needing_transcode)} 首" if len(needing_transcode) > 5 else ""
        raise ImportError_(
            f"有 {len(needing_transcode)} 首歌需要转码，但没有可用的转码器：{names}{more}\n"
            f"  请确认已安装 ffmpeg，或用 --no-transcode 跳过这些文件。"
        )

    if not plan.fits:
        raise ImportError_(
            f"iPod 剩余空间不足：需要 {human_size(plan.bytes_to_copy)}，"
            f"只剩 {human_size(device.free_bytes)}。"
        )

    # ── 1. 复制文件 ──────────────────────────────────────────────────
    total = len(plan.to_add)
    pc_file_paths: dict[int, str] = {}

    # 只有当确实有封面可写时才启用封面链路。否则内核会在 Artwork 目录下
    # 建一个临时文件又没人清理（.iop-*.tmp 残留），既占空间又脏。
    any_artwork = any(
        item.pc.has_embedded_artwork or item.pc.cover_file for item in plan.to_add
    )
    write_artwork = write_artwork and any_artwork
    if progress is not None and not any_artwork and total:
        progress("这些文件没有封面，跳过封面写入。")

    for index, item in enumerate(plan.to_add, start=1):
        name = item.pc.display_name
        if progress is not None:
            progress(f"正在复制 {index}/{total}：{name}")
        try:
            item.dest_path.parent.mkdir(parents=True, exist_ok=True)

            source = item.pc.source_path
            if item.action == "transcode" and transcode is not None:
                produced = transcode(item.pc, item.dest_path)
                source = produced
            else:
                shutil.copy2(source, item.dest_path)

            item.pc.size = item.dest_path.stat().st_size
            result.bytes_copied += item.pc.size
            result.added += 1

            # 封面靠 db_track_id → PC 源文件 的映射建立
            if write_artwork:
                pc_file_paths[item.db_track_id] = str(item.pc.source_path)
        except Exception as exc:
            result.failed.append((str(item.pc.source_path), str(exc)))
            # 复制失败的条目不能进数据库，否则会指向不存在的文件
            item.action = "error"
            item.reason = str(exc)

    # ── 2. 组装完整曲目清单（已有 + 新增）───────────────────────────
    track_infos, _ = build_track_infos(plan.existing_dicts, progress=progress)
    for item in plan.to_add:
        if item.action == "error":
            continue
        track_infos.append(_pc_to_track_info(item))

    # ── 3. 整库重写 + 重建播放列表 + 签名 + 刷盘 + 读回校验 ──────────
    write_result = write_library(
        device,
        plan.library,
        track_infos,
        progress=progress,
        # 传 None 时写入器完全不碰 ArtworkDB；所以只有在确实有封面可写时
        # 才传字典，避免无谓地重写 57MB 的封面库。
        pc_file_paths=pc_file_paths if pc_file_paths else None,
        database_label="iTunesDB",
        extra_playlists=extra_playlists,
    )

    result.database_written = write_result.database_written
    result.verified = write_result.verified
    result.verification_note = write_result.verification_note
    return result


def _pc_to_track_info(item: PlannedTrack) -> TrackInfo:
    """PC 曲目 → 写模型。转码过的用目标文件的实际属性。"""
    pc = item.pc
    info = TrackInfo(
        title=pc.title or item.pc.source_path.stem,
        location=item.ipod_location,
        size=pc.size,
        length=pc.duration_ms,
        filetype=ipod_filetype_for_extension(item.dest_path.suffix),
        bitrate=pc.bitrate,
        sample_rate=pc.sample_rate,
        vbr=bool(pc.vbr),
        artist=pc.artist or None,
        album=pc.album or None,
        album_artist=pc.album_artist or None,
        genre=pc.genre or None,
        composer=pc.composer or None,
        comment=pc.comment or None,
        grouping=pc.grouping or None,
        year=pc.year,
        track_number=pc.track_number,
        total_tracks=pc.total_tracks,
        disc_number=pc.disc_number,
        total_discs=pc.total_discs,
        db_track_id=item.db_track_id,
        media_type=1,          # MEDIA_TYPE_AUDIO
    )
    return info
