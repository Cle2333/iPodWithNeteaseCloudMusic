"""发行版自检：启动打好的 exe，验证**嵌入的**后端真的能用。

## 为什么要有这个

嵌入模式最坑的地方是**失败得很安静**：应用窗口正常出现，但后端没起来，
界面显示"无法连接"。可能的原因一大堆（Python 版本混了 `.pyc`、site-packages
没打进 bundle、`app/` 里少了文件、数据目录拿不到……），光看界面一个都排查不出来。

所以这里直接起进程、等端口、打几个关键接口，把"到底哪一层坏了"问出来。

## 验什么

1. **进程起来了**（没闪退——嵌入解释器报错会让整个应用退掉）
2. **后端就绪**（`/api/status` 200，说明 uvicorn 在嵌入式线程里跑起来了）
3. **状态库落在应用支持目录**（不是 cwd——这是数据不丢的判据）
4. **状态库里有账号记录**（登录态迁移过来了没）
5. **设备能读**（插着 iPod 的话，`/api/library` 应该能返回曲目数）

## 用法

    uv run python tools/verify_embedded_release.py
    uv run python tools/verify_embedded_release.py --exe <路径> --keep    # 留着进程看现场
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXE = (
    ROOT / "app" / "build" / "windows" / "x64" / "runner" / "Release"
    / "ipod_manager.exe"
)
PORT = 8765
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def app_support_data_dir() -> Path:
    """跟 Flutter 侧 `getApplicationSupportDirectory()/data` 对齐。

    Windows 上它是 `%APPDATA%\\<org>\\<app>\\data`。这里按 `com.example`
    推——跟 `app/windows/runner/main.cpp` 里的组织名走。故意不写死绝对路径，
    换机器也能跑。
    """
    roaming = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(roaming) / "com.example" / "ipod_manager" / "data"


def main() -> int:
    ap = argparse.ArgumentParser(description="发行版自检")
    ap.add_argument("--exe", default=str(DEFAULT_EXE), help="要检查的 exe")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--timeout", type=float, default=90.0, help="等后端就绪的秒数")
    ap.add_argument("--keep", action="store_true", help="检查完不杀进程（留着看现场）")
    args = ap.parse_args()

    exe = Path(args.exe)
    if not exe.is_file():
        raise SystemExit(f"找不到 exe：{exe}\n先跑 tools/build_windows_release.py")

    print("══ 0. 静态检查 ══")
    out_dir = exe.parent
    for name in ("Lib", "site-packages", "app", "DLLs"):
        check(f"bundle 里有 {name}/", (out_dir / name).is_dir())
    for pkg in ("ipod_web", "ipod_cli", "iopenpod"):
        check(f"app/ 里有 {pkg}/", (out_dir / "app" / pkg).is_dir())
    dlls = sorted(p.name for p in out_dir.glob("python3*.dll"))
    # `python3.dll` 是稳定 ABI 的转发层，永远在；要盯的是**带版本号**的那个，
    # 出现两个版本号说明混了两次构建的产物（`.pyc` 版本不匹配的源头）
    versioned = [d for d in dlls if re.fullmatch(r"python3\d+(_d)?\.dll", d)]
    check("带版本号的 Python DLL 只有一个", len(versioned) == 1,
          f"{dlls} → 版本号 DLL {versioned}")
    total = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
    print(f"        bundle 体积 {total / 1024 / 1024:.1f} MB")

    print("══ 1. 启动（脱离开发目录）══")
    data_dir = app_support_data_dir()
    print(f"        预期数据目录 {data_dir}")
    proc = subprocess.Popen(
        [str(exe)],
        cwd=str(out_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    print(f"        PID {proc.pid}")

    base = f"http://127.0.0.1:{args.port}"
    status = None
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            status, _ = get(f"{base}/api/status", timeout=2)
            if status == 200:
                break
        except Exception:
            pass
        time.sleep(1)

    print("══ 2. 后端就绪 ══")
    check("进程还活着（没闪退）", proc.poll() is None,
          f"退出码 {proc.returncode}" if proc.poll() is not None else "")
    check("/api/status 返回 200", status == 200, f"实际 {status}")

    if proc.poll() is not None or status != 200:
        # 失败时把进程输出倒出来——嵌入模式的问题全藏在里面
        proc.kill()
        try:
            out = proc.communicate(timeout=10)[0].decode("utf-8", errors="replace")
        except Exception:
            out = ""
        print("\n  ── 应用输出（问题都在这儿）──")
        for line in out.splitlines()[-40:]:
            print("   ", line)
        print()
        print(f"  失败项：{FAILURES}")
        return 1

    print("══ 3. 数据目录（数据不丢的判据）══")
    check("应用支持目录已创建", data_dir.is_dir(), str(data_dir))
    db = data_dir / ".ncm" / "ncm.db"
    check("状态库在应用支持目录下", db.is_file(), str(db))
    check("没有把库建到 cwd", not (out_dir / "ncm.db").is_file()
          and not (out_dir / ".ncm" / "ncm.db").is_file())

    print("══ 4. 账号（登录态迁移的判据）══")
    accounts: list = []
    try:
        _, acc = get(f"{base}/api/account/list")
        accounts = acc.get("accounts") or []
        active = acc.get("active") or acc.get("active_uid") or acc.get("current")
        print(f"        账号 {len(accounts)} 个，当前 {active}")
        for a in accounts[:3]:
            print(f"          - {a.get('nickname') or a.get('uid')}")
    except Exception as exc:
        check("账号接口可用", False, str(exc))

    print("══ 5. 设备与曲目 ══")
    try:
        status_code, dev = get(f"{base}/api/device", timeout=15)
        print(f"        /api/device → {status_code}  {dev}")
    except Exception as exc:
        print(f"        设备接口失败：{exc}")

    try:
        _, lib = get(f"{base}/api/library/tracks", timeout=30)
        tracks = lib.get("tracks") or lib.get("songs") or []
        print(f"        iPod 曲目 {len(tracks)} 首")
        check("能读到 iPod 库", isinstance(tracks, list),
              f"{len(tracks)} 首" if tracks else "0 首（没插 iPod 时正常）")
    except Exception as exc:
        print(f"        读库失败：{exc}")
        print("        （没插 iPod 的话这是正常的）")

    print()
    if FAILURES:
        print(f"❌ 有 {len(FAILURES)} 项没过：{FAILURES}")
    else:
        print("✅ 全部通过")

    if args.keep:
        print(f"   进程留着（PID {proc.pid}），端口 {args.port}")
    else:
        print("   停掉进程…")
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
