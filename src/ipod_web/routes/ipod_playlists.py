"""iPod 上的播放列表：查看、新建、改名、删除、增删成员。

## 跟 `routes/playlists.py` 的区别

那个文件管的是**网易云在线歌单**（浏览、下载、同步到 iPod）；这个文件管的是
**iPod 自己那份播放列表结构**。名字像，做的事完全不同——混在一起会让人以为
"同步歌单"和"管理歌单"是一回事。

## 三条安全边界（不做会出事）

1. **主播放列表不可删、不可改名**。iPod 的名字就存在它的标题里（判断逻辑跟
   `ipod_cli/verify.py` 一致：常规列表里曲目数等于总曲目数的那个）。
   删掉它之后设备名会变成默认的 "iPod"——属于静默改用户数据。
2. **智能播放列表只读**。它的成员是规则算出来的，手改没有意义（下次一算就回去）。
   必须给**中文原因**，不能静默失败或假装成功。
3. **删除必须带预览令牌**。沿用曲目删除那套 `PreviewStore`：没有真实预览过的
   令牌就直接拒绝。这样"先看清再删"是**结构性**保证，不是指望界面记得弹确认。

## 写库路径

复用同步在用的那条：`build_track_infos` → `write_library(..., extra_playlists=...)`。

* 它**从原库重建全部播放列表**（`_build_playlist_args`），所以只传要改的那个，
  其余原样保留。
* `extra_playlists` 按**名字**合并（同名替换），所以"改成员"= 算出新的完整成员
  列表再传进去。
* `pc_file_paths=None` → **不动封面库**。改歌单跟封面无关，少动一样就少一类风险。

**删除**是另一条路：`extra_playlists` 只能"新增/同名替换"，表达不了删除。
播放列表本来就从 `library.raw` 重建，所以**从 raw 里去掉那一行**即可。

## ID 空间（写错会写进错的歌）

* 读成员：`row["items"]` 里是 `track_id`（设备曲目 id）
* 写成员：`PlaylistInfo.track_ids` 要的是 **`db_track_id`**
* 所以中间必须过一次映射（`_tid_to_db_id`），不能混用。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ipod_cli.discovery import DeviceNotFoundError
from ipod_web.context import WebContext
from ipod_web.deps import get_ctx
from ipod_web.routes.library import PreviewStore, track_row

router = APIRouter(prefix="/api/library/playlists", tags=["iPod 歌单"])

log = logging.getLogger("ipod_web")

#: 三个播放列表数据集：(raw 里的 key, 中文名)
DATASETS: tuple[tuple[str, str], ...] = (
    ("mhlp", "普通"),
    ("mhlp_podcast", "播客"),
    ("mhlp_smart", "智能"),
)

#: 新建的播放列表统一落在这个数据集（普通播放列表）。智能列表要规则、
#: 播客列表属于播客，都不该由"新建歌单"产生。
TARGET_DATASET = "mhlp"

#: 歌单名长度上限。iPod 的 MHOD 字符串没那么长的必要，也给界面一个明确约束。
MAX_NAME_LEN = 120

#: 删除预览令牌。**独立实例**是有意的：歌单删除和曲目删除各管各的令牌，
#: 一个过期/被消费不影响另一个。类本身复用（语义必须一致）。
_previews = PreviewStore()


class CreateRequest(BaseModel):
    name: str = ""
    #: 可选：建好就把这些曲目放进去（IPod 曲目 id，**字符串**，理由见 library.py）
    track_ids: list[str] = Field(default_factory=list)


class RenameRequest(BaseModel):
    name: str = ""


class DeletePreviewRequest(BaseModel):
    playlist_id: str = ""


class DeleteRequest(BaseModel):
    playlist_id: str = ""
    preview_id: str = ""


class TracksRequest(BaseModel):
    #: 要加进去的曲目（iPod 曲目 id，字符串）
    add: list[str] = Field(default_factory=list)
    #: 要移出去的曲目
    remove: list[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────
# 读：解析播放列表结构
# ──────────────────────────────────────────────────────────────────────


def _all_rows(library) -> list[tuple[str, dict]]:
    """三个数据集里的所有播放列表行，带 (数据集 key, 行) 一起返回。"""
    raw = library.raw or {}
    rows: list[tuple[str, dict]] = []
    for key, _label in DATASETS:
        for row in raw.get(key) or []:
            if isinstance(row, dict):
                rows.append((key, row))
    return rows


def _playlist_id_of(row: dict) -> int:
    try:
        return int(row.get("playlist_id", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _name_of(row: dict) -> str:
    return str(row.get("Title") or row.get("name") or "").strip()


def _dataset_label(key: str) -> str:
    for k, label in DATASETS:
        if k == key:
            return label
    return key


def _dataset_master_ids(library) -> set[int]:
    """每个数据集的**主播放列表** id 集合。

    ★ 两个数据集各有自己的主列表：普通（`mhlp`）的和播客（`mhlp_podcast`）的，
    而且**两者的标题都是 iPod 的名字**（真机与虚拟设备实测都是如此）。所以：

    * 列表里会看到两条同名——那是设备的真实结构，不是重复。
    * 两个都不可删/不可改名：普通那个是 iPod 名的载体；播客那个是播客数据集的
      载体，删了播客结构就没了。

    判断跟 `verify.py` 一致：本数据集里曲目数等于总曲目数的那一行；没有就取
    第一行（写入器也依赖"第一行是主列表"这个结构，见 `_build_playlist_args`）。
    """
    raw = library.raw or {}
    total = len(library.tracks)
    out: set[int] = set()
    for key in ("mhlp", "mhlp_podcast"):
        rows = raw.get(key) or []
        if not rows:
            continue
        master = None
        for row in rows:
            if total and len(row.get("items") or []) == total:
                master = row
                break
        if master is None:
            master = rows[0]
        pid = _playlist_id_of(master)
        if pid:
            out.add(pid)
    return out


def _master_playlist_id(library) -> int:
    """**普通数据集**的主播放列表 id（iPod 的名字就存在它的标题里）。

    只认普通那个：改名设备、判断"能不能删"这类事指的是它。
    两个数据集的主列表都不可动——用 `_dataset_master_ids`。
    """
    raw = library.raw or {}
    standard = raw.get("mhlp") or []
    total = len(library.tracks)
    for row in standard:
        if total and len(row.get("items") or []) == total:
            return _playlist_id_of(row)
    if standard:
        return _playlist_id_of(standard[0])
    return 0


def _tid_to_db_id(library) -> dict[int, int]:
    """`track_id` → `db_track_id`。

    读成员拿到的是 `track_id`，写成员要的是 `db_track_id`，中间必须过这一层。
    """
    mapping: dict[int, int] = {}
    for track in library.tracks:
        try:
            tid = int(track.track_id or 0)
            db_id = int(track.db_track_id or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if tid and db_id:
            mapping[tid] = db_id
    return mapping


def _db_id_to_track(library) -> dict[int, Any]:
    out: dict[int, Any] = {}
    for track in library.tracks:
        try:
            out[int(track.db_track_id)] = track
        except (TypeError, ValueError, OverflowError):
            continue
    return out


def _member_db_ids(row: dict, tid_to_db: dict[int, int]) -> list[int]:
    """这一行的成员，转成 `db_track_id` 列表（保持原顺序，去重）。

    指向已不存在曲目的条目**直接丢掉**——它们就是"幽灵条目"，重建机制本来
    也会丢，这里先丢掉是为了让"改成员"算出的新列表不含幽灵。
    """
    out: list[int] = []
    seen: set[int] = set()
    for item in row.get("items") or []:
        if not isinstance(item, dict):
            continue
        raw_tid = item.get("track_id")
        try:
            tid = int(raw_tid or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        db_id = tid_to_db.get(tid)
        if db_id and db_id not in seen:
            seen.add(db_id)
            out.append(db_id)
    return out


def _find_row(library, playlist_id: str) -> tuple[str, dict]:
    """按 playlist_id 找 (数据集 key, 行)。找不到抛 404。"""
    try:
        want = int(playlist_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="歌单 id 不是数字。") from None
    for key, row in _all_rows(library):
        if _playlist_id_of(row) == want:
            return key, row
    raise HTTPException(status_code=404, detail="iPod 上没有这个歌单（可能已被删除）。")


def _editable_or_reason(library, key: str, row: dict,
                        *, for_delete: bool = False) -> str | None:
    """能不能改这个歌单。通过返回 None，否则返回中文原因。"""
    if key == "mhlp_smart":
        return (
            "「{name}」是智能播放列表，成员是按规则算出来的，不能手动改"
            "（改了下次一算也会回去）。要固定内容请新建一个普通歌单。"
        ).format(name=_name_of(row) or "（无标题）")
    if key == "mhlp_podcast":
        return (
            "「{name}」是播客播放列表，由播客订阅维护，这里不提供手工编辑。"
        ).format(name=_name_of(row) or "（无标题）")
    if _playlist_id_of(row) in _dataset_master_ids(library):
        # 两个数据集各有主列表（普通 / 播客），标题都是 iPod 的名字。
        # 它们的成员是"全部曲目"或整个数据集的结构，手工增删没有意义。
        return (
            "「{name}」是主播放列表（iPod 的名字就存在它的标题里），"
            "成员和名字都不由这里改——改名请用设备信息里的「重命名设备」。"
        ).format(name=_name_of(row) or "（无标题）")
    return None


def _require_device(ctx: WebContext):
    try:
        return ctx.device(), ctx.library()
    except DeviceNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ──────────────────────────────────────────────────────────────────────
# 接口：列表与曲目
# ──────────────────────────────────────────────────────────────────────


@router.get("")
def list_ipod_playlists(ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    """iPod 上的所有播放列表。

    三类都列出来（普通 / 播客 / 智能），而不是只列能编辑的——用户需要看到
    "设备上确实有这些"，同时每项带 `editable` 和不能编辑的**中文原因**，
    这样界面能解释清楚，而不是"列表里莫名少几个"。
    """
    _device, library = _require_device(ctx)
    master_id = _master_playlist_id(library)
    master_ids = _dataset_master_ids(library)
    tid_to_db = _tid_to_db_id(library)

    items: list[dict[str, Any]] = []
    for key, row in _all_rows(library):
        pid = _playlist_id_of(row)
        reason = _editable_or_reason(library, key, row)
        members = _member_db_ids(row, tid_to_db)
        items.append({
            "playlist_id": str(pid),
            "name": _name_of(row) or "（无标题）",
            "count": len(members),
            "dataset": key,
            "dataset_text": _dataset_label(key),
            # 两个数据集的主列表都标出来（名字一样、数据集不同，界面靠这个区分）
            "is_master": pid in master_ids,
            "is_device_name": pid == master_id,
            "editable": reason is None,
            "readonly_reason": reason or "",
        })

    # 主列表排最前，其余按数据集顺序、名字排——界面上顺序稳定才好找
    items.sort(key=lambda it: (
        0 if it["is_master"] else 1,
        [k for k, _ in DATASETS].index(it["dataset"]),
        it["name"],
    ))
    return {
        "ok": True,
        "playlists": items,
        "master_id": str(master_id),
        "total": len(items),
        "track_count": len(library.tracks),
    }


@router.get("/{playlist_id}/tracks")
def ipod_playlist_tracks(
    playlist_id: str, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """某个歌单里的曲目（按歌单里的顺序）。"""
    _device, library = _require_device(ctx)
    key, row = _find_row(library, playlist_id)
    by_db_id = _db_id_to_track(library)
    tid_to_db = _tid_to_db_id(library)

    # 用跟 `/api/library/tracks` **完全一样的行结构**：界面复用同一套渲染，
    # 不然同一个"曲目"在两条路径下长得不一样，维护时必然漏掉一边。
    tracks = []
    for db_id in _member_db_ids(row, tid_to_db):
        track = by_db_id.get(db_id)
        if track is None:
            continue
        tracks.append(track_row(track, len(tracks)))

    return {
        "ok": True,
        "playlist_id": playlist_id,
        "name": _name_of(row) or "（无标题）",
        "dataset": key,
        "dataset_text": _dataset_label(key),
        "tracks": tracks,
        "count": len(tracks),
        "editable": _editable_or_reason(library, key, row) is None,
        "readonly_reason": _editable_or_reason(library, key, row) or "",
    }


# ──────────────────────────────────────────────────────────────────────
# 写：统一的落库路径
# ──────────────────────────────────────────────────────────────────────


def _write_playlists(ctx: WebContext, handle, extra_playlists,
                     drop_playlist_ids: list[int] | None = None) -> dict[str, Any]:
    """把播放列表改动落库。

    `extra_playlists`：要新建/替换的（`PlaylistInfo`）。
    `drop_playlist_ids`：要从 raw 里去掉的歌单 id（删除用）。

    **先去掉、再写**：`_build_playlist_args` 是从 `library.raw` 重建的，
    所以删掉的歌单必须先从 raw 里摘掉，否则它会被"照原样重建"回来。
    """
    from ipod_cli.dbwrite import build_track_infos, write_library

    device = ctx.device()
    library = ctx.library()

    if drop_playlist_ids:
        wanted = {int(x) for x in drop_playlist_ids}
        raw = library.raw or {}
        removed = 0
        for key, _label in DATASETS:
            rows = raw.get(key) or []
            kept = [r for r in rows
                    if not (isinstance(r, dict) and _playlist_id_of(r) in wanted)]
            removed += len(rows) - len(kept)
            raw[key] = kept
        if removed != len(wanted):
            log.warning("要删的 %d 个歌单，实际摘掉 %d 个", len(wanted), removed)
        handle.log(f"已从播放列表结构里摘掉 {removed} 个歌单")

    infos, _ = build_track_infos(library.track_dicts, progress=handle.log)
    if not infos:
        raise RuntimeError("设备上没有可写入的曲目，已放弃（不然会把曲库写空）。")

    handle.log(f"正在重写 iTunesDB（保留 {len(infos)} 首曲目、其余歌单不动）…")
    result = write_library(
        device,
        library,
        infos,
        progress=handle.log,
        # 改歌单跟封面无关，少动一样就少一类风险
        pc_file_paths=None,
        extra_playlists=extra_playlists or None,
    )
    ctx.invalidate_library()

    return {
        "verified": getattr(result, "verified", False),
        "note": getattr(result, "verification_note", "") or "",
        "track_count": getattr(result, "track_count", len(infos)),
    }


def _playlist_info(name: str, db_ids: list[int], playlist_id: int | None = None):
    """构造要写进去的播放列表。

    `playlist_id` 传了就沿用——**改名必须传**：不传的话写入器会生成一个新 id，
    于是"换个名字"变成"换一个播放列表"：界面上刚拿到的 id 立刻失效
    （下一步操作 404，实测踩到），设备上按 id 引用它的东西也断了。
    """
    from iopenpod.itunesdb_writer import PlaylistInfo

    return PlaylistInfo(name=name, track_ids=list(db_ids),
                        playlist_id=playlist_id)


def _db_ids_from_payload(library, raw_ids: list[str]) -> list[int]:
    """把界面传来的曲目 id（字符串）解析成库里真实存在的 `db_track_id`。

    用字符串传的理由见 `library.py` 开头：64 位无符号数在 Dart 那边会静默溢出。
    """
    known = set(_db_id_to_track(library))
    out: list[int] = []
    for raw in raw_ids:
        try:
            db_id = int(raw)
        except (TypeError, ValueError):
            continue
        if db_id in known and db_id not in out:
            out.append(db_id)
    return out


# ──────────────────────────────────────────────────────────────────────
# 接口：新建 / 改名 / 删除 / 改成员
# ──────────────────────────────────────────────────────────────────────


@router.post("/create")
def create_ipod_playlist(
    payload: CreateRequest, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """新建一个普通播放列表（作业）。

    重名**直接拒绝**，不做"同名覆盖"——那会静默毁掉用户原来的歌单内容。
    （同步那条路是有意同名替换的，因为它的语义是"重算这个歌单"；这里是手动新建。）
    """
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="歌单名不能为空。")
    if len(name) > MAX_NAME_LEN:
        raise HTTPException(status_code=400,
                            detail=f"歌单名太长（最多 {MAX_NAME_LEN} 个字符）。")

    _device, library = _require_device(ctx)
    for _key, row in _all_rows(library):
        if _name_of(row) == name:
            raise HTTPException(
                status_code=409,
                detail=f"iPod 上已经有一个叫「{name}」的播放列表了。换个名字，"
                       "或者直接编辑那一个。",
            )

    job = ctx.jobs.submit(
        "playlist",
        f"新建歌单「{name}」",
        lambda handle: _run_create(ctx, handle, name, payload.track_ids),
    )
    return {"ok": True, "job_id": job.id, "message": f"已加入队列：新建歌单「{name}」"}


def _run_create(ctx: WebContext, handle, name: str, raw_ids: list[str]) -> dict[str, Any]:
    library = ctx.library()
    db_ids = _db_ids_from_payload(library, raw_ids)
    handle.log(f"新建播放列表「{name}」，初始 {len(db_ids)} 首")
    out = _write_playlists(ctx, handle, [_playlist_info(name, db_ids)])
    handle.log("读回校验通过" if out["verified"] else "⚠ 读回校验未通过，请检查设备")
    return {"name": name, "added": len(db_ids), **out}


@router.post("/{playlist_id}/rename")
def rename_ipod_playlist(
    playlist_id: str, payload: RenameRequest, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """改歌单名（作业）。主播放列表不许改名（那就是改设备名，另有入口）。"""
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="歌单名不能为空。")
    if len(name) > MAX_NAME_LEN:
        raise HTTPException(status_code=400,
                            detail=f"歌单名太长（最多 {MAX_NAME_LEN} 个字符）。")

    _device, library = _require_device(ctx)
    key, row = _find_row(library, playlist_id)
    reason = _editable_or_reason(library, key, row)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    if _playlist_id_of(row) in _dataset_master_ids(library):
        raise HTTPException(
            status_code=409,
            detail="这是主播放列表——改它的名字等于改 iPod 的名字，"
                   "请用设备信息里的「重命名设备」。",
        )
    for _k, other in _all_rows(library):
        if _name_of(other) == name and _playlist_id_of(other) != _playlist_id_of(row):
            raise HTTPException(status_code=409,
                                detail=f"已经有一个叫「{name}」的播放列表了。")

    old = _name_of(row)
    job = ctx.jobs.submit(
        "playlist", f"歌单「{old}」改名为「{name}」",
        lambda handle: _run_rename(ctx, handle, playlist_id, name),
    )
    return {"ok": True, "job_id": job.id, "message": "已加入队列：歌单改名"}


def _run_rename(ctx: WebContext, handle, playlist_id: str, name: str) -> dict[str, Any]:
    library = ctx.library()
    key, row = _find_row(library, playlist_id)
    tid_to_db = _tid_to_db_id(library)
    current = _member_db_ids(row, tid_to_db)

    handle.log(f"把「{_name_of(row)}」改名为「{name}」（{len(current)} 首不动）")
    # 改名 = 用新名字覆盖：老的 raw 行**先从 raw 里摘掉**，否则它会以旧名字
    # 被重建回来，变成"改名 + 多出旧歌单"。
    out = _write_playlists(
        ctx, handle,
        # ★ 带上原 id：改名是"同一个播放列表换个名字"，不是"新建一个"
        [_playlist_info(name, current, playlist_id=_playlist_id_of(row))],
        drop_playlist_ids=[_playlist_id_of(row)],
    )
    handle.log("读回校验通过" if out["verified"] else "⚠ 读回校验未通过，请检查设备")
    return {"name": name, "count": len(current), **out}


@router.post("/delete/preview")
def delete_preview(
    payload: DeletePreviewRequest, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """删除预览：这个歌单叫什么、有几首、**这些歌不会被删**。不碰 iPod。"""
    _device, library = _require_device(ctx)
    key, row = _find_row(library, payload.playlist_id)
    reason = _editable_or_reason(library, key, row, for_delete=True)
    if reason:
        raise HTTPException(status_code=409, detail=reason)

    tid_to_db = _tid_to_db_id(library)
    count = len(_member_db_ids(row, tid_to_db))
    return {
        "ok": True,
        "preview_id": _previews.issue([str(_playlist_id_of(row))]),
        "playlist_id": payload.playlist_id,
        "name": _name_of(row) or "（无标题）",
        "count": count,
        "note": f"只删这个播放列表，{count} 首歌本身**不会被删**，"
                "它们仍在「全部歌曲」里。",
    }


@router.post("/delete")
def delete_playlist(
    payload: DeleteRequest, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """删除播放列表（作业）。**必须带预览令牌。**"""
    problem = _previews.consume(payload.preview_id, [payload.playlist_id])
    if problem is not None:
        raise HTTPException(status_code=409, detail=problem)

    # 预览和执之间设备可能被外部改过，所以这里**重新查一遍**再确认可删
    _device, library = _require_device(ctx)
    key, row = _find_row(library, payload.playlist_id)
    reason = _editable_or_reason(library, key, row, for_delete=True)
    if reason:
        raise HTTPException(status_code=409, detail=reason)

    name = _name_of(row) or "（无标题）"
    job = ctx.jobs.submit(
        "playlist", f"删除歌单「{name}」",
        lambda handle: _run_delete(ctx, handle, payload.playlist_id, name),
    )
    return {"ok": True, "job_id": job.id, "message": f"已加入队列：删除歌单「{name}」"}


def _run_delete(ctx: WebContext, handle, playlist_id: str, name: str) -> dict[str, Any]:
    library = ctx.library()
    _key, row = _find_row(library, playlist_id)
    handle.log(f"删除播放列表「{name}」（曲目本身不动）")
    out = _write_playlists(ctx, handle, None,
                           drop_playlist_ids=[_playlist_id_of(row)])
    handle.log("读回校验通过" if out["verified"] else "⚠ 读回校验未通过，请检查设备")
    return {"name": name, **out}


@router.post("/{playlist_id}/tracks")
def edit_playlist_tracks(
    playlist_id: str, payload: TracksRequest, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """往歌单里加曲目 / 从歌单里移除曲目（作业）。

    传的是**增量**（add / remove），不是整份替换——界面上的操作就是
    "把选中的加进去" / "把选中的移出去"，增量语义跟用户意图一致，
    也不会因为界面拿的是旧列表而把别处刚加的曲目冲掉。
    """
    if not payload.add and not payload.remove:
        raise HTTPException(status_code=400, detail="没有要改的内容。")

    _device, library = _require_device(ctx)
    key, row = _find_row(library, playlist_id)
    reason = _editable_or_reason(library, key, row)
    if reason:
        raise HTTPException(status_code=409, detail=reason)

    job = ctx.jobs.submit(
        "playlist",
        f"调整歌单「{_name_of(row) or '（无标题）'}」",
        lambda handle: _run_edit_tracks(ctx, handle, playlist_id,
                                        payload.add, payload.remove),
    )
    return {"ok": True, "job_id": job.id, "message": "已加入队列：调整歌单内容"}


def _run_edit_tracks(ctx: WebContext, handle, playlist_id: str,
                     raw_add: list[str], raw_remove: list[str]) -> dict[str, Any]:
    library = ctx.library()
    key, row = _find_row(library, playlist_id)
    name = _name_of(row) or "（无标题）"
    tid_to_db = _tid_to_db_id(library)

    current = _member_db_ids(row, tid_to_db)
    before = len(current)

    to_add = _db_ids_from_payload(library, raw_add)
    to_remove = set(_db_ids_from_payload(library, raw_remove))

    # 先移除、再加：同一次请求里同时给 add/remove 同一首歌时，结果应该是"加进去"
    # （用户点的是"加入歌单"，不该因为它已经在里面又被移除掉）
    new_list = [db_id for db_id in current if db_id not in to_remove]
    added = 0
    for db_id in to_add:
        if db_id not in new_list:
            new_list.append(db_id)
            added += 1
    removed = before - (len(new_list) - added)

    if added == 0 and removed == 0:
        handle.log("没有变化（要加的都在里面、要移的都不在里面）")
        return {"name": name, "added": 0, "removed": 0, "count": len(new_list),
                "verified": True, "note": "无变化", "track_count": len(library.tracks)}

    handle.log(f"「{name}」：{before} 首 → {len(new_list)} 首"
               f"（加入 {added}、移出 {removed}）")
    out = _write_playlists(ctx, handle, [_playlist_info(name, new_list)])
    handle.log("读回校验通过" if out["verified"] else "⚠ 读回校验未通过，请检查设备")
    return {"name": name, "added": added, "removed": removed,
            "count": len(new_list), **out}
