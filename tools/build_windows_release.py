"""一键构建 Windows 发行版（把 Python 嵌进去）。

## 为什么要脚本

嵌入构建是**三步强耦合**的，任何一步单独跑都会产出坏包：

1. `assemble_python_app.py` —— 把 `src/` 摆成嵌入要的平铺布局
2. `dart run serious_python:main package` —— 用 pip 把依赖装进 site-packages
3. `flutter build windows` —— **原生构建阶段**才把 site-packages 和 app 拷进 bundle

第二步和第三步之间靠**三个环境变量**传递：

    SERIOUS_PYTHON_VERSION          解释器版本
    SERIOUS_PYTHON_SITE_PACKAGES    pip 装到哪
    SERIOUS_PYTHON_APP              处理过的 app 源码在哪

## ★ 两个必须知道的坑

**① 三个环境变量必须在同一个 shell 里设、覆盖第 2 和第 3 步。**
只在 `package` 那步设的后果：产物里是**另一个 Python 版本**的运行时而
site-packages 是本次装的，而且 site-packages / app **根本不会进 bundle**
（它们是第三步由原生构建拷进去的），表现为"构建成功但后端起不来"。

**② 换 `SERIOUS_PYTHON_VERSION` 必须先 `flutter clean`。**
不 clean 的话 `Lib/` 里会混着上一个版本的 `.pyc`，解释器一 import stdlib 就炸：

    ImportError: bad magic number in 'string'

**极隐蔽**——应用能起来、Python 完全没反应。所以下面宁可每次多花几十秒 clean。

**③ 路径一律用 Windows 形式（`C:/...`）。**
MSYS 风格的 `/c/...` 交给 Windows 原生的 pip，会被当成 `C:\\c\\...`，
依赖装到一个不存在的盘符路径下，而构建照样"成功"。

## 用法

    uv run python tools/build_windows_release.py
    uv run python tools/build_windows_release.py --debug     # 出 debug 包
    uv run python tools/build_windows_release.py --no-clean  # 跳过 clean（快，但有风险）
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
PYTHON_APP = APP / "build" / "python_app"
SITE_PACKAGES = APP / "build" / "site-packages"
STAGED_APP = APP / "build" / "python_app_flutter"

#: 跟 `app/python/main.py` 里的 `PORT_ENV` 无关，这是构建期参数
PYTHON_VERSION = "3.13"

FLUTTER_BIN = "C:/flutter/bin"


def _win(path: Path) -> str:
    """转成 Windows 形式的路径字符串。

    ★ 不能把 `Path` 直接交给 Windows 原生程序：在 MSYS 里 `str(Path)` 有时是
    `/c/...`，pip 会当相对路径解析成 `C:\\c\\...`（见模块开头第 ③ 条）。
    """
    return str(path).replace("\\", "/")


def _run(cmd: list[str], *, env: dict[str, str], cwd: Path | None = None,
         allow_failure: bool = False) -> int:
    print(f"  $ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, env=env, cwd=str(cwd or ROOT))
    if result.returncode != 0 and not allow_failure:
        raise SystemExit(f"命令失败（退出码 {result.returncode}）：{' '.join(cmd)}")
    return result.returncode


def _kill_leftover() -> None:
    """杀掉上一次跑起来的应用。

    它们会锁住 `build/` 里的文件，症状是 `flutter clean` 报
    "A program may still be using a file in the directory"——
    然后构建带着上一版的残留继续跑，出来一个说不清哪里不对的包。

    **只杀应用本身，绝不杀 `python.exe`**：本脚本自己就是 python.exe，
    那样等于自杀（而且 uv / 别的 venv 也会被误伤）。嵌入模式下应用就是后端，
    杀它一个就够。
    """
    for image in ("ipod_manager.exe", "sp_simple.exe"):
        subprocess.run(["taskkill", "/IM", image, "/F"], capture_output=True)


def _child_env() -> dict[str, str]:
    """子进程环境：PATH 补上 flutter，三个 SERIOUS_PYTHON_* 一次设齐。

    **一次设齐**是关键（见模块开头第 ① 条）——这个 dict 会同时交给
    `package` 和 `flutter build` 两步。
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # 本机 PYTHONPATH 会遮蔽 venv，摘掉
    sep = os.pathsep
    flutter = _win(Path(FLUTTER_BIN))
    env["PATH"] = f"{flutter}{sep}{env.get('PATH', '')}"

    env["SERIOUS_PYTHON_VERSION"] = PYTHON_VERSION
    env["SERIOUS_PYTHON_SITE_PACKAGES"] = _win(SITE_PACKAGES)
    env["SERIOUS_PYTHON_APP"] = _win(STAGED_APP)

    # 国内网络：pub.dev / Flutter 引擎资源走镜像，否则慢到不可用
    env.setdefault("PUB_HOSTED_URL", "https://pub.flutter-io.cn")
    env.setdefault("FLUTTER_STORAGE_BASE_URL", "https://storage.flutter-io.cn")
    return env


