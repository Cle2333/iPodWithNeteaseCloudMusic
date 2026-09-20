"""日志缓冲的单元测试。

这个缓冲是调试面板的唯一数据源，坏了的后果是"后端出问题但界面什么都看不到"，
所以边界要比普通工具代码抠得细一点。
"""

from __future__ import annotations

import logging

import pytest

from ipod_web.logbuf import LogBuffer


@pytest.fixture
def buffer() -> LogBuffer:
    return LogBuffer(capacity=50)


@pytest.fixture
def logger(buffer: LogBuffer) -> logging.Logger:
    lg = logging.getLogger("test.logbuf.unique")
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    lg.addHandler(buffer)
    return lg


class TestSeq:
    def test_seq_starts_at_one_and_increases(self, buffer, logger) -> None:
        """seq 从 1 开始、单调递增。

        界面拿 seq 当游标：重复或者回退都会导致丢行或重复刷屏。
        """
        for i in range(5):
            logger.info("第 %d 行", i)

        seqs = [line.seq for line in buffer.since(0)]
        assert seqs == [1, 2, 3, 4, 5]
        assert buffer.latest_seq == 5

    def test_seq_is_unique(self, buffer, logger) -> None:
        for i in range(30):
            logger.info("行 %d", i)
        seqs = [line.seq for line in buffer.since(0, limit=100)]
        assert len(set(seqs)) == len(seqs)


class TestSince:
    def test_since_returns_only_newer_lines(self, buffer, logger) -> None:
        for i in range(5):
            logger.info("第 %d 行", i)

        fresh = buffer.since(3)
        assert [line.seq for line in fresh] == [4, 5]
        assert "第 3 行" in fresh[0].text

    def test_since_zero_returns_everything(self, buffer, logger) -> None:
        logger.info("一")
        logger.info("二")
        assert len(buffer.since(0)) == 2

    def test_since_at_the_end_returns_nothing(self, buffer, logger) -> None:
        logger.info("一")
        assert buffer.since(buffer.latest_seq) == []

    def test_since_never_repeats_the_cursor_line(self, buffer, logger) -> None:
        """游标那一行本身不能重复返回。

        重复的话界面每次轮询都会把最后一行再插一遍，日志看起来像卡住了。
        """
        logger.info("一")
        logger.info("二")
        for _ in range(3):
            lines = buffer.since(1)
            assert all(line.seq > 1 for line in lines)

    def test_limit_keeps_the_newest(self, buffer, logger) -> None:
        """落后太多时给最新的，不是最旧的。

        界面卡了一阵之后，用户想看的是"刚刚发生了什么"，
        而不是几十秒前那几行。
        """
        for i in range(20):
            logger.info("第 %d 行", i)

        lines = buffer.since(0, limit=5)
        assert len(lines) == 5
        assert "第 19 行" in lines[-1].text


class TestCapacity:
    def test_ring_buffer_drops_the_oldest(self, logger) -> None:
        """长作业（几百首）会刷出巨量日志，不裁内存一直涨。"""
        buf = LogBuffer(capacity=10)
        lg = logging.getLogger("test.logbuf.small")
        lg.handlers.clear()
        lg.propagate = False
        # 必须显式设级别：logger 默认 NOTSET 会继承 root 的 WARNING，
        # 于是 info() 根本不会产生记录，缓冲自然是空的
        lg.setLevel(logging.DEBUG)
        lg.addHandler(buf)

        for i in range(40):
            lg.info("第 %d 行", i)

        kept = buf.since(0, limit=1000)
        assert len(kept) == 10
        assert "第 39 行" in kept[-1].text
        # seq 是全局递增的，不因裁剪而重置——否则界面拿旧游标会永远取不到新行
        assert kept[-1].seq == 40

    def test_clear_empties_but_seq_keeps_going(self, buffer, logger) -> None:
        """清空日志不该让 seq 归零。

        归零的话界面手里的旧游标（比如 37）比新 seq 还大，
        之后永远拉不到任何日志——表现为"清空之后日志再也不动了"。
        """
        logger.info("一")
        logger.info("二")
        before = buffer.latest_seq

        buffer.clear()
        assert buffer.since(0) == []

        logger.info("清空之后")
        assert buffer.latest_seq > before
        assert len(buffer.since(before)) == 1


class TestLevels:
    def test_level_is_kept(self, buffer, logger) -> None:
        """级别要留着——调试面板要能"只看错误"。"""
        logger.info("普通")
        logger.warning("警告")
        logger.error("错误")

        levels = [line.level for line in buffer.since(0)]
        assert levels == ["INFO", "WARNING", "ERROR"]

    def test_text_is_the_rendered_message(self, buffer, logger) -> None:
        """`%s` 占位符要渲染成实际内容，不能原样留着。"""
        logger.info("下载完成：%s", "STAY")
        assert buffer.since(0)[0].text == "下载完成：STAY"

    def test_timestamp_is_present(self, buffer, logger) -> None:
        logger.info("一")
        assert buffer.since(0)[0].ts
