"""作业队列：**整个程序里唯一碰外部世界的通道**。

这个模块存在的唯一理由是那条硬约束：**绝不并发**。

网易云对高频请求会风控封号（P1 就把"低频、绝不并发"定成了硬要求），
而 iPod 那边呢，两个作业同时重写同一个 iTunesDB 是**数据损坏级**的风险。

所以设计成一个队列、**一个工作线程**、全局串行：

* 界面上连点十次"下载"，实际也只有一个请求在飞——这是设计目标，不是副作用
* 路由层不允许直接调 `ncm/`，一律 `submit()` 进队列

用并行换来的那点速度，不值得拿账号和设备数据去赌。

取消是**协作式**的：作业体在 `progress()` 里被问一句"还继续吗"，
取消时抛 :class:`JobCancelled`。因为串行，所以能干净地停在两条歌之间，
不会留下半截文件。
"""

from __future__ import annotations

import faulthandler
import json
import logging
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 最多保留多少条历史作业。
#:
#: 落盘之后**重启不会清空**了（见 JobManager.apply_history）——这个上限
#: 同时管内存和磁盘。
MAX_HISTORY = 50

#: 追加多少条之后把日程文件重写一遍（见 ``JobManager._compact``）。
COMPACT_EVERY = 50

#: 单个作业最多保留多少行日志
MAX_LOG_LINES = 500


class JobCancelled(BaseException):
    """作业被用户取消。

    **故意继承 BaseException 而不是 Exception**：取消信号不能被
    ``except Exception`` 顺手吞掉。作业体里到处是"这一首失败了不要紧，
    继续下一首"的兜底逻辑，如果取消能被那些兜底吞掉，用户点了取消
    却还在继续下载——那才是最糟的情况。
    类比 KeyboardInterrupt：它也是 BaseException。
    """


@dataclass
class LogLine:
    seq: int
    ts: float
    level: str      # info / warn / error
    text: str

    @property
    def time_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.ts))

    def as_dict(self) -> dict[str, Any]:
        """给界面的形状。

        ``ts`` 出去的时候已经是 ``HH:MM:SS`` 的字符串——时间是给人看的，
        让前端再做一次时间格式化只会多一处可能不一致的地方。
        """
        return {
            "seq": self.seq,
            "ts": self.time_text,
            "level": self.level,
            "text": self.text,
        }


