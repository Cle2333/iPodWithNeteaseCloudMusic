"""调试路由的测试：后端日志、环境信息、环境自检。

这些接口是"打开黑箱"的那扇窗——后端被 app 自动拉起，用户看不到它的
控制台。坏了的话，出问题只能靠猜。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

# 调试日志相关的用例都要先把 root 调到 INFO，见 conftest 里的说明
pytestmark = pytest.mark.usefixtures("info_logging")


class TestDebugLog:
    def test_log_lines_flow_through(self, web_ctx, web_client) -> None:
        logging.getLogger("ipod_web.test").warning("一条测试日志")

        data = web_client.get("/api/debug/log?since=0").json()
        texts = [line["text"] for line in data["lines"]]
        assert "一条测试日志" in texts

    def test_log_is_incremental(self, web_ctx, web_client) -> None:
        """★ 增量拉取。全量重传的话，跑完一次全库同步界面就卡了。"""
        logging.getLogger("ipod_web.test").info("第一条")
        first = web_client.get("/api/debug/log?since=0").json()
        cursor = first["latest_seq"]
        assert any(line["text"] == "第一条" for line in first["lines"])

        fresh = web_client.get(f"/api/debug/log?since={cursor}").json()
        # 不能断言"结果为空"：httpx 自己会记一条 HTTP 请求日志，它落在
        # 游标之后，属于**新**行。要断言的是"旧行没有重传"。
        assert all(line["text"] != "第一条" for line in fresh["lines"]), (
            "增量拉取重复返回了旧行"
        )

        logging.getLogger("ipod_web.test").info("第二条")
        second = web_client.get(f"/api/debug/log?since={cursor}").json()
        texts = [line["text"] for line in second["lines"]]
        assert "第二条" in texts
        assert "第一条" not in texts

    def test_each_line_carries_seq_level_and_time(self, web_client) -> None:
        logging.getLogger("ipod_web.test").error("出错了")
        lines = web_client.get("/api/debug/log?since=0").json()["lines"]
        target = next(line for line in lines if line["text"] == "出错了")
        assert target["level"] == "ERROR"
        assert target["seq"] > 0
        assert target["ts"]

    def test_limit_is_capped(self, web_client) -> None:
        """界面上限由后端定，不能让前端要求一次返回一百万行。"""
        assert web_client.get("/api/debug/log?limit=999999").status_code == 422

    def test_clear_log(self, web_ctx, web_client) -> None:
        logging.getLogger("ipod_web.test").info("清空前")
        web_client.post("/api/debug/log/clear")
        data = web_client.get("/api/debug/log?since=0").json()
        assert all(line["text"] != "清空前" for line in data["lines"])

    def test_truncated_flag(self, web_ctx, web_client) -> None:
        """界面落后到缓冲区已经绕圈时，要如实说"你漏了行"。

        不报的话，用户会以为自己看到的是连续日志，然后基于缺失的信息排查。
        """
        from ipod_web.logbuf import LogBuffer

        small = LogBuffer(capacity=5)
        web_ctx.log_buffer = small
        lg = logging.getLogger("ipod_web.overflow")
        lg.handlers.clear()
        lg.propagate = False
        lg.addHandler(small)
        for i in range(20):
            lg.info("第 %d 行", i)

        data = web_client.get("/api/debug/log?since=0&limit=100").json()
        assert data["truncated"] is True


class TestDebugEnv:
    def test_env_reports_the_things_you_need_to_debug(self, web_ctx, web_client) -> None:
        data = web_client.get("/api/debug/env").json()
        for key in ("python", "ipod_cli", "platform", "db_path", "cache_dir", "base_url"):
            assert key in data, f"环境信息缺了 {key}"
        assert data["db_path"]
        assert "ipod-cli" not in data["ipod_cli"], "版本号带上了包名前缀"

    def test_ffmpeg_absence_is_not_an_error(self, web_ctx, web_client, monkeypatch) -> None:
        """没装 ffmpeg 是常见情况，接口不该挂，只是 ffmpeg_ok 为假。"""
        def boom():
            raise FileNotFoundError("ffmpeg")

        monkeypatch.setattr("ipod_cli.transcode.find_ffmpeg", boom)
        resp = web_client.get("/api/debug/env")
        assert resp.status_code == 200
        assert resp.json()["ffmpeg_ok"] is False


class TestDoctor:
    def test_doctor_runs_as_a_job(self, tmp_path, web_ctx, web_client) -> None:
        """★ 自检要碰设备（读 iTunesDB），必须走队列。

        跟正在进行的写入作业并行会读到半截状态，然后报一个不存在的故障。
        """
        # 指一个不存在的路径，让设备探测快速失败——不去扫真实磁盘
        web_ctx.ipod_path = str(tmp_path / "没有这个设备")

        resp = web_client.post("/api/debug/doctor")
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        assert web_ctx.jobs.wait_idle(20)
        detail = web_client.get(f"/api/jobs/{job_id}").json()
        assert detail["state"] in {"done", "failed"}

        texts = "\n".join(line["text"] for line in detail["log"])
        assert "环境自检" in texts
        assert "Python" in texts

    def test_doctor_output_mentions_the_device_problem(self, tmp_path, web_ctx, web_client) -> None:
        """没插设备时，自检输出要说明白，而不是静默失败。"""
        web_ctx.ipod_path = str(tmp_path / "没有这个设备")

        job_id = web_client.post("/api/debug/doctor").json()["job_id"]
        web_ctx.jobs.wait_idle(20)

        detail = web_client.get(f"/api/jobs/{job_id}").json()
        texts = "\n".join(line["text"] for line in detail["log"])
        assert "设备" in texts


class TestExportDiagnostics:
    """导出诊断记录。用户口述"界面上看着不对"没法定位问题，
    这个文件是他能发给别人的那份现场。"""

    @pytest.fixture
    def export_dir(self, tmp_path, monkeypatch):
        """别真往用户的"文档"目录里写。"""
        target = tmp_path / "exports"
        target.mkdir()
        monkeypatch.setattr(
            "ipod_web.routes.debug._export_dir", lambda: target
        )
        return target

    def test_export_writes_a_file(self, web_ctx, web_client, export_dir) -> None:
        resp = web_client.post("/api/debug/export")
        assert resp.status_code == 200

        data = resp.json()
        assert data["ok"] is True
        path = Path(data["path"])
        assert path.is_file(), "接口说导出了，文件却不在"
        assert path.parent == export_dir
        assert data["bytes"] > 0

    def test_file_has_every_section(self, web_client, export_dir) -> None:
        """环境 / 设置 / 设备 / 作业 / 后端日志——少一段就少一条线索。"""
        web_client.post("/api/debug/export")
        text = sorted(export_dir.glob("*.txt"))[0].read_text(encoding="utf-8-sig")

        for section in ("## 环境", "## 设置", "## 设备", "## 作业记录",
                        "## 后端日志"):
            assert section in text, f"导出里缺了「{section}」"

    def test_no_device_does_not_break_the_export(
        self, tmp_path, web_store, export_dir
    ) -> None:
        """没插设备是常见情况，导出不能因此失败。

        ★ 必须**强制**没有设备：这条以前是靠"跑测的这台机器恰好没插 iPod"
        过的。真机一插上就红——实测踩到，而且第一反应是"我改坏了什么"，
        白查一轮。**测试不该依赖跑测机器上插了什么硬件。**
        """
        from fastapi.testclient import TestClient

        from ipod_web.app import create_app
        from ipod_web.context import WebContext
        from ipod_web.jobs import JobManager

        ctx = WebContext(
            store=web_store,
            jobs=JobManager(),
            cache_dir=tmp_path / "cache",
            # 指一个不可能存在 iPod 的目录 → 必然 DeviceNotFoundError
            ipod_path=tmp_path / "这里没有 iPod",
        )
        with TestClient(create_app(ctx)) as client:
            assert client.post("/api/debug/export").status_code == 200

        text = sorted(export_dir.glob("*.txt"))[0].read_text(encoding="utf-8-sig")
        assert "读不到设备" in text

    def test_export_includes_job_and_per_song_results(
        self, web_ctx, web_client, export_dir
    ) -> None:
        """★ 作业里干了什么、每首歌成没成，是排查时最要紧的部分。"""
        web_ctx.jobs.submit("download", "下载「通勤歌单」", _fake_job)
        assert web_ctx.jobs.wait_idle(20)

        web_client.post("/api/debug/export")
        text = sorted(export_dir.glob("*.txt"))[0].read_text(encoding="utf-8-sig")

        assert "下载「通勤歌单」" in text
        assert "✓ [1] 好歌 - 好歌手" in text
        assert "✗ [2] 坏歌" in text
        assert "拿不到下载链接" in text, "失败原因没写进导出，等于没导"

    def test_file_is_utf8_with_bom(self, web_client, export_dir) -> None:
        """★ 必须是带 BOM 的 UTF-8。

        Windows 记事本按本地代码页猜编码，没 BOM 的话中文全是乱码——
        导出的东西看不懂就白导了。
        """
        web_client.post("/api/debug/export")
        raw = sorted(export_dir.glob("*.txt"))[0].read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), "少了 UTF-8 BOM"

    def test_export_does_not_queue_behind_other_jobs(
        self, web_ctx, web_client, export_dir
    ) -> None:
        """★ 导出**不走队列**。

        用户往往正是卡在某个作业上才来导出的，排在它后面就永远导不出来。
        """
        import threading

        release = threading.Event()

        def blocker(handle):
            handle.log("占着队列")
            release.wait(15)
            return {}

        web_ctx.jobs.submit("download", "一个很慢的作业", blocker)

        # 不等它——队列被占着的时候导出也得能用
        resp = web_client.post("/api/debug/export")
        assert resp.status_code == 200, "导出被前面的作业挡住了"

        release.set()
        web_ctx.jobs.wait_idle(20)


def _fake_job(handle) -> dict:
    """一个既有成功又有失败的作业，用来验证导出里逐首歌的结果。"""
    from ipod_cli.ncm.sync import (
        SONG_DONE,
        SONG_DOWNLOADING,
        SONG_FAILED,
        SongEvent,
    )

    class _Song:
        def __init__(self, sid, name, artist):
            self.id = sid
            self.name = name
            self.artist_text = artist

    good = _Song(1, "好歌", "好歌手")
    bad = _Song(2, "坏歌", "坏歌手")

    handle.on_song(SongEvent(1, 2, good, SONG_DOWNLOADING))
    handle.on_song(SongEvent(1, 2, good, SONG_DONE, size=7340032))
    handle.on_song(SongEvent(2, 2, bad, SONG_DOWNLOADING))
    handle.on_song(
        SongEvent(2, 2, bad, SONG_FAILED, reason="拿不到下载链接")
    )
    return {"downloaded": 1, "failed": 1}
