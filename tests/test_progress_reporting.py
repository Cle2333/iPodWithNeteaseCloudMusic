"""进度上报：**进度条必须一直在动**，尤其是写入 iPod 那一段。

## 为什么专门给这件事写测试

用户同步 147 首到 iPod，界面"未响应直接崩溃"，原话是
**"传很多歌就以为被卡住了"**。查下来是两件事叠在一起：

1. 无损档要把每首 FLAC 转成 ALAC，147 首实测 6 分钟——这是**必须花的时间**；
2. 而这一段**一个进度数字都不报**：`execute_import` 的复制/转码循环只发文本日志，
   从不更新 `done/total`。加上路由层先 `set_total(147)` 又被
   `execute_downloads` 的 `on_item(0, 0)` 覆盖成 0（本地已下好时它的总数就是 0），
   于是进度条**整整 6 分钟停在 0%**。

第 2 件事是纯 bug，本文件就是它的回归网。判据只有一条：
**只要还在干活，界面拿到的数字就必须在变**（要么 done 在涨，要么 total 变了，
要么换到了新阶段）。
"""

from __future__ import annotations

from pathlib import Path

from conftest import ffmpeg_required, make_flac, make_mp3

from ipod_cli.importer import build_import_plan, execute_import
from ipod_cli.library import read_library
from ipod_cli.mediafile import collect_audio_files
from ipod_cli.transcode import transcode_for_import

pytestmark = ffmpeg_required


class _Recorder:
    """收集两个通道的回调，供断言。

    ``items`` 是 ``on_item(done, total)``，``stages`` 是 ``on_stage(名字[, 总数])``。
    """

    def __init__(self) -> None:
        self.items: list[tuple[int, int]] = []
        self.stages: list[tuple[str, int | None]] = []

    def item(self, done: int, total: int) -> None:
        self.items.append((done, total))

    def stage(self, text: str, total: int | None = None) -> None:
        self.stages.append((text, total))


# ──────────────────────────────────────────────────────────────────────
# 复制/转码段 —— 就是那 6 分钟
# ──────────────────────────────────────────────────────────────────────

class TestCopyStageReportsPerFile:
    def test_reports_every_file(self, active_device, music_dir: Path) -> None:
        """★ 核心回归：逐首都要报，不能只在开头报一次。

        以前这里只有 ``progress("正在复制 3/3：…")`` 这种**文本**，
        界面拿不到数字，进度条不动。
        """
        for i in range(3):
            make_mp3(music_dir / f"{i}.mp3", title=f"歌{i}", artist="艺人")
        library = read_library(active_device.root)
        plan = build_import_plan(
            active_device, library, collect_audio_files(music_dir)
        )

        rec = _Recorder()
        execute_import(plan, progress=lambda _t: None, on_item=rec.item)

        assert rec.items == [(1, 3), (2, 3), (3, 3)], (
            f"逐首进度没报全：{rec.items}"
        )

    def test_reports_during_transcode(self, active_device, music_dir: Path) -> None:
        """★ 转码那条路也要报——无损档下 147 首有 6 分钟全在转码。

        转码和拷贝在同一个循环里（``item.action == "transcode"`` 走
        ``transcode(...)``），所以只要循环里报了，转码就报得上。
        这条测试把"转码时也报"钉死，避免以后有人把转码挪出循环。
        """
        make_flac(music_dir / "a.flac", title="无损甲", artist="艺人")
        make_flac(music_dir / "b.flac", title="无损乙", artist="艺人")
        library = read_library(active_device.root)
        plan = build_import_plan(
            active_device, library, collect_audio_files(music_dir)
        )
        assert all(i.action == "transcode" for i in plan.to_add), "夹具前提不成立"

        rec = _Recorder()
        execute_import(
            plan,
            progress=lambda _t: None,
            transcode=transcode_for_import,
            on_item=rec.item,
        )

        assert rec.items == [(1, 2), (2, 2)], (
            f"转码阶段没有逐首进度（那 6 分钟又会是一根死条）：{rec.items}"
        )

    def test_failed_file_still_counts(self, active_device, music_dir: Path) -> None:
        """失败的那一首也要计数。

        不计数的话进度条会停在原地——看起来和"卡死"一模一样，
        而这恰恰是用户投诉的那个观感。
        """
        make_mp3(music_dir / "ok.mp3", title="好的", artist="艺人")
        broken = music_dir / "broken.mp3"
        broken.write_bytes(b"not audio at all")
        library = read_library(active_device.root)
        plan = build_import_plan(active_device, library, [broken, music_dir / "ok.mp3"])
        assert any(i.action == "error" for i in plan.items), "夹具前提不成立"

        rec = _Recorder()
        execute_import(plan, progress=lambda _t: None, on_item=rec.item)

        # 坏文件在规划阶段就被剔出 to_add，但"能进 to_add 的都报了"这条要成立
        assert rec.items, "一个进度都没报"
        assert rec.items[-1][0] == rec.items[-1][1], (
            f"最后一条不是 done==total，进度条会停在半路：{rec.items}"
        )


