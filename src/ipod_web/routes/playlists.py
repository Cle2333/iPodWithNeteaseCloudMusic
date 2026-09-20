"""歌单浏览 + 下载 + 同步到 iPod。

这一层是「前端点一下，后端排队干」的核心。三条约束贯穿全文：

1. **所有打网易云的请求都走作业队列**。浏览歌单也算——它消耗的是有风控
   的配额，不该因为"用户来回翻页"就悄悄发出去几十个请求。
2. **歌单元数据 10 分钟缓存**，界面有显式「刷新」绕过。浏览不打接口。
3. **同步状态不缓存**。缓存了就会出现"明明刚下完，还显示未处理"。
   缓存的只有元数据，状态每次请求现算。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ipod_cli.discovery import DeviceNotFoundError
from ipod_cli.ncm.sync import (
    TARGET_IPOD,
    TARGET_LOCAL,
    SyncPlan,
    SyncSource,
    execute_downloads,
    fetch_source_songs,
    plan_sync,
    sync_to_ipod,
    synced_on_device,
)
from ipod_web.context import WebContext
from ipod_web.deps import get_ctx
from ipod_web.jobs import Job

router = APIRouter(prefix="/api/playlists", tags=["歌单"])

#: 一次"读歌单"最多让路由等多久（秒）。
#:
#: 超过了就先返回"正在加载"，界面显示进度并重试。**不再继续干等**：
#: 队列前面可能正跑着一个几百首的下载，等下去就是请求超时。
LOAD_WAIT = 25.0

#: 单页最多多少首。界面上限由后端定，不让前端一次要一万首。
MAX_PAGE_SIZE = 200


class DownloadRequest(BaseModel):
    #: 只处理前 N 首（0 = 全部）。给"先来 3 首试试"用的。
    limit: int = Field(default=0, ge=0)
    #: 是否在下载后直接写进 iPod（False = 只下到本地缓存）
    push: bool = False

    #: 只处理这些歌（界面上的"下载选中的 N 首"）。
    #:
    #: **`None` 和 `[]` 的区别是有意设计的**：
    #: * 不传（None）= 整个歌单
    #: * 传空列表 = "一首都没选" → **报错**，不是默默变成整单
    #:
    #: 把空列表当成整单是能出人命的：用户勾选框全取消之后点下载，
    #: 结果几百首开始往下跑，而且配额和磁盘空间都真花了。
    song_ids: list[int] | None = None


def _only_ids(song_ids: list[int] | None) -> set[int] | None:
    """把界面传来的选中集转成 `only_ids`。

    空列表**报错**而不是当"全部"——理由见 `DownloadRequest.song_ids`。
    """
    if song_ids is None:
        return None
    if not song_ids:
        raise HTTPException(
            status_code=400,
            detail="没有选中任何歌曲。要处理整个歌单请用「同步整个歌单」。",
        )
    return set(song_ids)


def _wait_job(job: Job, timeout: float) -> bool:
    """等这个作业结束。返回是否真的等到。

    只等**这一个**作业，不用 ``wait_idle``——后者会连队列里排在它前面的
    活儿一起等，前面有个大下载的话这里就永远等不到了。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.is_finished:
            return True
        time.sleep(0.1)
    return job.is_finished


def _source_of(ctx: WebContext, playlist_id: int) -> SyncSource:
    """把界面传来的歌单 ID 变成同步源。

    名字要从缓存里查——建同名播放列表、写日志都要用它，现编一个
    "歌单 12345" 这种东西会让 iPod 上多出一个名字很蠢的播放列表。
    """
    playlists = ctx.cached_playlists()
    if playlists is None:
        raise HTTPException(
            status_code=409,
            detail="还没有读到歌单列表，请先打开歌单页",
        )
    for item in playlists:
        if item["id"] == playlist_id:
            if item["liked"]:
                # 把歌单 id 一起带上：这样取曲目走 /playlist/track/all，
                # 顺序跟 App 一致，而且**不用再发一次请求去解析这个 id**
                return SyncSource.liked(item["name"], playlist_id=item["id"])
            return SyncSource.playlist(item["id"], item["name"])
    raise HTTPException(status_code=404, detail=f"没有这个歌单：{playlist_id}")


