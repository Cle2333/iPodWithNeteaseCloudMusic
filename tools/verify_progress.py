"""进度上报的端到端验证：真跑一次同步，把**进度时间线**打出来。

## 它守的是一条不变式

**只要作业还在干活，界面拿到的数字就必须在动。**

这条不变式是被用户打脸打出来的：同步 147 首到 iPod 时界面"未响应直接崩溃"，
原话是"传很多歌就以为被卡住了"。查下来那段最长的时间（转码 FLAC→ALAC + 拷
3.3 GB，实测 6 分钟）**一个进度数字都不报** —— 进度条整整 6 分钟停在 0%。

单元测试能守住"回调被调用了"，但守不住"跑起来之后数字真的在变"。这个脚本
补的就是那一层：起**打好的发行版**，走界面点「同步」时走的那条接口，然后按
固定间隔采样作业的 `stage / done / total / percent`，把时间线打出来。

## 判据

1. 至少出现一个 ``total == 0`` 的阶段 —— 那是"切不出等份"的段（刷盘、整库
   重写），界面据此画**滚动**的条。它不能是 0%，否则用户看到的还是卡住。
2. 至少有一个 ``total > 0`` 的阶段，且它的 ``done`` **单调不减**、``percent``
   真的涨上去。
3. 结束时 ``done == total``。
4. 全程采样到的阶段名要覆盖写入 iPod 那条主路径。

## 用法

    uv run python tools/verify_progress.py --ipod <彩排目录或 D:\\>
    uv run python tools/verify_progress.py --ipod D:\\ --count 5

**只挑已经下到本地的歌**（`local=true` 且设备上没有），所以整个验证过程
不产生下载请求——真机验证一次最多 3-5 首那条约束在这里自然满足。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = (ROOT / "app" / "build" / "windows" / "x64" / "runner" / "Release"
       / "ipod_manager.exe")
PORT = 8765
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {label}"
          + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def api(method: str, path: str, body: dict | None = None, timeout: float = 30):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}


def drain(proc: subprocess.Popen, keep: int = 200) -> list[str]:
    """把应用的输出一路读走。

    ★ 必须读：不读的话管道会填满，应用**卡在写日志上**，症状是"后端莫名其妙
    不响应了"，看着像死锁，其实是没人收它的输出。
    """
    lines: list[str] = []

    def run() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append(line.rstrip())
                if len(lines) > keep:
                    del lines[: len(lines) - keep]
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=run, daemon=True).start()
    return lines


def wait_ready(timeout: float = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            api("GET", "/api/status", timeout=3)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(1)
    return False


def pick_songs(playlist_id: int, count: int) -> tuple[list[dict], dict]:
    """挑几首**已经下到本地、但设备上没有**的歌。

    这样整条链路（读标签 → 转码/拷贝 → 刷盘 → 写库）都会真的跑，
    而且**不需要任何下载请求**。
    """
    page = api("GET", f"/api/playlists/{playlist_id}/songs?size=200&status=downloaded")
    rows = page.get("songs") or []
    ready = [r for r in rows if r.get("local") and r.get("device") == "off_ipod"]
    return ready[:count], page.get("counts") or {}


def sample_stage(job: dict) -> tuple:
    return (
        job.get("stage", ""),
        job.get("total", 0),
        job.get("done", 0),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="进度上报的端到端验证")
    ap.add_argument("--ipod", required=True, help="彩排目录或真机盘符（如 D:\\）")
    ap.add_argument("--exe", default=str(EXE))
    ap.add_argument("--playlist-id", type=int, default=0,
                    help="要同步的歌单 id（默认自动挑一个）")
    ap.add_argument("--count", type=int, default=5,
                    help="处理几首（默认 5 —— 真机验证一次最多 3-5 首）")
    ap.add_argument("--interval", type=float, default=0.5,
                    help="采样间隔（秒）")
    ap.add_argument("--timeout", type=float, default=900)
    args = ap.parse_args()

    ipod = Path(args.ipod)
    if not (ipod / "iPod_Control").is_dir():
        raise SystemExit(f"{ipod} 里没有 iPod_Control，不是 iPod（彩排目录或盘符）")
    exe = Path(args.exe)
    if not exe.is_file():
        raise SystemExit(f"没找到发行版 exe：{exe}\n先跑 tools/build_windows_release.py")

    env = dict(os.environ)
    # 指向目标设备，**不靠自动检测**——否则拿彩排验证时会被真机抢走
    env["IPOD_MANAGER_IPOD"] = str(ipod)
    env.setdefault("IPOD_MANAGER_PORT", str(PORT))

    print(f"══ 起发行版 ══  {exe.name}")
    print(f"   目标设备：{ipod}")
    proc = subprocess.Popen(
        [str(exe)], cwd=str(exe.parent), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    log = drain(proc)

    try:
        if not wait_ready():
            print("   ❌ 后端没起来")
            print("\n".join(log[-20:]))
            return 1
        print("   ✅ 后端就绪")

        status = api("GET", "/api/status")
        dev = status.get("ipod") or {}
        print(f"   ✅ 设备：{dev.get('name')} · {dev.get('tracks')} 首")

        playlist_id = args.playlist_id
        if not playlist_id:
            for p in api("GET", "/api/playlists").get("playlists") or []:
                songs, _ = pick_songs(p["id"], 1)
                if songs:
                    playlist_id = p["id"]
                    print(f"   ✅ 用歌单「{p['name']}」(id={playlist_id})")
                    break
        if not playlist_id:
            raise SystemExit("没有找到「本地有、设备上没有」的歌——换个歌单或先下几首")

        picks, counts = pick_songs(playlist_id, args.count)
        if not picks:
            raise SystemExit("这个歌单里没有可用的歌")
        print(f"   本地有/设备上没有：{counts.get('local_but_not_on_ipod')} 首，"
              f"这次处理 {len(picks)} 首")
        for r in picks:
            print(f"      · {r['name']} — {r['artist']}")

        ids = [r["id"] for r in picks]
        submitted = api("POST", f"/api/playlists/{playlist_id}/download",
                        {"push": True, "limit": 0, "song_ids": ids})
        job_id = submitted["job_id"]
        print(f"\n   ✅ 已提交：{submitted['message']}  job={job_id}")

        # ── 采样 ────────────────────────────────────────────────────────
        print("\n══ 进度时间线 ══")
        print(f"   {'时刻':>8}  {'阶段':<28} {'done/total':>10}  {'%':>6}  当前")
        timeline: list[dict] = []
        last_key = None
        started = time.time()
        final: dict = {}
        while time.time() - started < args.timeout:
            try:
                job = api("GET", f"/api/jobs/{job_id}", timeout=15)
            except Exception:  # noqa: BLE001 - 转码/写库时后端偶尔慢，正常
                time.sleep(args.interval)
                continue
            final = job
            key = sample_stage(job)
            if key != last_key:
                last_key = key
                elapsed = time.time() - started
                timeline.append(
                    {
                        "t": round(elapsed, 1),
                        "stage": job.get("stage", ""),
                        "done": job.get("done", 0),
                        "total": job.get("total", 0),
                        "percent": job.get("percent", 0),
                        "current": job.get("current", ""),
                        "state": job.get("state", ""),
                    }
                )
                pct = job.get("percent", 0)
                print(
                    f"   {elapsed:7.1f}s  {job.get('stage',''):<28} "
                    f"{job.get('done',0):>4}/{job.get('total',0):<5} "
                    f"{pct:>5.1f}%  {job.get('current','')}"
                )
            if job.get("state") in ("done", "failed", "cancelled"):
                break
            time.sleep(args.interval)

        # ── 判据 ────────────────────────────────────────────────────────
        print("\n══ 判据 ══")
        check("作业成功结束", final.get("state") == "done",
              f"实际 {final.get('state')} / {final.get('error','')}")

        indeterminate = [s for s in timeline if s["total"] == 0 and s["state"] == "running"]
        check(
            "有「切不出等份」的阶段（界面会画滚动的条，而不是停在 0%）",
            bool(indeterminate),
            "整段都在报 total>0 —— 刷盘/写库那几步没报 stage",
        )

        counted = [s for s in timeline if s["total"] > 0]
        check("有能算百分比的阶段", bool(counted))
        if counted:
            groups: dict[str, list[dict]] = {}
            for s in counted:
                groups.setdefault(s["stage"], []).append(s)
            # 每个阶段内部 done 必须单调不减（跨阶段会归零，那是设计）
            for stage, rows in groups.items():
                dones = [r["done"] for r in rows]
                check(
                    f"「{stage}」里 done 单调不减",
                    dones == sorted(dones),
                    f"实际 {dones}",
                )
            advanced = [g for g, rows in groups.items()
                        if len(rows) > 1 and rows[-1]["done"] > rows[0]["done"]]
            check(
                "至少有一个阶段**真的在往前走**（不是只有开头一条）",
                bool(advanced),
                f"这些阶段只有一条样本或无推进：{list(groups)}",
            )

        if final.get("total"):
            check("结束时 done == total",
                  final.get("done") == final.get("total"),
                  f"{final.get('done')}/{final.get('total')}")

        stages_seen = [s["stage"] for s in timeline if s["stage"]]
        check("写入 iPod 那条主路径的阶段都出现过",
              any("写入" in s for s in stages_seen),
              f"实际 {stages_seen}")

        print(f"\n   耗时 {time.time() - started:.1f} 秒，采样到 "
              f"{len(timeline)} 个状态变化")

        # 收尾：确认端口干净
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()

        print("\n" + ("❌ 有未通过项：" + "、".join(FAILURES) if FAILURES
                      else "✅ 全部通过"))
        return 1 if FAILURES else 0

    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
