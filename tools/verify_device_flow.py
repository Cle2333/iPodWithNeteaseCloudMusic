"""设备端到端验证：让**打好的发行版**真的对一台 iPod 做完整读写往返。

## 它解决什么问题

单元测试跑在 tmp 目录的虚拟 iPod 上，验的是引擎；发行版自检
（`verify_embedded_release.py`）验的是"进程起没起、接口通不通"。
中间缺一层：**打好的那个 exe，真的能读/写一台 iPod 吗**。

那一层只有真机能验，但很多问题不用真机也能提前抓出来（路径解析、签名、
封面格式定义、播放列表接线）。所以这个脚本两种目标都能跑：

* **彩排目录**（`tools/rehearsal_from_backup.py` 造的，含真机 GUID/SysInfo）
  —— 随时可跑，覆盖除"物理盘符/USB"之外的全部链路
* **真机盘符**（如 `D:\\`）—— 最终验收

## 走哪些步骤

1. 起发行版，用 `IPOD_MANAGER_IPOD` 指向目标（**不靠自动检测**，好断言）
2. 读设备信息 + 读库（曲目数、播放列表）
3. 导入一个真音频文件（先预览、再执行、轮询作业）
4. 读回：曲目数要涨、新曲目要能查到
5. 跑一次健康检查
6. 退出后确认端口/子进程都收干净

用法：

    uv run python tools/verify_device_flow.py --ipod <彩排目录或D:>
    uv run python tools/verify_device_flow.py --ipod D:\\ --audio 某首歌.mp3
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
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {label}" + (f"  {detail}" if detail else ""))
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


def wait_job(job_id: str, timeout: float = 180) -> dict:
    """轮询作业直到落定。

    ★ 必须容忍**偶发的慢响应/超时**：作业跑的时候后端在干重活（转码、写库、
    算签名），单次请求慢过阈值是正常的。第一版这里没兜住，直接 TimeoutError
    中断——而实际上导入早就成功了，报出来的却是"失败"。
    """
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            last = api("GET", f"/api/jobs/{job_id}", timeout=15)
        except Exception:  # noqa: BLE001 - 慢/暂时不通都继续等
            time.sleep(2)
            continue
        if last.get("state") in ("done", "failed", "cancelled"):
            return last
        time.sleep(1.5)
    return last


def pick_audio(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise SystemExit(f"指定的音频不存在：{p}")
        return p
    # 退而求其次：用下载缓存里的（真 MP3、带标签和封面，正好压到导入路径的所有分支）
    roaming = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    cache = Path(roaming) / "com.example" / "ipod_manager" / "data" / ".ncm" / "cache"
    if cache.is_dir():
        for f in sorted(cache.rglob("*.mp3")):
            return f
    raise SystemExit("没给 --audio，缓存里也没有 mp3。用 --audio <文件> 指定一个。")


def drain(proc: subprocess.Popen, keep: int = 400) -> list[str]:
    """把应用的 stdout 一路读走，保留最后若干行。

    ★ 必须读：不读的话管道会被填满，应用**卡在写日志上**——症状是"后端莫名其妙
    不响应了"，而真正的原因是没人收它的输出。这个坑很隐蔽，因为它长得像死锁。
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


