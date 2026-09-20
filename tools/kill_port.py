"""杀掉占着某个端口的进程（连同它的子进程）。

为什么需要这个：``uv run ipod-web`` 会起两层——``uv`` 启动器再拉出真正的
uvicorn 进程。**杀启动器不会杀到真正在监听的那个**，于是端口被占着、
日志文件被锁着，下次启动直接失败。

Windows 上 taskkill 的输出是 GBK，用 UTF-8 解会炸，所以这里统一用
``errors="replace"`` 兜住。

用法: python tools/kill_port.py 8765
"""

from __future__ import annotations

import csv
import io
import subprocess
import sys
import time


def _run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True)
    raw = result.stdout or b""
    # Windows 中文系统上这些命令吐 GBK；强行 UTF-8 解会抛异常。
    # 逐个编码试，最后兜底 replace——这里只做关键字匹配，字符错一点无所谓。
    for encoding in ("gbk", "utf-8", "cp936"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def listening_pids(port: int) -> list[int]:
    """谁在 LISTENING 这个端口。"""
    pids: list[int] = []
    for line in _run(["netstat", "-ano"]).splitlines():
        if f":{port}" not in line or "LISTENING" not in line:
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid not in pids:
            pids.append(pid)
    return pids


def process_name(pid: int) -> str:
    out = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
    for row in csv.reader(io.StringIO(out)):
        if row:
            return row[0]
    return "?"


def kill_tree(pid: int) -> str:
    """杀整棵树。只杀单个进程的话，父进程死了子进程还会活着占着端口。"""
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True
    )
    raw = (result.stdout or b"") + (result.stderr or b"")
    for encoding in ("gbk", "utf-8"):
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").strip()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    port = int(sys.argv[1])

    pids = listening_pids(port)
    if not pids:
        print(f"端口 {port} 没人占用")
        return 0

    for pid in pids:
        name = process_name(pid)
        print(f"端口 {port} 被 {name}（PID {pid}）占用，杀整棵树…")
        message = kill_tree(pid)
        print(f"  {message}")

    time.sleep(1.5)
    left = listening_pids(port)
    if left:
        print(f"❌ 端口 {port} 仍被占用：{left}")
        return 1
    print(f"✅ 端口 {port} 已释放")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
