"""打 Windows 发行包（嵌入 Python 的版本）。

## 跟上一版的结构差别

嵌入之后**不需要**再随包分发后端源码、`pyproject.toml`、`uv.lock` 了——
Python 运行时 + 依赖 + 源码全都进了 `ipod_manager.exe` 那个目录：

    iPodWithNeteaseCloudMusic-v0.2.0-windows-x64/
    ├── 启动.bat               ← 双击这个（不再检查 uv）
    ├── 使用说明.txt
    ├── ipod_manager.exe
    ├── Lib/ site-packages/ app/ DLLs/     ← 嵌入的 Python（构建产物，原样带）
    ├── python313.dll dart_bridge.dll ...
    ├── data/                              ← Flutter 资源
    └── LICENSE / THIRD_PARTY_NOTICES.md

也就是说：**发行包 = 构建产物 + 几个说明文件**，中间不再有"组装源码"这一步。
顺带把两个老坑一起消灭了：`pyproject.toml` 必须与 exe 同级、
首启要联网装 111 MB 依赖。

## 编码约定（两个都是踩出来的）

* `.bat` 用 **GBK + CRLF**：UTF-8 的中文 .bat 在 cmd 里乱码；LF 行尾会让
  cmd 把一行拆成几条命令执行
* `.txt` 用 **UTF-8 BOM + CRLF**：否则老版记事本打开是乱码

## 用法

    uv run python tools/package_release.py
    uv run python tools/package_release.py --version 0.2.0 --skip-build
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
RELEASE_BUILD = APP / "build" / "windows" / "x64" / "runner" / "Release"
OUT_ROOT = ROOT / "release"
NAME = "iPodWithNeteaseCloudMusic"

#: 构建产物里必须有的东西。缺一个都说明包是坏的，宁可在这里失败
REQUIRED = ("ipod_manager.exe", "Lib", "site-packages", "app", "DLLs", "data")


def launcher_bat() -> str:
    """启动器。

    内容故意保持很短：**所有运行环境（Python、node、网易云 API）都在包里**，
    所以没有"检查依赖"这一步可做——检查不存在的依赖比不检查更糟，用户会以为
    装漏了。剩下的只有"用 start 脱离本窗口启动"这一件事。
    """
    return """@echo off
chcp 936 >nul
title iPod 音乐管理器
cd /d "%~dp0"

rem 用 start 脱离本窗口，这样关掉黑窗口不会把应用一起关掉
start "" "ipod_manager.exe"
exit /b 0
"""


def readme_txt(version: str) -> str:
    return f"""iPod 音乐管理器 v{version}
============================================================

这是什么
------------------------------------------------------------
把网易云音乐的歌单 / 我喜欢的歌，下载到电脑再同步进 iPod Classic 的小工具。
也支持：把本地音乐文件导进 iPod、管理 iPod 上已有的歌、备份、删除、校验。

============================================================
怎么用
============================================================
1. 双击「启动.bat」（或直接双击 ipod_manager.exe）
2. 第一次打开请到「设置」页登录网易云账号（扫码登录）
3. 插上 iPod，等它出现在「此电脑」里
4. 到「歌单」页挑歌 → 下载 → 同步到 iPod

程序自带运行环境，**不需要**你再装 Python、node 或 uv，
也不需要联网初始化。

============================================================
数据存在哪
============================================================
登录状态、下载记录、作业日程、歌曲缓存都在：

    %APPDATA%\\com.example\\ipod_manager\\data\\.ncm\\

备份这个 data 目录就等于备份全部状态。
（iPod 上的歌和这个无关，那是直接写进 iPod 的。）

出问题时的日志也在这里：

    %APPDATA%\\com.example\\ipod_manager\\data\\.ncm\\logs\\backend-<日期>.log

程序内置的网易云服务（藏在 node_api\\ 目录里）会在启动后自动跑起来，
你不需要管它；程序退出时它会跟着退出。

