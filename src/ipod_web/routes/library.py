"""iPod 上的本地音乐：列表、导入、删除、健康检查。

两条贯穿全文的安全设计：

1. **`db_track_id` 一律用字符串传**。它是随机 64 位**无符号**整数，
   真机 118 首里有 61 首超过 2^63-1。当成 JSON 数字发出去，Dart 的
   `int`（有符号 64 位）会直接溢出——而且是**静默**变成负数，
   然后删除操作会删错歌。字符串是唯一安全的形状。

2. **破坏性操作一律"先预览、后执行"，且预览和实际执行走同一套计算**。
   预览说删 23 首、实际删 24 首，用户就再也信不过这个预览了。
   这里的做法是预览和执行**都重新 build 一次计划**——中间设备被外部
   改动的话，执行前会重新读库，宁可数字对不上也不删错。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ipod_cli.discovery import DeviceNotFoundError, human_size
from ipod_cli.importer import build_import_plan, execute_import
from ipod_cli.remover import RemoveError, build_remove_plan, execute_remove
from ipod_web.context import WebContext
from ipod_web.deps import get_ctx

router = APIRouter(prefix="/api/library", tags=["本地音乐"])

#: 单页最多多少首
MAX_PAGE_SIZE = 500

#: 预览里最多列几个文件名。列全的话几百首刷一屏，重点全被冲掉。
PREVIEW_ITEMS = 12

SORTS = {
    "title": "标题",
    "artist": "艺人",
    "album": "专辑",
    "size": "体积",
    "length": "时长",
    "recent": "最近添加",
}


class PathsRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)


class IdsRequest(BaseModel):
    #: iPod 的持久 ID。**字符串**，理由见模块开头。
    ids: list[str] = Field(default_factory=list)
    #: 是否连磁盘文件一起删（默认删）
    delete_files: bool = True
    #: 预览令牌。**执行删除时必须带上它**，见模块开头的说明。
    preview_id: str = ""


#: 预览令牌有效期（秒）。过期就得重新预览——用户盯着预览看了 10 分钟
#: 才点确认的话，设备状态多半已经变了，重新看一眼更安全。
PREVIEW_TTL = 600.0


class PreviewStore:
    """删除预览令牌（曲目删除、歌单删除共用这一套语义）。

    **存在的唯一目的：让「先预览」成为结构性的保证，而不是界面自觉。**

    没有它的话，一个界面 bug（比如漏掉确认对话框）就能直接删歌。
    有了它，`/api/library/remove` 必须带上一个**真实预览过**的令牌，
    否则直接拒绝——想绕过确认就得先伪造令牌，那就不是"手滑"能解释的了。

    令牌是**一次性**的：用过即失效。同一次预览删两次这种情况不该存在。
    """

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[float, frozenset[str]]] = {}

    def issue(self, ids: list[str]) -> str:
        token = uuid.uuid4().hex
        now = time.monotonic()
        # 顺手清过期的，免得越攒越多
        self._tokens = {
            k: v for k, v in self._tokens.items() if now - v[0] < PREVIEW_TTL
        }
        self._tokens[token] = (now, frozenset(ids))
        return token

    def consume(self, token: str, ids: list[str]) -> str | None:
        """校验并作废令牌。通过返回 None，否则返回中文原因。"""
        entry = self._tokens.pop(token, None)
        if entry is None:
            return (
                "这次删除没有经过预览确认。请先点删除、看清要删哪些，再确认——"
                "这个限制是有意留的。"
            )
        issued_at, previewed = entry
        if (time.monotonic() - issued_at) > PREVIEW_TTL:
            return "预览已经过期了（设备状态可能变过），请重新预览一次。"
        if previewed != frozenset(ids):
            return (
                "要删的曲目和预览时看到的不一致。请重新预览确认，"
                "避免误删。"
            )
        return None


#: 删除预览令牌仓库。**进程级**——重启后端就等于作废所有令牌，这是对的：
#: 重启后设备可能已经被别的东西改过，旧的预览结论不该继续有效。
_previews = PreviewStore()

# 兼容别名：本模块内和测试里还有 `_PreviewStore` 的旧引用
_PreviewStore = PreviewStore



def _ms_text(ms: int) -> str:
    if ms <= 0:
        return "--:--"
    total = ms // 1000
    return f"{total // 60}:{total % 60:02d}"


def track_row(track, index: int) -> dict[str, Any]:
    """曲目行的统一形状。

    歌单曲目接口也用它——两边形状一致，界面就不用写第二套渲染。
    """
    return {
        "index": index,
        "db_id": str(track.db_track_id),
        "title": track.title or "（无标题）",
        "artist": track.artist,
        "album": track.album,
        "size": track.size,
        "size_text": human_size(track.size) if track.size else "0 B",
        "length_ms": track.length,
        "length_text": _ms_text(track.length),
        "bitrate": track.bitrate,
        "play_count": track.play_count,
    }


# ──────────────────────────────────────────────────────────────────────
# 曲目列表
# ──────────────────────────────────────────────────────────────────────


@router.get("/tracks")
def list_tracks(
    search: str = "",
    sort: str = Query("title"),
    page: int = Query(1, ge=1),
    size: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
    ctx: WebContext = Depends(get_ctx),
) -> dict[str, Any]:
    """设备上的曲目，带搜索、排序、分页。"""
    try:
        library = ctx.library()
    except DeviceNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if sort not in SORTS:
        raise HTTPException(
            status_code=400,
            detail=f"不认识的排序方式「{sort}」，可选：{'、'.join(SORTS)}",
        )

    indexed = list(enumerate(library.tracks))
    needle = search.strip().lower()
    if needle:
        indexed = [
            (i, t)
            for i, t in indexed
            if needle in (t.title or "").lower()
            or needle in (t.artist or "").lower()
            or needle in (t.album or "").lower()
        ]

    # 先筛后排：排全量再筛是白做功，几千首的时候看得出来
    keyed = {
        "title": lambda p: (p[1].title or "").lower(),
        "artist": lambda p: (p[1].artist or "").lower(),
        "album": lambda p: (p[1].album or "").lower(),
        "size": lambda p: -p[1].size,
        "length": lambda p: -p[1].length,
        "recent": lambda p: -p[1].date_added,
    }[sort]
    indexed.sort(key=keyed)

    filtered_total = len(indexed)
    start = (page - 1) * size
    window = indexed[start : start + size]

    # 筛选后的总体积。界面在"全选筛选结果"时要用它显示"已选 N 首 · X MB"——
    # 只靠当前页算的话，选中的几千首里大部分体积是未知的，数字就没意义了。
    filtered_bytes = sum(t.size for _, t in indexed)
    all_bytes = sum(t.size for t in library.tracks)

    return {
        "total": len(library.tracks),
        "filtered": filtered_total,
        "page": page,
        "size": size,
        "pages": max((filtered_total + size - 1) // size, 1),
        "filtered_bytes": filtered_bytes,
        "all_bytes": all_bytes,
        "total_text": human_size(filtered_bytes) if filtered_bytes else "0 B",
        "ipod_name": library.ipod_name,
        "sorts": [{"value": k, "label": v} for k, v in SORTS.items()],
        "tracks": [track_row(t, i) for i, t in window],
    }


# ──────────────────────────────────────────────────────────────────────
# 导入
# ──────────────────────────────────────────────────────────────────────


def _classify(plan) -> dict[str, Any]:
    """把导入计划分成界面要显示的几类。

    分类要在**动手之前**给出来——"23 个文件 187 MB，其中 5 个要转码、
    3 个已存在会跳过、1 个不支持"这种话，用户看到才敢点确认。
    """
    transcode = [i for i in plan.to_add if i.action == "transcode"]
    return {
        "files": len(plan.items),
        "bytes": plan.bytes_to_copy,
        "size_text": human_size(plan.bytes_to_copy) if plan.bytes_to_copy else "0 B",
        "to_add": len(plan.to_add),
        "transcode": len(transcode),
        "skipped": len(plan.skipped),
        "errored": len(plan.errored),
        "fits": plan.fits,
        "free_bytes": plan.device.free_bytes,
        "free_text": plan.device.free_text,
        "items": [
            {
                "path": str(i.pc.source_path),
                "name": i.pc.source_path.name,
                "title": i.pc.title or "",
                "action": i.action,
                "reason": i.reason,
                "size_text": human_size(i.pc.size) if i.pc.size else "0 B",
            }
            for i in plan.items[:PREVIEW_ITEMS]
        ],
    }


def _build_import(ctx: WebContext, paths: list[str]):
    if not paths:
        raise HTTPException(status_code=400, detail="没有选择任何文件")

    files = [Path(p) for p in paths]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"有 {len(missing)} 个文件不存在，比如：{missing[0]}",
        )

    device = ctx.device()
    library = ctx.library(force=True)
    return device, library, build_import_plan(device, library, files)


@router.post("/import/preview")
def import_preview(
    payload: PathsRequest, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """预览这次导入会干什么。**不碰 iPod。**"""
    try:
        _device, _library, plan = _build_import(ctx, payload.paths)
    except DeviceNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {"ok": True, **_classify(plan)}


@router.post("/import")
def do_import(
    payload: PathsRequest, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """执行导入（作业）。"""
    job = ctx.jobs.submit(
        "import",
        f"导入 {len(payload.paths)} 个文件",
        lambda handle: _run_import(ctx, handle, payload.paths),
    )
    return {"ok": True, "job_id": job.id, "message": "已加入队列：导入音乐"}


def _run_import(ctx: WebContext, handle, paths: list[str]) -> dict[str, Any]:
    handle.log(f"准备导入 {len(paths)} 个文件…")
    _device, _library, plan = _build_import(ctx, paths)

    handle.set_total(len(plan.to_add))
    handle.log(
        f"待拷入 {len(plan.to_add)} 个"
        + (f"（其中 {sum(1 for i in plan.to_add if i.action == 'transcode')} 个需要转码）"
           if any(i.action == "transcode" for i in plan.to_add) else "")
        + (f"，跳过 {len(plan.skipped)} 个已存在的" if plan.skipped else "")
        + (f"，{len(plan.errored)} 个不支持" if plan.errored else "")
    )

    if not plan.fits:
        raise RuntimeError(
            f"空间不够：需要 {human_size(plan.bytes_to_copy)}，"
            f"设备只剩 {plan.device.free_text}"
        )

    result = execute_import(plan, progress=handle.progress)
    ctx.invalidate_library()

    if result.failed:
        for name, reason in result.failed[:20]:
            handle.log(f"失败：{name} —— {reason}", level="error")

    handle.log(
        f"导入完成：{result.added} 首、{human_size(result.bytes_copied)}"
        + ("，读回校验通过" if result.verified else "，⚠ 读回校验未通过")
    )

    return {
        "added": result.added,
        "failed": len(result.failed),
        "bytes_copied": result.bytes_copied,
        "verified": result.verified,
        "skipped": len(plan.skipped),
    }


# ──────────────────────────────────────────────────────────────────────
# 删除
# ──────────────────────────────────────────────────────────────────────


def _resolve_ids(ctx: WebContext, ids: list[str]):
    """把界面传来的 ID 映射回 Track。

    用**字符串比对**，不做 int 转换：`db_track_id` 是无符号 64 位，
    转成有符号的 int 会让超过 2^63-1 的那一半变成负数，然后匹配不上
    （或者更糟——匹配到别的歌）。
    """
    if not ids:
        raise HTTPException(status_code=400, detail="没有选中任何曲目")

    library = ctx.library(force=True)
    wanted = set(ids)
    tracks = [t for t in library.tracks if str(t.db_track_id) in wanted]

    if not tracks:
        raise HTTPException(
            status_code=404,
            detail=f"选中的 {len(ids)} 首在设备上都找不到（可能已经被删了）",
        )
    return library, tracks, len(ids) - len(tracks)


@router.post("/remove/preview")
def remove_preview(
    payload: IdsRequest, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """预览删除：多少首、释放多少空间、前几首叫什么。**不碰 iPod。**"""
    try:
        library, tracks, missing = _resolve_ids(ctx, payload.ids)
    except DeviceNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        plan = build_remove_plan(library=library, device=ctx.device(), tracks=tracks)
    except RemoveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "ok": True,
        # 执行删除时必须把这个令牌带回来（见 _PreviewStore 的说明）
        "preview_id": _previews.issue(payload.ids),
        "count": plan.count,
        "bytes": plan.bytes_freed,
        "size_text": human_size(plan.bytes_freed) if plan.bytes_freed else "0 B",
        "remaining": len(library.tracks) - plan.count,
        "missing": missing,
        "items": [
            {"title": t.title or "（无标题）", "artist": t.artist,
             "size_text": human_size(t.size) if t.size else "0 B"}
            for t in plan.to_remove[:PREVIEW_ITEMS]
        ],
    }


@router.post("/remove")
def do_remove(
    payload: IdsRequest, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """执行删除（作业）。

    **必须传 delete_files**：删库不删文件的后果是磁盘上留下孤儿文件，
    iPod 不显示但空间占着——那是更难查的问题。

    **必须带 preview_id**：没有它就直接拒绝。这条限制的意义是让
    「先预览再删」成为结构性的保证，而不是指望界面每次都记得弹确认框。
    """
    problem = _previews.consume(payload.preview_id, payload.ids)
    if problem is not None:
        raise HTTPException(status_code=409, detail=problem)

    job = ctx.jobs.submit(
        "remove",
        f"删除 {len(payload.ids)} 首",
        lambda handle: _run_remove(ctx, handle, payload.ids, payload.delete_files),
    )
    return {"ok": True, "job_id": job.id, "message": "已加入队列：删除曲目"}


def _run_remove(ctx: WebContext, handle, ids: list[str], delete_files: bool) -> dict[str, Any]:
    library, tracks, missing = _resolve_ids(ctx, ids)
    if missing:
        handle.log(
            f"有 {missing} 首选中的曲目在设备上找不到，已跳过（可能已被删）",
            level="warn",
        )

    plan = build_remove_plan(library=library, device=ctx.device(), tracks=tracks)
    handle.set_total(plan.count)
    handle.log(
        f"将删除 {plan.count} 首、释放 {human_size(plan.bytes_freed)}；"
        f"保留 {len(library.tracks) - plan.count} 首"
    )

    result = execute_remove(plan, progress=handle.progress)
    ctx.invalidate_library()

    handle.log(
        f"删除完成：{result.files_deleted} 个文件、"
        f"释放 {human_size(plan.bytes_freed)}"
        + ("，读回校验通过" if result.verified else "，⚠ 读回校验未通过")
    )
    # 删文件出错的要逐条列出来：库已经重写了、文件却没删掉的曲目，
    # 在 iPod 上不显示但空间还占着——不报出来就永远查不到
    for name, reason in result.file_errors[:20]:
        handle.log(f"删文件失败：{name} —— {reason}", level="error")

    return {
        # 曲目数按计划走（库已经重写成功），文件删除数单独报
        "removed": plan.count,
        "files_deleted": result.files_deleted,
        "file_errors": len(result.file_errors),
        "bytes_freed": plan.bytes_freed,
        "verified": result.verified,
    }


# ──────────────────────────────────────────────────────────────────────
# 健康检查
# ──────────────────────────────────────────────────────────────────────


@router.post("/verify")
def verify(ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    """跑一次完整健康检查（作业）。

    走队列而不是直接跑：检查要逐项读数据库和磁盘，跟正在进行的写入并行
    会读到半截状态，然后报一个不存在的故障。
    """
    job = ctx.jobs.submit("verify", "健康检查", lambda handle: _run_verify(ctx, handle))
    return {"ok": True, "job_id": job.id, "message": "已开始健康检查"}


def _run_verify(ctx: WebContext, handle) -> dict[str, Any]:
    from ipod_cli.verify import check_device

    device = ctx.device()
    handle.log(f"正在检查 {device.display_name}…")

    report = check_device(device, progress=handle.progress)

    for check in report.checks:
        level = {"ok": "info", "warn": "warn", "fail": "error"}.get(
            check.status, "info"
        )
        handle.log(f"{check.name}：{check.summary}", level=level)
        # 明细单独打出来——"封面有问题"这种话不说明是哪些图，
        # 用户拿它没法排查
        for line in check.details:
            handle.log(f"    {line}", level=level)

    failed = len(report.failures)
    warned = len(report.warnings)
    handle.log(
        f"检查完成：{'全部通过' if report.ok else f'{failed} 项失败、{warned} 项警告'}"
    )

    return {
        "ok": report.ok,
        "total": len(report.checks),
        "failed": failed,
        "warned": warned,
        "checks": [
            {
                "name": check.name,
                "status": check.status,
                "summary": check.summary,
                "details": check.details,
            }
            for check in report.checks
        ],
    }
