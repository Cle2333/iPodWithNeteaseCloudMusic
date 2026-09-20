"""FastAPI 应用与启动入口。

这是给 Flutter 桌面端用的**本地**后端。两条硬规矩：

1. **只监听 127.0.0.1**。这个后端能改 iPod 数据库、能删歌，
   一旦暴露到局域网就是灾难。绑地址的地方写死，不给配置项。
2. 路由层**不碰** ``ncm/``。所有碰外部世界的活都走 ``JobManager`` 队列，
   见 ``jobs.py`` 里关于「绝不并发」的说明。
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ipod_web.context import WebContext
from ipod_web.routes import (
    account,
    debug,
    jobs,
    library,
    playlists,
    settings,
    status,
)

#: 只允许监听回环地址——见模块开头的说明
HOST = "127.0.0.1"
DEFAULT_PORT = 8765

VERSION = "0.1.0"

log = logging.getLogger("ipod_web")


def create_app(ctx: WebContext | None = None) -> FastAPI:
    """建应用。``ctx`` 可注入，方便测试。"""
    context = ctx or WebContext()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # 退出时把作业队列收干净（正在跑的作业会被请求取消）
        context.shutdown()

    app = FastAPI(
        title="iPod 音乐管理器",
        description="本机自用的 iPod Classic 管理后端",
        version=VERSION,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.ctx = context

    # 日志缓冲挂在 root logger 上：调试面板要看到的是"整个进程在说什么"，
    # 不只是 ipod_web 自己的话。uvicorn 的启动/错误日志也会一并进去。
    #
    # 判重是必要的：`create_app` 在同一进程里可能被调多次（测试就是），
    # 每调一次挂一个 handler，日志会被写进 N 个缓冲、越跑越慢。
    root = logging.getLogger()
    if context.log_buffer not in root.handlers:
        root.addHandler(context.log_buffer)
    log.info("后端启动：%s", VERSION)

    app.include_router(status.router)
    app.include_router(account.router)
    app.include_router(settings.router)
    app.include_router(playlists.router)
    app.include_router(library.router)
    app.include_router(jobs.router)
    app.include_router(debug.router)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """兜底：**别把英文堆栈甩给界面**。

        堆栈进日志（设置页的调试面板能看到），界面只拿一句中文。
        """
        log.exception("未处理的异常：%s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": f"服务内部出错：{type(exc).__name__}: {exc}"},
        )

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ipod-web",
        description="iPod 音乐管理器的本地后端（给 Flutter 桌面端用）",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--ipod", default=None, metavar="路径",
                        help="iPod 盘符/挂载点（默认自动检测）")
    parser.add_argument("--base-url", default=None, metavar="地址",
                        help="网易云 API 服务地址（默认 localhost:4000）")
    parser.add_argument("--cache-dir", default=None, metavar="目录",
                        help="下载缓存目录")
    parser.add_argument("--db", default=None, metavar="文件",
                        help="状态库路径（默认 .ncm/ncm.db）")
    parser.add_argument("--log-file", default=None, metavar="文件",
                        help="把日志同时写到这里（调试面板显示的是同一份）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="输出调试日志")
    return parser


def _setup_logging(verbose: bool, log_file: str | None) -> None:
    """日志同时输出到控制台和文件。

    Flutter 那边会把子进程的 stdout/stderr 收进调试面板，所以控制台这路
    必须保持中文、可读。文件那路给事后排查用。
    """
    fmt = "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s"
    datefmt = "%H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=fmt,
        datefmt=datefmt,
        handlers=handlers,
        force=True,
    )

    # **降噪**：vendored 内核的解析器每一步都打 INFO，一次读库能刷出几十行
    # "Play Counts: header=96, entry_len=28..."。这些对排查问题几乎没用，
    # 却会把真正有用的信息淹掉——调试面板是给人看的，不是给解析器当账本的。
    # 想看得手动开 --verbose。
    for noisy in (
        "iopenpod.itunesdb_parser",
        "iopenpod.itunesdb_writer",
        "iopenpod.artworkdb_writer",
    ):
        logging.getLogger(noisy).setLevel(
            logging.DEBUG if verbose else logging.WARNING
        )

    # uvicorn 自己的启动日志走我们这套格式，风格统一
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True


def build_context(args) -> WebContext:
    """从命令行参数建运行时上下文。

    **别在这里传 ``jobs=``**：作业日程的落盘位置是跟着状态库走的
    （``<库目录>/logs/jobs.jsonl``），交给 ``WebContext`` 自己接线。
    传一个裸的 ``JobManager()`` 进来，日程就变成只存在内存里——
    重启即清空，而"导出排查"恰恰是重启之后才想起要做的事。
    实测踩过：单元测试全绿，真实入口却颗粒无收。
    """
    from ipod_cli.ncm.client import DEFAULT_BASE_URL
    from ipod_cli.ncm.state import StateStore

    return WebContext(
        store=StateStore(args.db) if args.db else StateStore(),
        base_url=args.base_url or DEFAULT_BASE_URL,
        ipod_path=args.ipod,
        cache_dir=args.cache_dir,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, args.log_file)

    ctx = build_context(args)

    app = create_app(ctx)
    address = f"http://{HOST}:{args.port}"
    print(f"iPod 音乐管理器后端已启动：{address}")
    print(f"  状态库   {ctx.store.path}")
    print(f"  网易云   {ctx.base_url}")
    print(f"  设备     {args.ipod or '自动检测'}")
    print()
    print("按 Ctrl+C 停止。")

    import uvicorn

    uvicorn.run(
        app,
        host=HOST,          # ← 写死回环，不给配置项
        port=args.port,
        log_level="warning",   # uvicorn 自己的访问日志太吵，关到 warning
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