# ──────────────────────────────────────────────────────────────────────
# 读歌单列表
# ──────────────────────────────────────────────────────────────────────


@router.get("")
def list_playlists(
    refresh: bool = False, ctx: WebContext = Depends(get_ctx)
) -> dict[str, Any]:
    """网易云的歌单列表。

    10 分钟内走缓存；要打接口时**走作业队列**（消耗的是有风控的配额）。
    """
    if not refresh:
        cached = ctx.cached_playlists()
        if cached is not None:
            return {"playlists": cached, "cached": True, "loading": False}

    job = ctx.jobs.submit(
        "playlists", "读取歌单列表", lambda handle: _load_playlists(ctx, handle)
    )

    if not _wait_job(job, LOAD_WAIT):
        return {
            "playlists": ctx.cached_playlists() or [],
            "cached": False,
            "loading": True,
            "job_id": job.id,
            "message": "正在读取歌单（前面有作业在排队）…",
        }

    if job.state == "failed":
        raise HTTPException(status_code=502, detail=f"读取歌单失败：{job.error}")

    return {
        "playlists": ctx.cached_playlists() or [],
        "cached": False,
        "loading": False,
        "request_count": job.result.get("request_count", 0),
    }


def _load_playlists(ctx: WebContext, handle) -> dict[str, Any]:
    account = ctx.store.active_account()
    if account is None:
        raise RuntimeError("还没有登录网易云账号，请先到设置里扫码登录")

    client = ctx.client()
    handle.log(f"读取 {account.nickname} 的歌单列表…")
    raw = client.user_playlists(account.uid, cookie=account.cookie)

    data = [
        {
            "id": playlist.id,
            "name": playlist.name,
            "track_count": playlist.track_count,
            "creator": playlist.creator,
            "subscribed": playlist.subscribed,
            "liked": playlist.is_liked,
        }
        for playlist in raw
    ]
    ctx.set_playlists(data)

    total = sum(item["track_count"] for item in data)
    handle.log(f"共 {len(data)} 个歌单、{total} 条曲目（含重复）")
    return {"count": len(data), "request_count": client.request_count}


# ──────────────────────────────────────────────────────────────────────
# 读某个歌单的曲目
# ──────────────────────────────────────────────────────────────────────


#: 曲目列表可以按状态筛。界面上的下拉就是这几个。
SONG_FILTERS = ("all", "pending", "downloaded", "on_ipod")


