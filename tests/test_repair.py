"""设备修复：扫孤儿文件 / 断链记录 / 残留临时文件，并清理。

跑在**虚拟 iPod** 上（tmp_path 里的），不碰真机。

## 为什么这个文件值得单独存在

这套逻辑是**破坏性**的——它删文件、重写数据库。站得住脚的理由只有一条：
**绝不删错**。所以下面的用例有一半是在测"不该删的别删"：

* 路径比对必须折叠大小写（FAT32 不可靠）——按大小写敏感去比，会把正常
  文件判成孤儿然后删掉
* 只有数据库不引用的文件才算孤儿
* 扫完到删之间文件被外部删掉，不能崩、不能算成失败
* 每一条都断言"数据库引用的文件还在"
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ffmpeg_required, make_mp3

pytestmark = pytest.mark.usefixtures("info_logging")


@pytest.fixture
def ipod_ctx(web_store, ipod_root, tmp_path):
    from ipod_web.context import WebContext
    from ipod_web.jobs import JobManager

    return WebContext(
        store=web_store,
        jobs=JobManager(),
        ipod_path=str(ipod_root),
        cache_dir=tmp_path / "cache",
    )


@pytest.fixture
def ipod_client(ipod_ctx):
    from fastapi.testclient import TestClient

    from ipod_web.app import create_app

    with TestClient(create_app(ipod_ctx)) as c:
        yield c


def import_tracks(ipod_ctx, music_dir, count: int = 3):
    """往虚拟 iPod 里导入几首歌，返回导入后的库。"""
    from ipod_cli.importer import build_import_plan, execute_import

    files = [
        make_mp3(music_dir / f"t{i}.mp3", title=f"测试歌曲{i}", artist=f"歌手{i}")
        for i in range(count)
    ]
    library = ipod_ctx.library(force=True)
    plan = build_import_plan(ipod_ctx.device(), library, files)
    execute_import(plan)
    ipod_ctx.invalidate_library()
    return ipod_ctx.library(force=True)


def drop_orphan(ipod_root: Path, name: str = "ORPH.m4a", size: int = 4096) -> Path:
    """往 Music/ 里丢一个数据库不认的文件（= 同步中途失败的残留）。"""
    music = Path(ipod_root) / "iPod_Control" / "Music" / "F00"
    music.mkdir(parents=True, exist_ok=True)
    path = music / name
    path.write_bytes(b"\0" * size)
    return path


def wait_job(client, job_id: str, timeout: float = 60) -> dict:
    """轮询作业到落定。"""
    import time

    from ipod_web.jobs import JobManager  # noqa: F401  (仅为让类型提示可读)

    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        if last.get("state") in ("done", "failed", "cancelled"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"作业没在 {timeout} 秒内结束：{last.get('state')}")


def submit_and_wait(ipod_ctx, body_callable, timeout: float = 60) -> dict:
    """在**不经过 HTTP** 的情况下跑一次作业体，拿到结果。

    作业体是纯函数（收 ctx + handle），直接 `submit` 更省事，也避免
    HTTP 层的超时把慢用例弄红。
    """
    job = ipod_ctx.jobs.submit("test", "测试", body_callable)
    assert ipod_ctx.jobs.wait_idle(timeout)
    assert job.state == "done", f"作业失败了：{job.error}"
    return job.result


# ──────────────────────────────────────────────────────────────────────
# 路径归一化 —— 这一个函数错了，后果是"删掉好文件"
# ──────────────────────────────────────────────────────────────────────


class TestPathNorm:
    def test_database_and_disk_forms_match(self) -> None:
        """数据库存 ``:iPod_Control:Music:F00:X.mp3``，磁盘那边是斜杠形式。"""
        from ipod_cli.repair import _norm_rel

        assert _norm_rel(":iPod_Control:Music:F00:AB12.mp3") == _norm_rel(
            "iPod_Control/Music/F00/AB12.mp3"
        )

    def test_case_is_ignored(self) -> None:
        """★ **必须折叠大小写**。

        iPod 是 FAT32，同一个文件在不同场合回显的大小写可能不一致。
        按大小写敏感去比，正常文件会被判成孤儿，然后被删掉。
        """
        from ipod_cli.repair import _norm_rel

        assert _norm_rel(":iPod_Control:Music:F00:AB12.MP3") == _norm_rel(
            "iPod_Control/Music/F00/ab12.mp3"
        )

    def test_backslashes_are_normalized(self) -> None:
        from ipod_cli.repair import _norm_rel

        assert _norm_rel(r"iPod_Control\Music\F00\ab12.mp3") == _norm_rel(
            "iPod_Control/Music/F00/ab12.mp3"
        )


# ──────────────────────────────────────────────────────────────────────
# 扫描
# ──────────────────────────────────────────────────────────────────────


class TestScan:
    def test_clean_device_reports_nothing_to_fix(self, ipod_ctx, ipod_root) -> None:
        from ipod_cli.repair import scan_device

        scan = scan_device(ipod_ctx.device())

        assert scan.is_clean, f"干净的设备被报出问题：{scan.summary_text()}"
        assert scan.orphans == [] and scan.broken == [] and scan.stray_temp == []
        assert not scan.has_fixable

    @ffmpeg_required
    def test_imported_tracks_are_not_orphans(self, ipod_ctx, music_dir) -> None:
        """★ 导入过的文件**不是**孤儿——判错就等于下次修复把它删了。"""
        from ipod_cli.repair import scan_device

        library = import_tracks(ipod_ctx, music_dir, 3)
        assert len(library.tracks) == 3

        scan = scan_device(ipod_ctx.device())

        assert scan.db_tracks == 3
        assert scan.disk_files == 3
        assert scan.orphans == [], f"把正常文件判成孤儿了：{scan.orphans}"

    def test_detects_orphan_with_size_and_real_name(self, ipod_ctx, ipod_root) -> None:
        """孤儿要被认出来，而且**保留磁盘上的原始大小写**。

        折叠出来的小写路径拿去删在 FAT32 上能成功，但打印给用户看会让人
        以为文件叫这个名字。
        """
        from ipod_cli.repair import scan_device

        drop_orphan(ipod_root, "OrPhAn.M4A", size=8192)

        scan = scan_device(ipod_ctx.device())

        assert len(scan.orphans) == 1
        orphan = scan.orphans[0]
        assert orphan.size == 8192
        assert orphan.name == "OrPhAn.M4A", f"大小写被折叠了：{orphan.name}"
        assert orphan.rel == "iPod_Control/Music/F00/OrPhAn.M4A"
        assert scan.orphan_text.endswith("KB")
        assert scan.has_fixable and not scan.is_clean

    @ffmpeg_required
    def test_detects_broken_record(self, ipod_ctx, ipod_root, music_dir) -> None:
        """★ 反过来那一半：库里有记录、磁盘没文件（iPod 上显示得出来但播不了）。"""
        from ipod_cli.repair import scan_device

        library = import_tracks(ipod_ctx, music_dir, 3)
        # 把其中一首的文件从磁盘上抹掉（模拟被人手动删了）
        victim = library.tracks[0]
        rel = str(victim.location).strip(":").replace(":", "/")
        (Path(ipod_root) / rel).unlink()

        scan = scan_device(ipod_ctx.device())

        assert len(scan.broken) == 1
        assert scan.broken[0].db_track_id == victim.db_track_id
        assert scan.orphans == [], "被删的文件不该同时算成孤儿"
        assert "1 首有记录但没文件" in scan.summary_text()

    def test_detects_stray_temp(self, ipod_ctx, ipod_root) -> None:
        """残留临时文件：内核原子写入的 ``.iop-*.tmp`` 和别人留下的 ``*.tmp``。"""
        from ipod_cli.repair import scan_device

        itunes = Path(ipod_root) / "iPod_Control" / "iTunes"
        (itunes / "iT.tmp").write_bytes(b"x" * 16)
        (itunes / ".iop-abc123.tmp").write_bytes(b"x" * 16)
        artwork = Path(ipod_root) / "iPod_Control" / "Artwork"
        artwork.mkdir(parents=True, exist_ok=True)
        (artwork / ".iop-def456.tmp").write_bytes(b"x" * 16)

        scan = scan_device(ipod_ctx.device())

        names = sorted(Path(rel).name for rel in scan.stray_temp)
        assert names == [".iop-abc123.tmp", ".iop-def456.tmp", "iT.tmp"]
        assert len(scan.stray_temp) == 3


# ──────────────────────────────────────────────────────────────────────
# 清理
# ──────────────────────────────────────────────────────────────────────


class TestCleanOrphans:
    def test_removes_only_orphans(self, ipod_ctx, ipod_root, music_dir) -> None:
        """★★ **核心安全断言**：删孤儿不许碰数据库引用的文件。

        这条红了就是"修复功能删了用户的歌"——比不修还糟。
        """
        from ipod_cli.repair import clean_orphans, scan_device

        library = import_tracks(ipod_ctx, music_dir, 3)
        real_files = []
        for track in library.tracks:
            rel = str(track.location).strip(":").replace(":", "/")
            real_files.append(Path(ipod_root) / rel)
        assert all(p.is_file() for p in real_files)

        drop_orphan(ipod_root, "junk1.m4a", 1000)
        drop_orphan(ipod_root, "junk2.m4a", 2000)

        scan = scan_device(ipod_ctx.device())
        result = clean_orphans(ipod_ctx.device(), scan.orphans)

        assert result.removed == 2
        assert result.bytes_freed == 3000
        assert result.errors == []
        assert not (Path(ipod_root) / "iPod_Control/Music/F00/junk1.m4a").exists()
        assert not (Path(ipod_root) / "iPod_Control/Music/F00/junk2.m4a").exists()
        for path in real_files:
            assert path.is_file(), f"把数据库引用的文件删了：{path}"

        # 再扫一遍应该干净了
        assert scan_device(ipod_ctx.device()).is_clean

    def test_survives_a_file_that_vanished_meanwhile(
        self, ipod_ctx, ipod_root
    ) -> None:
        """扫到删之间文件被外部删掉：不崩、不算失败。

        真实场景：用户扫完看了列表，又在文件管理器里手动清了一部分，再点确认。
        """
        from ipod_cli.repair import clean_orphans, scan_device

        drop_orphan(ipod_root, "gone.m4a", 500)
        drop_orphan(ipod_root, "still.m4a", 700)

        scan = scan_device(ipod_ctx.device())
        assert len(scan.orphans) == 2

        # 用户手动删掉了一个
        (Path(ipod_root) / "iPod_Control/Music/F00/gone.m4a").unlink()

        result = clean_orphans(ipod_ctx.device(), scan.orphans)

        assert result.removed == 1, "已经不存在的那个不该被算成删成功"
        assert result.bytes_freed == 700
        assert result.errors == [], f"文件不存在不该报错：{result.errors}"

    def test_prunes_empty_dirs_but_keeps_music(self, ipod_ctx, ipod_root) -> None:
        """清完顺手收掉空目录，但 ``Music`` 本身永远留着（固件会检查它）。"""
        from ipod_cli.repair import clean_orphans, scan_device

        drop_orphan(ipod_root, "only.m4a", 100)
        music = Path(ipod_root) / "iPod_Control" / "Music"

        result = clean_orphans(ipod_ctx.device(), scan_device(ipod_ctx.device()).orphans)

        assert result.removed == 1
        assert music.is_dir(), "Music 目录被删了——有些固件靠它判断设备结构"
        assert list(music.iterdir()) == [], f"空子目录没收掉：{list(music.iterdir())}"

    def test_nothing_to_do_is_not_an_error(self, ipod_ctx) -> None:
        from ipod_cli.repair import clean_orphans

        result = clean_orphans(ipod_ctx.device(), [])

        assert result.removed == 0
        assert result.note == "没有可删的孤儿文件"


class TestCleanStrayTemp:
    def test_removes_temp_files(self, ipod_ctx, ipod_root) -> None:
        from ipod_cli.repair import clean_stray_temp, scan_device

        itunes = Path(ipod_root) / "iPod_Control" / "iTunes"
        (itunes / "iT.tmp").write_bytes(b"x" * 700)
        # 一个不是 .tmp 的普通文件：**不能被当成临时文件删掉**。
        # （别拿 iTunesDB 当这个角色——把它写坏会让 read_library 直接报错，
        # 那样测的就成了"数据库损坏"，不是"非临时文件要保住"。）
        keep = itunes / "iTunesDB.backup"
        before = keep.read_bytes() if keep.is_file() else b"keep"
        keep.write_bytes(before)

        scan = scan_device(ipod_ctx.device())
        result = clean_stray_temp(ipod_ctx.device(), scan.stray_temp)

        assert result.removed == 1
        assert result.bytes_freed == 700
        assert not (itunes / "iT.tmp").exists()
        assert keep.read_bytes() == before, "把不该删的文件删了"


class TestCleanBrokenRecords:
    @ffmpeg_required
    def test_removes_only_broken_records(self, ipod_ctx, ipod_root, music_dir) -> None:
        """★ 清断链记录 = 整库重写。要删对、留对，且读回校验通过。"""
        from ipod_cli.repair import clean_broken_records, scan_device

        library = import_tracks(ipod_ctx, music_dir, 3)
        victim = library.tracks[0]
        rel = str(victim.location).strip(":").replace(":", "/")
        (Path(ipod_root) / rel).unlink()

        scan = scan_device(ipod_ctx.device())
        assert len(scan.broken) == 1

        result = clean_broken_records(
            ipod_ctx.device(), scan.library, scan.broken
        )

        assert result.removed == 1
        assert result.errors == [], f"写库/校验出问题：{result.errors}"
        assert "校验通过" in result.note

        after = scan_device(ipod_ctx.device())
        assert after.db_tracks == 2, f"曲目数不对：{after.db_tracks}"
        assert after.broken == []
        assert after.orphans == [], "清记录不该留下孤儿文件"

    def test_no_broken_records_is_a_noop(self, ipod_ctx) -> None:
        from ipod_cli.repair import clean_broken_records

        library = ipod_ctx.library(force=True)
        result = clean_broken_records(ipod_ctx.device(), library, [])

        assert result.removed == 0
        assert result.note == "没有断链记录"


# ──────────────────────────────────────────────────────────────────────
# 接口
# ──────────────────────────────────────────────────────────────────────


class TestRepairRoutes:
    def test_scan_returns_the_shape_the_ui_needs(self, ipod_ctx, ipod_client, ipod_root) -> None:
        drop_orphan(ipod_root, "lone.m4a", size=2048)

        job = ipod_client.post("/api/repair/scan").json()
        final = wait_job(ipod_client, job["job_id"])

        assert final["state"] == "done"
        result = final["result"]
        assert result["clean"] is False
        assert result["orphans"]["count"] == 1
        assert result["orphans"]["items"][0]["name"] == "lone.m4a"
        assert result["orphans"]["bytes"] == 2048
        assert result["db_tracks"] == 0
        assert "孤儿" in result["summary"]

    def test_scan_says_clean_when_it_is(self, ipod_client) -> None:
        job = ipod_client.post("/api/repair/scan").json()
        final = wait_job(ipod_client, job["job_id"])

        assert final["result"]["clean"] is True
        assert "没有需要修复" in final["result"]["summary"]

    @ffmpeg_required
    def test_scan_broken_track_id_is_a_string(
        self, ipod_ctx, ipod_client, ipod_root, music_dir
    ) -> None:
        """★ ``db_track_id`` 必须是字符串。

        iPod 的持久 ID 是随机 64 位**无符号**数，真机里超过 2^63-1 的一抓一大把。
        当 JSON 数字发出去，Dart 的有符号 int 会**静默**溢出成负数。
        """
        library = import_tracks(ipod_ctx, music_dir, 2)
        rel = str(library.tracks[0].location).strip(":").replace(":", "/")
        (Path(ipod_root) / rel).unlink()

        job = ipod_client.post("/api/repair/scan").json()
        final = wait_job(ipod_client, job["job_id"])

        items = final["result"]["broken"]["items"]
        assert len(items) == 1
        assert isinstance(items[0]["id"], str), f"id 不是字符串：{items[0]['id']!r}"

    def test_clean_refuses_when_nothing_is_selected(self, ipod_client) -> None:
        """一个都没勾 → 明确拒绝，别默默"成功"。"""
        data = ipod_client.post("/api/repair/clean", json={}).json()

        assert data["ok"] is False
        assert "没有勾选" in data["message"]
        assert "job_id" not in data

    def test_clean_orphans_through_the_api(self, ipod_ctx, ipod_client, ipod_root) -> None:
        drop_orphan(ipod_root, "a.m4a", 1000)
        drop_orphan(ipod_root, "b.m4a", 2000)

        job = ipod_client.post(
            "/api/repair/clean", json={"orphans": True}
        ).json()
        final = wait_job(ipod_client, job["job_id"])

        assert final["state"] == "done"
        assert final["result"]["cleaned"] == 2
        assert final["result"]["freed_bytes"] == 3000
        assert not (Path(ipod_root) / "iPod_Control/Music/F00/a.m4a").exists()
        # 界面上"修复完成"那行日志要能看出清了什么
        assert any("孤儿" in line["text"] for line in final["log"])

    def test_clean_rescans_instead_of_trusting_the_client(
        self, ipod_ctx, ipod_client, ipod_root
    ) -> None:
        """★ 执行时**重新扫**，不用界面拿回来的清单。

        界面扫完到用户点确认之间可能过了几分钟，设备也许被外部改过。
        拿旧清单按路径删，删错的风险是真实的；重扫最多多花几秒。
        这里造的是"扫完之后又多出来一个孤儿"——重新扫就应该两个都清掉。
        """
        drop_orphan(ipod_root, "known.m4a", 100)

        job = ipod_client.post("/api/repair/scan").json()
        wait_job(ipod_client, job["job_id"])

        # 用户看完列表之后，设备上又多出一个孤儿（另一个工具、或者又失败了一次）
        drop_orphan(ipod_root, "appeared_later.m4a", 200)

        clean_job = ipod_client.post(
            "/api/repair/clean", json={"orphans": True}
        ).json()
        final = wait_job(ipod_client, clean_job["job_id"])

        assert final["result"]["cleaned"] == 2, "没有重新扫（只清了旧清单里的那个）"
        assert not (
            Path(ipod_root) / "iPod_Control/Music/F00/appeared_later.m4a"
        ).exists()

    def test_clean_temp_files_through_the_api(self, ipod_client, ipod_root) -> None:
        itunes = Path(ipod_root) / "iPod_Control" / "iTunes"
        (itunes / "iT.tmp").write_bytes(b"x" * 512)

        job = ipod_client.post(
            "/api/repair/clean", json={"stray_temp": True}
        ).json()
        final = wait_job(ipod_client, job["job_id"])

        assert final["result"]["cleaned"] == 1
        assert not (itunes / "iT.tmp").exists()

    def test_clean_logs_a_summary(self, ipod_client, ipod_root) -> None:
        """作业日志要能事后看出做了什么——出问题全靠它。"""
        drop_orphan(ipod_root, "x.m4a", 64)

        job = ipod_client.post("/api/repair/clean", json={"orphans": True}).json()
        final = wait_job(ipod_client, job["job_id"])

        texts = [line["text"] for line in final["log"]]
        assert any("重新核对" in t for t in texts), f"没写重扫这一步：{texts}"
        assert any("修复完成" in t for t in texts), f"没写总结：{texts}"

    def test_clean_with_nothing_to_do_is_not_a_failure(
        self, ipod_client
    ) -> None:
        job = ipod_client.post(
            "/api/repair/clean", json={"orphans": True}
        ).json()
        final = wait_job(ipod_client, job["job_id"])

        assert final["state"] == "done", f"没东西可清不该算失败：{final.get('error')}"
        assert final["result"]["cleaned"] == 0


class TestUnreadableDatabase:
    """数据库读不出来时，报错要说人话。

    内核在这种情况下抛的是 ``InsufficientDataError`` 这类底层异常，
    直接甩给用户等于没说。而"数据库读不出来"是个**需要用户动手**的状态
    （设备可能真坏了），消息里得带上下一步该干什么。
    """

    def test_scan_explains_an_unreadable_database(
        self, ipod_ctx, ipod_client, ipod_root
    ) -> None:
        db = Path(ipod_root) / "iPod_Control" / "iTunes" / "iTunesDB"
        db.write_bytes(b"xx")           # 变成一个解析不出来的坏文件

        job = ipod_client.post("/api/repair/scan").json()
        final = wait_job(ipod_client, job["job_id"])

        assert final["state"] == "failed", "坏数据库居然报成功了"
        error = final.get("error", "")
        assert "读不出 iPod 的数据库" in error, f"没给人话：{error}"
        assert "备份恢复" in error, f"没给下一步怎么办：{error}"