从旧版本升级
------------------------------------------------------------
旧版本（v0.1.0）把数据放在你运行命令的目录下的 .ncm\\ 里。
新版不会自动去找那个位置——如果登录状态没了，把这个目录拷到：

    %APPDATA%\\com.example\\ipod_manager\\data\\.ncm\\

再重新打开程序即可。

============================================================
注意
------------------------------------------------------------
* 请先备份 iPod 里的数据再大批量操作（程序里有「备份」功能）
* 本工具仅用于个人已合法获取的音乐，不要用于分发受版权保护的内容
* 同步过程中不要拔线

许可
------------------------------------------------------------
MIT。详见 LICENSE 与 THIRD_PARTY_NOTICES.md（含 iOpenPod 的 MIT 许可、
api-enhanced 与 node 的许可）。
"""


def _write(path: Path, text: str, encoding: str) -> None:
    """按 CRLF 写文件。

    ★ 不能直接 `write_text`：Windows 上默认会把 `\\n` 转成 `\\r\\n`，
    而这里必须**显式**控制——`.bat` 要 GBK+CRLF，`.txt` 要 UTF-8-BOM+CRLF，
    两边都要 CRLF 但编码不同，交给默认行为只会得到一个对一半的文件。
    """
    data = text.replace("\r\n", "\n").replace("\n", "\r\n")
    path.write_bytes(data.encode(encoding))


def build(env: dict[str, str]) -> None:
    print("══ 先跑一次完整构建 ══")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "build_windows_release.py")],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        raise SystemExit("构建失败，先修构建")


def main() -> int:
    ap = argparse.ArgumentParser(description="打 Windows 发行包")
    ap.add_argument("--version", default="0.2.0", help="版本号（进文件名和说明）")
    ap.add_argument("--skip-build", action="store_true", help="用现成的构建产物")
    args = ap.parse_args()

    if not args.skip_build:
        build(dict())

    if not RELEASE_BUILD.is_dir():
        raise SystemExit(f"找不到构建产物：{RELEASE_BUILD}\n先跑 tools/build_windows_release.py")

    missing = [n for n in REQUIRED if not (RELEASE_BUILD / n).exists()]
    if missing:
        raise SystemExit(f"构建产物缺东西：{missing}——包一定是坏的，先修构建")

    folder = f"{NAME}-v{args.version}-windows-x64"
    dest = OUT_ROOT / folder
    print(f"══ 组装 {dest} ══")
    if dest.exists():
        shutil.rmtree(dest)

    # 构建产物整体带过去（它**就是**应用，不再有"挑文件"这一步）
    shutil.copytree(RELEASE_BUILD, dest)

    for src, name in (
        (ROOT / "LICENSE", "LICENSE"),
        (ROOT / "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"),
    ):
        if src.is_file():
            shutil.copy2(src, dest / name)
        else:
            print(f"  ⚠ 缺 {src.name}（MIT 要求随分发附带）")

    _write(dest / "启动.bat", launcher_bat(), "gbk")
    _write(dest / "使用说明.txt", readme_txt(args.version), "utf-8-sig")
    print("  已写 启动.bat（GBK+CRLF）/ 使用说明.txt（UTF-8 BOM）")

    # ── 打 zip ──
    zip_path = OUT_ROOT / f"{folder}.zip"
    print(f"══ 打 zip {zip_path.name} ══")
    files = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for item in sorted(dest.rglob("*")):
            if item.is_file():
                zf.write(item, item.relative_to(OUT_ROOT))
                files += 1

    print()
    print("✅ 完成")
    print(f"   目录 {dest}")
    print(f"   zip  {zip_path}  {zip_path.stat().st_size / 1024 / 1024:.1f} MB  {files} 个文件")
    print()
    print("   下一步（**别跳过**）：")
    print("     1. 解压 zip 到**别的路径**，双击 启动.bat 试一次")
    print("     2. uv run python tools/verify_embedded_release.py --exe <解压路径>/ipod_manager.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
