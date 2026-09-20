"""作业队列的测试。**这个文件守的是整个项目最重要的那条约束。**

「绝不并发」不是编码风格问题：
* 网易云那边高频请求会**风控封号**（P1 就把它定成了硬要求）
* iPod 那边两个作业同时重写同一个 iTunesDB 是**数据损坏级**风险

所以这些测试不是"锦上添花的覆盖率"，而是**这条约束的唯一证据**。
改动 JobManager 时这里红了，就是在往外放风控和设备安全。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ipod_cli.ncm.state import StateStore
from ipod_web.context import WebContext
from ipod_web.jobs import COMPACT_EVERY, MAX_HISTORY, JobManager


@pytest.fixture
def manager():
    mgr = JobManager()
    yield mgr
    mgr.shutdown(wait=False)


# ──────────────────────────────────────────────────────────────────────
# T1：绝不并发 —— 本文件的核心
# ──────────────────────────────────────────────────────────────────────


class TestNeverConcurrent:
    """★ 整个 P3 的地基。这里红了什么都别继续。"""

    def test_two_jobs_do_not_overlap(self, manager: JobManager) -> None:
        """两个作业的执行区间**不能重叠**。"""
        spans: list[tuple[str, float, float]] = []
        lock = threading.Lock()

        def make_body(name: str, hold: float):
            def body(handle):
                start = time.monotonic()
                time.sleep(hold)
                with lock:
                    spans.append((name, start, time.monotonic()))
            return body

        manager.submit("t", "作业A", make_body("A", 0.25))
        manager.submit("t", "作业B", make_body("B", 0.05))

        assert manager.wait_idle(10), "作业没跑完"

        assert len(spans) == 2
        first, second = sorted(spans, key=lambda s: s[1])
        assert first[2] <= second[1], (
            f"两个作业重叠了！{first[0]} 在 {first[2]:.3f} 结束，"
            f"{second[0]} 在 {second[1]:.3f} 就开始了——这违反绝不并发"
        )

    def test_max_concurrency_is_exactly_one(self, manager: JobManager) -> None:
        """从**多个线程同时提交**，仍然只能有一个在跑。"""
        lock = threading.Lock()
        state = {"now": 0, "max": 0, "ran": 0}

        def body(handle):
            with lock:
                state["now"] += 1
                state["ran"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.04)
            with lock:
                state["now"] -= 1

        # 用默认参数把 i 绑到定义时——不然 lambda 是晚绑定，
        # 八个线程会全部提交同一个编号（ruff 的 B023 说的就是这事儿）
        threads = [
            threading.Thread(
                target=lambda n=i: manager.submit("t", f"作业{n}", body)
            )
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert manager.wait_idle(15), "作业没跑完"

        assert state["ran"] == 8, f"只跑了 {state['ran']} 个作业"
        assert state["max"] == 1, (
            f"同时有 {state['max']} 个作业在跑——绝不并发被破坏了"
        )

    def test_default_executor_is_single_threaded(self) -> None:
        """默认执行器必须是单线程。

        有人"优化"成 max_workers=4 的话，上面两个测试也会红，
        但那条报错信息不够直指病灶——这条把原因说清楚。
        """
        manager = JobManager()
        try:
            executor = manager._executor
            assert isinstance(executor, ThreadPoolExecutor)
            assert executor._max_workers == 1, (
                "默认执行器不是单线程——绝不并发的前提没了。"
                "要提速得先改掉风控和设备安全的设计，不是改这个数字。"
            )
        finally:
            manager.shutdown(wait=False)


# ──────────────────────────────────────────────────────────────────────
# 作业生命周期
# ──────────────────────────────────────────────────────────────────────


class TestLifecycle:
    def test_runs_and_finishes(self, manager: JobManager) -> None:
        seen: list[str] = []

        def body(handle):
            seen.append(handle.job_id)
            handle.log("干了点活")
            return {"added": 3}

        job = manager.submit("download", "下载 3 首", body)
        assert manager.wait_idle(10)

        assert job.state == "done"
        assert job.result == {"added": 3}
        assert seen == [job.id]
        assert job.started_at is not None
        assert job.finished_at is not None
        assert job.percent == 100.0

    def test_submit_returns_immediately(self, manager: JobManager) -> None:
        """submit 不能阻塞——界面点一下要立刻拿到 job_id 去轮询。"""
        started = threading.Event()

        def body(handle):
            started.set()
            time.sleep(0.4)

        started_at = time.monotonic()
        job = manager.submit("t", "慢作业", body)
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.2, f"submit 阻塞了 {elapsed:.2f}s"
        assert job.state in ("queued", "running")

    def test_failure_is_recorded(self, manager: JobManager) -> None:
        def body(handle):
            handle.log("要炸了", "warn")
            raise RuntimeError("模拟失败")

        job = manager.submit("t", "会失败的作业", body)
        assert manager.wait_idle(10)

        assert job.state == "failed"
        assert "模拟失败" in job.error
        assert "RuntimeError" in job.error
        # 失败原因要同时进日志，否则界面上只看到一个状态没有上下文
        assert any("模拟失败" in line.text for line in job.log)

    def test_failure_does_not_block_the_queue(self, manager: JobManager) -> None:
        """前一个作业炸了，后面的必须照跑。"""
        def boom(handle):
            raise ValueError("炸")

        def ok(handle):
            handle.log("我没事")

        first = manager.submit("t", "炸的", boom)
        second = manager.submit("t", "好的", ok)
        assert manager.wait_idle(10)

        assert first.state == "failed"
        assert second.state == "done", "前一个作业失败把队列卡死了"

    def test_queued_state_visible(self, manager: JobManager) -> None:
        """排队中的作业要能看到——界面得显示"你的操作会排队"。"""
        release = threading.Event()

        def slow(handle):
            release.wait(5)

        def quick(handle):
            pass

        manager.submit("t", "慢的", slow)
        time.sleep(0.1)          # 让第一个真正跑起来
        second = manager.submit("t", "排队的", quick)

        assert second.state == "queued", f"状态是 {second.state}"
        assert manager.queued_count == 1
        assert manager.running_job is not None

        release.set()
        assert manager.wait_idle(10)


# ──────────────────────────────────────────────────────────────────────
# 取消
# ──────────────────────────────────────────────────────────────────────


class TestCancellation:
    def test_cancel_stops_at_next_checkpoint(self, manager: JobManager) -> None:
        """取消后干净地停在检查点，**不再继续处理**。"""
        processed: list[int] = []

        def body(handle):
            handle.set_total(10)
            for i in range(10):
                handle.progress(f"处理第 {i} 首")     # ← 检查点
                processed.append(i)
                handle.on_item(i + 1, 10)
                if i == 2:
                    manager.cancel(handle.job_id)     # 用户点了取消

        job = manager.submit("t", "十首", body)
        assert manager.wait_idle(10)

        assert job.state == "cancelled"
        # 已经开工的三首保留（不重做），第四首开始不再处理
        assert processed == [0, 1, 2], f"取消后还在继续：{processed}"
        assert job.done == 3, f"进度计数不对：{job.done}"

    def test_cancel_survives_a_generic_except_block(self, manager: JobManager) -> None:
        """★ ``JobCancelled`` 故意继承 ``BaseException``。

        作业体里到处是"这一首失败了不要紧，继续下一首"的 ``except Exception`` 兜底
        （比如 ``execute_downloads`` 逐首 try/except）。
        取消如果是个普通 Exception，就会被那些兜底吞掉——
        用户点了取消，程序却还在闷头下载。那是这类工具最糟的体验。
        """
        processed: list[int] = []

        def body(handle):
            for i in range(10):
                try:
                    handle.progress(f"第 {i} 首")
                except Exception:            # noqa: BLE001  ← 兜底，绝不能吃掉取消
                    pass
                processed.append(i)
                if i == 2:
                    manager.cancel(handle.job_id)

        job = manager.submit("t", "带兜底的作业", body)
        assert manager.wait_idle(10)

        assert processed == [0, 1, 2], f"取消被 except Exception 吞掉了：{processed}"
        assert job.state == "cancelled"

    def test_cancelled_job_is_not_done(self, manager: JobManager) -> None:
        """取消 ≠ 完成。搞混的话界面会显示"已完成"，用户以为全下完了。"""
        def body(handle):
            handle.set_total(100)
            for i in range(100):
                handle.progress(f"第 {i} 首")
                if i == 1:
                    manager.cancel(handle.job_id)

        job = manager.submit("t", "取消掉", body)
        assert manager.wait_idle(10)

        assert job.state == "cancelled"
        assert job.state != "done"
        assert job.to_dict()["state_text"] == "已取消"

    def test_cancel_finished_job_returns_false(self, manager: JobManager) -> None:
        job = manager.submit("t", "很快", lambda handle: None)
        assert manager.wait_idle(10)
        assert manager.cancel(job.id) is False

    def test_cancel_unknown_job_returns_false(self, manager: JobManager) -> None:
        assert manager.cancel("不存在的id") is False

    def test_cancel_only_affects_the_target(self, manager: JobManager) -> None:
        """取消一个作业，不能顺手把排队的另一个也取消了。"""
        release = threading.Event()
        ran: list[str] = []

        def slow(handle):
            release.wait(5)
            ran.append("slow")

        def other(handle):
            ran.append("other")

        first = manager.submit("t", "慢的", slow)
        second = manager.submit("t", "另一个", other)
        time.sleep(0.1)

        manager.cancel(first.id)
        release.set()
        assert manager.wait_idle(10)

        assert first.state == "cancelled"
        assert second.state == "done", "无辜的作业被一起取消了"
        assert "other" in ran


# ──────────────────────────────────────────────────────────────────────
# 进度、日志、展示用的派生数据
# ──────────────────────────────────────────────────────────────────────


class TestProgressAndLog:
    def test_progress_matches_ncm_callback_signature(self, manager: JobManager) -> None:
        """``handle.progress`` 要能**直接**塞给 ``ncm/`` 里收
        ``Callable[[str], None]`` 的那些函数。

        签名对不上的话就得在每处调用点写一层 lambda 转换——
        多一层就多一处可能漏掉取消检查。
        """
        captured: list[str] = []

        def fake_ncm_worker(progress):
            """假装自己是 execute_downloads：收一个 progress 回调。"""
            progress("正在下载 1/3：富士山下")
            progress("正在下载 2/3：晴天")

        def body(handle):
            fake_ncm_worker(handle.progress)

        job = manager.submit("t", "模拟下载", body)
        assert manager.wait_idle(10)

        captured = [line.text for line in job.log]
        assert any("富士山下" in t for t in captured)
        assert any("晴天" in t for t in captured)

    def test_total_and_step(self, manager: JobManager) -> None:
        def body(handle):
            handle.set_total(4)
            for i in range(4):
                handle.step(f"第{i}首")

        job = manager.submit("t", "四首", body)
        assert manager.wait_idle(10)

        assert job.total == 4
        assert job.done == 4
        assert job.current == "第3首"
        assert job.percent == 100.0

    def test_percent_never_exceeds_100(self, manager: JobManager) -> None:
        """多报了一步也不能让进度条超过 100%。"""
        def body(handle):
            handle.set_total(2)
            handle.step()
            handle.step()
            handle.step()          # 多出来的一步

        job = manager.submit("t", "多报", body)
        assert manager.wait_idle(10)
        assert job.percent == 100.0

    def test_zero_total_is_not_division_by_zero(self, manager: JobManager) -> None:
        job = manager.submit("t", "没设总数", lambda handle: None)
        assert manager.wait_idle(10)
        assert job.percent == 100.0
        assert job.eta_seconds is None

    def test_log_levels_are_kept(self, manager: JobManager) -> None:
        def body(handle):
            handle.log("普通信息")
            handle.warn("警告信息")

        job = manager.submit("t", "日志", body)
        assert manager.wait_idle(10)

        levels = {line.text: line.level for line in job.log}
        assert levels["普通信息"] == "info"
        assert levels["警告信息"] == "warn"

    def test_log_is_ring_buffered(self, manager: JobManager) -> None:
        """长作业（几百首）会刷出巨量日志，必须裁——否则内存一直涨。"""
        def body(handle):
            for i in range(900):
                handle.log(f"第 {i} 行")

        job = manager.submit("t", "话多的作业", body)
        assert manager.wait_idle(15)

        assert len(job.log) <= 500, f"日志没裁剪，攒了 {len(job.log)} 行"

        texts = [line.text for line in job.log]
        # 裁的是**最老的**：最早的没了，最近的必须还在
        assert "第 0 行" not in texts, "砍掉了不该砍的"
        assert "第 899 行" in texts, "最新的日志被裁掉了"
        assert any("完成" in t for t in texts), "收尾日志不该被裁掉"

    def test_eta_available_while_running(self, manager: JobManager) -> None:
        """跑的过程中要能算出剩余时间（界面显示"预计还要 X 分"）。

        作业体必须比采样点长——太短的话采样时早就 done 了，
        speed/eta 会老老实实返回 None，看起来像功能坏了，其实是作业跑完了。
        """
        def body(handle):
            for _ in range(40):
                time.sleep(0.05)          # 约 2 秒
                handle.on_item(handle._job.done + 1, 40)

        job = manager.submit("t", "有速度的作业", body)
        time.sleep(0.8)

        assert job.state == "running", "采样时作业已经结束了，测试没测到东西"
        eta = job.eta_seconds
        percent = job.percent

        manager.wait_idle(20)

        assert job.done > 0
        assert percent > 0, "进度百分比没动"
        assert eta is not None, "跑了一半还算不出剩余时间"
        assert eta > 0
    def test_log_seq_is_monotonic(self, manager: JobManager) -> None:
        """seq 是给界面做增量拉取的游标，必须单调递增。

        界面靠 ``since=<seq>`` 只取新行；seq 乱了就会丢行或者重复刷屏。
        """
        def body(handle):
            for i in range(20):
                handle.log(f"第 {i} 行")

        job = manager.submit("t", "序号", body)
        assert manager.wait_idle(10)

        seqs = [line.seq for line in job.log]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs), "seq 有重复"



# ──────────────────────────────────────────────────────────────────────
# 列表与历史
# ──────────────────────────────────────────────────────────────────────


class TestListing:
    def test_newest_first(self, manager: JobManager) -> None:
        a = manager.submit("t", "第一个", lambda h: None)
        b = manager.submit("t", "第二个", lambda h: None)
        assert manager.wait_idle(10)

        ids = [j.id for j in manager.list_jobs()]
        assert ids.index(b.id) < ids.index(a.id), "作业列表不是新的在前"

    def test_history_is_trimmed_but_keeps_unfinished(
        self, manager: JobManager
    ) -> None:
        """历史有上限，但**绝不能丢掉还没跑完的作业**。

        丢掉的后果是用户点了"取消"却找不到那个作业了。
        """
        from ipod_web import jobs as jobs_module

        release = threading.Event()
        held = manager.submit("t", "占着不走的", lambda h: release.wait(10))

        for i in range(jobs_module.MAX_HISTORY + 10):
            manager.submit("t", f"填充{i}", lambda h: None)

        # 未完成的必须还在
        assert manager.get(held.id) is not None, "未完成的作业被裁掉了"

        release.set()
        assert manager.wait_idle(20)

    def test_get_unknown_returns_none(self, manager: JobManager) -> None:
        assert manager.get("没有这个") is None

    def test_to_dict_has_chinese_state(self, manager: JobManager) -> None:
        job = manager.submit("t", "标题", lambda h: None)
        assert manager.wait_idle(10)

        data = job.to_dict()
        assert data["state_text"] == "已完成"
        assert data["title"] == "标题"
        assert "percent" in data and "elapsed" in data


class TestPerSongProgress:
    """逐首歌的进度。

    界面上的下载页要显示"下过哪些歌、哪几首失败了"，靠的就是这个。
    文本日志不能用：得去 parse "✓ [3/246] 歌名 → 7.2 MB" 才能拿到数字，
    改一次措辞就坏，歌名里带数字（《7/11》）还会被认成进度。
    """

    class _Song:
        def __init__(self, sid: int, name: str, artist: str = "歌手") -> None:
            self.id = sid
            self.name = name
            self.artist_text = artist

    def test_items_record_each_song(self, manager: JobManager) -> None:
        from ipod_cli.ncm.sync import (
            SONG_DONE,
            SONG_DOWNLOADING,
            SONG_FAILED,
            SongEvent,
        )

        ok = self._Song(1, "好歌", "好歌手")
        bad = self._Song(2, "坏歌", "坏歌手")

        def work(handle) -> dict:
            handle.on_song(SongEvent(1, 2, ok, SONG_DOWNLOADING))
            handle.on_song(SongEvent(1, 2, ok, SONG_DONE, size=7340032))
            handle.on_song(SongEvent(2, 2, bad, SONG_DOWNLOADING))
            handle.on_song(SongEvent(2, 2, bad, SONG_FAILED, reason="没版权"))
            return {}

        job = manager.submit("download", "下载", work)
        assert manager.wait_idle(20)

        data = job.to_dict()
        assert len(data["items"]) == 2
        first, second = data["items"]
        assert first["name"] == "好歌"
        assert first["artist"] == "好歌手"
        assert first["status"] == "done"
        assert first["size"] == 7340032
        assert second["status"] == "failed"
        assert second["reason"] == "没版权"
        assert data["items_done"] == 1
        assert data["items_failed"] == 1

    def test_downloading_song_shows_up_as_current(
        self, manager: JobManager
    ) -> None:
        """正在下的那首要能显示出来——大文件要等十几秒，
        界面上光转圈看不出在干嘛。"""
        from ipod_cli.ncm.sync import SONG_DOWNLOADING, SongEvent

        def work(handle) -> dict:
            handle.on_song(SongEvent(1, 1, self._Song(1, "大文件"), SONG_DOWNLOADING))
            return {}

        job = manager.submit("download", "下载", work)
        assert manager.wait_idle(20)

        assert job.to_dict()["current"] == "大文件"

    def test_items_are_copied_on_read(self, manager: JobManager) -> None:
        """★ 读出来的必须是快照。

        工作线程还在往里写，直接把 list/dict 的引用交出去的话，
        序列化到一半列表变长了，接口就吐出一份自相矛盾的数据。
        """
        from ipod_cli.ncm.sync import SONG_DONE, SongEvent

        def work(handle) -> dict:
            handle.on_song(SongEvent(1, 1, self._Song(1, "歌"), SONG_DONE, size=1))
            return {}

        job = manager.submit("download", "下载", work)
        assert manager.wait_idle(20)

        snapshot = job.to_dict()["items"]
        snapshot.append({"篡改": True})
        snapshot[0]["name"] = "被改了"

        assert len(job.to_dict()["items"]) == 1, "交出去的是引用而不是快照"
        assert job.to_dict()["items"][0]["name"] == "歌"

    def test_total_set_from_song_event(self, manager: JobManager) -> None:
        """没走 on_item 也要能把总数显示出来。"""
        from ipod_cli.ncm.sync import SONG_DOWNLOADING, SongEvent

        def work(handle) -> dict:
            for i in range(1, 4):
                handle.on_song(
                    SongEvent(i, 3, self._Song(i, f"歌{i}"), SONG_DOWNLOADING)
                )
            return {}

        job = manager.submit("download", "下载", work)
        assert manager.wait_idle(20)

        assert job.to_dict()["total"] == 3
        assert len(job.to_dict()["items"]) == 3


class TestJobHistoryPersistence:
    """作业日程落盘。

    这东西存在的意义就是"出 bug 时导出给开发看"。内存里的历史进程一重启
    就没了——而用户往往是重启一次之后才想起来要导出，导出来一片空白，
    等于没有这个功能。
    """

    @staticmethod
    def _run(mgr, path=None):
        job = mgr.submit("tracks", "读歌单", lambda h: {"count": 3})
        assert mgr.wait_idle(10)
        return job

    def test_finished_job_bytes_are_on_disk(self, tmp_path: Path) -> None:
        import json

        history = tmp_path / "logs" / "jobs.jsonl"
        mgr = JobManager(history_path=history)
        job = self._run(mgr)

        assert history.is_file(), "作业结束了却没落盘"
        record = json.loads(history.read_text(encoding="utf-8").strip())
        assert record["id"] == job.id
        assert record["title"] == "读歌单"
        assert record["state"] == "done"
        assert record["result"] == {"count": 3}
        assert any("完成" in line["text"] for line in record["log"])

    def test_history_survives_a_restart(self, tmp_path: Path) -> None:
        """★ 新开一个 manager（模拟重启）之后，日程还在。"""
        history = tmp_path / "logs" / "jobs.jsonl"

        first = JobManager(history_path=history)
        job = self._run(first)

        second = JobManager(history_path=history)
        restored = second.get(job.id)

        assert restored is not None, "重启之后日程没了——那导出就是空的"
        assert restored.title == "读歌单"
        assert restored.state == "done"
        assert restored.result == {"count": 3}
        assert restored.log, "日志也该一起回来"

    def test_restored_job_is_usable_not_just_readable(self, tmp_path: Path) -> None:
        """还原出来的作业要能正常序列化给界面，不能只是"能读"。"""
        history = tmp_path / "logs" / "jobs.jsonl"
        first = JobManager(history_path=history)
        first.submit("download", "下载歌单", lambda h: {"ok": True})
        assert first.wait_idle(5), "先等它跑完，不然还没落盘"

        mgr = JobManager(history_path=history)

        data = mgr.list_jobs()[0].to_dict()
        assert data["state_text"] == "已完成"
        assert data["elapsed"] >= 0

    def test_unfinished_jobs_are_not_resurrected_as_running(
        self, tmp_path: Path
    ) -> None:
        """★ 上次进程被杀时"进行中"的作业，重启后不能报成还在跑。

        报成进行中的话，界面上会永远转着圈等一个不存在的任务，
        状态条也会一直显示"正在下载"。
        """
        import json

        history = tmp_path / "logs" / "jobs.jsonl"
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(
            json.dumps(
                {
                    "id": "killed1",
                    "kind": "download",
                    "title": "下载到一半被杀",
                    "state": "running",
                    "total": 100,
                    "done": 30,
                    "items": [],
                    "log": [],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        mgr = JobManager(history_path=history)
        job = mgr.get("killed1")
        assert job is not None
        assert job.state == "cancelled", "被杀掉的作业不该显示成还在跑"
        assert not mgr.has_work

    def test_corrupt_lines_are_skipped(self, tmp_path: Path) -> None:
        """半截的写入（上一次正好被杀）不能把整个应用带崩。"""
        import json

        history = tmp_path / "logs" / "jobs.jsonl"
        history.parent.mkdir(parents=True, exist_ok=True)
        good = json.dumps(
            {"id": "ok1", "kind": "tracks", "title": "好的", "state": "done",
             "items": [], "log": []},
            ensure_ascii=False,
        )
        history.write_text(
            '{"id": "broken", "titl\n' + good + "\n不是 json\n",
            encoding="utf-8",
        )

        mgr = JobManager(history_path=history)
        assert mgr.get("ok1") is not None, "坏行后面的好记录也不该丢"

    def test_no_history_path_means_no_file(self, tmp_path: Path) -> None:
        """不给路径就不落盘（测试默认走这条，不往用户目录写东西）。"""
        mgr = JobManager()
        self._run(mgr)
        assert list(tmp_path.rglob("*.jsonl")) == []

    def test_compaction_keeps_the_file_bounded(self, tmp_path: Path) -> None:
        """追加式的文件会长到没边，压缩之后只剩最近的。"""
        import json

        history = tmp_path / "logs" / "jobs.jsonl"
        mgr = JobManager(history_path=history)
        for i in range(COMPACT_EVERY + 5):
            mgr.submit("tracks", f"第 {i} 个", lambda h: None)
        assert mgr.wait_idle(30)

        lines = [
            line
            for line in history.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) <= MAX_HISTORY + COMPACT_EVERY
        # 还能正常解析
        for line in lines:
            json.loads(line)

    def test_history_path_is_beside_the_state_db(self, tmp_path: Path) -> None:
        """日程跟状态库放一块儿——分开配置迟早错位。"""
        ctx = WebContext(store=StateStore(tmp_path / "sub" / "ncm.db"))
        assert ctx.jobs.history_path == tmp_path / "sub" / "logs" / "jobs.jsonl"

class TestEntrypointWiring:
    """真实启动路径必须把日程接到盘上。

    ★ 这一条是被真机打脸之后补的：`WebContext` 里明明接好了线，
    但 `main()` 自己传了个裸的 `JobManager()` 进来，把它整个绕过去。
    单元测试全绿（它们直接构造 JobManager 并传 history_path），
    真实入口却一条记录都没落盘——只有跑起来看才发现。
    """

    def test_cli_entry_wires_history_beside_the_db(self, tmp_path: Path,
                                                   monkeypatch) -> None:
        """`main()` 解析出来的参数，必须**一路传到**建上下文那一步。

        这里不直接调 ``build_context`` 就完事——那只证明"函数本身没问题"，
        证明不了"入口把值传对了"。实测踩过：`WebContext` 里接得好好的线，
        被入口自己传的裸 `JobManager()` 整个绕过去，单元测试全绿、真实入口
        一条记录都不落盘。

        所以用 monkeypatch 把阻塞的 ``serve`` 换成捕获器，走**真实的
        ``main()``**，既验参数传递、又不用真起 uvicorn。
        """
        from ipod_web import app as web_app

        db = tmp_path / "ncm.db"
        captured: dict = {}

        def fake_serve(**kw):
            captured.update(kw)
            return 0

        monkeypatch.setattr(web_app, "serve", fake_serve)

        assert web_app.main(["--db", str(db)]) == 0
        assert captured["db"] == str(db), "入口没把 --db 传下去"

        # `serve` 还收 port/log_file 这些，但建上下文用不到——只取它认的那几个
        ctx_kwargs = {
            k: v for k, v in captured.items()
            if k in {"db", "base_url", "ipod", "cache_dir", "data_dir"}
        }
        ctx = web_app.build_context(**ctx_kwargs)
        assert ctx.store.path == db
        assert ctx.jobs.history_path == tmp_path / "logs" / "jobs.jsonl", (
            "启动路径没把日程接到盘上——重启就丢，导出等于没有"
        )
        ctx.shutdown()

    def test_cli_entry_passes_data_dir_and_env_falls_back(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """数据目录：``--data-dir`` 参数优先，没传时读环境变量。

        嵌入模式全靠这条链路——宿主只给一个数据目录，状态库和缓存都要
        从它推导出来。传丢了就是"用户登录态凭空消失"。
        """
        from ipod_web import app as web_app
        from ipod_web.paths import DATA_DIR_ENV

        # ① 显式参数优先（环境变量也在，但不该被用）
        monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "来自环境变量"))
        captured: dict = {}
        monkeypatch.setattr(web_app, "serve", lambda **kw: captured.update(kw))
        web_app.main(["--data-dir", str(tmp_path / "来自参数")])
        assert captured["data_dir"] == tmp_path / "来自参数"

        # ② 没传参数 → 用环境变量
        captured.clear()
        web_app.main([])
        assert captured["data_dir"] == tmp_path / "来自环境变量"

        # ③ 两者都没有 → None（退回相对 cwd 的老行为）
        monkeypatch.delenv(DATA_DIR_ENV)
        captured.clear()
        web_app.main([])
        assert captured["data_dir"] is None

    def test_embedded_entry_derives_db_and_cache_from_data_dir(
        self, tmp_path: Path
    ) -> None:
        """嵌入模式：只给数据目录，状态库和缓存都得落在它下面。

        以前这两条都是相对 cwd 推的。cwd 在嵌入环境里不可靠（宿主那句
        `Directory.current = ...` 实测不生效），所以必须由数据目录显式推导。
        """
        from ipod_web.app import build_context
        from ipod_web.paths import db_and_cache

        data_dir = tmp_path / "应用支持目录"
        ctx = build_context(data_dir=data_dir)

        want_db, want_cache = db_and_cache(data_dir)
        assert ctx.store.path == want_db
        assert ctx.cache_dir == want_cache
        assert ctx.store.path.parent.parent == data_dir, "状态库没落在数据目录下"
        ctx.shutdown()