@dataclass
class Job:
    """一个作业的全部状态。"""

    id: str
    kind: str                       # download / push / remove / import / verify ...
    title: str                      # 给用户看的中文标题
    state: str = "queued"           # queued / running / done / failed / cancelled
    total: int = 0
    done: int = 0
    current: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    #: 当前**阶段**的中文名（"下载到本地" / "写入 iPod" / "重建数据库" …）。
    #:
    #: 为什么单独搞一个字段：同步一次要经过好几段，每段耗时差一个数量级
    #: （下载几十秒、转码几百秒、写库几秒）。只有 done/total 的话，用户在
    #: 转码那 6 分钟里看到的就是"进度条不动"，以为卡死了——实测就是这么
    #: 被投诉的。阶段名 + 进度条一起看，才知道"在动，且在干哪一步"。
    stage: str = ""

    #: 当前阶段是**什么时候开始的**。
    #:
    #: 为什么要单独记：速度/剩余时间是拿"已完成 / 已耗时"算的。用整个作业的
    #: 耗时去算当前阶段的话，跨段之后数字会荒谬到没法看——实测同步 147 首时
    #: 进到"写入 iPod"那一刻，作业已经跑了 40 秒、done 才 0，一旦 done=1 就会
    #: 算出"还需 24 小时"。分段计时之后，剩余时间说的是"这一步还要多久"，
    #: 那才是用户想知道的。
    stage_started_at: float | None = None

    log: list[LogLine] = field(default_factory=list)

    #: 逐首歌的进度（只下载/同步类作业会有）。
    #:
    #: 界面要的是"下过哪些歌、哪几首失败了"，文本日志没法用——
    #: 得去 parse "✓ [3/246] 歌名 → 7.2 MB" 才能拿到，改个措辞就坏。
    #: 这里存的就是结构化的：`{index, name, artist, status, size, reason}`。
    #:
    #: 更新方式是**整个 dict 换掉**，不是改字段——工作线程在写、
    #: HTTP 线程在读，就地改字段会读到半截状态。
    items: list[dict[str, Any]] = field(default_factory=list)

    #: 内部用：请求取消的标志
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _next_seq: int = 0

    #: 最后一次**有推进**（记日志 / 报进度）的时刻。
    #:
    #: 卡死看门狗用它判断"这个作业是不是不动了"。以前没有这个字段，
    #: 作业一旦卡住就只能看见"一直在跑"，查不出卡在哪——实测用户同步
    #: 140 首时应用卡到未响应，事后翻遍日志只知道它停在那一步之前，
    #: 具体停在哪一行完全没有线索。
    last_progress_at: float = field(default_factory=time.time)

    # ── 给界面看的派生信息 ────────────────────────────────────────────

    @property
    def is_finished(self) -> bool:
        return self.state in ("done", "failed", "cancelled")

    @property
    def percent(self) -> float:
        if self.total <= 0:
            return 0.0 if self.state != "done" else 100.0
        return min(self.done / self.total * 100.0, 100.0)

    @property
    def elapsed(self) -> float:
        start = self.started_at or self.created_at
        end = self.finished_at or time.time()
        return max(end - start, 0.0)

    @property
    def stage_elapsed(self) -> float:
        """**当前阶段**已经跑了多久。

        进度条上的速度和剩余时间都按它算，不按整个作业的耗时——
        理由见 ``stage_started_at``。
        """
        start = self.stage_started_at or self.started_at or self.created_at
        end = self.finished_at or time.time()
        return max(end - start, 0.0)

    @property
    def speed(self) -> float:
        """这一阶段每秒处理多少个。界面拿它算剩余时间。"""
        elapsed = self.stage_elapsed
        if self.state != "running" or elapsed < 0.5 or self.done <= 0:
            return 0.0
        return self.done / elapsed

    @property
    def eta_seconds(self) -> float | None:
        """预计剩余秒数。总数未知或速度未知时返回 None（界面显示"--"）。"""
        if self.total <= 0 or self.done <= 0:
            return None
        speed = self.speed
        if speed <= 0:
            return None
        return max((self.total - self.done) / speed, 0.0)

    def add_log(self, text: str, level: str = "info") -> LogLine:
        line = LogLine(seq=self._next_seq, ts=time.time(), level=level, text=text)
        # 任何一行日志都算"有推进"——卡死看门狗拿它当心跳（见 mark_progress）
        self.mark_progress()
        self._next_seq += 1
        self.log.append(line)
        # 环形裁剪：长作业（几百首）会刷出巨量日志，不裁的话内存一直涨
        if len(self.log) > MAX_LOG_LINES:
            del self.log[: len(self.log) - MAX_LOG_LINES]
        return line

    def mark_progress(self) -> None:
        """记一次心跳。看门狗靠它区分"在干活"和"卡住了"。"""
        self.last_progress_at = time.time()

    @property
    def stalled_for(self) -> float:
        """已经多久没有推进了（秒）。仅在 running 时有意义。"""
        if self.state != "running":
            return 0.0
        return max(time.time() - self.last_progress_at, 0.0)

    def logs_since(self, seq: int = 0, limit: int = 400) -> list[LogLine]:
        """取 ``seq`` 之后的日志行（不含 seq 本身）。

        界面拿 seq 当游标增量拉取。不增量的话，几百首的下载作业每轮轮询
        都要把整份日志重传一遍，界面会越用越卡。
        """
        fresh = [line for line in self.log if line.seq > seq]
        if len(fresh) > limit:
            fresh = fresh[-limit:]
        return fresh

    def to_record(self) -> dict[str, Any]:
        """落盘用的完整快照（比 :meth:`to_dict` 多日志，少派生字段）。

        **不存派生字段**（state_text / percent / elapsed / speed）：那些是
        从 started_at/finished_at/total/done 算出来的。存了的话，改了算法
        之后老记录会跟新记录对不上，而 log 里已经写着当时的时间戳了。
        """
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "state": self.state,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total": self.total,
            "done": self.done,
            "current": self.current,
            "stage": self.stage,
            "stage_started_at": self.stage_started_at,
            "result": self.result,
            "items": [dict(item) for item in self.items],
            "log": [
                {"seq": line.seq, "ts": line.ts, "level": line.level, "text": line.text}
                for line in self.log
            ],
        }

    @classmethod
    def from_record(cls, data: dict[str, Any]) -> Job:
        """从 :meth:`to_record` 的快照还原。

        还原出来的作业一律当成**已结束**的历史——退一步说，进程重启之后
        原来那些"进行中"的作业本来也不会再动了，报成进行中会让界面永远
        转着圈等一个不存在的任务。
        """
        job = cls(
            id=str(data.get("id", "")),
            kind=str(data.get("kind", "")),
            title=str(data.get("title", "")),
        )
        job.state = str(data.get("state", "done"))
        if job.state in ("queued", "running"):
            job.state = "cancelled"
        job.error = str(data.get("error", ""))
        job.started_at = data.get("started_at")
        job.finished_at = data.get("finished_at")
        job.total = int(data.get("total", 0))
        job.done = int(data.get("done", 0))
        job.current = str(data.get("current", ""))
        job.stage = str(data.get("stage", ""))
        job.stage_started_at = data.get("stage_started_at")
        job.result = dict(data.get("result") or {})
        job.items = [dict(i) for i in (data.get("items") or [])]
        job.log = [
            LogLine(
                seq=int(line.get("seq", 0)),
                ts=float(line.get("ts", 0)),
                level=str(line.get("level", "info")),
                text=str(line.get("text", "")),
            )
            for line in (data.get("log") or [])
        ]
        job.next_seq = (job.log[-1].seq + 1) if job.log else 0
        return job

    def to_dict(self) -> dict[str, Any]:
        # items 复制一份再发出去：工作线程还在往里写，直接给引用的话
        # 序列化过程中列表可能变长
        snapshot = [dict(item) for item in self.items]
        done = sum(1 for i in snapshot if i.get("status") == "done")
        failed = sum(1 for i in snapshot if i.get("status") == "failed")
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "state": self.state,
            "state_text": STATE_TEXT.get(self.state, self.state),
            "total": self.total,
            "done": self.done,
            "current": self.current,
            # 阶段名（"写入 iPod" / "重建数据库并签名" …）。界面把它显示在
            # 进度条上方——那 6 分钟里唯一能告诉用户"在动、在干哪一步"的东西。
            "stage": self.stage,
            "percent": round(self.percent, 1),
            "error": self.error,
            "result": self.result,
            "elapsed": round(self.elapsed, 1),
            "speed": round(self.speed, 2),
            "eta_seconds": round(self.eta_seconds, 1) if self.eta_seconds else None,
            "items": snapshot,
            "items_done": done,
            "items_failed": failed,
        }


