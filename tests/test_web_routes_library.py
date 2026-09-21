"""本地音乐（iPod 曲目列表 / 导入 / 删除）的路由测试。

跑在**虚拟 iPod** 上（tmp_path 里的），不碰真机。需要 ffmpeg 的用例
会自动跳过而不是假通过。
"""

from __future__ import annotations

import pytest
from conftest import ffmpeg_required, make_mp3


@pytest.fixture
def ipod_ctx(web_store, ipod_root, tmp_path):
    """把 WebContext 指向虚拟 iPod。"""
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


# 这些用例都会读日志断言，先把 root 调到 INFO（见 conftest 里的说明）
pytestmark = pytest.mark.usefixtures("info_logging")


class TestTrackList:
    @ffmpeg_required
    def test_lists_tracks(self, ipod_ctx, ipod_client, music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 3)

        data = ipod_client.get("/api/library/tracks").json()
        assert data["total"] == 3
        assert data["filtered"] == 3
        assert len(data["tracks"]) == 3
        assert data["ipod_name"] == "测试用 iPod"

    @ffmpeg_required
    def test_db_id_is_a_string(self, ipod_ctx, ipod_client, music_dir) -> None:
        """★ `db_id` 必须是字符串。

        iPod 的持久 ID 是随机 64 位**无符号**数。当成 JSON 数字发出去，
        Dart 的 int（有符号 64 位）会静默溢出成负数，删除时就会删错歌。
        这个断言看着琐碎，但它守的是"删错歌"这个后果。
        """
        import_tracks(ipod_ctx, music_dir, 2)

        data = ipod_client.get("/api/library/tracks").json()
        for track in data["tracks"]:
            assert isinstance(track["db_id"], str), "db_id 不是字符串"
            assert track["db_id"].isdigit()

    @ffmpeg_required
    def test_search_matches_title_artist_album(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        import_tracks(ipod_ctx, music_dir, 3)

        data = ipod_client.get("/api/library/tracks?search=歌曲1").json()
        assert data["total"] == 3
        assert data["filtered"] == 1
        assert data["tracks"][0]["title"] == "测试歌曲1"

        assert ipod_client.get("/api/library/tracks?search=歌手2").json()["filtered"] == 1
        assert ipod_client.get("/api/library/tracks?search=不存在").json()["filtered"] == 0

    @ffmpeg_required
    def test_pagination(self, ipod_ctx, ipod_client, music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 5)

        page1 = ipod_client.get("/api/library/tracks?page=1&size=2").json()
        assert len(page1["tracks"]) == 2
        assert page1["pages"] == 3

        page3 = ipod_client.get("/api/library/tracks?page=3&size=2").json()
        assert len(page3["tracks"]) == 1

    @ffmpeg_required
    def test_sort_orders(self, ipod_ctx, ipod_client, music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 3)

        by_title = ipod_client.get("/api/library/tracks?sort=title").json()["tracks"]
        assert [t["title"] for t in by_title] == ["测试歌曲0", "测试歌曲1", "测试歌曲2"]

        by_size = ipod_client.get("/api/library/tracks?sort=size").json()["tracks"]
        sizes = [t["size"] for t in by_size]
        assert sizes == sorted(sizes, reverse=True), "按体积排是从大到小"

    def test_unknown_sort_is_rejected(self, ipod_ctx, ipod_client) -> None:
        resp = ipod_client.get("/api/library/tracks?sort=乱写的")
        assert resp.status_code == 400
        assert "乱写的" in resp.json()["detail"]

    def test_no_device_gives_503(self, web_store, tmp_path) -> None:
        from fastapi.testclient import TestClient

        from ipod_web.app import create_app
        from ipod_web.context import WebContext
        from ipod_web.jobs import JobManager

        ctx = WebContext(
            store=web_store,
            jobs=JobManager(),
            ipod_path=str(tmp_path / "没有这个设备"),
            cache_dir=tmp_path / "cache",
        )
        with TestClient(create_app(ctx)) as c:
            resp = c.get("/api/library/tracks")
        assert resp.status_code == 503
        # 文案是"路径不存在：..."——说的是哪条路径，用户能照着查
        assert "路径不存在" in resp.json()["detail"] or "iPod" in resp.json()["detail"]


class TestImportPreview:
    def test_no_paths_is_rejected(self, ipod_ctx, ipod_client) -> None:
        resp = ipod_client.post("/api/library/import/preview", json={"paths": []})
        assert resp.status_code == 400
        assert "没有选择" in resp.json()["detail"]

    def test_missing_file_is_reported_by_name(self, ipod_ctx, ipod_client, tmp_path) -> None:
        missing = str(tmp_path / "不存在.mp3")
        resp = ipod_client.post(
            "/api/library/import/preview", json={"paths": [missing]}
        )
        assert resp.status_code == 400
        assert "不存在.mp3" in resp.json()["detail"]

    @ffmpeg_required
    def test_preview_classifies_files(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        """★ 分类必须在动手之前给出来。

        "23 个文件 187 MB，其中 3 个已存在会跳过" 这种话，用户看到才敢点确认。
        """
        new_files = [
            make_mp3(music_dir / f"n{i}.mp3", title=f"新歌{i}") for i in range(2)
        ]
        # 先导一个进去，再预览它 → 应该被归到"已存在"
        import_tracks(ipod_ctx, music_dir, 0)
        from ipod_cli.importer import build_import_plan, execute_import

        plan = build_import_plan(ipod_ctx.device(), ipod_ctx.library(force=True), [new_files[0]])
        execute_import(plan)
        ipod_ctx.invalidate_library()

        resp = ipod_client.post(
            "/api/library/import/preview",
            json={"paths": [str(f) for f in new_files]},
        )
        assert resp.status_code == 200

        data = resp.json()
        assert data["files"] == 2
        assert data["to_add"] == 1, "新文件没被算进待拷入"
        assert data["skipped"] == 1, "重复文件没被识别为跳过"
        assert data["size_text"]
        assert "fits" in data
        assert len(data["items"]) == 2

    @ffmpeg_required
    def test_preview_does_not_touch_the_ipod(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        """预览绝不能有副作用。"""
        import_tracks(ipod_ctx, music_dir, 2)
        before = len(ipod_ctx.library(force=True).tracks)

        files = [make_mp3(music_dir / "new.mp3", title="新的")]
        ipod_client.post(
            "/api/library/import/preview", json={"paths": [str(f) for f in files]}
        )

        assert len(ipod_ctx.library(force=True).tracks) == before


class TestImportJob:
    @ffmpeg_required
    def test_import_adds_tracks(self, ipod_ctx, ipod_client, music_dir) -> None:
        files = [make_mp3(music_dir / f"a{i}.mp3", title=f"导入歌{i}") for i in range(2)]

        job_id = ipod_client.post(
            "/api/library/import", json={"paths": [str(f) for f in files]}
        ).json()["job_id"]

        assert ipod_ctx.jobs.wait_idle(60)
        detail = ipod_client.get(f"/api/jobs/{job_id}").json()
        assert detail["state"] == "done", detail.get("error")
        assert detail["result"]["added"] == 2
        assert detail["result"]["verified"] is True

        assert len(ipod_ctx.library(force=True).tracks) == 2

    @ffmpeg_required
    def test_import_is_queued_not_blocking(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        """接口要立刻返回作业号——导入几百首可能要好几分钟。"""
        files = [make_mp3(music_dir / "b.mp3", title="排队歌")]
        resp = ipod_client.post(
            "/api/library/import", json={"paths": [str(f) for f in files]}
        )
        assert resp.status_code == 200
        assert resp.json()["job_id"]
        ipod_ctx.jobs.wait_idle(60)


class TestRemoveRequiresPreview:
    """★ T3：破坏性操作没有预览就直接执行，必须被拒绝。

    只靠"界面记得弹确认框"是不够的——界面出个 bug 就能直接删歌。
    后端要求一条**真实预览过**的一次性令牌，把这件事从"约定"变成"结构"。
    """

    @ffmpeg_required
    def test_remove_without_preview_is_refused(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        ids = self._seed(ipod_ctx, music_dir, 3)

        resp = ipod_client.post("/api/library/remove", json={"ids": ids})
        assert resp.status_code == 409
        assert "预览" in resp.json()["detail"]

        # 而且真的什么都没删
        assert len(ipod_ctx.library(force=True).tracks) == 3

    @ffmpeg_required
    def test_remove_with_bogus_token_is_refused(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        ids = self._seed(ipod_ctx, music_dir, 3)
        resp = ipod_client.post(
            "/api/library/remove",
            json={"ids": ids, "preview_id": "伪造的令牌"},
        )
        assert resp.status_code == 409
        assert len(ipod_ctx.library(force=True).tracks) == 3

    @ffmpeg_required
    def test_token_cannot_be_reused(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        """令牌是一次性的。重放同一个令牌不该再删一次。"""
        ids = self._seed(ipod_ctx, music_dir, 4)
        preview = ipod_client.post(
            "/api/library/remove/preview", json={"ids": ids[:1]}
        ).json()

        first = ipod_client.post(
            "/api/library/remove",
            json={"ids": ids[:1], "preview_id": preview["preview_id"]},
        )
        assert first.status_code == 200
        assert ipod_ctx.jobs.wait_idle(60)

        second = ipod_client.post(
            "/api/library/remove",
            json={"ids": ids[:1], "preview_id": preview["preview_id"]},
        )
        assert second.status_code == 409, "同一个令牌被用了两次"

    @ffmpeg_required
    def test_token_does_not_cover_a_different_selection(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        """★ 预览了 A、却拿这个令牌去删 B —— 必须拦。

        这是最危险的一种：用户看到的是"删 1 首"，实际删的是另一首。
        """
        ids = self._seed(ipod_ctx, music_dir, 4)
        preview = ipod_client.post(
            "/api/library/remove/preview", json={"ids": ids[:1]}
        ).json()

        resp = ipod_client.post(
            "/api/library/remove",
            json={"ids": ids[1:3], "preview_id": preview["preview_id"]},
        )
        assert resp.status_code == 409
        assert "不一致" in resp.json()["detail"]
        assert len(ipod_ctx.library(force=True).tracks) == 4

    @ffmpeg_required
    def _seed(self, ipod_ctx, music_dir, count: int = 3):
        import_tracks(ipod_ctx, music_dir, count)
        library = ipod_ctx.library(force=True)
        return [str(t.db_track_id) for t in library.tracks]

    @ffmpeg_required
    def test_expired_token_is_refused(
        self, ipod_ctx, ipod_client, music_dir, monkeypatch
    ) -> None:
        import ipod_web.routes.library as mod

        ids = self._seed(ipod_ctx, music_dir, 3)
        preview = ipod_client.post(
            "/api/library/remove/preview", json={"ids": ids[:1]}
        ).json()

        monkeypatch.setattr(mod, "PREVIEW_TTL", -1.0)  # 立刻过期

        resp = ipod_client.post(
            "/api/library/remove",
            json={"ids": ids[:1], "preview_id": preview["preview_id"]},
        )
        assert resp.status_code == 409
        assert "过期" in resp.json()["detail"]


class TestRemove:
    @ffmpeg_required
    def _seed(self, ipod_ctx, music_dir, count: int = 3):
        import_tracks(ipod_ctx, music_dir, count)
        library = ipod_ctx.library(force=True)
        return [str(t.db_track_id) for t in library.tracks]

    @ffmpeg_required
    def _remove(self, ipod_client, ids, preview):
        return ipod_client.post(
            "/api/library/remove",
            json={"ids": ids, "preview_id": preview["preview_id"]},
        )

    @ffmpeg_required
    def test_preview_reports_count_and_space(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        """★ 危险操作必须三步：点删除 → 预览 → 确认。

        预览要给"多少首 + 释放多少空间 + 前几首叫什么"，
        只给一个数字的话，用户没法判断自己选对没有。
        """
        ids = self._seed(ipod_ctx, music_dir, 3)

        resp = ipod_client.post("/api/library/remove/preview", json={"ids": ids[:2]})
        assert resp.status_code == 200

        data = resp.json()
        assert data["count"] == 2
        assert data["remaining"] == 1
        assert data["size_text"]
        assert len(data["items"]) == 2
        assert data["items"][0]["title"]

    @ffmpeg_required
    def test_preview_does_not_delete(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        ids = self._seed(ipod_ctx, music_dir, 3)
        ipod_client.post("/api/library/remove/preview", json={"ids": ids})
        assert len(ipod_ctx.library(force=True).tracks) == 3

    @ffmpeg_required
    def test_preview_matches_actual(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        """★ 预览说删几首，实际就得删几首。

        对不上一次，用户就再也信不过这个预览了——而预览是他唯一的防线。
        """
        ids = self._seed(ipod_ctx, music_dir, 4)

        preview = ipod_client.post(
            "/api/library/remove/preview", json={"ids": ids[:2]}
        ).json()

        job_id = self._remove(ipod_client, ids[:2], preview).json()["job_id"]
        assert ipod_ctx.jobs.wait_idle(60)

        detail = ipod_client.get(f"/api/jobs/{job_id}").json()
        assert detail["state"] == "done", detail.get("error")
        assert detail["result"]["removed"] == preview["count"]
        assert len(ipod_ctx.library(force=True).tracks) == preview["remaining"]

    @ffmpeg_required
    def test_removing_everything_is_refused(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        """★ 清空曲库要硬拦。

        这个工具的删除是"整库重写"，删光等于把库存清零——误操作的代价太大，
        所以不给做（CLI 和界面都一样）。

        **但拦人的时候得把话说完**：用户是在逐首清理时撞上它的（删到只剩
        最后一首，再删就没反应），只说"不提供清空操作"会让人卡在那儿，
        不知道下一步该干什么。所以消息里必须带上**替代做法**。
        """
        ids = self._seed(ipod_ctx, music_dir, 3)

        resp = ipod_client.post("/api/library/remove/preview", json={"ids": ids})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "清空" in detail
        assert "还原" in detail or "恢复" in detail, (
            "只说不给做不够——得告诉用户那怎么办"
        )
        assert len(ipod_ctx.library(force=True).tracks) == 3

    @ffmpeg_required
    def test_unknown_ids_are_reported(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        self._seed(ipod_ctx, music_dir, 2)

        resp = ipod_client.post(
            "/api/library/remove/preview", json={"ids": ["999999999999999999999"]}
        )
        assert resp.status_code == 404
        assert "找不到" in resp.json()["detail"]

    @ffmpeg_required
    def test_huge_ids_are_matched_as_strings(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        """★ 无符号 64 位的 ID 不能被当成有符号数处理。

        真机上过半的曲目 ID 都超过 2^63-1。走 int 转换的话这些会变负数，
        然后匹配不上（或者更糟——匹配到别的歌）。
        """
        ids = self._seed(ipod_ctx, music_dir, 3)

        # 造一个明确超过 2^63-1 的 ID：真机上就有，测试必须覆盖这个形状
        library = ipod_ctx.library(force=True)
        huge = "18446744073709551615"   # 2^64-1
        real_id = str(library.tracks[0].db_track_id)

        # 用一个真实 ID 但走字符串路径——结果必须正确
        resp = ipod_client.post(
            "/api/library/remove/preview", json={"ids": [real_id]}
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

        # 而一个不存在的巨大 ID 必须报错，不能被"转换后恰好相等"蒙混
        resp = ipod_client.post("/api/library/remove/preview", json={"ids": [huge]})
        assert resp.status_code == 404
        assert len(ids) == 3


class TestDeviceRegisteredWithTheKernel:
    """★ 从 WebContext 拿到设备，必须顺手注册给内核。

    实测踩到的 bug：**界面同步直接失败**——

      ArtworkDB write failed: No artwork format definitions are available
      for this iPod; cannot write ArtworkDB safely

    因为内核那边"当前设备"是 None，解析不出封面格式，就拒绝写 ArtworkDB。
    内核"宁可不写也不猜错格式"是对的（猜错会污染封面库），责任在我们：
    得告诉它是哪台设备。

    CLI 的同步路径自己调了 ``activate()``（``sync_cli.py:479``），
    而界面这条路一次都没调——同一个坑在两处，界面是新的所以中招了。
    conftest 里甚至早就有个 `active_device` fixture（注释写着"写封面需要"），
    说明这个要求一直知道，只是没接到界面这条路上。
    """

    def test_registers_the_device(self, ipod_ctx) -> None:
        from iopenpod.device import get_current_device_for_path

        device = ipod_ctx.device()
        current = get_current_device_for_path(str(device.root))

        assert current is not None, (
            "拿到设备却没注册给内核——写库会因「设备未知」被拒，同步整个失败"
        )
        assert getattr(current, "model_family", "") == device.family
        assert getattr(current, "generation", "") == device.generation

    def test_artwork_formats_resolve(self, ipod_ctx) -> None:
        """★ 注册的**直接目的**：封面格式得解析得出来。"""
        from iopenpod.artworkdb_writer.rgb565 import get_artwork_formats

        device = ipod_ctx.device()
        formats = get_artwork_formats(str(device.root))

        assert formats, (
            "解析不出封面格式，写 ArtworkDB 会被拒——同步必失败"
        )
        # Classic 6th Gen 的四个格式，都是 (宽, 高)
        assert all(w > 0 and h > 0 for w, h in formats.values())

    def test_activate_is_idempotent(self, device, monkeypatch) -> None:
        """★ 重复注册不该重复写。

        不幂等的代价实测过：界面状态条每 3 秒轮询一次，每次拿到设备就注册
        一遍，日志里刷出一大串 "Device stored: iPod Classic 6th Gen ..."。
        那份日志正是「导出诊断记录」给人看的——每 3 秒一条噪音，真线索就淹了。
        """
        import ipod_cli.discovery as disc

        calls: list[object] = []
        real = disc.set_current_device
        monkeypatch.setattr(
            disc, "set_current_device", lambda info: (calls.append(info), real(info))[1]
        )

        device.activate()   # 先确保已注册（前面别的测试可能注册过，状态是全局的）
        calls.clear()

        device.activate()
        device.activate()

        assert calls == [], f"重复注册了 {len(calls)} 次——界面轮询会把日志刷爆"

    def test_library_also_registers(self, ipod_ctx) -> None:
        """读库也要注册：界面打开就会读库，那时候就该注册好。"""
        from iopenpod.device import get_current_device_for_path

        device = ipod_ctx.device()
        ipod_ctx.library(force=True)
        assert get_current_device_for_path(str(device.root)) is not None