@router.get("/{playlist_id}/songs")
def playlist_songs(
    playlist_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    status: str = Query("all"),
    search: str = Query(""),
    ctx: WebContext = Depends(get_ctx),
) -> dict[str, Any]:
    """分页给曲目，并标出每首的同步状态。

    状态是**现算**的（已同步 / 已下载 / 未下载），不走缓存——
    缓存了就会出现"刚下完还显示未下载"。

    ``search`` 按歌名/艺人做子串匹配（不分大小写）。**在服务端筛是有意的**：
    客户端只能筛当前页的 50 首，246 首的歌单"搜了跟没搜一样"。
    """
    if status not in SONG_FILTERS:
        raise HTTPException(
            status_code=400,
            detail=f"不认识的筛选：{status}。可用的是 {'、'.join(SONG_FILTERS)}",
        )

    source = _source_of(ctx, playlist_id)
    tracks = _tracks_of(ctx, playlist_id, source)

    synced = _synced_ids(ctx)
    downloaded = ctx.store.downloaded_song_ids()

    # counts 是**整单**的，不受筛选影响——界面要显示
    # "共 246 首 · 未处理 243 · 已在 iPod 3"，那是整单的概况。
    counts = {
        "on_ipod": sum(1 for t in tracks if t.id in synced),
        "downloaded": sum(
            1 for t in tracks if t.id not in synced and t.id in downloaded
        ),
        "pending": sum(
            1 for t in tracks if t.id not in synced and t.id not in downloaded
        ),
        # 已经在设备上、但本地那份已经没了。单独数出来：这部分用户
        # 点「下载到本地」时**是会有事做的**，而以前会被算进"已就绪"。
        "on_ipod_but_no_local": sum(
            1 for t in tracks if t.id in synced and t.id not in downloaded
        ),
    }
    counts["all"] = len(tracks)

    matched = [
        t for t in tracks if status == "all" or _status_of(t.id, synced, downloaded) == status
    ]
    if search.strip():
        matched = [t for t in matched if _matches_query(t, search)]

    start = (page - 1) * size
    window = matched[start : start + size]

    return {
        "playlist": source.name,
        "loading": False,
        "page": page,
        "size": size,
        "status": status,
        "total": counts["all"],
        "filtered": len(matched),
        "pages": max((len(matched) + size - 1) // size, 1),
        "counts": counts,
        "songs": [
            {
                "id": song.id,
                "name": song.name,
                "artist": song.artist_text,
                "album": song.album,
                "duration_ms": song.duration_ms,
                "status": _status_of(song.id, synced, downloaded),
                # 两个维度分开给。**合起来判断会掩盖"在 iPod 上但本地没留"**
                # 这种情况——用户删了本地文件之后就靠它提醒自己。
                "on_ipod": song.id in synced,
                "local": song.id in downloaded,
            }
            for song in window
        ],
    }


@router.get("/{playlist_id}/ids")
def playlist_ids(
    playlist_id: int,
    status: str = Query("all"),
    ctx: WebContext = Depends(get_ctx),
) -> dict[str, Any]:
    """只要 ID 的轻量版，给界面上的「一键选中所有未处理的（N 首）」用。

    没有它的话，界面只能选中"当前这一页已经加载出来的"——246 首的歌单
    50 首一页，用户点"全选未处理"实际只选到 50 首，然后以为任务在跑。
    曲目本来就缓存着，这个接口不产生任何网易云请求。
    """
    if status not in SONG_FILTERS:
        raise HTTPException(
            status_code=400,
            detail=f"不认识的筛选：{status}。可用的是 {'、'.join(SONG_FILTERS)}",
        )

    source = _source_of(ctx, playlist_id)
    tracks = _tracks_of(ctx, playlist_id, source)

    synced = _synced_ids(ctx)
    downloaded = ctx.store.downloaded_song_ids()

    ids = [
        t.id
        for t in tracks
        if status == "all" or _status_of(t.id, synced, downloaded) == status
    ]
    return {"playlist": source.name, "status": status, "ids": ids, "count": len(ids)}


def _tracks_of(ctx: WebContext, playlist_id: int, source: SyncSource) -> list[Any]:
    """拿这个歌单的曲目（缓存的，没有就先读一次）。

    读的时候走作业队列——曲目列表是打接口拿的，消耗有风控的配额。
    """
    tracks = ctx.cached_tracks(playlist_id)
    if tracks is not None:
        return tracks

    job = ctx.jobs.submit(
        "tracks",
        f"读取歌单「{source.name}」曲目",
        lambda handle: _load_tracks(ctx, handle, source, playlist_id),
    )
    if not _wait_job(job, LOAD_WAIT):
        raise HTTPException(
            status_code=409,
            detail="正在读取曲目（前面有作业在排队），请稍后重试。",
        )
    if job.state == "failed":
        raise HTTPException(status_code=502, detail=f"读取曲目失败：{job.error}")
    return ctx.cached_tracks(playlist_id) or []


def _load_tracks(
    ctx: WebContext, handle, source: SyncSource, playlist_id: int
) -> dict[str, Any]:
    """读曲目并缓存。

    ``playlist_id`` 单独传是**缓存键**。它必须是真的歌单 id：
    拿 ``source.playlist_id`` 当键的话，万一拿到 0（比如调用方没解析出来），
    会存到 0 而按真实 id 去读——结果是**每次都重新打接口**（白烧有风控的
    配额），而且第一次读还会返回空列表。
    """
    account = ctx.store.active_account()
    if account is None:
        raise RuntimeError("还没有登录网易云账号，请先到设置里扫码登录")

    client = ctx.client()
    handle.log(f"读取「{source.name}」的曲目…")

    # ★ 取曲目**只在引擎里实现一处**（`fetch_source_songs`），别在这儿再写一遍。
    #
    # 这里以前自己写了一套：liked 走 /likelist + song/detail、playlist 走
    # /playlist/track/all。规划那边改成"liked 也走歌单接口（App 的顺序）"
    # 之后，这份副本没跟上——于是**下载的顺序对了、界面上看到的顺序还是乱的**
    # （用户反馈："我喜欢歌单的顺序我这里还是乱的"）。
    #
    # 同一套逻辑有两份实现，就必然有一份会掉队。合并成一份。
    #
    # 顺带：liked 现在只发 1 次请求（歌单接口自带元数据），
    # 原来是 likelist + 每 100 首一次 song/detail。
    tracks = fetch_source_songs(
        client,
        source,
        uid=account.uid,
        cookie=account.cookie,
    )

    ctx.set_tracks(playlist_id, tracks)
    handle.log(f"读到 {len(tracks)} 首")
    return {"count": len(tracks), "request_count": client.request_count}


def _synced_ids(ctx: WebContext) -> set[int]:
    """真正在设备上的歌 ID。没插设备就当空集（界面只是不显示"已在 iPod"）。"""
    try:
        library = ctx.library()
    except DeviceNotFoundError:
        return set()
    return synced_on_device(ctx.store, library)


def _matches_query(song: Any, needle: str) -> bool:
    """歌名或艺人里含这个子串（不分大小写）。

    用 ``str.lower()`` 而不是 casefold：中文没有大小写，这里的场景不需要
    更严格的 Unicode 折叠，简单直接更好懂。
    """
    text = f"{song.name}\n{song.artist_text}".lower()
    return needle.strip().lower() in text


def _status_of(song_id: int, synced: set[int], downloaded: set[int]) -> str:
    """三种状态：未下载 / 已下载 / 已同步。

    **没有"无版权"这一档**：要判断一首歌能不能下，得逐首问下载链接，
    每首 1~3 次请求——几百首的歌单根本不划算。真实的不可用由下载作业
    跑完之后报出来（那时候已经问过了，是顺带的）。
    """
    if song_id in synced:
        return "on_ipod"
    if song_id in downloaded:
        return "downloaded"
    return "pending"


# ──────────────────────────────────────────────────────────────────────
# 下载 / 同步
# ──────────────────────────────────────────────────────────────────────


@router.post("/{playlist_id}/download")
def download_playlist(
    playlist_id: int,
    payload: DownloadRequest,
    ctx: WebContext = Depends(get_ctx),
) -> dict[str, Any]:
    """把歌单下到本地缓存。``push=true`` 时顺带写进 iPod。

    带 ``song_ids`` 就只处理选中的那些（界面上的"下载选中的 N 首"）。
    """
    only = _only_ids(payload.song_ids)
    source = _source_of(ctx, playlist_id)

    if only is None:
        what = source.name
    else:
        what = f"「{source.name}」选中的 {len(only)} 首"
    title = f"{'同步' if payload.push else '下载'}{what}"

    job = ctx.jobs.submit(
        "sync" if payload.push else "download",
        title,
        lambda handle: _run_sync(
            ctx, handle, source, payload.limit, payload.push, only
        ),
    )
    return {
        "ok": True,
        "job_id": job.id,
        "message": f"已加入队列：{title}",
        "queued": ctx.jobs.queued_count,
    }


def _run_sync(
    ctx: WebContext,
    handle,
    source: SyncSource,
    limit: int,
    push: bool,
    only_ids: set[int] | None = None,
) -> dict[str, Any]:
    """作业体：规划 → 下载 →（可选）写 iPod。

    规划是**纯计算**，先算清楚"要下几首、大概多大"再动手——
    直接开干的话，用户会在毫不知情的情况下开始下载几百首。
    """
    account = ctx.store.active_account()
    if account is None:
        raise RuntimeError("还没有登录网易云账号，请先到设置里扫码登录")

    client = ctx.client()
    level = ctx.get_setting("quality")

    handle.log(f"音质档位：{level}；请求间隔：{ctx.min_interval():.2f} 秒")
    if only_ids is not None:
        handle.log(f"只处理选中的 {len(only_ids)} 首")

    # 要写 iPod 的话，拿设备库来核对"哪些真的已经在设备上"
    device = None
    library = None
    if push:
        device = ctx.device()
        library = ctx.library(force=True)
        handle.log(f"目标设备：{library.ipod_name or device.display_name}")

    handle.log(f"正在读取「{source.name}」…")
    plan = plan_sync(
        client,
        ctx.store,
        source,
        level=level,
        uid=account.uid,
        cookie=account.cookie,
        limit=limit,
        library=library,
        progress=handle.progress,
        only_ids=only_ids,
        # 要写 iPod 就按"设备上有没有"跳过；只下载就按"本地有没有"跳过。
        # 混用会让删掉本地文件之后点下载却一首都不下。
        target=TARGET_IPOD if push else TARGET_LOCAL,
    )

    if not plan.items:
        # 选中集里的歌一首都没落到计划上——多半是 ID 对不上（歌单已经变过）。
        # 静默"什么都没做"会让用户以为在后台跑着，必须说清楚。
        raise RuntimeError(
            "选中的曲目一首都没匹配上。歌单可能已经变过，"
            "请点「刷新」重新读取后再试。"
        )

    to_fetch = plan.needs_fetch
    handle.set_total(len(to_fetch) if not push else len(plan.to_download))
    handle.log(
        f"要处理 {len(plan.to_download)} 首"
        f"（本地还缺 {len(to_fetch)} 首需要下载）"
        + (f"，另有 {len(plan.to_skip)} 首已就绪" if plan.to_skip else "")
    )
    if plan.unavailable:
        handle.log(f"有 {len(plan.unavailable)} 首拿不到（多半无版权）", level="warn")

    outcome = execute_downloads(
        client,
        ctx.store,
        plan,
        ctx.cache_dir,
        cookie=account.cookie,
        progress=handle.progress,
        on_item=handle.on_item,
        # 逐首歌的结构化进度：界面靠它显示"下过哪些歌、哪几首失败了"，
        # 不依赖日志文本
        on_song=handle.on_song,
    )

    result: dict[str, Any] = {
        "downloaded": len(outcome.downloaded),
        "failed": len(outcome.failed),
        "downloaded_mb": round(outcome.total_mb, 1),
    }

    if outcome.failed:
        # 失败的逐首记下来。"有 3 首失败"这种话对排查毫无帮助。
        for song, reason in outcome.failed[:20]:
            handle.log(f"失败：{song.label} —— {reason}", level="error")

    if not push:
        handle.log(
            f"下载完成：{len(outcome.downloaded)} 首、{outcome.total_mb:.1f} MB"
        )
        result["request_count"] = client.request_count
        return result

    # ── 写进 iPod ──
    handle.log("开始写入 iPod…（会整库重写，期间请不要拔设备）")
    ipod_outcome = sync_to_ipod(
        client,
        ctx.store,
        plan,
        device,
        library,
        cookie=account.cookie,
        make_playlist=True,
        progress=handle.progress,
        on_item=handle.on_item,
    )

    # 写过设备了，缓存必须失效——否则后面算"要删哪些"会拿着旧库去算，
    # 那个后果是真的删错歌。
    ctx.invalidate_library()

    result.update(
        {
            "added": ipod_outcome.added,
            "verified": ipod_outcome.verified,
            "playlist_names": ipod_outcome.playlist_names,
            "copied_mb": round(ipod_outcome.copied_mb, 1),
            "request_count": client.request_count,
        }
    )

    if ipod_outcome.failed:
        for name, reason in ipod_outcome.failed[:20]:
            handle.log(f"写入失败：{name} —— {reason}", level="error")

    if not ipod_outcome.verified:
        handle.log("⚠ 读回校验未通过，请到音乐库页跑一次健康检查", level="warn")

    handle.log(
        f"完成：写入 {ipod_outcome.added} 首"
        + (f"，播放列表：{'、'.join(ipod_outcome.playlist_names)}"
           if ipod_outcome.playlist_names else "")
    )
    return result


# ──────────────────────────────────────────────────────────────────────
# 只做规划（不下载）——给界面显示"这一下会下多少"
# ──────────────────────────────────────────────────────────────────────


@router.post("/{playlist_id}/plan")
def plan_only(
    playlist_id: int,
    payload: DownloadRequest,
    ctx: WebContext = Depends(get_ctx),
) -> dict[str, Any]:
    """预览：这一下会处理多少首、大概多大。

    界面上"开始下载"之前先给用户看这个——不然一句"下载整个歌单"点了
    就是几百首、几个 GB。选中子集时也按**选中的那些**算，不然预览说
    "要下 246 首"、实际只下 5 首，那这个预览就是假的。

    用 POST 而不是 GET：选中几百首的话 query string 会长到离谱。
    """
    only = _only_ids(payload.song_ids)
    source = _source_of(ctx, playlist_id)
    job = ctx.jobs.submit(
        "plan",
        f"规划「{source.name}」",
        lambda handle: _run_plan(ctx, handle, source, payload.limit, payload.push, only),
    )
    if not _wait_job(job, LOAD_WAIT):
        return {"loading": True, "job_id": job.id, "message": "正在规划…"}
    if job.state == "failed":
        raise HTTPException(status_code=502, detail=f"规划失败：{job.error}")
    return {"loading": False, **job.result}


def _run_plan(
    ctx: WebContext,
    handle,
    source: SyncSource,
    limit: int,
    push: bool,
    only_ids: set[int] | None = None,
) -> dict[str, Any]:
    account = ctx.store.active_account()
    if account is None:
        raise RuntimeError("还没有登录网易云账号，请先到设置里扫码登录")

    library = ctx.library(force=True) if push else None
    client = ctx.client()
    plan: SyncPlan = plan_sync(
        client,
        ctx.store,
        source,
        level=ctx.get_setting("quality"),
        uid=account.uid,
        cookie=account.cookie,
        limit=limit,
        library=library,
        progress=handle.progress,
        only_ids=only_ids,
        target=TARGET_IPOD if push else TARGET_LOCAL,
    )

    # 跳过的原因分门别类地给出来。
    #
    # 只说"已就绪 N 首"会让用户以为没事可做——但"已同步"和
    # "本地已有"是两件完全不同的事，尤其在他选了"下载到本地"的时候。
    skip_reasons: dict[str, int] = {}
    for item in plan.to_skip:
        skip_reasons[item.reason] = skip_reasons.get(item.reason, 0) + 1

    return {
        "playlist": source.name,
        "target": TARGET_IPOD if push else TARGET_LOCAL,
        "total": len(plan.items),
        "to_download": len(plan.to_download),
        "needs_fetch": len(plan.needs_fetch),
        "already_ready": len(plan.to_skip),
        "skip_reasons": skip_reasons,
        "unavailable": len(plan.unavailable),
        "estimated_mb": round(plan.estimated_mb, 1),
        "preview": [
            {
                "name": item.song.name,
                "artist": item.song.artist_text,
                "cached": item.cached,
            }
            for item in plan.needs_fetch[:10]
        ],
        "request_count": client.request_count,
    }
