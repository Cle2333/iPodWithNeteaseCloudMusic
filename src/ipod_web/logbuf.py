"""全局日志环形缓冲：调试面板的数据源。

后端子进程被 app 自动拉起，用户看不到它的控制台——所以必须有一条让
日志"流回界面"的路，否则出问题只能干瞪眼。

两个设计点：

1. **每条日志带一个单调递增的 ``seq``**。界面用 ``?since=<seq>`` 增量拉取，
   只取新行。不做增量的话每次轮询都要把 2000 行全传一遍，界面越用越卡。
2. **环形缓冲**。跑一次全库同步会刷出几千行，不裁的话内存一直涨。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

# 默认保留最近 2000 行。够翻查一次完整同步的记录，又不会占多少内存。
DEFAULT_CAPACITY = 2000

# 单次最多返回多少行。界面一次渲染几千行也会卡，够用就行。
DEFAULT_PAGE = 400


@dataclass(frozen=True)
class LogLine:
    seq: int
    ts: str
    level: str
    text: str

    def as_dict(self) -> dict[str, object]:
        return {"seq": self.seq, "ts": self.ts, "level": self.level, "text": self.text}


class LogBuffer(logging.Handler):
    """把日志收进内存环形缓冲。

    挂在 root logger 上，所以后端的日志、uvicorn 的日志都会进来——
    调试面板要的就是"整个进程在说什么"，不是某个模块的自言自语。
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        super().__init__()
        self._lines: deque[LogLine] = deque(maxlen=capacity)
        self._seq = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - 日志格式化失败不该把程序带崩
            text = "<日志格式化失败>"

        if record.exc_info:
            # 异常堆栈必须留下——调试面板看不到堆栈就白搭了
            try:
                text = f"{text}\n{self.format(record)}"
            except Exception:  # noqa: BLE001
                pass

        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        with self._lock:
            self._seq += 1
            self._lines.append(
                LogLine(
                    seq=self._seq,
                    ts=stamp,
                    level=record.levelname,
                    text=text,
                )
            )

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def since(self, seq: int = 0, limit: int = DEFAULT_PAGE) -> list[LogLine]:
        """取 ``seq`` 之后的日志行（不含 seq 本身）。

        注意 ``since`` 永远不会让界面重复拿到同一行：seq 是单调的，
        界面只管把拿到的最大 seq 存下来当游标。
        """
        with self._lock:
            fresh = [line for line in self._lines if line.seq > seq]
        # 只返回最新的 limit 行：界面落后太多时，看最新的比看最旧的有用
        if len(fresh) > limit:
            fresh = fresh[-limit:]
        return fresh

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