def main() -> int:
    ap = argparse.ArgumentParser(description="设备端到端验证")
    ap.add_argument("--ipod", required=True, help="彩排目录或真机盘符（如 D:\\）")
    ap.add_argument("--exe", default=str(EXE))
    ap.add_argument("--audio", default=None, help="要导入的音频文件")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    ipod = Path(args.ipod)
    if not (ipod / "iPod_Control").is_dir():
        raise SystemExit(f"{ipod} 里没有 iPod_Control，不是 iPod（彩排目录或盘符）")

    exe = Path(args.exe)
    if not exe.is_file():
        raise SystemExit(f"找不到 {exe}，先跑 tools/build_windows_release.py")

    audio = pick_audio(args.audio)
    print("══ 目标 ══")
    print(f"    设备   {ipod}")
    print(f"    音频   {audio.name}（{audio.stat().st_size / 1024 / 1024:.1f} MB）")
    print(f"    发行版 {exe.parent}")

    # 先清场：上一次留下的后端会占着端口
    subprocess.run(["taskkill", "/IM", "ipod_manager.exe", "/F"], capture_output=True)
    time.sleep(1)

    env = {**os.environ,
           "IPOD_MANAGER_IPOD": str(ipod),
           "IPOD_MANAGER_PORT": str(args.port)}
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    app_log = drain(proc)

    print("══ 1. 起发行版并指向目标设备 ══")
    ready = False
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            api("GET", "/api/status", timeout=3)
            ready = True
            break
        except Exception:
            time.sleep(1)
    check("后端就绪", ready, f"退出码 {proc.returncode}" if not ready else "")
    if not ready:
        proc.kill()
        out = proc.communicate(timeout=10)[0].decode("utf-8", errors="replace")
        print("\n".join("     " + ln for ln in out.splitlines()[-25:]))
        return 1

    print("══ 2. 设备与曲库 ══")
    dev = api("GET", "/api/device")
    check("认出了指定的设备（不是自动检测）", bool(dev.get("connected")),
          f"{dev.get('name') or dev.get('model') or dev}")
    before = None
    for _ in range(5):
        try:
            before = api("GET", "/api/library/tracks", timeout=60)
            break
        except Exception as exc:  # noqa: BLE001
            print(f"        读库重试：{exc}")
            time.sleep(3)
    if before is None:
        check("读库成功", False, "试了 5 次都不通")
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        return 1
    tracks_before = before.get("tracks") or []
    print(f"        曲目 {len(tracks_before)} 首")
    names_before = {(t.get("title") or "").strip() for t in tracks_before}

    print("══ 3. 导入（先预览）══")
    try:
        prev = api("POST", "/api/library/import/preview",
                   {"paths": [str(audio)]}, timeout=120)
        print(f"        预览原始返回 {json.dumps(prev, ensure_ascii=False)[:300]}")
        check("预览成功", bool(prev.get("ok")), "")
    except urllib.error.HTTPError as exc:
        check("预览成功", False, f"HTTP {exc.code}: {exc.read()[:200]!r}")

    print("══ 4. 真的导入并等它跑完 ══")
    try:
        started = api("POST", "/api/library/import", {"paths": [str(audio)]}, timeout=60)
        job_id = started.get("job_id")
        check("导入作业已提交", bool(job_id), str(job_id))
        if job_id:
            job = wait_job(job_id)
            state = job.get("state")
            check("导入作业成功", state == "done", f"state={state} {job.get('error') or ''}")
            print(f"        作业结果 {json.dumps(job.get('result'), ensure_ascii=False)[:220]}")
    except urllib.error.HTTPError as exc:
        check("导入作业已提交", False, f"HTTP {exc.code}: {exc.read()[:200]!r}")

    print("══ 5. 读回（写进去了没有）══")
    after = api("GET", "/api/library/tracks", timeout=60)
    tracks_after = after.get("tracks") or []
    print(f"        曲目 {len(tracks_after)} 首（导入前 {len(tracks_before)}）")
    check("曲目数增加了", len(tracks_after) > len(tracks_before),
          f"{len(tracks_before)} → {len(tracks_after)}")
    names_after = {(t.get("title") or "").strip() for t in tracks_after}
    check("新曲目能查到", len(names_after - names_before) > 0,
          f"新增 {sorted(names_after - names_before)[:3]}")

    print("══ 6. 健康检查 ══")
    try:
        vstart = api("POST", "/api/library/verify", timeout=60)
        vjob = wait_job(vstart.get("job_id"), timeout=300)
        check("健康检查跑完", vjob.get("state") == "done", f"state={vjob.get('state')}")
        result = vjob.get("result") or {}
        for c in result.get("checks") or []:
            mark = {"ok": "OK  ", "fail": "FAIL", "warn": "WARN"}.get(
                c.get("status"), str(c.get("status")))
            print(f"        [{mark}] {c.get('name')}  {str(c.get('summary') or '')[:70]}")

        # ★ 彩排目录**故意不含 Music/**（rehearsal_from_backup 只拷
        #   Device/iTunes/Artwork），所以"文件对应"这一项必然失败。那不是 bug，
        #   是健康检查**正确地**发现了"库里有记录、磁盘没文件"。
        #   真机上这一项必须是 ok——所以只在真机上把它当作失败。
        is_rehearsal = not str(ipod).rstrip("\\/").endswith(":")
        bad = [c for c in (result.get("checks") or [])
               if c.get("status") == "fail"
               and not (is_rehearsal and c.get("name") == "文件对应")]
        if is_rehearsal and any(c.get("name") == "文件对应"
                                and c.get("status") == "fail"
                                for c in (result.get("checks") or [])):
            print("        （彩排不含 Music/，这一项失败是预期的；真机上必须过）")
        check("健康检查无真实失败项", not bad,
              "；".join(f"{c.get('name')}: {c.get('summary')}" for c in bad))
    except urllib.error.HTTPError as exc:
        check("健康检查跑完", False, f"HTTP {exc.code}")

    print("══ 7. 收尾 ══")
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    time.sleep(4)
    try:
        api("GET", "/api/status", timeout=2)
        check("退出后端口已释放", False, f"{args.port} 还在响应")
    except Exception:
        check("退出后端口已释放", True)

    print()
    if FAILURES:
        print(f"❌ 有 {len(FAILURES)} 项没过：{FAILURES}")
        print("\n  ── 应用的输出（最后 40 行）──")
        for line in app_log[-40:]:
            print("   ", line)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
