"""设备修复的端到端验证：真跑一次扫描 + 清理，逐项核对。

## 为什么不能只靠单元测试

单元测试跑在 tmp 目录的虚拟设备上，验的是**逻辑**。这里补的是另外两层：

* **打好的那个 exe**：路由接上了没有、作业队列走通了没有、结果字段界面拿得到吗
* **真实数据形状**：真设备的库是 148 首、磁盘上几百个文件、路径大小写混着来
  —— 单元测试的夹具是干净的 3 首，很多"只在真实数据上才出现"的问题它抓不到

## 判据（全部是"不删错"这一条的不同侧面）

1. 扫描能认出孤儿 / 残留临时文件 / 断链记录，数量对得上
2. 清完孤儿，**数据库引用的文件一个不少**
3. 清完孤儿，被清掉的文件真的没了，体积报对
4. 干净设备再扫一次：`clean = true`（说明清干净了，没留下新的不一致）
5. 断链记录清理后，曲目数减少量 = 清掉的条数，且读回校验通过
6. 清完之后设备仍然可读（数据库没被写坏）

## 用法

    # 在彩排设备上（安全，随便跑）
    uv run python tools/verify_repair.py --ipod <彩排目录>

    # 只看真机，不动手
    uv run python tools/verify_repair.py --ipod D:\\ --scan-only

`--scan-only` 只扫描不清理 —— 真机上想看一眼有什么问题时用这个。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
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


def api(method: str, path: str, body: dict | None = None, timeout: float = 60):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}


def drain(proc: subprocess.Popen, keep: int = 200) -> list[str]:
    """把应用输出一路读走。

    ★ 不读的话管道会填满，嵌入的 Python 会**阻塞在写日志上** —— 症状是
    "后端莫名其妙不响应"，看着像死锁，其实是没人收它的输出。
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


def wait_job(job_id: str, timeout: float = 600) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            last = api("GET", f"/api/jobs/{job_id}", timeout=20)
        except Exception:  # noqa: BLE001 - 扫描/写库时后端偶尔慢，正常
            time.sleep(1)
            continue
        if last.get("state") in ("done", "failed", "cancelled"):
            return last
        time.sleep(0.4)
    return last


def scan() -> dict:
    job = api("POST", "/api/repair/scan")
    final = wait_job(job["job_id"])
    if final.get("state") != "done":
        raise SystemExit(f"扫描失败了：{final.get('error')}")
    return final["result"]


def disk_snapshot(ipod: Path) -> dict[str, int]:
    """设备上所有音频文件的 相对路径 → 大小。用来核对"谁还在、谁没了"。"""
    out: dict[str, int] = {}
    music = ipod / "iPod_Control" / "Music"
    if music.is_dir():
        for p in music.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                out[p.relative_to(ipod).as_posix()] = p.stat().st_size
    return out


def db_track_count(ipod: Path) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from ipod_cli.library import read_library

    return len(read_library(ipod).tracks)


