"""调试：后端日志、环境信息、环境自检。

后端是被 app 自动拉起的子进程，用户看不见它的控制台——这些接口就是
"打开黑箱"的那扇窗。没有它们，出问题只能靠猜。
"""

from __future__ import annotations

import io
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query

from ipod_cli.discovery import human_size
from ipod_web.context import WebContext
from ipod_web.deps import get_ctx

router = APIRouter(prefix="/api/debug", tags=["调试"])


@router.get("/log")
def read_log(
    since: int = Query(0, ge=0, description="只返回 seq 大于它的行（增量拉取）"),
    limit: int = Query(400, ge=1, le=2000),
    ctx: WebContext = Depends(get_ctx),
) -> dict[str, Any]:
    """后端日志。

    **按 ``seq`` 增量拉取**。界面把上次拿到的最大 seq 存下来当游标，
    每次只要新行。全量重传的话，跑一次全库同步之后每轮轮询都要搬几千行，
    界面会肉眼可见地卡起来。
    """
    lines = ctx.log_buffer.since(since, limit=limit)
    latest = ctx.log_buffer.latest_seq
    return {
        "lines": [line.as_dict() for line in lines],
        "latest_seq": latest,
        # 界面落后太多（缓冲区已经绕圈了）时如实告诉它，免得它以为自己
        # 拿到了连续的日志
        "truncated": bool(lines) and lines[0].seq > since + 1,
    }


