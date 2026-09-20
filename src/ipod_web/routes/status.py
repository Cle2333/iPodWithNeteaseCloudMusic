"""状态、设备信息、健康检查。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ipod_cli.discovery import DeviceNotFoundError, human_size
from ipod_web.context import WebContext
from ipod_web.deps import get_ctx
from ipod_web.jobs import Job

router = APIRouter(prefix="/api", tags=["状态"])


def _job_brief(job: Job | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        "id": job.id,
        "title": job.title,
        "state": job.state,
        "state_text": job.to_dict()["state_text"],
        "percent": round(job.percent, 1),
    }


def _duration_text(total_ms: int) -> str:
    """毫秒 → "7 小时 52 分"。界面直接用，省得前端各写一套。"""
    if total_ms <= 0:
        return "0 分"
    minutes = int(total_ms / 60000)
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {mins} 分"
    return f"{mins} 分"


def _device_or_none(ctx: WebContext):
    """找设备。找不到返回 None，**不抛异常**——状态条上"没插设备"是常态。"""
    try:
        return ctx.device()
    except DeviceNotFoundError:
        return None


# ──────────────────────────────────────────────────────────────────────
# 就绪探测
# ──────────────────────────────────────────────────────────────────────


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    """就绪探测。Flutter 启动后端后轮询这个，通了才进主界面。

    **故意做成极轻量**：不读设备、不碰网络。启动期设备可能还没插，
    那时候也该让界面起来（否则用户看到的是"启动失败"而不是"没插 iPod"）。
    """
    import time as _time

    ctx = get_ctx(request)
    return {
        "status": "ok",
        "message": "后端就绪",
        "uptime_seconds": round(_time.time() - ctx.started_at, 1),
    }


# ──────────────────────────────────────────────────────────────────────
# 顶部状态条
# ──────────────────────────────────────────────────────────────────────


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    """一屏看全：设备 / 账号 / 网易云服务 / 作业队列。"""
    ctx = get_ctx(request)

    device = _device_or_none(ctx)
    if device is None:
        ipod: dict[str, Any] = {
            "connected": False,
            "name": "",
            "tracks": 0,
            "free_text": "",
            "hint": "未检测到 iPod。检查数据线、确认盘符，或点重试。",
        }
    else:
        # 曲目数要读库；读不动（库损坏等）也不能让状态条整个挂掉
        try:
            library = ctx.library()
            tracks = len(library.tracks)
            ipod_name = library.ipod_name
        except Exception as exc:                      # noqa: BLE001
            tracks = 0
            ipod_name = ""
            ipod = {
                "connected": True,
                "name": "",
                "tracks": 0,
                "free_text": device.free_text,
                "hint": f"设备在，但数据库读不出来：{exc}",
            }
        else:
            ipod = {
                "connected": True,
                "name": ipod_name,
                "tracks": tracks,
                "free_text": device.free_text,
                "hint": "",
            }

    service = ctx.netease_service()
    running = ctx.jobs.running_job

    return {
        "ipod": ipod,
        "account": ctx.account_summary(),
        "netease_service": {
            "reachable": service.reachable,
            "base_url": service.base_url,
            "detail": service.detail,
        },
        "jobs": {
            "running": _job_brief(running),
            "queued": ctx.jobs.queued_count,
            "has_work": ctx.jobs.has_work,
        },
    }


# ──────────────────────────────────────────────────────────────────────
# 设备信息（设置页那组卡片）
# ──────────────────────────────────────────────────────────────────────


@router.get("/device")
def device_info(request: Request) -> dict[str, Any]:
    """设备详情：型号 / 序列号 / 存储 / 内容统计。

    ``editable_fields`` 明确标出哪些能改——**这是有意的区分**：
    设备名存在 iTunesDB 的主播放列表标题里，可以改；而序列号、
    FireWire GUID 来自 ``Device/SysInfo``，是硬件信息，改不了。
    不标清楚的话用户会以为都能改，然后找不到入口。
    """
    ctx = get_ctx(request)

    found = _device_or_none(ctx)
    if found is None:
        return {
            "connected": False,
            "error": "未检测到 iPod",
            "hint": (
                "请确认：① 数据线插好、iPod 已解锁；"
                "② 在「此电脑」里能看到它的盘符；"
                "③ 盘符不是被别的程序独占（iTunes 有时会占）。"
            ),
        }

    try:
        library = ctx.library()
    except Exception as exc:                          # noqa: BLE001
        return {
            "connected": True,
            "error": f"设备在，但数据库读不出来：{exc}",
            "hint": "可以用 `ipod verify` 看详细诊断，或者从备份还原。",
        }

    summary = library.summary()
    used_bytes = max(found.total_bytes - found.free_bytes, 0)
    music_bytes = int(summary.get("total_bytes", 0))

    # 主播放列表的标题就是设备名，别把它当普通列表列出来
    names = [
        title for title in (
            str(p.get("Title") or "") for p in library.playlists
        )
        if title and title != library.ipod_name
    ]

    return {
        "connected": True,
        "identity": {
            "name": library.ipod_name,
            "model_number": found.model_number,
            "display_name": found.display_name,
            "family": found.family,
            "generation": found.generation,
            "capacity": found.capacity,
            "color": found.color,
            "serial": found.serial,
            "firewire_guid": found.firewire_guid,
            "checksum": found.checksum,
            "mount_point": str(found.root),
            "is_supported": found.is_supported,
        },
        "storage": {
            "total_bytes": found.total_bytes,
            "used_bytes": used_bytes,
            "free_bytes": found.free_bytes,
            "music_bytes": music_bytes,
            "total_text": found.total_text,
            "used_text": human_size(used_bytes),
            "free_text": found.free_text,
            "music_text": human_size(music_bytes),
            "used_percent": (
                round(used_bytes / found.total_bytes * 100, 1)
                if found.total_bytes else 0.0
            ),
        },
        "content": {
            "tracks": int(summary.get("track_count", 0)),
            "albums": int(summary.get("album_count", 0)),
            "artists": int(summary.get("artist_count", 0)),
            "playlists": len(names),
            "playlist_names": names,
            "total_ms": int(summary.get("total_ms", 0)),
            "duration_text": _duration_text(int(summary.get("total_ms", 0))),
        },
        # 可改的字段只有设备名。其余是硬件信息。
        "editable_fields": ["name"],
    }
