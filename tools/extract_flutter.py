"""解压 Flutter SDK 到 C:\\flutter。

不用 MSYS 的 unzip：那是 6.00 老版本，对长路径和非 ASCII 文件名有已知问题。
Python 的 zipfile 更稳，而且能顺手报告进度——2GB 解压要一两分钟，
没进度的话不知道是不是卡住了。
"""

from __future__ import annotations

import os
import sys
import time
import zipfile
from pathlib import Path

ZIP = Path(Path(os.environ.get("TEMP", ".")) / "flutter_windows.zip")
DEST = Path(r"C:/")


def main() -> int:
    if not ZIP.is_file():
        print(f"❌ 找不到 {ZIP}")
        return 1

    # 目标已存在时先确认——覆盖是破坏性的
    target = DEST / "flutter"
    if target.exists():
        print(f"⚠  {target} 已存在。先删掉它（脚本不自动删，避免误删你手动装的那份）")
        return 2

    started = time.monotonic()
    with zipfile.ZipFile(ZIP) as zf:
        names = zf.namelist()
        total = len(names)
        size = sum(i.file_size for i in zf.infolist())
        print(f"共 {total:,} 个文件，解压后约 {size / 1073741824:.2f} GB")
        print(f"目标：{target}")
        print()

        done = 0
        report_at = 0
        for info in zf.infolist():
            try:
                zf.extract(info, DEST)
            except OSError as exc:
                # 长路径之类的毛病要单独报出来，不能静静跳过——
                # 缺文件的 SDK 后面会以"莫名其妙"的方式失败
                print(f"   ❌ {info.filename}: {exc}")
                return 3
            done += 1
            if done >= report_at:
                pct = done / total * 100
                elapsed = time.monotonic() - started
                print(f"   {pct:5.1f}%  {done:,}/{total:,}  已用 {elapsed:.0f}s")
                report_at = done + 2000

    elapsed = time.monotonic() - started
    print()
    print(f"✅ 解压完成，耗时 {elapsed:.0f} 秒")

    flutter = target / "bin" / "flutter.bat"
    if flutter.is_file():
        print(f"✅ 找到 {flutter}")
    else:
        print(f"❌ 没找到 {flutter} —— 解压结果不对")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