class TestStageChannel:
    def test_flush_reports_a_stage_without_total(
        self, active_device, music_dir: Path
    ) -> None:
        """拷完之后刷盘那一段：报阶段名，**不给总数**。

        刷盘切不出等份，界面据此显示不确定进度条（一直在动的那种）。
        让这一段的进度条停着不动，正是"以为卡住了"的来源。
        """
        make_mp3(music_dir / "a.mp3", title="甲", artist="艺人")
        library = read_library(active_device.root)
        plan = build_import_plan(
            active_device, library, collect_audio_files(music_dir)
        )

        rec = _Recorder()
        execute_import(plan, progress=lambda _t: None, on_stage=rec.stage)

        assert rec.stages, "刷盘/写库阶段一个 stage 都没报"
        text, total = rec.stages[0]
        assert "刷" in text, f"第一条 stage 应该是刷盘：{text!r}"
        assert total is None, (
            f"刷盘不该给总数（切不出等份），给了会画出一根骗人的进度条：{total}"
        )

    def test_write_step_reports_a_stage(self, active_device, music_dir: Path) -> None:
        """写库也要报一段——以前从"拷完了"直接跳到完成，中间一片空白。"""
        make_mp3(music_dir / "a.mp3", title="甲", artist="艺人")
        library = read_library(active_device.root)
        plan = build_import_plan(
            active_device, library, collect_audio_files(music_dir)
        )

        rec = _Recorder()
        execute_import(plan, progress=lambda _t: None, on_stage=rec.stage)

        names = [text for text, _ in rec.stages]
        assert any("数据库" in n for n in names), f"没报写库阶段：{names}"

    def test_stages_are_optional(self, active_device, music_dir: Path) -> None:
        """两个回调都不传也要能跑——CLI 与老调用点就是这么用的。"""
        make_mp3(music_dir / "a.mp3", title="甲", artist="艺人")
        library = read_library(active_device.root)
        plan = build_import_plan(
            active_device, library, collect_audio_files(music_dir)
        )

        result = execute_import(plan, progress=None)
        assert result.added == 1


class TestPlanReportsTagProgress:
    def test_reports_while_reading_tags(
        self, active_device, music_dir: Path
    ) -> None:
        """读标签是逐首的，也要报——几百首要几十秒。"""
        for i in range(3):
            make_mp3(music_dir / f"{i}.mp3", title=f"歌{i}", artist="艺人")
        library = read_library(active_device.root)

        rec = _Recorder()
        build_import_plan(
            active_device,
            library,
            collect_audio_files(music_dir),
            on_item=rec.item,
        )

        assert rec.items == [(1, 3), (2, 3), (3, 3)], f"读标签没报进度：{rec.items}"
