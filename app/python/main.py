"""嵌入模式入口——serious_python 在**独立线程**里跑这个文件。

这个文件跟 ``ipod-web`` 命令行是**同一个后端的两个入口**，业务代码一行不改，
所以这里只做三件事：确保包能 import、读宿主传的环境变量、把 ``serve()`` 调起来。

## 三条硬约束

1. **必须阻塞**。宿主把 Python 跑在一条独立线程里，``main()`` 一返回线程就结束、
   Python 就停了。``serve()`` 里的 ``uvicorn.run()`` 正好是阻塞的，别改成
   ``asyncio.create_task`` 之类的"先返回再说"。

2. **不能靠 cwd 推路径**。宿主那句 `Directory.current = <app-support>/data` 实测
   跟 Python 看到的 `os.getcwd()` 对不上（原因在宿主侧，不是我们能修的）。
   所以数据目录一律由 ``IPOD_MANAGER_DATA_DIR`` **显式传进来**——
   押注 cwd 的结果是换种启动方式用户的登录态就"凭空消失"。

3. **不能用 `__file__`**。★ 实测踩过：嵌入环境里**它根本没定义**，
   照着"`__file__` 总是可靠的"写就会在启动第一行 `NameError` 崩掉，
   而且因为崩在入口，界面只表现成"后端一直没起来"。
   定位自身目录改用 ``sys.path`` 反查（见 ``_app_dir()``）。

## 失败时不要退出应用

★ 实测踩过：异常路径上写 `sys.exit(1)`，**整个 Flutter 应用会跟着退掉**
（宿主把嵌入解释器的退出当成了致命错），表现为"点开就闪退"——
比"后端没起来"更糟，因为界面都没了，用户连日志都看不到。

所以失败时只做两件事：把中文原因打出来、然后**停住这条线程**。
宿主会按超时处理并显示错误，界面还在、调试面板还能看到现场。
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

#: 宿主传的端口（可选，默认沿用命令行的 8765）
PORT_ENV = "IPOD_MANAGER_PORT"
#: 宿主传的数据目录（**嵌入模式的必需项**，见模块开头第 2 条）
DATA_DIR_ENV = "IPOD_MANAGER_DATA_DIR"

#: 靠这个包反查自身目录——它是我们自己写的，一定跟着 main.py 一起被打包
_MARKER_PACKAGE = "ipod_web"


def _app_dir() -> Path | None:
    """找到本文件所在的目录（打包后即 ``<exe>/app``）。

    **不用 `__file__`**（嵌入环境里没有它，见模块开头第 3 条）。

    实际顺序是：

    1. **``sys.path`` 里谁的下面有我们的包**——宿主本来就会把 app 目录放进去，
       所以这一条在正常情况下一击即中，而且找出来的就是**真正被 import 的那个**
       目录（比猜路径更准）
    2. ``sys.argv[0]`` 所在目录（宿主一般会把它设成入口文件路径）
    3. ``cwd`` 兜底

    全都落空就返回 ``None``——那时 import 本来也会失败，让真正的报错说话，
    比硬塞一个错路径进 ``sys.path`` 更容易查。
    """
    for entry in list(sys.path):
        if not entry:
            continue
        try:
            candidate = Path(entry)
        except (TypeError, ValueError):
            continue
        if (candidate / _MARKER_PACKAGE).is_dir():
            return candidate.resolve()

    argv0 = (sys.argv[0] if sys.argv else "") or ""
    if argv0:
        parent = Path(argv0).parent
        if parent.is_dir():
            return parent.resolve()

    cwd = Path.cwd()
    if (cwd / _MARKER_PACKAGE).is_dir():
        return cwd.resolve()

    return None


def main() -> int:
    here = _app_dir()
    if here is not None and str(here) not in sys.path:
        # 正常情况宿主已经加过了；这里只是保证"从命令行手动跑这个文件"也能用
        sys.path.insert(0, str(here))

    port = int(os.environ.get(PORT_ENV) or 8765)
    raw_dir = (os.environ.get(DATA_DIR_ENV) or "").strip()
    data_dir = Path(raw_dir).expanduser() if raw_dir else None

    print(f"嵌入模式启动：app 目录 {here or '（没找到，靠 sys.path）'}", flush=True)
    print(f"  端口     {port}", flush=True)
    print(f"  数据目录 {data_dir or '（未指定，退回当前目录）'}", flush=True)

    from ipod_web.app import serve

    serve(port=port, data_dir=data_dir)
    return 0


def _block_forever() -> None:
    """停住这条线程，让宿主自己去超时。

    ★ **不要用 ``sys.exit()``**——实测那会把整个应用带下去，见模块开头。
    这里刻意不返回也不抛，宿主那边的就绪探测会超时并显示错误。
    """
    threading.Event().wait()


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001 - 兜底就是为了把它变成可读的中文
        # 嵌入环境下没人看得到英文堆栈，界面只会表现成"后端一直没起来"。
        # 把话说清楚，调试面板才有得查。
        print("【后端启动失败】", flush=True)
        traceback.print_exc()
        sys.stdout.flush()
        _block_forever()
