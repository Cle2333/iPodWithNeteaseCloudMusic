"""管好本机那个网易云 API 服务（api-enhanced）。

## 它是什么、为什么要在这里管

「从网易云下载」的整条链路都打在这个本地 API 上（默认 `localhost:4000`）。
发行版**自带** node 运行时和 api-enhanced，用户不需要装 node、也不需要部署那个服务。
这个模块就是"把它拉起来、并在该收的时候收干净"。

## ★ 孤儿进程：这是打包 node 必须一起解决的问题

node 是**子进程**。上一版（后端起在独立进程里）就吃过这个亏：应用被强杀之后
node 还活着，占着 4000 端口；下次启动撞端口，报错长得像"代码坏了"。

两道防线，缺一不可：

1. **看门狗**（`app/node/launcher.js`）：node 每 2 秒问一次父进程还在不在，
   不在就自己退出。孤儿**根本活不到**下一次启动。
2. **进来先探测**：端口已经通了就复用，不再拉一个（"先探测"是上一版留下的经验）。

第 1 条是主防线：它让孤儿存在的时间从"直到下次启动"缩到"最多 2 秒"。

## 什么时候拉起来

`ensure_async()` 在服务启动时**后台**拉，不阻塞界面——它是网易云功能的前提，
不是应用可用的前提。没装/没带这个组件时也不要报错，界面那边会显示
「网易云服务不可用」，那才是正确的用户可见状态。
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("ipod_web")

#: 宿主（Flutter）传的 bundle 目录，即 exe 所在目录——嵌入模式下不能用
#: `sys.executable` 推：那是**打包机**的路径（见 `app/python/main.py` 的说明）
BUNDLE_DIR_ENV = "IPOD_MANAGER_BUNDLE_DIR"
#: 手动指定（开发时用）：node 可执行文件 与 api 目录
NODE_ENV = "IPOD_MANAGER_NODE"
NODE_API_ENV = "IPOD_MANAGER_NODE_API"
#: 设成 1 就完全不自动拉起（测试用——绝不能在测试里真起 node）
DISABLE_ENV = "IPOD_MANAGER_NO_NODE_API"

#: 就绪等待上限。node 冷启动要读一堆 JS，给足但别无限等
READY_TIMEOUT = 25.0

_process: subprocess.Popen | None = None
_lock = threading.Lock()


def _split_host_port(base_url: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(base_url if "//" in base_url else f"//{base_url}")
    return parsed.hostname or "127.0.0.1", parsed.port or 4000


def port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    """端口通不通。**只用 TCP 连一下**，不打 HTTP——

    这里要回答的是"有没有东西在听"，不是"它健康吗"。打 HTTP 会在服务还没
    完全就绪时给出假阴性，而那正是我们最关心的时刻。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def bundle_dir_from_env() -> Path | None:
    """宿主传的 bundle 目录（exe 所在目录）。

    嵌入模式下**不能**用 `sys.executable` 推——实测那是打包机的路径，
    不是用户机器上的位置。所以一律由宿主显式传。
    """
    raw = (os.environ.get(BUNDLE_DIR_ENV) or "").strip()
    return Path(raw).expanduser() if raw else None


def locate(bundle_dir: Path | None) -> tuple[Path, Path] | None:
    """找到 (node 可执行文件, api 目录)。找不到返回 None，**不抛异常**。

    找不到不是错误——用户可能装的正是"不带网易云下载"的用法，
    让它变成启动失败才是不对的。
    """
    override_node = os.environ.get(NODE_ENV)
    override_api = os.environ.get(NODE_API_ENV)

    if override_node and override_api:
        node = Path(override_node)
        api = Path(override_api)
        if node.is_file() and (api / "app.js").is_file():
            return node, api
        log.warning("环境变量指定的 node/api 不完整：%s / %s", node, api)

    if bundle_dir is None:
        bundle_dir = bundle_dir_from_env()
    if bundle_dir is None:
        return None

    root = Path(bundle_dir) / "node_api"
    node = root / "node.exe"
    api = root / "api"
    if node.is_file() and (api / "app.js").is_file():
        return node, api

    log.info("没找到自带的 node_api（%s），不自动拉起网易云服务", root)
    return None


def is_running(base_url: str) -> bool:
    host, port = _split_host_port(base_url)
    return port_open(host, port)


