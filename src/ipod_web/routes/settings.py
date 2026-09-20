"""设置：音质、请求间隔、本地缓存。

音质和间隔这两项**不是普通偏好，是有安全含义的**：
* 间隔调太小 → 高频请求 → 网易云风控封号
* 无损全库装不下（144GB > 79.6GB）

所以后端对这两个值都有兜底校验，不信任界面传来的任何数字。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ipod_cli.discovery import human_size
from ipod_cli.ncm.client import QUALITY_LABEL
from ipod_web.context import MIN_INTERVAL_FLOOR, WebContext
from ipod_web.deps import get_ctx

router = APIRouter(prefix="/api", tags=["设置"])


class SettingsUpdate(BaseModel):
    quality: str | None = None
    min_interval: float | None = Field(default=None)


class RemoveLocalRequest(BaseModel):
    #: 要删掉的歌 ID。**空列表 = 报错**，不是"全删"——全删走 /cache/clear。
    song_ids: list[int] = Field(default_factory=list)
    #: 是否连磁盘上的文件一起删（默认删）。
    #:
    #: 传 False 就是"只清记录、留着文件"——用于缓存被外部动过、
    #: 想重新对一遍的情况。
    delete_files: bool = True


@router.get("/settings")
def read_settings(ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    return ctx.all_settings()


@router.put("/settings")
def write_settings(
    payload: SettingsUpdate, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """改设置。两项都是可选的，只改传过来的那些。"""
    changed: list[str] = []

    if payload.quality is not None:
        if payload.quality not in QUALITY_LABEL:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"不认识的音质档位「{payload.quality}」，"
                    f"可选：{'、'.join(QUALITY_LABEL)}"
                ),
            )
        ctx.set_setting("quality", payload.quality)
        changed.append(f"音质 → {QUALITY_LABEL[payload.quality]}")

    if payload.min_interval is not None:
        # 兜住下界：这是防封号的安全阀，不能靠界面自觉
        if payload.min_interval < MIN_INTERVAL_FLOOR:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"请求间隔不能小于 {MIN_INTERVAL_FLOOR:.2f} 秒。"
                    "调得太小会被网易云风控，这个下限是有意留的。"
                ),
            )
        ctx.set_setting("min_interval", f"{payload.min_interval:.2f}")
        changed.append(f"请求间隔 → {payload.min_interval:.2f} 秒")

    return {
        "ok": True,
        "message": "；".join(changed) if changed else "没有变化",
        "settings": ctx.all_settings(),
    }


@router.get("/cache")
def read_cache(ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    """本地下载缓存的用量。"""
    info = ctx.cache_info()
    records = ctx.store.count_downloads()
    return {
        **info,
        "records": records,
        # 文件数和记录数对不上 = 缓存被外部动过（手删了文件 / 从别处拷来的）。
        # 界面该提示这个，而不是假装没事。
        "consistent": info["files"] == records,
    }


@router.post("/cache/clear")
def clear_cache(ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    """清空本地下载缓存。

    删文件 **同时** 清下载记录。只删文件的话记录还在，界面会显示
    "已下载"而文件其实不在——虽然 `cached_download` 下次访问时会自我修正，
    但中间那段时间是在骗用户。
    """
    removed_files = 0
    freed = 0
    if ctx.cache_dir.is_dir():
        for item in ctx.cache_dir.iterdir():
            if not item.is_file():
                continue
            try:
                size = item.stat().st_size
                item.unlink()
            except OSError:
                # 被别的进程占着（比如同步作业正在读）——跳过而不是中断，
                # 剩下能删的继续删
                continue
            removed_files += 1
            freed += size

    removed_records = ctx.store.clear_downloads()

    return {
        "ok": True,
        "message": (
            f"已删除 {removed_files} 个缓存文件、{removed_records} 条下载记录，"
            f"释放 {human_size(freed)}"
        ),
        "removed_files": removed_files,
        "removed_records": removed_records,
        "freed_bytes": freed,
    }


@router.post("/cache/remove")
def remove_cache_entries(
    payload: RemoveLocalRequest,
    ctx: WebContext = Depends(get_ctx),
) -> dict[str, Any]:
    """删掉本地下载的**指定几首**（记录 + 文件）。

    放在"本地缓存"这一组里（跟 /cache、/cache/clear 一起）：它们管的是
    同一件事——电脑上那份缓存。歌单页的右键「删除本地那份」和多选操作条
    都打这个接口。

    **空列表报错**，不是"全删"——把空集当"全部"会出事：用户点了个没勾选的
    删除，结果 40 GB 缓存没了。清空有专门的 /cache/clear。
    """
    ids = [int(i) for i in payload.song_ids]
    if not ids:
        raise HTTPException(
            status_code=400,
            detail="没有选中任何歌曲。要清空全部请用「清空缓存」。",
        )

    job = ctx.jobs.submit(
        "remove_local",
        f"删除本地 {len(ids)} 首",
        lambda handle: _run_remove_local(ctx, handle, ids, payload.delete_files),
    )
    return {"ok": True, "job_id": job.id, "message": f"已加入队列：删除本地 {len(ids)} 首"}


def _run_remove_local(
    ctx: WebContext, handle, ids: list[int], delete_files: bool
) -> dict[str, Any]:
    """作业体：先把确切路径记下来，再删记录，最后删那些文件。

    顺序和"先探明目标"这两件事都是有意的：

    * **先探明路径**。不能删完记录再回头猜文件在哪——靠文件名里带
      song_id 去 glob 看着能用，实际上 song_id=123 会把 1234 的文件
      一起匹配进来，那是**删错歌**。`cached_download` 拿的是记录里
      存的确切路径，顺带还会核实文件真的在。
    * **先删记录再删文件**。反过来的话，中途失败会留下"记录说文件在、
      其实不在"的状态，界面会一直显示幽灵条目；先删记录最坏只留下
      孤儿文件，无害，下次清缓存就带走了。跟 remover 是同一套道理。
    """
    targets: list[Any] = []
    if delete_files:
        for song_id in ids:
            item = ctx.store.cached_download(song_id)
            if item is not None:
                targets.append(item.path)

    removed = ctx.store.remove_downloads(ids)
    handle.log(f"清掉 {removed} 条下载记录")

    if not delete_files:
        handle.log("按要求保留磁盘上的文件")
        return {"records_removed": removed, "files_deleted": 0, "bytes_freed": 0}

    freed = 0
    deleted = 0
    for path in targets:
        try:
            freed += path.stat().st_size
            path.unlink()
            deleted += 1
        except OSError as exc:
            handle.log(f"删不掉 {path.name}：{exc}", level="warn")

    if deleted == 0 and removed == 0:
        handle.log("这些歌的文件本来就不在磁盘上了", level="warn")

    handle.log(
        f"完成：删了 {deleted} 个文件、{removed} 条记录，释放 {human_size(freed)}"
    )
    return {
        "records_removed": removed,
        "files_deleted": deleted,
        "bytes_freed": freed,
        "bytes_freed_text": human_size(freed),
    }
