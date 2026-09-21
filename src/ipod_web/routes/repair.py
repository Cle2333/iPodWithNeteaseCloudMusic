"""设备修复接口：扫描"数据库与磁盘对不上"的地方，并清掉。

## 为什么走作业队列

两条理由，都不是"顺手"：

1. **扫描要读数据库和磁盘**。有作业在跑（正在写库、正在拷文件）时读，
   会读到半截状态，然后报一个不存在的故障。串行队列天然避开了这件事。
2. **清理是破坏性的**。作业队列有取消、有落盘的历史记录、有卡死看门狗
   ——出问题能查（`logs/jobs.jsonl`），这正是这类操作该有的。

## 为什么扫描也要"先预览、后执行"

删文件不可逆。所以扫描（``POST /api/repair/scan``）和执行
（``POST /api/repair/clean``）是**两个独立的作业**：界面必须先把扫描结果
摆给用户看，用户勾了哪几类、点了确认，才发第二个请求。清理请求里带的是
**类别开关**，不是"我刚扫到的那批文件"——执行时重新扫一遍再删，
中间设备被外部改动也不会删错（宁可少删，绝不多删）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ipod_cli.repair import (
    CleanResult,
    clean_broken_records,
    clean_orphans,
    clean_stray_temp,
    scan_device,
)
from ipod_web.context import WebContext
from ipod_web.deps import get_ctx

router = APIRouter(prefix="/api/repair", tags=["设备修复"])

#: 列表里最多回几个明细。全列的话上千个孤儿刷一屏，重点全被冲掉；
#: 界面要的是"有几个、多大、长什么样"，不是完整清单。
PREVIEW_ITEMS = 20


class CleanRequest(BaseModel):
    """要清哪几类。**默认全 False** —— 清理不可逆，必须用户显式勾选。"""

    orphans: bool = False
    stray_temp: bool = False
    #: 断链记录（库里有、磁盘没有）要整库重写，风险最高，单独一档
    broken_records: bool = False


# ──────────────────────────────────────────────────────────────────────
# 扫描
# ──────────────────────────────────────────────────────────────────────


@router.post("/scan")
def scan(ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    """扫描设备（作业）。结果在作业的 ``result`` 里。"""
    job = ctx.jobs.submit("repair_scan", "扫描设备", lambda h: _run_scan(ctx, h))
    return {"ok": True, "job_id": job.id, "message": "已开始扫描"}


def _read_device(ctx: WebContext, handle, *, action: str):
    """取设备并扫描。数据库读不出来时给一句**能照着做**的中文说明。

    内核在这种情况下抛的是 ``InsufficientDataError`` 这类底层异常，
    直接甩给用户等于没说。而"数据库读不出来"是个需要用户动手的状态
    （设备可能真的坏了），必须说清下一步干什么。
    """
    device = ctx.device()
    handle.log(f"正在{action} {device.display_name}…")
    try:
        return device, scan_device(device, progress=handle.progress)
    except Exception as exc:  # noqa: BLE001 - 统一翻译成中文提示
        raise RuntimeError(
            f"读不出 iPod 的数据库，没法比对当前状态（{type(exc).__name__}）。\n"
            f"  这通常意味着数据库文件损坏。先在电脑上用 iTunes / Finder 看一眼\n"
            f"  这个 iPod 是否正常，必要时用备份恢复 iPod_Control 目录，再回来重试。"
        ) from exc


def _run_scan(ctx: WebContext, handle) -> dict[str, Any]:
    device, scan = _read_device(ctx, handle, action="扫描")

    handle.log(f"数据库 {scan.db_tracks} 首，磁盘 {scan.disk_files} 个文件")
    if scan.orphans:
        handle.log(
            f"孤儿文件 {len(scan.orphans)} 个（{scan.orphan_text}）——"
            f"磁盘上有、数据库不认，iPod 上看不见但占着空间",
            level="warn",
        )
    if scan.broken:
        handle.log(
            f"断链记录 {len(scan.broken)} 首——数据库里有，磁盘上没文件，"
            f"iPod 上显示得出来但播不了",
            level="error" if len(scan.broken) else "warn",
        )
    if scan.stray_temp:
        handle.log(f"残留临时文件 {len(scan.stray_temp)} 个", level="warn")

    if scan.is_clean:
        handle.log("数据库和磁盘完全一致，没有需要修复的地方 ✓")

    return scan_as_dict(scan)


def scan_as_dict(scan) -> dict[str, Any]:
    """扫描结果 → 界面要的形状。**纯数据**，界面自己决定怎么呈现。"""
    return {
        "db_tracks": scan.db_tracks,
        "disk_files": scan.disk_files,
        "clean": scan.is_clean,
        "summary": scan.summary_text(),
        "orphans": {
            "count": len(scan.orphans),
            "bytes": scan.orphan_bytes,
            "size_text": scan.orphan_text,
            "items": [
                {"name": o.name, "rel": o.rel, "size": o.size,
                 "size_text": o.size_text}
                for o in scan.orphans[:PREVIEW_ITEMS]
            ],
        },
        "broken": {
            "count": len(scan.broken),
            "bytes": scan.broken_bytes,
            "size_text": scan.broken_text,
            "items": [
                {
                    # db_track_id 一律字符串：它是随机 64 位无符号数，
                    # 真机里有超过 2^63-1 的，当 JSON 数字发出去 Dart 的
                    # 有符号 int 会静默溢出成负数。
                    "id": str(t.db_track_id),
                    "title": t.title or "（没有标题）",
                    "artist": t.artist or "",
                    "rel": str(t.location).strip(":").replace(":", "/"),
                    "size_text": _human(t.size),
                }
                for t in scan.broken[:PREVIEW_ITEMS]
            ],
        },
        "stray_temp": {
            "count": len(scan.stray_temp),
            "items": [
                {"rel": rel, "name": rel.rsplit("/", 1)[-1]}
                for rel in scan.stray_temp[:PREVIEW_ITEMS]
            ],
        },
    }


def _human(size: int) -> str:
    from ipod_cli.discovery import human_size

    return human_size(size)


# ──────────────────────────────────────────────────────────────────────
# 清理
# ──────────────────────────────────────────────────────────────────────


@router.post("/clean")
def clean(
    payload: CleanRequest,
    ctx: WebContext = Depends(get_ctx),
) -> dict[str, Any]:
    """清掉选中的类别（作业）。"""
    if not (payload.orphans or payload.stray_temp or payload.broken_records):
        return {"ok": False, "message": "没有勾选任何要清理的内容，什么都没做。"}

    job = ctx.jobs.submit(
        "repair_clean",
        "修复设备",
        lambda h: _run_clean(ctx, h, payload),
    )
    return {"ok": True, "job_id": job.id, "message": "已加入队列：修复设备"}


def _run_clean(ctx: WebContext, handle, payload: CleanRequest) -> dict[str, Any]:
    # ★ 重新扫一遍，**不信任界面拿回来的清单**。
    #
    # 从"扫到"到"用户点确认"之间可能过了几分钟，设备也许被外部改过
    # （iTunes、另一台机器、甚至用户自己拷文件）。拿旧清单去按路径删，
    # 删错的风险是真实存在的。重扫一遍最多多花几秒，但保证删的每一刀
    # 都看的是当前状态——宁可少删，绝不多删。
    handle.stage("重新核对设备")
    handle.progress("正在重新核对设备当前状态…")
    device, scan = _read_device(ctx, handle, action="重新核对")

    results: list[CleanResult] = []

    if payload.orphans and scan.orphans:
        handle.stage("删除孤儿文件", len(scan.orphans))
        results.append(clean_orphans(device, scan.orphans, progress=handle.progress))
    elif payload.orphans:
        handle.log("没有孤儿文件需要清理")

    if payload.stray_temp and scan.stray_temp:
        handle.stage("删除残留临时文件", len(scan.stray_temp))
        results.append(
            clean_stray_temp(device, scan.stray_temp, progress=handle.progress)
        )
    elif payload.stray_temp:
        handle.log("没有残留临时文件需要清理")

    if payload.broken_records and scan.broken:
        handle.stage("重建数据库", None)
        handle.log(
            f"正在删掉 {len(scan.broken)} 条断链记录"
            f"（要整库重写，期间请不要拔设备）",
            level="warn",
        )
        results.append(
            clean_broken_records(
                device, scan.library, scan.broken, progress=handle.progress
            )
        )
    elif payload.broken_records:
        handle.log("没有断链记录需要清理")

    if not results:
        handle.log("没有需要处理的内容")
        return {"cleaned": 0, "freed_bytes": 0, "freed_text": "0 B", "kinds": []}

    for item in results:
        level = "error" if item.errors else "info"
        handle.log(item.note, level=level)
        for path, reason in item.errors[:10]:
            handle.log(f"    {path} —— {reason}", level="error")

    freed = sum(r.bytes_freed for r in results)
    removed = sum(r.removed for r in results)
    handle.log(f"修复完成：处理 {removed} 项，释放 {_human(freed)}")

    # 设备内容变了（可能还重写过数据库），缓存必须失效
    ctx.invalidate_library()

    return {
        "cleaned": removed,
        "freed_bytes": freed,
        "freed_text": _human(freed),
        "kinds": [
            {"kind": r.kind, "removed": r.removed, "bytes": r.bytes_freed,
             "note": r.note, "errors": len(r.errors)}
            for r in results
        ],
    }