def start(base_url: str, bundle_dir: Path | None) -> bool:
    """拉起服务并等它就绪。已经在跑就直接返回 True（复用）。

    失败一律返回 False 并记日志——**不抛异常**：网易云服务不可用时，
    "导本地音乐进 iPod" 那条路照样要能用。
    """
    global _process

    if os.environ.get(DISABLE_ENV) == "1":
        log.info("环境变量 %s=1，不自动拉起网易云服务", DISABLE_ENV)
        return False

    host, port = _split_host_port(base_url)

    # ① 先探测：已经有东西在听就复用，别再拉一个撞端口
    if port_open(host, port):
        log.info("端口 %s:%s 已有服务在跑，直接复用", host, port)
        return True

    located = locate(bundle_dir)
    if located is None:
        return False
    node, api = located
    launcher = api.parent / "launcher.js"
    if not launcher.is_file():
        log.warning("缺启动器 %s，不自动拉起", launcher)
        return False

    with _lock:
        if _process is not None and _process.poll() is None:
            return True

        # 父进程 PID 交给看门狗——它自己盯着，父死子退
        cmd = [str(node), str(launcher), str(os.getpid()), str(port)]
        log.info("拉起网易云 API：%s", " ".join(cmd))

        kwargs: dict = {
            "cwd": str(api.parent),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if sys.platform == "win32":
            # 别弹一个黑窗口出来
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True

        try:
            _process = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            log.warning("拉起网易云 API 失败：%s", exc)
            return False

        # 把它的输出转进我们的日志（调试面板看得到）
        threading.Thread(
            target=_pump, args=(_process,), name="ncm-api-log", daemon=True
        ).start()

    # 等就绪
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if port_open(host, port):
            log.info("网易云 API 已就绪（%s:%s）", host, port)
            return True
        proc = _process
        if proc is not None and proc.poll() is not None:
            log.warning("网易云 API 进程退出了，退出码 %s", proc.returncode)
            return False
        time.sleep(0.4)

    log.warning("等网易云 API 就绪超时（%.0f 秒）", READY_TIMEOUT)
    return False


def _redact(text: str) -> str:
    """把 node 输出里的凭据抹掉再转发进日志。

    ★ 为什么必须做：api-enhanced 会把它收到的**整条请求 URL** 打出来，而那条
    URL 上带着 `cookie=...`——网易云的 cookie 就是**完整的登录凭据**（拿去
    就能当用户用）。它在界面上显示一下无所谓，但落到**日志文件**里就是另一回事：

    * 用户报障时会把日志发出来
    * 日志在 `%APPDATA%` 下，备份/同步盘/截图都可能带走

    这里在**转发的那一道**统一抹掉，而不是去改第三方服务的日志（改不动，
    而且它升级就没了）。
    """
    if not text:
        return text
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


#: (正则, 替换)。宁可多抹一点：日志里少一个参数不影响排查，
#: 泄一次凭据是另一回事。
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(cookie=)[^&\s\"']+", re.I), r"\1<已隐去>"),
    (re.compile(r"((?:__csrf|token|MUSIC_[AUR]_T|MUSIC_[AUR]_U)=)[^&;\s\"']+", re.I),
     r"\1<已隐去>"),
    (re.compile(r"(\"?(?:cookie|authorization|set-cookie)\"?\s*[:=]\s*\"?)[^\"\n]+", re.I),
     r"\1<已隐去>"),
)


def _pump(proc: subprocess.Popen) -> None:
    """把子进程输出搬进日志（凭据先抹掉）。"""
    try:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            text = _redact(line.rstrip())
            if text:
                log.info("%s", text)
    except Exception:  # noqa: BLE001 - 收尾阶段的读取失败不值得炸
        pass


def ensure_async(base_url: str, bundle_dir: Path | None) -> threading.Thread:
    """后台拉起（不阻塞启动）。返回线程，便于测试等待。"""

    def run() -> None:
        try:
            start(base_url, bundle_dir)
        except Exception:  # noqa: BLE001 - 这是后台线程，异常必须自己咽掉
            log.exception("后台拉起网易云 API 时出错")

    thread = threading.Thread(target=run, name="ncm-api-start", daemon=True)
    thread.start()
    return thread


def stop() -> None:
    """收掉我们拉起来的那个。

    只杀**我们自己拉起的**——复用来的（用户自己起的）不该由我们结束。
    看门狗是主防线（父进程没了它 2 秒内自退），这里是正常退出时的优雅路径。
    """
    global _process
    proc = _process
    if proc is None or proc.poll() is not None:
        _process = None
        return

    log.info("停掉网易云 API（PID %s）", proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.warning("它没在 5 秒内退出，强杀")
        proc.kill()
    except Exception as exc:  # noqa: BLE001
        log.warning("停它时出错：%s", exc)
    finally:
        _process = None