def _flutter(args: list[str], env: dict[str, str],
             allow_failure: bool = False) -> int:
    exe = shutil.which("flutter", path=env["PATH"]) or "flutter"
    return _run([exe, *args], env=env, cwd=APP, allow_failure=allow_failure)


def main() -> int:
    ap = argparse.ArgumentParser(description="构建嵌入了 Python 的 Windows 发行版")
    ap.add_argument("--debug", action="store_true", help="出 debug 包（默认 release）")
    ap.add_argument(
        "--no-clean",
        action="store_true",
        help="跳过 flutter clean。只在确认 Python 版本没变过时用（见模块开头第 ② 条）",
    )
    args = ap.parse_args()

    started = time.time()
    env = _child_env()

    print("══ ① 准备 Flutter 侧 ══")
    # 上一次跑起来的应用会锁住 build/ 里的文件，让 clean 删不干净
    _kill_leftover()

    if not args.no_clean:
        # 见模块开头第 ② 条：换版本不 clean 会留下别的版本的 .pyc
        #
        # ★ clean 必须**排在组装之前**：它会删掉整个 app/build/，
        #   而下面组装的 python_app 和后面要写的 site-packages 都在那儿。
        #   顺序反了的话第一步白干，报错是 "Source directory does not exist."
        _flutter(["clean"], env)

    print("══ ② 组装 Python 源码 ══")
    _run([sys.executable, str(ROOT / "tools" / "assemble_python_app.py")], env=env)

    # ★ pub get 会**因插件软链失败而返回非零**（Windows 没开开发者模式时），
    #   但它该做的事（解析依赖、写 .flutter-plugins-dependencies 和
    #   package_config.json）**在做软链之前就做完了**，所以这里允许它失败。
    #   接着补 junction，再跑一次就干净通过了。
    #   顺序不能反：插件列表是 pub get 生成的，先跑 fix 会看到空列表。
    _flutter(["pub", "get"], env, allow_failure=True)

    print("══ ③ 补插件 junction（没开开发者模式的机器必须跑）══")
    _run(
        [sys.executable, str(ROOT / "tools" / "fix_plugin_symlinks.py"),
         "--project", _win(APP)],
        env=env,
    )
    # 软链齐了，这次应该正常通过（不通过就是真有问题，别再吞掉）
    _flutter(["pub", "get"], env)

    print(f"══ ④ 装依赖进 site-packages（Python {PYTHON_VERSION}）══")
    # `dart` 在 Windows 上是 dart.bat：CreateProcess 不会替我们猜扩展名，
    # 得自己解析（shutil.which 认 PATHEXT）。
    dart = shutil.which("dart", path=env["PATH"]) or "dart"
    _run(
        [dart, "run", "serious_python:main", "package",
         _win(PYTHON_APP), "-p", "Windows", "-r", "-r", "-r",
         _win(PYTHON_APP / "requirements.txt")],
        env=env,
        cwd=APP,
    )

    print("══ ⑤ 构建（原生阶段会把 site-packages 和 app 拷进 bundle）══")
    _flutter(["build", "windows", "--debug" if args.debug else "--release"], env)

    mode = "Debug" if args.debug else "Release"
    out = APP / "build" / "windows" / "x64" / "runner" / mode
    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) if out.is_dir() else 0

    print()
    print(f"✅ 完成，用时 {time.time() - started:.0f} 秒")
    print(f"   产物 {out}")
    print(f"   体积 {total / 1024 / 1024:.1f} MB")
    print()
    print("   自检（Release 默认嵌入模式，不需要 uv）：直接双击 exe，")
    print("   或跑： uv run python tools/verify_embedded_release.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
