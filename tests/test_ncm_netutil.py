"""`netutil.read_with_deadline` 的测试。

它守的是"应用不会因为一个卡住不动的网络响应而永远等下去"。

真实现场（实测）：同步 147 首时应用卡死，Windows 判定"未响应"然后被杀。
抓到的栈是 SSL read ← 一直在等响应 —— 而 `urlopen(timeout=N)` 里的 timeout
是**每次 recv** 的超时，服务器慢慢吐数据时它永远不会触发。所以这个函数
自己拿 time.monotonic() 掐总时长。
"""

from __future__ import annotations

import io
import time

import pytest

from ipod_cli.ncm.netutil import ReadTimeoutError, read_with_deadline


class _TrickleResponse:
    """一个"慢慢吐数据"的假响应：每次 recv 都有数据，但总量涨得极慢。

    这正是让 socket 级超时失效的那种对端：每次 recv 都算成功，
    timeout 一直被重置，于是永远读不完。
    """

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.reads = 0

    def read(self, size: int = -1) -> bytes:
        self.reads += 1
        time.sleep(self.delay)
        return b"x"          # 永远还有数据

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestReadWithDeadline:
    def test_正常的响应能读完(self) -> None:
        resp = io.BytesIO(b"hello world")
        assert read_with_deadline(resp, seconds=5) == b"hello world"

    def test_空响应返回空(self) -> None:
        assert read_with_deadline(io.BytesIO(b""), seconds=5) == b""

    def test_慢慢吐数据的响应会超时(self) -> None:
        """★ 核心：这就是卡死现场。必须放弃，不能永远等。"""
        resp = _TrickleResponse(delay=0.05)
        started = time.monotonic()
        with pytest.raises(ReadTimeoutError) as excinfo:
            read_with_deadline(resp, seconds=0.3, chunk_size=1)
        elapsed = time.monotonic() - started

        assert elapsed < 3.0, f"应该在 0.3 秒左右就放弃，实际拖了 {elapsed:.1f} 秒"
        assert "超过" in str(excinfo.value)
        # 报错里要说清已经收到多少，方便排查
        assert "MB" in str(excinfo.value)

    def test_超体积上限会中止(self) -> None:
        resp = io.BytesIO(b"x" * 10000)
        with pytest.raises(ValueError, match="上限"):
            read_with_deadline(resp, seconds=5, chunk_size=1024, max_bytes=2048)

    def test_大响应能分块读完(self) -> None:
        payload = b"abcdefgh" * 10000
        got = read_with_deadline(io.BytesIO(payload), seconds=5, chunk_size=1024)
        assert got == payload