@router.post("/log/clear")
def clear_log(ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    ctx.log_buffer.clear()
    return {"ok": True, "message": "日志已清空"}


@router.get("/env")
def read_env(ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    """环境信息：出问题时第一眼要看的东西。"""
    from ipod_cli import __version__
    from ipod_cli.transcode import find_ffmpeg

    try:
        ffmpeg = str(find_ffmpeg())
    except Exception:  # noqa: BLE001 - 没装 ffmpeg 是常见情况，不该让接口挂掉
        ffmpeg = ""

    return {
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "ipod_cli": __version__,
        "ffmpeg": ffmpeg,
        "ffmpeg_ok": bool(ffmpeg),
        "platform": sys.platform,
        "cwd": str(Path.cwd()),
        "db_path": str(ctx.store.path),
        "cache_dir": str(ctx.cache_dir),
        "base_url": ctx.base_url,
        "ipod_path": ctx.ipod_path or "自动检测",
    }


@router.post("/doctor")
def run_doctor(ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    """跑一次环境自检。

    走作业队列而不是直接跑：自检要碰设备（读 iTunesDB），
    跟正在进行的写入作业并行会读到半截状态。
    """
    job = ctx.jobs.submit("doctor", "环境自检", lambda handle: _doctor(ctx, handle))
    return {"ok": True, "job_id": job.id, "message": "已开始环境自检"}


def _doctor(ctx: WebContext, handle) -> dict[str, Any]:
    """作业体：捕获 ``ipod doctor`` 的输出，逐行转成作业日志。"""
    import argparse

    from ipod_cli import cli

    args = argparse.Namespace(ipod=ctx.ipod_path)

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = cli.cmd_doctor(args)
        except Exception as exc:  # noqa: BLE001
            code = 1
            stderr.write(f"{type(exc).__name__}: {exc}")

    text = stdout.getvalue()
    err_text = stderr.getvalue()
    for line in text.splitlines():
        handle.log(line)
    for line in err_text.splitlines():
        if line.strip():
            handle.log(line, level="error")

    return {"exit_code": code, "text": text, "stderr": err_text}


# ──────────────────────────────────────────────────────────────────────
# 导出诊断记录
# ──────────────────────────────────────────────────────────────────────

#: 每个作业最多导出多少行日志。几百首的下载会刷出几千行，
#: 不截一下的话导出的文件能到几 MB，发起来费劲、看起来也费劲。
MAX_LOG_LINES_PER_JOB = 3000

#: 后端日志缓冲最多导多少行。
MAX_BACKEND_LOG_LINES = 2000


def _export_dir() -> Path:
    """导出到哪。

    优先"文档"目录——用户要能找到、要能发给别人；找不到再退回临时目录。
    写进仓库里的 .ncm/ 是不行的：太隐蔽，用户根本找不到。
    """
    docs = Path.home() / "Documents"
    if docs.is_dir():
        return docs
    return Path(tempfile.gettempdir())


def _job_report(job) -> list[str]:
    """一个作业的完整记录：元信息 + 逐首歌的结果 + 日志。"""
    out: list[str] = []
    out.append(f"### 作业 {job.id} —— {job.title}")
    out.append(f"  类型：{job.kind}")
    out.append(f"  状态：{job.state}")
    out.append(f"  进度：{job.done}/{job.total}")
    out.append(f"  耗时：{job.elapsed:.1f} 秒")
    if job.result:
        out.append(f"  结果：{job.result}")
    if job.error:
        out.append(f"  ★ 错误：{job.error}")

    items = [i for i in job.items if i]
    if items:
        ok = sum(1 for i in items if i.get("status") == "done")
        bad = sum(1 for i in items if i.get("status") == "failed")
        out.append(f"  曲目：共 {len(items)} 首，成功 {ok}，失败 {bad}")
        for item in items:
            mark = {"done": "✓", "failed": "✗", "downloading": "…"}.get(
                item.get("status", ""), "?"
            )
            line = f"    {mark} [{item.get('index')}] {item.get('name')}"
            if item.get("artist"):
                line += f" - {item['artist']}"
            if item.get("size"):
                line += f"  {human_size(item['size'])}"
            if item.get("reason"):
                line += f"  ← {item['reason']}"
            out.append(line)

    lines = job.log[-MAX_LOG_LINES_PER_JOB:]
    if len(job.log) > len(lines):
        out.append(f"  （日志共 {len(job.log)} 行，只导出最后 {len(lines)} 行）")
    if lines:
        out.append("  日志：")
        for line in lines:
            out.append(f"    [{line.level}] {line.time_text} {line.text}")
    out.append("")
    return out


@router.post("/export")
def export_diagnostics(ctx: WebContext = Depends(get_ctx)) -> dict[str, Any]:
    """把一份完整的现场导出成文本，方便排查问题。

    为什么要这个：出了 bug 的时候，"界面上看着不对"这句话没法定位问题。
    这里一次性把环境、设备、设置、每个作业干了什么、逐首歌的结果、
    完整日志全写进一个文件，直接发出来就能查。

    **不走作业队列**：这是纯读操作，而且用户往往正是卡在某个作业上
    才来导出的——排在它后面就永远导不出来了。
    """
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    out: list[str] = []

    out.append("=" * 72)
    out.append("iPod 音乐管理器 · 诊断记录")
    out.append(f"生成时间：{started}")
    out.append("=" * 72)
    out.append("")

    # ── 环境 ──
    out.append("## 环境")
    try:
        for key, value in read_env(ctx).items():
            out.append(f"  {key}: {value}")
    except Exception as exc:  # noqa: BLE001 - 读环境失败也要把文件导出来
        out.append(f"  （读取失败：{type(exc).__name__}: {exc}）")
    out.append("")

    # ── 设置 ──
    out.append("## 设置")
    out.append(f"  音质档位：{ctx.get_setting('quality')}")
    out.append(f"  请求间隔：{ctx.min_interval():.2f} 秒")
    try:
        out.append(f"  缓存：{ctx.store.count_downloads()} 条下载记录")
    except Exception as exc:  # noqa: BLE001
        out.append(f"  缓存：（读取失败：{exc}）")
    if ctx.store.active_account() is not None:
        account = ctx.store.active_account()
        out.append(f"  账号：{account.nickname}（uid {account.uid}）")
    else:
        out.append("  账号：未登录")
    out.append("")

    # ── 设备 ──
    out.append("## 设备")
    try:
        library = ctx.library()
        out.append(f"  名称：{library.ipod_name}")
        out.append(f"  曲目：{len(library.tracks)} 首")
        out.append(f"  播放列表：{len(library.playlists)} 个")
    except Exception as exc:  # noqa: BLE001 - 没插设备是常见情况
        out.append(f"  （读不到设备：{type(exc).__name__}: {exc}）")
    out.append("")

    # ── 作业（新的在前）──
    jobs = ctx.jobs.list_jobs()
    out.append(f"## 作业记录（共 {len(jobs)} 个，新的在前）")
    out.append("")
    if not jobs:
        out.append("  （还没有跑过任何作业）")
        out.append("")
    for job in jobs:
        out.extend(_job_report(job))

    # ── 后端日志 ──
    out.append(f"## 后端日志（最后 {MAX_BACKEND_LOG_LINES} 行）")
    buffer = ctx.log_buffer
    recent = buffer.since(
        max(buffer.latest_seq - MAX_BACKEND_LOG_LINES, 0),
        limit=MAX_BACKEND_LOG_LINES,
    )
    if not recent:
        out.append("  （空）")
    # 注意：这是 logbuf 的 LogLine，它的 ts **已经是** "HH:MM:SS" 字符串。
    # （jobs.py 里那个同名类的 ts 是 float 且有 time_text——两个类不一样，
    #   这里用混了会 AttributeError。）
    for line in recent:
        out.append(f"  [{line.level}] {line.ts} {line.text}")
    out.append("")

    text = "\n".join(out)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = _export_dir() / f"iPod管理器诊断-{stamp}.txt"
    # 用 utf-8-sig：Windows 的记事本按本地代码页猜编码，
    # 没有 BOM 的话中文全是乱码——导出的东西看不懂就白导了
    target.write_text(text, encoding="utf-8-sig")

    return {
        "ok": True,
        "path": str(target),
        "bytes": target.stat().st_size,
        "lines": len(out),
        "jobs": len(jobs),
        "message": f"已导出到 {target.name}",
    }
