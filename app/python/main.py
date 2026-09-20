"""嵌入模式入口——serious_python 在**独立线程**里跑这个文件。

这个文件跟 ``ipod-web`` 命令行是**同一个后端的两个入口**，业务代码一行不改，
所以这里只做三件事：修 ``sys.path``、读宿主传的环境变量、把 ``serve()`` 调起来。

## 三条硬约束

1. **必须阻塞**。宿主把 Python 跑在一条独立线程里，``main()`` 一返回线程就结束、
   Python 就停了。``serve()`` 里的 ``uvicorn.run()`` 正好是阻塞的，别改成
   ``asyncio.create_task`` 之类的"先返回再说"。

2. **不能靠 cwd 推路径**。宿主那句 `Directory.current = <app-support>/data` 实测
   跟 Python 看到的 `os.getcwd()` 对不上（原因在宿主侧，不是我们能修的）。
   所以数据目录一律由 ``IPOD_MANAGER_DATA_DIR`` **显式传进来**——
   押注 cwd 的结果是换种启动方式用户的登录态就"凭空消失"。

3. **修 sys.path 要指向自己所在目录**。打包后 ``ipod_cli`` / ``ipod_web`` /
   ``iopenpod`` 跟本文件平级（由 ``tools/assemble_python_app.py`` 摆好），
   不是源码仓库那个 ``src/`` 布局。用 ``__file__`` 定位，别用 cwd。

## 顺带说一句：日志

宿主的调试面板看的是**这个进程的 stdout/stderr**，所以这里的输出必须保持中文、
可读。``serve()`` 已经把格式化接好了，这里只负责在**异常时**把中文话说清楚——
英文堆栈直接甩到界面上是最没用的一种报错。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

#: 宿主传的端口（可选，默认沿用命令行的 8765）
PORT_ENV = "IPOD_MANAGER_PORT"
#: 宿主传的数据目录（**嵌入模式的必需项**，见模块开头第 2 条）
DATA_DIR_ENV = "IPOD_MANAGER_DATA_DIR"


def _ensure_sys_path() -> Path:
    """把本文件所在目录放进 ``sys.path``，返回它。

    ``__file__`` 在嵌入环境里是可靠的（宿主要读文件才能执行它），
    比 cwd 可靠得多。
    """
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    return here


def main() -> int:
    here = _ensure_sys_path()

    port = int(os.environ.get(PORT_ENV) or 8765)
    raw_dir = (os.environ.get(DATA_DIR_ENV) or "").strip()
    data_dir = Path(raw_dir).expanduser() if raw_dir else None

    print(f"嵌入模式启动：目录 {here}", flush=True)
    print(f"  端口     {port}", flush=True)
    print(f"  数据目录 {data_dir or '（未指定，退回当前目录）'}", flush=True)

    from ipod_web.app import serve

    serve(port=port, data_dir=data_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - 兜底就是为了把它变成可读的中文
        # 这里**不能**只 `raise` 就走：嵌入环境下没人看得到英文堆栈，
        # 界面只会表现成"后端一直没起来"。把话说清楚，日志面板才有得查。
        print("【后端启动失败】", flush=True)
        traceback.print_exc()
        sys.stdout.flush()
        sys.exit(1)
