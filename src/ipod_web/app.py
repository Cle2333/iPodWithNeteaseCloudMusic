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

from ipod_web import paths
from ipod_web.context import WebContext
from ipod_web.routes import (
    account,
    debug,
    ipod_playlists,
    jobs,
    library,
    playlists,
    settings,
    status,
)

#: 只允许监听回环地址——见模块开头的说明
HOST = "127.0.0.1"
DEFAULT_PORT = 8765

VERSION = "0.3.0"

log = logging.getLogger("ipod_web")


def create_app(ctx: WebContext | None = None) -> FastAPI:
    """建应用。``ctx`` 可注入，方便测试。"""
    context = ctx or WebContext()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # 退出时把作业队列收干净（正在跑的作业会被请求取消）
        context.shutdown()
        # 以及我们自己拉起来的网易云 API 子进程。
        # 看门狗（node 侧）是主防线——父进程没了它 2 秒内自退；这里是正常退出时的
        # 优雅路径，让端口立刻释放，下一次启动不用等。
        from ipod_cli.ncm import server as ncm_server

        ncm_server.stop()

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
    # iPod 上的歌单管理（跟 playlists.router 是两回事：那个是网易云在线歌单）
    app.include_router(ipod_playlists.router)
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
    parser.add_argument("--data-dir", default=None, metavar="目录",
                        help="运行时数据目录（状态库/缓存/日志都在它下面）。"
                             f"命令行不传时读环境变量 {paths.DATA_DIR_ENV}；"
                             "两者都没有就按老规矩用相对当前目录的 .ncm/")
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


def build_context(
    *,
    db: str | None = None,
    base_url: str | None = None,
    ipod: str | None = None,
    cache_dir: str | None = None,
    data_dir: Path | None = None,
) -> WebContext:
    """建运行时上下文。

    参数**显式列出**，不接 argparse 的 Namespace：命令行和嵌入模式（宿主直接
    调用）都要用这一个函数，接 Namespace 会逼着嵌入那侧伪造一个假对象。

    ``data_dir`` 是**嵌入模式**用的：宿主（Flutter）解析好应用支持目录传进来，
    状态库/缓存就从它推导。为 ``None`` 时行为跟以前一字不差（相对 cwd 的
    ``.ncm/``），所以命令行用法不受影响。推导与迁移的细节见 ``paths.py``。

    **别在这里传 ``jobs=``**：作业日程的落盘位置是跟着状态库走的
    （``<库目录>/logs/jobs.jsonl``），交给 ``WebContext`` 自己接线。
    传一个裸的 ``JobManager()`` 进来，日程就变成只存在内存里——
    重启即清空，而"导出排查"恰恰是重启之后才想起要做的事。
    实测踩过：单元测试全绿，真实入口却颗粒无收。
    """
    from ipod_cli.ncm.client import DEFAULT_BASE_URL
    from ipod_cli.ncm.state import StateStore

    if data_dir is not None:
        want_db, want_cache = paths.db_and_cache(data_dir)
        # 用户以前在哪跑过命令，`.ncm/` 就在哪。找不到就重新登录，不阻塞启动。
        paths.migrate_if_needed(want_db, paths.migration_candidates(data_dir))
        db = db or str(want_db)
        cache_dir = cache_dir or str(want_cache)

    return WebContext(
        store=StateStore(db) if db else StateStore(),
        base_url=base_url or DEFAULT_BASE_URL,
        ipod_path=ipod,
        cache_dir=cache_dir,
    )


def serve(
    *,
    port: int = DEFAULT_PORT,
    data_dir: Path | None = None,
    ipod: str | None = None,
    base_url: str | None = None,
    cache_dir: str | None = None,
    db: str | None = None,
    log_file: str | None = None,
    verbose: bool = False,
) -> int:
    """起服务（**阻塞**，直到进程被停）。

    命令行 ``ipod-web`` 和**嵌入模式**共用这一份实现——宿主（Flutter）把
    应用支持目录直接传成 ``data_dir``，这个函数在独立线程里跑，靠 uvicorn 的
    阻塞撑住那条线程。两份实现一定会漂移，所以只留一份。

    ``serve`` 只收**最终值**，自己不解析命令行：参数来源（argv / 环境变量 /
    宿主传参）由各自的入口负责归一，这里不再猜。
    """
    _setup_logging(verbose, log_file)
    ctx = build_context(
        db=db,
        base_url=base_url,
        ipod=ipod,
        cache_dir=cache_dir,
        data_dir=data_dir,
    )

    app = create_app(ctx)
    print(f"iPod 音乐管理器后端已启动：http://{HOST}:{port}", flush=True)
    print(f"  状态库   {ctx.store.path}", flush=True)
    print(f"  网易云   {ctx.base_url}", flush=True)
    print(f"  设备     {ipod or '自动检测'}", flush=True)

    # 自带网易云 API 时**后台**把它拉起来。
    #
    # 后台的理由：它是「从网易云下载」的前提，不是应用可用的前提。串在这里等
    # 会让启动慢 2–5 秒，而界面本来就能先把"设备/音乐库"这些不依赖它的页面画出来。
    # 拉起失败也不报错——界面会显示「网易云服务不可用」，那才是正确的可见状态。
    from ipod_cli.ncm import server as ncm_server

    ncm_server.ensure_async(ctx.base_url, None)

    import uvicorn

    uvicorn.run(
        app,
        host=HOST,          # ← 写死回环，不给配置项
        port=port,
        log_level="warning",   # uvicorn 自己的访问日志太吵，关到 warning
        access_log=False,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    args = build_parser().parse_args(argv)

    # 数据目录三级优先：--data-dir 参数 > 环境变量 > 不用（相对 cwd 的老行为）
    data_dir = Path(args.data_dir) if args.data_dir else paths.data_dir_from_env()

    return serve(
        port=args.port,
        data_dir=data_dir,
        ipod=args.ipod,
        base_url=args.base_url,
        cache_dir=args.cache_dir,
        db=args.db,
        log_file=args.log_file,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())
