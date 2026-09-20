"""把 Python 源码摆成嵌入模式要的目录结构。

## 干什么

serious_python 打包的是**一个目录**，打包后它整体成为 ``<exe>/app/``，
解释器把那个目录放进 ``sys.path``。它期望的布局是**平铺**的：

    app/build/python_app/
      main.py          ← 入口（app/python/main.py 原样拷来）
      requirements.txt ← 从 pyproject.toml 派生
      ipod_cli/        ← 从 src/ 拷来
      ipod_web/
      iopenpod/

而仓库里是 ``src/`` 布局（``src/ipod_cli`` 等）。这个脚本就是那次搬运。

## 为什么不直接把源码放成平铺

因为仓库的其他部分——``pytest``（``pythonpath = ["src"]``）、hatch 打包
（``packages = ["src/..."]``）、``uv run``——全都依赖 ``src/`` 布局。
为了嵌入去改仓库结构，是让构建去支配开发。搬一份到构建产物里更省事，
代价只是"多一次拷贝"，而且**产物在 build/ 下、已被 gitignore**，不会漂移。

## 为什么不手写 requirements.txt

手写就会和 ``pyproject.toml`` 的 ``dependencies`` 漂移：改了 pyproject 忘了改它，
表现为"开发时好好的，打包出来少个包"。这里直接读 pyproject，**只有一份真相**。
"""

from __future__ import annotations

import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
ENTRY = ROOT / "app" / "python" / "main.py"
OUT = ROOT / "app" / "build" / "python_app"

#: 要从 src/ 搬进去的包
PACKAGES = ("iopenpod", "ipod_cli", "ipod_web")

#: 不搬的东西：字节码缓存和测试（测试要 pytest，嵌入环境里没有也不需要）
SKIP_DIRS = {"__pycache__", ".pytest_cache", "tests"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def read_requirements() -> list[str]:
    """从 pyproject 读运行时依赖——**不手写**，免得两处漂移。"""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return list(data["project"]["dependencies"])


def copy_package(name: str) -> tuple[int, int]:
    """拷一个包，返回 (文件数, 字节数)。"""
    src = SRC / name
    if not src.is_dir():
        raise SystemExit(f"找不到包：{src}")
    dst = OUT / name

    files = 0
    size = 0
    for item in src.rglob("*"):
        if any(part in SKIP_DIRS for part in item.relative_to(src).parts):
            continue
        if item.is_dir():
            continue
        if item.suffix in SKIP_SUFFIXES:
            continue

        target = dst / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        files += 1
        size += item.stat().st_size
    return files, size


def main() -> int:
    if not ENTRY.is_file():
        raise SystemExit(f"找不到入口文件：{ENTRY}")

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    shutil.copy2(ENTRY, OUT / "main.py")

    requirements = read_requirements()
    (OUT / "requirements.txt").write_text(
        "\n".join(requirements) + "\n", encoding="utf-8"
    )

    total_files = 1
    total_size = (OUT / "main.py").stat().st_size

    print(f"  组装到 {OUT}")
    print("  ── 依赖（来自 pyproject.toml）")
    for req in requirements:
        print(f"     {req}")

    print("  ── 包")
    for name in PACKAGES:
        files, size = copy_package(name)
        total_files += files
        total_size += size
        print(f"     {name:12} {files:4} 个文件  {size / 1024:8.1f} KB")

    print(f"  ── 合计 {total_files} 个文件  {total_size / 1024:.1f} KB")
    print(f"  下一步：dart run serious_python:main package {OUT} ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
