"""数据库写入核心：导入与删除共用。

`import` 和 `remove` 的动作方向相反，但**写入阶段完全一样**：

  组装完整曲目清单 → 重建播放列表 → 整库重写 + HASH58 签名 → 刷盘 → 读回校验

差别只在于曲目清单是"已有 + 新增"还是"已有 - 删除"。所以抽出这个模块，
让两条路径共用同一套经过真机验证的写入逻辑——修复只需要修一处。

这个模块里最要命的一条是**播放列表重建**（见 `_build_playlist_args`）：
漏了它，用户的播放列表和 iPod 名字会在一次"成功"的写入里静默消失。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from iopenpod.device import flush_filesystem
from iopenpod.itunesdb_writer import TrackInfo, write_itunesdb
from iopenpod.sync import track_dict_to_info

from .discovery import IpodDevice
from .library import LibraryData, read_library

ProgressCallback = Callable[[str], None]


class DatabaseWriteError(RuntimeError):
    """写入数据库无法安全进行时抛出。抛出前 iPod 未被修改。"""


@dataclass
class WriteResult:
    """一次数据库写入的结果。"""

    track_count: int = 0
    database_written: bool = False
    verified: bool = False
    verification_note: str = ""
    playlists_rebuilt: dict[str, int] = field(default_factory=dict)
    stray_files_removed: int = 0
    artwork_touched: bool = False
    #: 清掉了播放次数增量文件（"Play Counts"）。
    play_counts_cleared: bool = False


def build_track_infos(
    track_dicts: list[dict],
    *,
    progress: ProgressCallback | None = None,
) -> tuple[list[TrackInfo], int]:
    """把扁平字典批量转成写模型。

    返回 (成功列表, 失败条数)。单条转换失败时跳过该条而不是整体失败——
    宁少一条也不要因为一条坏记录放弃整次写入。
    """
    infos: list[TrackInfo] = []
    skipped = 0
    for data in track_dicts:
        try:
            infos.append(track_dict_to_info(data))
        except Exception:
            skipped += 1
    if skipped and progress is not None:
        progress(f"警告：{skipped} 条曲目记录无法转换，已跳过。")
    return infos, skipped


def write_library(
    device: IpodDevice,
    library: LibraryData,
    track_infos: list[TrackInfo],
    *,
    progress: ProgressCallback | None = None,
    pc_file_paths: dict[int, str] | None = None,
    database_label: str = "iTunesDB",
    master_playlist_name_override: str | None = None,
    extra_playlists: list | None = None,
) -> WriteResult:
    """整库重写 + 重建播放列表 + 签名 + 刷盘 + 读回校验。

    ``pc_file_paths`` 决定封面行为：

    * 传 ``{db_track_id: PC 路径}`` —— 为这些曲目写入封面，其余曲目的**已有封面
      会被保留**（写入器先按 ``song_id`` = ``db_track_id`` 匹配，再退回
      ``mhii_link`` / ``artwork_id_ref``）。
    * 传 ``None`` —— **完全不动 ArtworkDB**。删除曲目时用这个：剩余曲目的封面引用
      原样有效，零风险；代价是被删曲目的封面条目会成为孤儿（不可见，但占空间）。

    ``master_playlist_name_override`` 用来改 iPod 的名字（名字就存在主播放列表
    标题里）。

    ``extra_playlists`` 是**要新建或覆盖**的播放列表（``PlaylistInfo`` 列表）。
    同名会替换而不是并存——同步每次都会重算播放列表成员，不覆盖的话
    用户会看到一堆同名列表越堆越多。
    """
    if not track_infos:
        raise DatabaseWriteError("没有可写入的曲目，已放弃重写数据库。")

    playlist_args, counts = _build_playlist_args(library, track_infos, progress)
    if master_playlist_name_override:
        playlist_args["master_playlist_name"] = master_playlist_name_override

    if extra_playlists:
        playlist_args["playlists"] = _upsert_playlists(
            playlist_args.get("playlists") or [], extra_playlists
        )
        counts = dict(counts)
        counts["synced"] = len(extra_playlists)

    if progress is not None:
        progress(f"正在重建 {database_label}…")

    ok = write_itunesdb(
        str(device.root),
        track_infos,
        pc_file_paths=pc_file_paths,
        capabilities=None,
        backup=True,
        **playlist_args,
    )

    result = WriteResult(
        track_count=len(track_infos),
        database_written=bool(ok),
        playlists_rebuilt=counts,
        artwork_touched=pc_file_paths is not None,
    )
    if not ok:
        raise DatabaseWriteError(
            "写入 iTunesDB 失败，iPod 数据库未被修改（已保留备份）。"
        )

    # FAT32 不刷缓存就拔线会丢数据
    try:
        flush_filesystem(str(device.root))
    except Exception:
        pass

    result.stray_files_removed = cleanup_stray_temp_files(device.root)
    if result.stray_files_removed and progress is not None:
        progress(f"已清理 {result.stray_files_removed} 个残留临时文件。")

    if progress is not None:
        progress("正在读回校验…")
    result.verified, result.verification_note = verify_reload(
        device, expected_total=len(track_infos)
    )

    # 播放次数增量已经合并进曲目、随这次写库落盘了，把增量文件清掉。
    # 详见 _clear_play_counts 的说明。
    result.play_counts_cleared = _clear_play_counts(device)
    if result.play_counts_cleared and progress is not None:
        progress("已清理播放次数增量文件（内容已并入曲库）。")
    return result


def _clear_play_counts(device: IpodDevice) -> bool:
    """清掉已经吸收进库的播放次数增量文件。

    ``iPod_Control/iTunes/Play Counts`` 存的是"iPod 上自上次同步以来积累的
    播放/跳过次数"增量。读库时内核会把它合并进曲目（libgpod 的 get_mhit
    逻辑），**但写完之后必须清掉**——iTunes 就是这么做的。

    实测不清的后果：

    * 每读一次库就重新合并一遍，日志里每 6 秒一条
      ``Track count (2) != Play Counts entry count (3)``
      ——那份残留是 **2023-10-08** 的，库里早换过好几轮了。
    * 更糟的是合并**按下标配对**（``entries[i]`` ↔ ``tracks[i]``）：
      库里的歌换过之后，旧歌的播放次数会算到新歌头上，而且随写库固化下来。

    只删这一个文件；删不掉也不影响写库结果（最多继续报那条警告）。
    """
    path = Path(device.root) / "iPod_Control" / "iTunes" / "Play Counts"
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        pass
    return False


def _upsert_playlists(existing: list, incoming: list) -> list:
    """按名字合并播放列表：同名的用新的替换，其余原样保留。

    这样"同步同一个歌单"每次跑都是覆盖，不会在 iPod 上堆出一串同名列表。

    ★ **替换时必须继承旧列表的 `playlist_id`。**

    写入器在 `playlist_id is None` 时**会生成一个新的**。于是"改一次成员 =
    换一个身份"，后果有两个：

    * 界面上刚拿到的 id 立刻失效——下一次拿它去操作就是 404（实测踩到：
      加完歌再读这个歌单，报"iPod 上没有这个歌单"，其实就在那儿）。
    * **同步每跑一次，同名播放列表就换一个 id**。设备上任何按 id 引用它的
      东西（On-The-Go、Genius 等）就断了。这是静默的数据完整性问题，
      不会报错，只会让人觉得"iPod 上的播放列表怪怪的"。

    同名就是同一个播放列表，身份必须延续。
    """
    import dataclasses

    incoming_names = {getattr(p, "name", "") for p in incoming}
    kept = [p for p in existing if getattr(p, "name", "") not in incoming_names]
    by_name = {getattr(p, "name", ""): p for p in existing}

    merged: list = []
    for item in incoming:
        old = by_name.get(getattr(item, "name", ""))
        old_id = getattr(old, "playlist_id", None) if old is not None else None
        if old_id and not getattr(item, "playlist_id", None):
            if dataclasses.is_dataclass(item):
                item = dataclasses.replace(item, playlist_id=old_id)
            else:
                try:
                    item.playlist_id = old_id
                except Exception:  # noqa: BLE001 - 不可写就算了，别因此写坏库
                    pass
        merged.append(item)
    return kept + merged


def _build_playlist_args(
    library: LibraryData,
    track_infos: list[TrackInfo],
    progress: ProgressCallback | None,
) -> tuple[dict, dict[str, int]]:
    """从原库重建播放列表，返回 (write_itunesdb 参数, 各类播放列表数量)。

    为什么非做不可：``write_itunesdb()`` 是整库重写。不传 ``playlists`` 参数，
    它就只写一个默认主播放列表——名字默认叫 "iPod"。后果是用户播放列表丢失、
    智能播放列表丢失，**连 iPod 的名字都被改掉**（名字存在主播放列表标题里）。
    整个过程不报错，属于静默数据丢失。

    这里传进去的 ``track_infos`` 是**最终**要留在库里的曲目。重建函数会据此算出
    合法曲目集合，把播放列表里指向已不存在曲目的条目自动丢弃——删除曲目时
    靠的就是这个机制清理幽灵条目，不需要单独写清理逻辑。

    重建失败时**拒绝写入**而不是退回默认值：宁可报错也不能悄悄抹掉用户数据。
    """
    raw = library.raw or {}
    ds2_raw = raw.get("mhlp") or []
    ds3_raw = raw.get("mhlp_podcast") or []
    ds5_raw = raw.get("mhlp_smart") or []

    # 除主列表外没有别的播放列表，重建没有意义
    has_extra = len(ds2_raw) > 1 or ds3_raw or ds5_raw
    counts: dict[str, int] = {}

    try:
        from iopenpod.sync import build_and_evaluate_playlists

        (
            ds2_name,
            ds2_id,
            ds2_playlists,
            ds3_name,
            ds3_id,
            ds3_playlists,
            ds5_playlists,
        ) = build_and_evaluate_playlists(
            library.track_dicts,
            ds2_raw,
            ds3_raw,
            ds5_raw,
            track_infos,
        )
    except Exception as exc:
        if has_extra:
            raise DatabaseWriteError(
                f"重建播放列表失败，为避免丢失你现有的播放列表，已放弃写入。\n"
                f"  原因：{exc}\n"
                f"  iPod 未被修改。请把这个错误反馈出来。"
            ) from exc
        return {}, counts

    args: dict = {}
    if ds2_name:
        args["master_playlist_name"] = ds2_name
    if ds2_id is not None:
        args["master_playlist_id"] = ds2_id
    if ds2_playlists:
        args["playlists"] = ds2_playlists
    if ds3_name:
        args["podcast_master_playlist_name"] = ds3_name
    if ds3_id is not None:
        args["podcast_master_playlist_id"] = ds3_id
    if ds3_raw:
        # ★ **必须显式传**，哪怕列表是空的。
        #
        # 写入器那条路径的语义是：`podcast_playlists=None` → **把 dataset 2 的歌单
        # 克隆一份到 dataset 3**（`mhbd_writer.py` 里 `source_playlists_type3 =
        # playlists_type2 if playlists_type3 is None else playlists_type3`）。
        # 那是 libgpod **新建库**时的兼容行为；**重写已有设备库**时用它是错的：
        # 设备上"没有非主播客歌单"本来就是常态（`ds3_playlists` 会是空的），
        # 于是每导一次歌，普通歌单就被复制一份进播客数据集，越堆越多。
        #
        # 实测症状：新建一个歌单后，`mhlp` 和 `mhlp_podcast` 里各出现一条同名，
        # 界面上看到两个；改名/删除只动得掉普通那份，播客那份留下——
        # 看起来就像"删了没删掉"。
        #
        # 传空列表是有意义的（写入器文档明说）：dataset 3 只写它自己生成的主列表。
        # 设备**完全没有** ds3 时不传（空 `ds3_raw`），保持新建库的兼容行为。
        args["podcast_playlists"] = list(ds3_playlists or [])
    elif ds3_playlists:
        args["podcast_playlists"] = ds3_playlists
    if ds5_playlists:
        args["smart_playlists"] = ds5_playlists

    counts = {
        "standard": len(ds2_playlists),
        "podcast": len(ds3_playlists),
        "smart": len(ds5_playlists),
    }
    if progress is not None and has_extra:
        progress(
            f"已重建播放列表：普通 {counts['standard']} 个，"
            f"播客 {counts['podcast']} 个，智能 {counts['smart']} 个"
        )
    return args, counts


def cleanup_stray_temp_files(ipod_root: Path) -> int:
    """清掉 iPod 上残留的 ``.iop-*.tmp`` 临时文件。

    内核的原子写入用"临时文件 + 改名"实现。正常路径下临时文件会被改名掉，
    但当没有任何封面可写时，Artwork 目录里会留下一个没人管的临时文件。
    这里做一次兜底清扫，避免空间被慢慢吃掉。
    """
    removed = 0
    for directory in (
        Path(ipod_root) / "iPod_Control" / "Artwork",
        Path(ipod_root) / "iPod_Control" / "iTunes",
        Path(ipod_root) / "iPod_Control" / "Music",
    ):
        if not directory.is_dir():
            continue
        try:
            for entry in directory.rglob(".iop-*.tmp"):
                try:
                    entry.unlink()
                    removed += 1
                except OSError:
                    continue
        except OSError:
            continue
    return removed


def verify_reload(device: IpodDevice, *, expected_total: int) -> tuple[bool, str]:
    """重读数据库，确认曲目数对得上。

    这是唯一能发现"写坏了"的手段：写入函数返回 True 不代表固件能读。
    """
    try:
        reloaded = read_library(device.root)
    except Exception as exc:
        return False, f"读回失败：{exc}"

    actual = len(reloaded.tracks)
    if actual != expected_total:
        return False, (
            f"曲目数不一致：期望 {expected_total} 首，实际读到 {actual} 首。\n"
            f"  建议用备份还原 iPod_Control（写前已生成 iTunesDB.backup），"
            f"或重新连接 iPod 检查。"
        )
    return True, f"校验通过：共 {actual} 首曲目"
