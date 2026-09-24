"""从 iPod 上删除歌曲。

删除比导入危险，因为它是破坏性的。所以流程刻意保守，三处设计值得一提：

**一、先写库，后删文件。** 反过来的话，一旦写库失败就会得到"数据库里有记录、
磁盘上没文件"的状态——那是最糟的：iPod 上显示这首歌，点进去播不出来，
而且报错信息对不上。先写库最坏情况只是留下孤儿文件（白占空间，无害）。

**二、播放列表的幽灵条目不用单独清理。** 写入核心重建播放列表时会算出"合法曲目
集合"，自动丢弃指向已不存在曲目的条目。所以删除曲目后播放列表自动干净。

**三、默认不动封面库。** 传 ``pc_file_paths=None`` 让写入器完全不碰 ArtworkDB，
剩余曲目的封面引用原样有效，零风险。代价是被删曲目的封面条目变成孤儿
（不可见、占空间）。``compact_artwork=True`` 会连封面一起重写去重，
把孤儿收掉——多花时间，换回空间。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .dbwrite import build_track_infos, write_library
from .discovery import IpodDevice, human_size
from .library import LibraryData, Track
from .mediafile import collect_audio_files  # noqa: F401  (供 CLI 复用筛选路径)

ProgressCallback = Callable[[str], None]


class RemoveError(RuntimeError):
    """删除无法安全进行时抛出。抛出前 iPod 未被修改。"""


@dataclass
class RemovePlan:
    """一次删除的完整计划（不产生任何副作用）。"""

    device: IpodDevice
    library: LibraryData
    to_remove: list[Track] = field(default_factory=list)
    keep_dicts: list[dict] = field(default_factory=list, repr=False)

    @property
    def count(self) -> int:
        return len(self.to_remove)

    @property
    def bytes_freed(self) -> int:
        return sum(t.size for t in self.to_remove)

    @property
    def file_paths(self) -> list[Path | None]:
        """每首待删曲目对应的磁盘路径，**与 ``to_remove`` 一一对齐**。

        路径异常（location 为空）的曲目占位为 None 而不是被过滤掉——
        过滤会让两个列表长度不一致，按位置配对时就串行了。
        """
        root = Path(self.device.root)
        out: list[Path | None] = []
        for track in self.to_remove:
            rel = track.location.strip(":").replace(":", "/")
            out.append((root / rel) if rel else None)
        return out

    @property
    def files_to_delete(self) -> list[Path]:
        """真正存在于磁盘、可以删的文件。"""
        return [
            path
            for path in self.file_paths
            if path is not None and path.is_file()
        ]

    @property
    def remaining(self) -> int:
        return len(self.library.tracks) - self.count


@dataclass
class RemoveResult:
    plan: RemovePlan
    database_written: bool = False
    verified: bool = False
    verification_note: str = ""
    files_deleted: int = 0
    #: 实际从磁盘删掉的字节数。**不等于 ``plan.bytes_freed``**——那个是数据库里
    #: 记的大小，文件早就不在时它照样非零（设备修复清断链记录正是这个情形）。
    bytes_deleted: int = 0
    file_errors: list[tuple[str, str]] = field(default_factory=list)
    playlists_rebuilt: dict[str, int] = field(default_factory=dict)


def build_remove_plan(
    device: IpodDevice,
    library: LibraryData,
    tracks: list[Track],
) -> RemovePlan:
    """生成删除计划。

    ``tracks`` 是要删的曲目（通常来自 ``exporter.select_tracks`` 的筛选结果）。
    用 ``db_track_id`` 反查原始字典构建保留清单——比按标题匹配可靠，
    标题可能重复。
    """
    if not tracks:
        raise RemoveError("没有匹配到任何曲目，什么都没做。")

    doomed_ids = {t.db_track_id for t in tracks}
    keep = [d for d in library.track_dicts if d.get("db_track_id") not in doomed_ids]

    if not keep:
        # 这条拦的是"把曲库删空"。**故意不放开**（用户明确要求保留）：
        # 删除是整库重写，写下去不可逆，误点的代价太大。
        #
        # 所以消息不能只说"不给做"——用户是在逐首清理时撞上它的
        # （实测：删到只剩 1 首，再删就没反应了），光说"不提供清空操作"
        # 只会让人卡在那儿。得把**为什么**和**那怎么办**一起给出来。
        raise RemoveError(
            f"这会删掉 iPod 上全部 {len(tracks)} 首曲目，把曲库清空。\n"
            f"\n"
            f"本工具不提供清空操作——删除是整库重写，写下去没法撤销，\n"
            f"误点的代价太大。想清空有更稳妥的路子：\n"
            f"\n"
            f"  · 用 Finder / iTunes 的「恢复 iPod」\n"
            f"  · 或在 iPod 上「设置 → 通用 → 还原 → 抹掉所有内容和设置」\n"
            f"\n"
            f"只是想换歌的话，留一首、把其余的删掉就行。"
        )

    return RemovePlan(device=device, library=library, to_remove=list(tracks), keep_dicts=keep)


def execute_remove(
    plan: RemovePlan,
    *,
    progress: ProgressCallback | None = None,
    compact_artwork: bool = False,
) -> RemoveResult:
    """执行删除：先重写数据库，再删文件。"""
    result = RemoveResult(plan=plan)
    device = plan.device

    # ── 1. 组装保留清单 ──────────────────────────────────────────────
    keep_infos, _ = build_track_infos(plan.keep_dicts, progress=progress)
    if not keep_infos:
        raise RemoveError("保留清单为空，已放弃写入（数据库未被修改）。")

    # ── 2. 重写数据库（此刻文件还没删）───────────────────────────────
    #
    # pc_file_paths=None → 完全不碰 ArtworkDB，剩余曲目的封面引用原样有效。
    # compact_artwork 时传空字典，让写入器重写封面库并顺手收掉孤儿条目。
    write_result = write_library(
        device,
        plan.library,
        keep_infos,
        progress=progress,
        pc_file_paths={} if compact_artwork else None,
        database_label="iTunesDB",
    )
    result.database_written = write_result.database_written
    result.verified = write_result.verified
    result.verification_note = write_result.verification_note
    result.playlists_rebuilt = write_result.playlists_rebuilt

    # ── 3. 删文件 ────────────────────────────────────────────────────
    #
    # 数据库已经不含这些曲目了，所以文件删不掉也无所谓——最多留几个孤儿文件，
    # 用户可以跑一次设备修复（桌面端「iPod 音乐管理」→「数据库修复」）清掉：
    # 它就是靠 scan_device 把这类孤儿扫出来再删的。
    total = len(plan.to_remove)
    for index, (track, path) in enumerate(
        zip(plan.to_remove, plan.file_paths, strict=True), start=1
    ):
        if path is None:
            continue
        if progress is not None:
            progress(f"正在删除文件 {index}/{total}：{track.title or path.name}")
        try:
            if path.is_file():
                # 字节数先量后删，量不到就当 0：**删不删只取决于文件在不在**，
                # 统计失败不能把这次删除也带下去
                try:
                    result.bytes_deleted += path.stat().st_size
                except OSError:
                    pass
                path.unlink()
                result.files_deleted += 1
        except OSError as exc:
            result.file_errors.append((str(path), str(exc)))

    # ── 4. 清掉空目录里遗留的残骸 ────────────────────────────────────
    prune_empty_music_dirs(Path(device.root))
    return result


def prune_empty_music_dirs(ipod_root: Path) -> int:
    """删掉 Music 下的空子目录，返回删了几个。

    只删 ``F00`` 这类空目录；``Music`` 本身永远保留——有些固件会检查它是否存在。

    **公开**：设备修复（``ipod_cli.repair``）清完孤儿文件后也要收一遍空目录，
    两处必须用同一份实现——分成两份迟早会有一份忘了保留 ``Music`` 本身。
    """
    music = Path(ipod_root) / "iPod_Control" / "Music"
    if not music.is_dir():
        return 0
    removed = 0
    for child in music.iterdir():
        if not child.is_dir():
            continue
        try:
            if not any(child.iterdir()):
                child.rmdir()
                removed += 1
        except OSError:
            continue
    return removed


def summarize(plan: RemovePlan) -> str:
    """一行摘要，给确认提示用。"""
    return (
        f"将删除 {plan.count} 首，释放 {human_size(plan.bytes_freed)}，"
        f"剩余 {plan.remaining} 首"
    )


__all__ = [
    "RemoveError",
    "RemovePlan",
    "RemoveResult",
    "build_remove_plan",
    "execute_remove",
    "summarize",
]
