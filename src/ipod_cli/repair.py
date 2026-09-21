"""设备修复：找出并清掉"数据库与磁盘对不上"的部分。

## 为什么需要它

同步是「先拷文件、再写数据库」两步。第二步失败（异常、或者进程被杀），
文件已经在设备上，而 iPod 的界面**只认数据库**——于是这些文件既看不见、
又白占空间，下次同步还会重新分配文件名再拷一遍，失败一次多一份。
实测堆到过 295 个文件、6.4 GB。

反过来也会坏：**数据库里有记录、磁盘上没文件**。那更糟——iPod 上显示这首歌，
点进去播不出来，还查不出原因。

所以修复要管三个方向，各自独立开关（清理是不可逆的，得让用户逐项决定）：

| 类别 | 现象 | 处理 |
| --- | --- | --- |
| ``orphans`` | 文件在磁盘上、数据库不认 | 删文件（安全：数据库不引用它就等于不存在）|
| ``broken`` | 数据库认、文件不在 | 删记录（**整库重写**，风险高）|
| ``stray_temp`` | 写入中断留下的临时文件 | 删文件（安全）|

## 一条容易写错的地方

路径比对**必须不分大小写**。iPod 用的是 FAT32，同一个文件在不同场合回显的
大小写可能不一致；按大小写敏感去比，会把好好的文件报成孤儿然后**删掉**。
`verify.py` 里的同类检查没有折叠大小写，这里**故意不一样**——修坏东西的
代价比漏报大得多。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .discovery import IpodDevice, human_size
from .library import LibraryData, Track, read_library
from .remover import prune_empty_music_dirs

ProgressCallback = Callable[[str], None]

#: 内核原子写入用的临时文件前缀（``.iop-xxxx.tmp``）。正常路径会被改名掉，
#: 中途失败就留下。
KERNEL_TEMP_GLOB = ".iop-*.tmp"

#: iTunes / Artwork 目录下的临时文件。这些不是内核写的，是 iTunes 或别的
#: 工具留下的写入暂存；同样没人清理。
HOST_TEMP_SUFFIXES = (".tmp", ".partial")


class RepairError(RuntimeError):
    """修复无法安全进行时抛出。抛出前设备未被修改。"""


@dataclass
class OrphanFile:
    """磁盘上有、数据库不认的一个文件。"""

    rel: str        # 相对设备根，形如 iPod_Control/Music/F00/XXXX.m4a
    size: int

    @property
    def name(self) -> str:
        return self.rel.rsplit("/", 1)[-1]

    @property
    def size_text(self) -> str:
        return human_size(self.size)


@dataclass
class RepairScan:
    """一次完整扫描的结果。**只读**，不产生任何副作用。"""

    device: IpodDevice
    library: LibraryData | None = None

    #: 数据库里的曲目数
    db_tracks: int = 0
    #: 磁盘 Music/ 里的音频文件数
    disk_files: int = 0

    #: 文件在、记录不在
    orphans: list[OrphanFile] = field(default_factory=list)
    #: 记录在、文件不在
    broken: list[Track] = field(default_factory=list)
    #: 残留临时文件（相对设备根）
    stray_temp: list[str] = field(default_factory=list)

    @property
    def orphan_bytes(self) -> int:
        return sum(o.size for o in self.orphans)

    @property
    def orphan_text(self) -> str:
        return human_size(self.orphan_bytes)

    @property
    def broken_bytes(self) -> int:
        return sum(t.size for t in self.broken)

    @property
    def broken_text(self) -> str:
        return human_size(self.broken_bytes)

    @property
    def temp_count(self) -> int:
        return len(self.stray_temp)

    @property
    def is_clean(self) -> bool:
        """三样都没有 —— 数据库和磁盘完全一致。"""
        return not (self.orphans or self.broken or self.stray_temp)

    @property
    def has_fixable(self) -> bool:
        """有可以清理的东西（孤儿 / 临时文件）。断链记录不算"可以随手清"。"""
        return bool(self.orphans or self.stray_temp)

    def summary_text(self) -> str:
        if self.is_clean:
            return "数据库和磁盘完全一致，没有需要修复的地方。"
        parts: list[str] = []
        if self.orphans:
            parts.append(f"{len(self.orphans)} 个孤儿文件（{self.orphan_text}）")
        if self.broken:
            parts.append(f"{len(self.broken)} 首有记录但没文件")
        if self.stray_temp:
            parts.append(f"{len(self.stray_temp)} 个残留临时文件")
        return "、".join(parts)


# ──────────────────────────────────────────────────────────────────────
# 扫描
# ──────────────────────────────────────────────────────────────────────


def _music_root(device: IpodDevice) -> Path:
    return Path(device.root) / "iPod_Control" / "Music"


def _temp_roots(device: IpodDevice) -> list[Path]:
    control = Path(device.root) / "iPod_Control"
    return [control / "iTunes", control / "Artwork", control / "Music"]


def scan_device(
    device: IpodDevice,
    *,
    library: LibraryData | None = None,
    progress: ProgressCallback | None = None,
) -> RepairScan:
    """扫描设备，返回三类问题的清单。**不修改任何东西。**"""
    root = Path(device.root)
    if library is None:
        if progress is not None:
            progress("正在读取数据库…")
        library = read_library(root)

    scan = RepairScan(device=device, library=library, db_tracks=len(library.tracks))

    # ── 数据库引用的文件位置（折叠大小写，理由见模块开头）──────────────
    referenced = {
        _norm_rel(str(track.location))
        for track in library.tracks
        if track.location
    }

    # ── 磁盘上的音频文件 ────────────────────────────────────────────
    if progress is not None:
        progress("正在核对磁盘文件…")
    # 键是归一化后的路径（只用来比对），值是**原始大小写**的路径 + 大小
    # —— 显示和删除都得用真名字。折叠出来的小写路径拿去 unlink 在 FAT32 上
    # 能成功，但打印给用户看会让人以为文件叫这个名字。
    on_disk: dict[str, tuple[str, int]] = {}
    music = _music_root(device)
    if music.is_dir():
        for path in music.rglob("*"):
            if not path.is_file() or path.name.startswith("."):
                continue
            rel = path.relative_to(root).as_posix()
            try:
                on_disk[_norm_rel(rel)] = (rel, path.stat().st_size)
            except OSError:
                continue

    scan.disk_files = len(on_disk)

    # 磁盘有、数据库不认 → 孤儿
    scan.orphans = sorted(
        (
            OrphanFile(rel=real_rel, size=size)
            for key, (real_rel, size) in on_disk.items()
            if key not in referenced
        ),
        key=lambda o: o.rel,
    )

    # 数据库认、磁盘没有 → 断链记录
    scan.broken = [
        track
        for track in library.tracks
        if track.location and _norm_rel(str(track.location)) not in on_disk
    ]

    # ── 残留临时文件 ────────────────────────────────────────────────
    for directory in _temp_roots(device):
        if not directory.is_dir():
            continue
        for entry in _iter_temp_files(directory):
            try:
                scan.stray_temp.append(entry.relative_to(root).as_posix())
            except ValueError:
                scan.stray_temp.append(str(entry))
    scan.stray_temp.sort()

    return scan


def _iter_temp_files(directory: Path):
    """目录下所有"没人管的临时文件"。

    两来源：内核原子写入的 ``.iop-*.tmp``，以及 iTunes / 别的工具留下的
    ``*.tmp``。**刻意不递归进 Music 的子目录之外**——只扫传入的那几个目录，
    避免误删别的东西。
    """
    try:
        for entry in directory.rglob("*"):
            if not entry.is_file():
                continue
            name = entry.name
            if name.endswith(".tmp") and name.startswith(".iop-"):
                yield entry
            elif name.lower().endswith(HOST_TEMP_SUFFIXES):
                yield entry
    except OSError:
        return


def _norm_rel(rel: str) -> str:
    """把数据库里的 location 和磁盘相对路径归一成同一种形状。

    数据库存的是 ``:iPod_Control:Music:F00:XXXX.mp3``，磁盘那边是
    ``iPod_Control/Music/F00/XXXX.mp3``。另外 **FAT32 的大小写不可靠**，
    所以一律折叠成小写再比——按大小写敏感去比会把正常文件判成孤儿，然后删掉。
    """
    return rel.strip(":").replace(":", "/").replace("\\", "/").strip("/").casefold()


# ──────────────────────────────────────────────────────────────────────
# 清理
# ──────────────────────────────────────────────────────────────────────


@dataclass
class CleanResult:
    """一次清理的结果。"""

    kind: str                                  # orphans / broken / stray_temp
    removed: int = 0
    bytes_freed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    note: str = ""

    @property
    def freed_text(self) -> str:
        return human_size(self.bytes_freed)


def clean_orphans(
    device: IpodDevice,
    orphans: list[OrphanFile],
    *,
    progress: ProgressCallback | None = None,
) -> CleanResult:
    """删掉孤儿文件。清完顺手收掉空目录。

    ``orphans`` 由 :func:`scan_device` 给出——**不要**在这里重新扫一遍然后
    按位置配对删：扫到删之间设备可能被外部改动，那会删错文件。调用方负责
    "扫完给用户看过、确认没错"这一步。
    """
    result = CleanResult(kind="orphans")
    root = Path(device.root)
    total = len(orphans)
    for index, orphan in enumerate(orphans, start=1):
        if progress is not None:
            progress(f"正在删除孤儿文件 {index}/{total}：{orphan.name}")
        path = root / orphan.rel
        try:
            if not path.is_file():
                # 已经被外部删掉了：不算失败，不释放空间
                continue
            size = path.stat().st_size
            path.unlink()
            result.removed += 1
            result.bytes_freed += size
        except OSError as exc:
            result.errors.append((orphan.rel, str(exc)))

    prune_empty_music_dirs(root)
    if result.removed:
        result.note = f"已删除 {result.removed} 个孤儿文件，释放 {result.freed_text}"
    else:
        result.note = "没有可删的孤儿文件"
    return result


def clean_stray_temp(
    device: IpodDevice,
    stray: list[str],
    *,
    progress: ProgressCallback | None = None,
) -> CleanResult:
    """删掉残留临时文件。"""
    result = CleanResult(kind="stray_temp")
    root = Path(device.root)
    total = len(stray)
    for index, rel in enumerate(stray, start=1):
        if progress is not None:
            progress(f"正在删除临时文件 {index}/{total}：{Path(rel).name}")
        path = root / rel
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
            path.unlink()
            result.removed += 1
            result.bytes_freed += size
        except OSError as exc:
            result.errors.append((rel, str(exc)))

    result.note = (
        f"已删除 {result.removed} 个临时文件，释放 {result.freed_text}"
        if result.removed else "没有可删的临时文件"
    )
    return result


def clean_broken_records(
    device: IpodDevice,
    library: LibraryData,
    broken: list[Track],
    *,
    progress: ProgressCallback | None = None,
) -> CleanResult:
    """删掉"有记录、没文件"的曲目记录。

    **这是整库重写**，写下去不好撤销，所以单独一个函数、单独一个开关，
    界面上也必须说清楚。复用删除曲目那条已经验证过的链路
    （``build_remove_plan`` → ``execute_remove``）：它先写库、后删文件，
    而且播放列表的幽灵条目由写入器自动丢弃。
    """
    from .remover import build_remove_plan, execute_remove  # noqa: PLC0415

    result = CleanResult(kind="broken")
    if not broken:
        result.note = "没有断链记录"
        return result

    plan = build_remove_plan(device, library, list(broken))
    outcome = execute_remove(plan, progress=progress)

    result.removed = plan.count
    result.bytes_freed = plan.bytes_freed
    if not outcome.verified:
        result.note = (
            f"已清掉 {plan.count} 条断链记录，但**读回校验未通过**："
            f"{outcome.verification_note}"
        )
        result.errors.append(("校验", outcome.verification_note))
    else:
        result.note = f"已清掉 {plan.count} 条断链记录（数据库已重写并校验通过）"
    return result


__all__ = [
    "CleanResult",
    "OrphanFile",
    "RepairError",
    "RepairScan",
    "clean_broken_records",
    "clean_orphans",
    "clean_stray_temp",
    "prune_empty_music_dirs",
    "scan_device",
]