#: 状态的中文说法（界面直接用，避免前端各写一套）
logger = logging.getLogger("ipod_web")

#: 状态的中文说法（界面直接用，避免前端各写一套）
STATE_TEXT = {
    "queued": "排队中",
    "running": "进行中",
    "done": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


class JobHandle:
    """作业体拿到的东西。

    只暴露它该用的能力——不给它直接改 state 的权力，
    否则一个写错的作业体就能伪造"已完成"，而界面会当真。
    """

    def __init__(self, job: Job) -> None:
        self._job = job

    @property
    def job_id(self) -> str:
        """本作业的 ID。作业体要取消自己、或者往结果里带 ID 时用得上。"""
        return self._job.id

    @property
    def cancelled(self) -> bool:
        return self._job._cancel.is_set()

    def check_cancelled(self) -> None:
        """被取消就抛 :class:`JobCancelled`，中断当前作业。"""
        if self.cancelled:
            raise JobCancelled()

    def log(self, text: str, level: str = "info") -> None:
        self._job.add_log(text, level)

    def warn(self, text: str) -> None:
        self._job.add_log(text, "warn")

    def stage(self, text: str, total: int | None = None) -> None:
        """换一个阶段，并把这一段的进度**归零重算**。

        ``total`` 是这一段大约要处理多少个（首歌 / 个文件）。给 ``None`` 表示
        这一段没法切成等份（刷盘、写库、校验）——界面会显示**不确定进度条**
        （一直动的那个），而不是一根不动的空条。这一条是重点：用户抱怨
        "以为卡住了"，正是因为以前这几段什么都不报。
        """
        self._job.stage = text
        self._job.current = ""
        self._job.done = 0
        self._job.total = int(total) if total else 0
        self._job.stage_started_at = time.time()
        self._job.mark_progress()
        self._job.add_log(text)

    def set_total(self, total: int) -> None:
        self._job.total = max(int(total), 0)

    def step(self, current: str = "") -> None:
        """完成一个。串行作业的最基本推进单位。"""
        self._job.done += 1
        self._job.mark_progress()
        if current:
            self._job.current = current

    def progress(self, text: str) -> None:
        """**直接当 ``progress`` 回调传给 ``ncm/`` 里的函数用。**

        那些函数（plan_sync / execute_downloads / sync_to_ipod）都收
        ``Callable[[str], None]``，这里签名对齐，所以能直接塞进去。

        顺带在这里做取消检查——它们是串行循环，每条之间过一下这里，
        取消就能干净地停在两条歌中间。
        """
        self.check_cancelled()
        self._job.add_log(text)

    def on_item(self, done: int, total: int) -> None:
        """结构化进度：第几首 / 共几首。**直接传给 ``ncm`` 的 ``on_item``。**

        为什么不让界面去 parse ``✓ [3/246]`` 这种字符串：改个措辞就坏，
        而且歌名里恰好出现的数字会被认成进度（真有歌名叫《7/11》）。

        和 ``progress`` 一样在这里做取消检查——串行循环每条之间过一下这里。
        """
        self.check_cancelled()
        self._job.total = max(int(total), 0)
        self._job.done = max(int(done), 0)
        self._job.mark_progress()

    def on_song(self, event: Any) -> None:
        """逐首歌的进度。**直接传给 ``ncm`` 的 ``on_song``。**

        界面要显示"下过哪些歌、哪几首失败了"，靠的就是这里记下来的东西。
        文本日志仍照常写（排查问题看日志更舒服），但界面不依赖它。

        ``items`` 按 index 存，**每次整个 dict 换掉**：工作线程在写、
        HTTP 线程在读，就地改字段会读到半截状态。
        """
        self.check_cancelled()

        index = int(getattr(event, "index", 0))
        if index <= 0:
            return
        song = getattr(event, "song", None)
        total = int(getattr(event, "total", 0))
        size = int(getattr(event, "size", 0) or 0)

        entry = {
            "index": index,
            "name": getattr(song, "name", "") or "",
            "artist": getattr(song, "artist_text", "") or "",
            "song_id": int(getattr(song, "id", 0) or 0),
            "status": str(getattr(event, "status", "")),
            "size": size,
            "reason": str(getattr(event, "reason", "") or ""),
        }

        while len(self._job.items) < index:
            self._job.items.append({})
        self._job.items[index - 1] = entry

        if total:
            self._job.total = total

        # 当前正在下的那首也要显示出来——大文件要等十几秒，
        # 界面上光转圈看不出在干嘛
        if entry["status"] == "downloading":
            self._job.current = entry["name"]

    def finish(self, **result: Any) -> None:
        self._job.result.update(result)


#: 作业多久没推进就认为"卡住了"，并把所有线程的调用栈打进日志（秒）。
#:
#: 为什么需要它：实测用户同步 140 首时，应用卡到 Windows 报"未响应"然后被杀。
#: 事后能拿到的只有"某一步之后就没有日志了"——具体卡在哪一行**完全没有线索**，
#: 因为 Python 卡在阻塞调用里时不会自己说话。有了这个看门狗，下次再卡住，
#: 日志里会直接出现每个线程的调用栈，一眼就能看出卡在哪个函数。
#:
#: ★ 别设太小：实测一次成功的同步里，单次网络读取卡住 60 秒是**正常**的
#: （网易云 CDN 忽快忽慢）。60 秒就报警会在正常同步里刷出"可能卡住了"，
#: 用户看了以为坏了。180 秒足够区分"慢"和"真的不动了"。
STALL_REPORT_SECONDS = 180.0

#: 每次报栈之后隔多久才允许再报一次（避免日志被刷爆）
STALL_REPORT_COOLDOWN = 300.0

_log = logging.getLogger(__name__)


def _dump_thread_stacks(reason: str) -> str:
    """抓所有线程的调用栈，返回可读文本。

    ``faulthandler`` 是标准库里唯一能在**不打断目标线程**的前提下拿到
    "它现在到底停在哪一行"的手段——普通的 traceback 只能看崩溃，看不了卡住。
    """
    # ★ 必须写**真实文件**：faulthandler 要的是文件描述符，StringIO 没有
    #   fileno()，会抛 UnsupportedOperation。实测踩到——第一次真卡住时
    #   看门狗报了"作业卡住"却没能附上调用栈，白等了一轮。
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as fh:
        try:
            faulthandler.dump_traceback(file=fh, all_threads=True)
            fh.flush()
            fh.seek(0)
            body = fh.read().strip()
        except Exception as exc:  # noqa: BLE001 - 抓栈失败也不能把作业搞崩
            return f"（抓调用栈失败：{type(exc).__name__}: {exc}）"
    return f"{reason}\n{body}" if body else f"{reason}\n（没有可用信息）"


class JobManager:
    """单线程作业队列。

    ``executor`` 可注入，测试时方便控制；默认就是一个单线程池——
    **这个 ``max_workers=1`` 就是"绝不并发"的落点**，改成 2 就等于
    把风控和设备安全全交出去了。
    """

    def __init__(
        self,
        *,
        executor: ThreadPoolExecutor | None = None,
        history_path: Path | str | None = None,
    ) -> None:
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ipod-job"
        )
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._running = 0
        self._idle = threading.Event()
        self._idle.set()

        #: 作业日程落盘到哪儿。None = 不落盘（测试默认走这条）。
        #:
        #: 为什么必须落盘：这东西存在的意义就是"出 bug 的时候导出给开发看"。
        #: 而内存里的历史**进程一重启就没了**——用户往往是重启一次应用之后
        #: 才想起来要导出，导出来一片空白，等于没有这个功能。
        self.history_path = Path(history_path) if history_path else None
        self._appends = 0
        self._load_history()

    # ── 提交 ──────────────────────────────────────────────────────────

    def submit(
        self,
        kind: str,
        title: str,
        body: Callable[[JobHandle], Any],
    ) -> Job:
        """把一个作业排进队列，立刻返回（不等它跑完）。

        ``body`` 收一个 :class:`JobHandle`，在里面干活、报进度。
        它抛异常 = 作业失败；抛 :class:`JobCancelled` = 作业被取消。
        """
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, title=title)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._trim_history()
            self._running += 1
            self._idle.clear()
        job.add_log(f"{title} —— 已进入队列")
        self._executor.submit(self._run, job, body)
        return job

    # ── 日程落盘 ──────────────────────────────────────────────────────

    def _load_history(self) -> None:
        """启动时把上次的作业日程读回来。损坏的行跳过，不因此起不来。"""
        if self.history_path is None or not self.history_path.is_file():
            return
        for line in self.history_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                job = Job.from_record(record)
            except Exception:                              # noqa: BLE001
                # 半截的写入（上一次正好被杀）或格式变了——跳过这一条就好，
                # 没必要让整个应用起不来
                continue
            if job.id:
                self._jobs[job.id] = job
                self._order.append(job.id)

        # 只保留最近的。磁盘上可能攒了很多。
        while len(self._order) > MAX_HISTORY:
            dropped = self._order.pop(0)
            self._jobs.pop(dropped, None)

    def _persist(self, job: Job) -> None:
        """把一个刚结束的作业追加到日程文件。**失败只记日志，不抛。**"""
        if self.history_path is None:
            return
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(job.to_record(), ensure_ascii=False) + "\n")
            self._appends += 1
            if self._appends >= COMPACT_EVERY:
                self._appends = 0
                self._compact()
        except OSError as exc:
            logger.warning("作业日程写不进去（%s）：%s", self.history_path, exc)

    def _compact(self) -> None:
        """把日程文件重写成最近 MAX_HISTORY 条。

        追加式的文件会长到没边（246 首的一次下载光 items 就 30 KB），
        起步时读全文会越来越慢。
        """
        if self.history_path is None:
            return
        keep = self.list_jobs()[:MAX_HISTORY]
        try:
            tmp = self.history_path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for job in reversed(keep):      # 文件里保持"旧在前"
                    fh.write(
                        json.dumps(job.to_record(), ensure_ascii=False) + "\n"
                    )
            tmp.replace(self.history_path)
        except OSError as exc:
            logger.warning("作业日程压缩失败：%s", exc)

    def _trim_history(self) -> None:
        """历史只留最近 MAX_HISTORY 条，且**绝不丢还没跑完的**。"""
        while len(self._order) > MAX_HISTORY:
            for index, job_id in enumerate(self._order):
                if self._jobs[job_id].is_finished:
                    del self._jobs[job_id]
                    del self._order[index]
                    break
            else:
                return      # 全都是未完成的，不裁

    def _start_stall_watchdog(self, job: Job) -> None:
        """盯着这个作业；没推进超过阈值就把所有线程栈打进日志。

        只报**一次**（之后按冷却时间），因为卡住时栈是同一份，刷屏没有意义。
        """

        def watch() -> None:
            last_report = 0.0
            while not job.is_finished:
                time.sleep(5.0)
                if job.is_finished:
                    return
                stalled = job.stalled_for
                if stalled < STALL_REPORT_SECONDS:
                    continue
                now = time.time()
                if now - last_report < STALL_REPORT_COOLDOWN:
                    continue
                last_report = now
                report = _dump_thread_stacks(
                    f"作业「{job.title}」已经 {stalled:.0f} 秒没有任何进展，"
                    f"当前步骤：{job.current or '（未报告）'}"
                )
                # 进日志文件 + 界面调试面板：用户报障时把日志发出来就够了
                _log.warning("作业卡住，附全部线程调用栈：\n%s", report)
                job.add_log(
                    f"⚠ 已经 {stalled:.0f} 秒没有任何进展（可能卡住了）。"
                    f"所有线程的调用栈已写进日志文件，请把它发给开发者。",
                    "warn",
                )
                # 同时塞进作业自己的日志尾部：界面能直接看到
                for line in report.splitlines()[:40]:
                    job.add_log(f"  {line}", "warn")

        threading.Thread(
            target=watch, name=f"stall-watchdog-{job.id}", daemon=True
        ).start()

    def _run(self, job: Job, body: Callable[[JobHandle], Any]) -> None:
        handle = JobHandle(job)
        try:
            job.state = "running"
            job.started_at = time.time()
            # 阶段起点也设上：没换过 stage 的作业（比如纯下载）靠它算速度，
            # 不然 speed 拿不到起点会一直是 0，界面就不显示剩余时间了。
            job.stage_started_at = job.started_at
            job.last_progress_at = time.time()
            job.add_log("开始执行")
            self._start_stall_watchdog(job)
            result = body(handle)
            if isinstance(result, dict):
                job.result.update(result)
            # 取消信号可能在作业体返回**之后**才被置上（比如最后一步跑完
            # 用户刚好点了取消）。这种情况下如实报"已取消"更诚实。
            job.state = "cancelled" if handle.cancelled else "done"
            job.add_log("已取消" if job.state == "cancelled" else "完成")
        except JobCancelled:
            job.state = "cancelled"
            job.add_log("已取消", "warn")
        except Exception as exc:                      # noqa: BLE001
            job.state = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.add_log(f"失败：{job.error}", "error")
        finally:
            job.finished_at = time.time()
            self._persist(job)
            with self._lock:
                self._running -= 1
                if self._running <= 0:
                    self._idle.set()

    # ── 查询 ──────────────────────────────────────────────────────────

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        """新作业在前。"""
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order) if i in self._jobs]

    @property
    def running_job(self) -> Job | None:
        for job in self.list_jobs():
            if job.state == "running":
                return job
        return None

    @property
    def queued_count(self) -> int:
        return sum(1 for j in self.list_jobs() if j.state == "queued")

    @property
    def has_work(self) -> bool:
        return any(not j.is_finished for j in self.list_jobs())

    # ── 取消 ──────────────────────────────────────────────────────────

    def cancel(self, job_id: str) -> bool:
        """请求取消。**不等待**——真正停下要等作业体跑到下一个检查点。

        返回 False 表示这个作业已经结束了，取消没有意义。
        """
        job = self._jobs.get(job_id)
        if job is None or job.is_finished:
            return False
        job._cancel.set()
        job.add_log("收到取消请求，将在当前这一首结束后停止", "warn")
        return True

    # ── 等待（测试和优雅退出用）──────────────────────────────────────

    def wait_idle(self, timeout: float | None = None) -> bool:
        """阻塞到队列里没有未完成的作业。返回是否真的等到。"""
        return self._idle.wait(timeout)

    def shutdown(self, wait: bool = True) -> None:
        # 先请求取消所有排队的，否则 shutdown 会一直等它们跑完
        for job in self.list_jobs():
            if job.state == "queued":
                job._cancel.set()
        self._executor.shutdown(wait=wait, cancel_futures=True)