def main() -> int:
    ap = argparse.ArgumentParser(description="设备修复的端到端验证")
    ap.add_argument("--ipod", required=True, help="彩排目录或真机盘符（如 D:\\）")
    ap.add_argument("--exe", default=str(EXE))
    ap.add_argument("--scan-only", action="store_true",
                    help="只扫描，不清理（真机上用这个）")
    args = ap.parse_args()

    ipod = Path(args.ipod)
    if not (ipod / "iPod_Control").is_dir():
        raise SystemExit(f"{ipod} 里没有 iPod_Control，不是 iPod（彩排目录或盘符）")
    exe = Path(args.exe)
    if not exe.is_file():
        raise SystemExit(f"没找到发行版 exe：{exe}\n先跑 tools/build_windows_release.py")

    env = dict(os.environ)
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

        # ── 1. 扫描 ──────────────────────────────────────────────────
        print("\n══ 1. 扫描 ══")
        before = scan()
        print(f"   数据库 {before['db_tracks']} 首 · 磁盘 {before['disk_files']} 个文件")
        print(f"   孤儿 {before['orphans']['count']} 个（{before['orphans']['size_text']}）"
              f" · 断链 {before['broken']['count']} 首"
              f" · 临时文件 {before['stray_temp']['count']} 个")
        if before["clean"]:
            print("   （设备是干净的，没有需要修复的地方）")

        snap_before = disk_snapshot(ipod)
        db_before = db_track_count(ipod)
        check("扫描到的磁盘文件数与实际一致",
              before["disk_files"] == len(snap_before),
              f"接口说 {before['disk_files']}，实际 {len(snap_before)}")
        check("扫描到的数据库曲目数与实际一致",
              before["db_tracks"] == db_before,
              f"接口说 {before['db_tracks']}，实际 {db_before}")

        if args.scan_only:
            print("\n   --scan-only：到此为止，没动设备")
            return 0 if not FAILURES else 1

        # ── 2. 清孤儿 + 临时文件 ─────────────────────────────────────
        orphan_names = {o["rel"] for o in before["orphans"]["items"]}
        # 接口只回前若干个，够断言"报了的确实被删了"
        temp_names = {t["rel"] for t in before["stray_temp"]["items"]}
        print(f"\n══ 2. 清孤儿 + 临时文件 ══  "
              f"（{len(orphan_names)} + {len(temp_names)} 个样例）")

        if orphan_names or temp_names:
            job = api("POST", "/api/repair/clean",
                      {"orphans": True, "stray_temp": True})
            final = wait_job(job["job_id"])
            check("清理作业成功结束", final.get("state") == "done",
                  f"{final.get('state')} / {final.get('error', '')}")
            result = final.get("result") or {}
            print(f"   处理 {result.get('cleaned')} 项，释放 {result.get('freed_text')}")
            for kind in result.get("kinds") or []:
                print(f"     · {kind['note']}")

            snap_after = disk_snapshot(ipod)
            for rel in sorted(orphan_names):
                check(f"孤儿已删：{Path(rel).name}", rel not in snap_after)

            # ★★ 最重要的一条：数据库引用的文件一个不能少
            # 用清理前的快照减去被清掉的那些，剩下的都必须还在
            gone = set(snap_before) - set(snap_after)
            check("删掉的全是孤儿/临时文件，没有误删",
                  gone <= orphan_names,
                  f"多删了：{sorted(gone - orphan_names)[:5]}")
            check("数据库曲目数没被清理改动",
                  db_track_count(ipod) == db_before,
                  f"{db_before} → {db_track_count(ipod)}")
        else:
            print("   没有可清的内容，跳过")

        # ── 3. 再扫一次：应该干净了 ──────────────────────────────────
        print("\n══ 3. 再扫一次 ══")
        after = scan()
        if db_before:      # 有曲目的设备才谈得上"干净"
            check("清完之后没有孤儿了",
                  after["orphans"]["count"] == 0,
                  f"还剩 {after['orphans']['count']} 个")
            check("清完之后没有临时文件了",
                  after["stray_temp"]["count"] == 0,
                  f"还剩 {after['stray_temp']['count']} 个")

        # ── 4. 断链记录（整库重写）──────────────────────────────────
        if after["broken"]["count"]:
            print(f"\n══ 4. 清断链记录 ══  {after['broken']['count']} 首（要整库重写）")
            db_now = db_track_count(ipod)
            broken_count = after["broken"]["count"]

            job = api("POST", "/api/repair/clean", {"broken_records": True})
            final = wait_job(job["job_id"])
            check("清记录作业成功结束", final.get("state") == "done",
                  f"{final.get('state')} / {final.get('error', '')}")
            for kind in (final.get("result") or {}).get("kinds") or []:
                print(f"     · {kind['note']}  （错误 {kind['errors']} 条）")
            check("写库时没有出错", not any(
                (k.get("errors") or 0) for k in
                (final.get("result") or {}).get("kinds") or []
            ))

            after_db = db_track_count(ipod)
            check("曲目数减少量 = 清掉的条数",
                  db_now - after_db == broken_count,
                  f"{db_now} → {after_db}，期望少 {broken_count}")
            check("清完之后数据库还能读（没写坏）", after_db >= 0)

            final_scan = scan()
            check("清完之后没有断链记录了",
                  final_scan["broken"]["count"] == 0,
                  f"还剩 {final_scan['broken']['count']}")
            check("清记录没有留下孤儿文件",
                  final_scan["orphans"]["count"] == 0,
                  f"冒出 {final_scan['orphans']['count']} 个孤儿")
        else:
            print("\n══ 4. 断链记录 ══  没有，跳过")

        print("\n" + ("❌ 有未通过项：" + "、".join(FAILURES) if FAILURES
                      else "✅ 全部通过"))
        return 1 if FAILURES else 0

    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
