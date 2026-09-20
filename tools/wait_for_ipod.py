"""等待 iPod 插上：轮询盘符，一旦出现带 iPod_Control 的新盘就退出。

用来在用户插设备时自动唤醒，而不是让用户回来说"好了"。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

POLL_SECONDS = 2
TIMEOUT_SECONDS = 900  # 15 分钟


def drive_letters() -> list[str]:
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return []
    return [
        f"{chr(ord('A') + i)}:"
        for i in range(26)
        if bitmask & (1 << i)
    ]


def ipod_like(letter: str) -> bool:
    """这个盘符看起来是 iPod 吗（有 iPod_Control 目录）。"""
    try:
        return (Path(f"{letter}\\") / "iPod_Control").is_dir()
    except OSError:
        return False


def describe(letter: str) -> str:
    """尽量给出有用的信息。"""
    root = Path(f"{letter}\\")
    lines = [f"检测到 iPod 挂载点：{letter}\\"]

    sysinfo = root / "iPod_Control" / "Device" / "SysInfo"
    if sysinfo.is_file():
        try:
            for raw in sysinfo.read_text(errors="ignore").splitlines():
                line = raw.strip().replace("\x00", "")
                if line.lower().startswith(("modelnumstr", "firewireguid", "pszserialnumber")):
                    lines.append(f"    {line}")
        except OSError:
            pass

    for name in ("iTunesDB", "iTunesCDB"):
        candidate = root / "iPod_Control" / "iTunes" / name
        if candidate.is_file():
            lines.append(f"    数据库: {name}  {candidate.stat().st_size:,} 字节")
            break

    music = root / "iPod_Control" / "Music"
    if music.is_dir():
        dirs = [p for p in music.iterdir() if p.is_dir()]
        files = [p for p in music.rglob("*") if p.is_file()]
        lines.append(f"    音乐目录: {len(dirs)} 个，其中文件 {len(files)} 个")

    return "\n".join(lines)


def main() -> int:
    print(f"开始监听新盘符（每 {POLL_SECONDS}s 一次，最多 {TIMEOUT_SECONDS // 60} 分钟）…")
    known = set(drive_letters())

    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        current = set(drive_letters())
        for letter in sorted(current - known):
            if ipod_like(letter):
                print()
                print("=" * 60)
                print(describe(letter))
                print("=" * 60)
                return 0
            print(f"  新盘符 {letter} 出现，但没有 iPod_Control，继续等…")
        known = current
        time.sleep(POLL_SECONDS)

    print("超时：15 分钟内没有检测到 iPod。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
