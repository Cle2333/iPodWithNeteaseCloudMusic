"""作业队列的接口：列表、详情（含增量日志）、取消。

界面所有"正在干活"的显示都来自这里。**日志用 ``since`` 增量拉取**——
一个几百首的下载作业会刷出几千行，每轮轮询重传一遍的话界面会越来越卡。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ipod_web.context import WebContext
from ipod_web.deps import get_ctx

router = APIRouter(prefix="/api/jobs", tags=["作业"])


#: 列表里带逐首歌明细的作业类型。下载页只用得上这两个。
_DOWNLOAD_KINDS = ("download", "sync")


def _brief(job, *, with_items: bool = False) -> dict[str, Any]:
    """列表里用的精简版。不带日志。

    ``with_items`` 控制要不要带逐首歌的明细。**默认不带**：
    一个几百首的下载作业，明细就有几十 KB，20 个作业全带上、
    每 3 秒轮询一次，就是每轮几百 KB 的纯浪费——而界面一次只看一个作业。
    计数（items_done / items_failed）始终保留，那个很便宜。
    """
    data = job.to_dict()
    data.pop("result", None)
    if not with_items:
        data["items"] = []
    return data


@router.get("")
def list_jobs(ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    """当前作业快照：正在跑的、排队的、最近完成的。"""
    jobs = ctx.jobs.list_jobs()
    running = ctx.jobs.running_job

    # 只给"最该看的那个"下载作业带明细：最新的一个，
    # 外加正在跑的那个（如果它是下载类）。最多两个，体积可控。
    keep: set[str] = set()
    for job in jobs:
        if job.kind in _DOWNLOAD_KINDS:
            keep.add(job.id)
            break
    if running is not None and running.kind in _DOWNLOAD_KINDS:
        keep.add(running.id)

    return {
        "running": _brief(running, with_items=running.id in keep) if running else None,
        "queued": ctx.jobs.queued_count,
        "has_work": ctx.jobs.has_work,
        # list_jobs() 已经是"新在前"，取前 20 个就是最近 20 个
        "jobs": [_brief(job, with_items=job.id in keep) for job in jobs[:20]],
    }


@router.get("/{job_id}")
def job_detail(
    job_id: str,
    since: int = Query(0, ge=0, description="只返回 seq 大于它的日志行"),
    ctx: WebContext = Depends(get_ctx),
) -> dict[str, Any]:
    job = ctx.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"没有这个作业：{job_id}")

    lines = job.logs_since(since)
    # 详情**一定带全**：逐首歌的明细和 result 都要——用户点进来就是冲它们来的
    return {
        **job.to_dict(),
        "log": [line.as_dict() for line in lines],
        "latest_seq": job.log[-1].seq if job.log else 0,
        "log_truncated": bool(lines) and lines[0].seq > since + 1,
    }


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    """取消作业。

    取消是**协程式**的：作业体在每条之间检查取消标志，所以能干净地停在
    两首歌中间，不会留下半截文件。
    """
    job = ctx.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"没有这个作业：{job_id}")

    if not ctx.jobs.cancel(job_id):
        return {
            "ok": False,
            "message": f"「{job.title}」已经结束了，取消不了",
        }
    return {"ok": True, "message": f"已请求取消「{job.title}」"}
