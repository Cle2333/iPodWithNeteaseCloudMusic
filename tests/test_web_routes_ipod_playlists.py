"""iPod 歌单管理（查看 / 新建 / 改名 / 删除 / 增删成员）的路由测试。

跑在**虚拟 iPod** 上（tmp_path 里的），不碰真机。需要 ffmpeg 的用例自动跳过。

这里守的是几件"错了会毁用户数据"的事：

* **删除必须带真实预览过的令牌**——不是指望界面弹确认框，是后端拒绝
* **主播放列表不能删**——iPod 的名字就存在它的标题里
* **智能 / 播客播放列表只读**——成员是算出来的，手改没意义
* **新建重名要拒绝**，不能静默覆盖掉用户原来的歌单
"""

from __future__ import annotations

import time

import pytest
from conftest import ffmpeg_required, make_mp3


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
    """往虚拟 iPod 里导入几首歌（歌单操作要有曲目才有意义）。"""
    from ipod_cli.importer import build_import_plan, execute_import

    files = [
        make_mp3(music_dir / f"p{i}.mp3", title=f"歌单测试{i}", artist=f"歌手{i}")
        for i in range(count)
    ]
    library = ipod_ctx.library(force=True)
    plan = build_import_plan(ipod_ctx.device(), library, files)
    execute_import(plan)
    ipod_ctx.invalidate_library()
    return ipod_ctx.library(force=True)


def run_job(client, job_id: str, timeout: float = 120.0) -> dict:
    """等作业跑完，返回它的详情。"""
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = client.get(f"/api/jobs/{job_id}").json()
        if detail.get("state") in ("done", "failed", "cancelled"):
            return detail
        time.sleep(0.2)
    return detail


def submit(client, path: str, payload: dict | None = None) -> dict:
    """提交一个写操作并等它跑完。"""
    resp = client.post(path, json=payload or {})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("job_id"), body
    detail = run_job(client, body["job_id"])
    assert detail.get("state") == "done", detail
    return detail.get("result") or {}


def all_ids(ipod_ctx) -> list[str]:
    return [str(t.db_track_id) for t in ipod_ctx.library(force=True).tracks]


def playlist_by_name(client, name: str) -> dict:
    data = client.get("/api/library/playlists").json()
    for item in data["playlists"]:
        if item["name"] == name:
            return item
    raise AssertionError(f"没有叫 {name!r} 的歌单：{[p['name'] for p in data['playlists']]}")


pytestmark = pytest.mark.usefixtures("info_logging")


class TestList:
    @ffmpeg_required
    def test_lists_master_first(self, ipod_ctx, ipod_client, music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 3)

        data = ipod_client.get("/api/library/playlists").json()

        assert data["ok"] is True
        names = [p["name"] for p in data["playlists"]]
        assert "测试用 iPod" in names, f"主播放列表应该在里面：{names}"
        # 主列表排最前，且被标出来
        assert data["playlists"][0]["name"] == "测试用 iPod"
        assert data["playlists"][0]["is_master"] is True
        assert data["master_id"] == data["playlists"][0]["playlist_id"]
        assert data["track_count"] == 3

    @ffmpeg_required
    def test_master_is_not_deletable(self, ipod_ctx, ipod_client, music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 2)
        master = ipod_client.get("/api/library/playlists").json()["master_id"]

        resp = ipod_client.post("/api/library/playlists/delete/preview",
                                json={"playlist_id": master})

        assert resp.status_code == 409, resp.text
        assert "主播放列表" in resp.json()["detail"]
        # 而且说清了为什么
        assert "名字" in resp.json()["detail"]


class TestCreate:
    @ffmpeg_required
    def test_create_then_visible(self, ipod_ctx, ipod_client, music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 3)

        result = submit(ipod_client, "/api/library/playlists/create",
                        {"name": "我的新歌单"})

        assert result["verified"] is True
        item = playlist_by_name(ipod_client, "我的新歌单")
        assert item["count"] == 0
        assert item["editable"] is True
        assert item["dataset"] == "mhlp"

    @ffmpeg_required
    def test_create_with_initial_tracks(self, ipod_ctx, ipod_client, music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 3)
        ids = all_ids(ipod_ctx)[:2]

        submit(ipod_client, "/api/library/playlists/create",
               {"name": "带歌的", "track_ids": ids})

        item = playlist_by_name(ipod_client, "带歌的")
        assert item["count"] == 2

    @ffmpeg_required
    def test_duplicate_name_is_rejected(self, ipod_ctx, ipod_client, music_dir) -> None:
        """★ 重名必须拒绝，不能静默覆盖——那会毁掉用户原来的歌单内容。"""
        import_tracks(ipod_ctx, music_dir, 3)
        submit(ipod_client, "/api/library/playlists/create",
               {"name": "重名测试", "track_ids": all_ids(ipod_ctx)[:2]})

        resp = ipod_client.post("/api/library/playlists/create",
                                json={"name": "重名测试"})

        assert resp.status_code == 409, resp.text
        assert "已经有一个" in resp.json()["detail"]
        # 原来的内容没被动过
        assert playlist_by_name(ipod_client, "重名测试")["count"] == 2

    @ffmpeg_required
    def test_empty_name_is_rejected(self, ipod_client) -> None:
        resp = ipod_client.post("/api/library/playlists/create", json={"name": "   "})
        assert resp.status_code == 400
        assert "不能为空" in resp.json()["detail"]


class TestNoPodcastClone:
    """★ 回归：普通歌单不能被克隆进播客数据集。

    `write_itunesdb` 的语义是 `podcast_playlists=None` → **把 dataset 2 的歌单
    克隆一份到 dataset 3**（这是 libgpod 新建库时的兼容行为）。而
    `dbwrite._build_playlist_args` 原本只在 `ds3_playlists` 非空时才传这个参数，
    于是"设备上没有非主播客歌单"（常态）就会触发克隆：

    * 每写一次库，普通歌单就在播客数据集里多一份
    * 界面上同一个歌单显示两条
    * 改名/删除只动得掉普通那份，播客那份留下 —— 看起来像"删了没删掉"

    这个 bug 影响的不只是歌单管理，**同步和导入走的是同一条写库路径**。
    """

    @ffmpeg_required
    def test_create_does_not_clone_into_podcast(self, ipod_ctx, ipod_client,
                                                music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 3)

        submit(ipod_client, "/api/library/playlists/create",
               {"name": "不该被克隆", "track_ids": all_ids(ipod_ctx)[:1]})

        library = ipod_ctx.library(force=True)
        podcast_rows = library.raw.get("mhlp_podcast") or []
        names = [str(r.get("Title") or "") for r in podcast_rows]
        assert "不该被克隆" not in names, (
            f"普通歌单被克隆进播客数据集了：{names}"
        )
        # 播客数据集里应该只剩它自己的主列表
        assert len(podcast_rows) == 1, f"播客数据集多出了东西：{names}"

    @ffmpeg_required
    def test_import_does_not_clone_either(self, ipod_ctx, ipod_client,
                                          music_dir) -> None:
        """导入也走同一条写库路径，同样不能被克隆。"""
        import_tracks(ipod_ctx, music_dir, 2)
        submit(ipod_client, "/api/library/playlists/create", {"name": "甲"})
        submit(ipod_client, "/api/library/playlists/create", {"name": "乙"})

        library = ipod_ctx.library(force=True)
        podcast_names = [str(r.get("Title") or "")
                         for r in (library.raw.get("mhlp_podcast") or [])]
        assert "甲" not in podcast_names and "乙" not in podcast_names, (
            f"歌单被克隆到播客数据集：{podcast_names}"
        )

    @ffmpeg_required
    def test_podcast_master_is_not_editable(self, ipod_ctx, ipod_client,
                                            music_dir) -> None:
        """两个数据集各有主列表，标题都是 iPod 的名字——两个都不可动。

        所以界面上看到两条同名是**设备的真实结构**，靠 `dataset` 区分。
        """
        import_tracks(ipod_ctx, music_dir, 2)
        data = ipod_client.get("/api/library/playlists").json()

        masters = [p for p in data["playlists"] if p["is_master"]]
        assert len(masters) == 2, f"应该有两个主列表（普通 + 播客）：{masters}"
        assert {p["dataset"] for p in masters} == {"mhlp", "mhlp_podcast"}
        for item in masters:
            assert item["editable"] is False
        # 只有普通那个是"设备名字的载体"
        assert [p["is_device_name"] for p in masters].count(True) == 1

        for item in masters:
            resp = ipod_client.post("/api/library/playlists/delete/preview",
                                    json={"playlist_id": item["playlist_id"]})
            assert resp.status_code == 409, f"{item['dataset']} 的主列表不该可删"


class TestRename:
    @ffmpeg_required
    def test_rename_keeps_members(self, ipod_ctx, ipod_client, music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 3)
        submit(ipod_client, "/api/library/playlists/create",
               {"name": "旧名字", "track_ids": all_ids(ipod_ctx)[:2]})
        target = playlist_by_name(ipod_client, "旧名字")

        submit(ipod_client, f"/api/library/playlists/{target['playlist_id']}/rename",
               {"name": "新名字"})

        item = playlist_by_name(ipod_client, "新名字")
        assert item["count"] == 2, "改名不该动成员"
        names = [p["name"] for p in
                 ipod_client.get("/api/library/playlists").json()["playlists"]]
        assert "旧名字" not in names, f"改名后旧名字不该还在：{names}"

    @ffmpeg_required
    def test_rename_keeps_the_playlist_id(self, ipod_ctx, ipod_client,
                                          music_dir) -> None:
        """★ 改名是"同一个播放列表换个名字"，**id 必须不变**。

        变了的话：界面上刚拿到的 id 立刻失效（下一步操作 404——实测踩到），
        设备上按 id 引用它的东西也断了。
        """
        import_tracks(ipod_ctx, music_dir, 2)
        submit(ipod_client, "/api/library/playlists/create",
               {"name": "原名", "track_ids": all_ids(ipod_ctx)[:1]})
        before = playlist_by_name(ipod_client, "原名")["playlist_id"]

        submit(ipod_client, f"/api/library/playlists/{before}/rename",
               {"name": "改成这个"})

        after = playlist_by_name(ipod_client, "改成这个")
        assert after["playlist_id"] == before, (
            f"改名换了身份：{before} → {after['playlist_id']}"
        )
        # 而且拿旧 id 还能直接读到它
        detail = ipod_client.get(f"/api/library/playlists/{before}/tracks").json()
        assert detail["name"] == "改成这个"
        assert detail["count"] == 1

    @ffmpeg_required
    def test_edit_tracks_keeps_the_playlist_id(self, ipod_ctx, ipod_client,
                                               music_dir) -> None:
        """改成员同样不能换身份。"""
        import_tracks(ipod_ctx, music_dir, 3)
        ids = all_ids(ipod_ctx)
        submit(ipod_client, "/api/library/playlists/create", {"name": "身份"})
        before = playlist_by_name(ipod_client, "身份")["playlist_id"]

        submit(ipod_client, f"/api/library/playlists/{before}/tracks",
               {"add": ids[:2]})

        after = playlist_by_name(ipod_client, "身份")
        assert after["playlist_id"] == before, "加歌换了身份"
        assert after["count"] == 2

    @ffmpeg_required
    def test_rename_to_existing_name_is_rejected(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        import_tracks(ipod_ctx, music_dir, 3)
        submit(ipod_client, "/api/library/playlists/create", {"name": "甲"})
        target = playlist_by_name(ipod_client, "甲")
        submit(ipod_client, "/api/library/playlists/create", {"name": "乙"})

        resp = ipod_client.post(
            f"/api/library/playlists/{target['playlist_id']}/rename",
            json={"name": "乙"},
        )
        assert resp.status_code == 409
        assert "已经有一个" in resp.json()["detail"]

    @ffmpeg_required
    def test_master_cannot_be_renamed(self, ipod_ctx, ipod_client, music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 2)
        master = ipod_client.get("/api/library/playlists").json()["master_id"]

        resp = ipod_client.post(f"/api/library/playlists/{master}/rename",
                                json={"name": "改个名"})

        assert resp.status_code == 409
        assert "iPod 的名字" in resp.json()["detail"]


class TestDelete:
    @ffmpeg_required
    def test_delete_without_preview_is_refused(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        """★★ 结构性保证：没有真实预览过的令牌，后端直接拒绝。

        这条不依赖界面记得弹确认框——界面漏了确认也删不掉。
        """
        import_tracks(ipod_ctx, music_dir, 3)
        submit(ipod_client, "/api/library/playlists/create", {"name": "要删的"})
        target = playlist_by_name(ipod_client, "要删的")

        resp = ipod_client.post("/api/library/playlists/delete",
                                json={"playlist_id": target["playlist_id"],
                                      "preview_id": "伪造的令牌"})

        assert resp.status_code == 409, resp.text
        assert "预览" in resp.json()["detail"]
        # 歌单还在
        playlist_by_name(ipod_client, "要删的")

    @ffmpeg_required
    def test_preview_then_delete(self, ipod_ctx, ipod_client, music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 3)
        ids = all_ids(ipod_ctx)
        submit(ipod_client, "/api/library/playlists/create",
               {"name": "待删歌单", "track_ids": ids[:2]})
        target = playlist_by_name(ipod_client, "待删歌单")

        preview = ipod_client.post("/api/library/playlists/delete/preview",
                                   json={"playlist_id": target["playlist_id"]}).json()
        assert preview["count"] == 2
        # ★ 预览必须说清"歌不会被删"——这是用户最怕的那个误解
        assert "不会被删" in preview["note"]

        submit(ipod_client, "/api/library/playlists/delete",
               {"playlist_id": target["playlist_id"],
                "preview_id": preview["preview_id"]})

        names = [p["name"] for p in
                 ipod_client.get("/api/library/playlists").json()["playlists"]]
        assert "待删歌单" not in names, f"歌单没删掉：{names}"
        # 曲目本身必须还在
        assert len(all_ids(ipod_ctx)) == len(ids), "删歌单不该删歌"

    @ffmpeg_required
    def test_preview_token_is_single_use(self, ipod_ctx, ipod_client, music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 2)
        submit(ipod_client, "/api/library/playlists/create", {"name": "一次性"})
        target = playlist_by_name(ipod_client, "一次性")
        preview = ipod_client.post("/api/library/playlists/delete/preview",
                                   json={"playlist_id": target["playlist_id"]}).json()

        submit(ipod_client, "/api/library/playlists/delete",
               {"playlist_id": target["playlist_id"],
                "preview_id": preview["preview_id"]})
        # 再用同一个令牌 → 必须失败
        resp = ipod_client.post("/api/library/playlists/delete",
                                json={"playlist_id": target["playlist_id"],
                                      "preview_id": preview["preview_id"]})
        assert resp.status_code == 409


class TestEditTracks:
    @ffmpeg_required
    def test_add_tracks(self, ipod_ctx, ipod_client, music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 3)
        ids = all_ids(ipod_ctx)
        submit(ipod_client, "/api/library/playlists/create", {"name": "收集夹"})
        target = playlist_by_name(ipod_client, "收集夹")

        result = submit(ipod_client,
                        f"/api/library/playlists/{target['playlist_id']}/tracks",
                        {"add": ids[:2]})

        assert result["added"] == 2
        assert result["verified"] is True
        assert playlist_by_name(ipod_client, "收集夹")["count"] == 2

    @ffmpeg_required
    def test_remove_tracks(self, ipod_ctx, ipod_client, music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 3)
        ids = all_ids(ipod_ctx)
        submit(ipod_client, "/api/library/playlists/create",
               {"name": "要移出的", "track_ids": ids[:3]})
        target = playlist_by_name(ipod_client, "要移出的")

        result = submit(ipod_client,
                        f"/api/library/playlists/{target['playlist_id']}/tracks",
                        {"remove": ids[:1]})

        assert result["removed"] == 1
        assert playlist_by_name(ipod_client, "要移出的")["count"] == 2
        # 曲目本身还在库里
        assert len(all_ids(ipod_ctx)) == 3

    @ffmpeg_required
    def test_add_is_idempotent(self, ipod_ctx, ipod_client, music_dir) -> None:
        import_tracks(ipod_ctx, music_dir, 3)
        ids = all_ids(ipod_ctx)
        submit(ipod_client, "/api/library/playlists/create",
               {"name": "重复加", "track_ids": ids[:2]})
        target = playlist_by_name(ipod_client, "重复加")

        result = submit(ipod_client,
                        f"/api/library/playlists/{target['playlist_id']}/tracks",
                        {"add": ids[:2]})

        assert result["added"] == 0, "已经在里面的不该重复加"
        assert playlist_by_name(ipod_client, "重复加")["count"] == 2

    @ffmpeg_required
    def test_add_wins_over_remove_in_same_call(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        """同一次请求里 add 和 remove 同一首歌 → 结果应该是"在里面"。

        用户点的是「加入歌单」，不该因为它也出现在 remove 里就被移出去。
        """
        import_tracks(ipod_ctx, music_dir, 2)
        ids = all_ids(ipod_ctx)
        submit(ipod_client, "/api/library/playlists/create", {"name": "冲突"})
        target = playlist_by_name(ipod_client, "冲突")

        submit(ipod_client, f"/api/library/playlists/{target['playlist_id']}/tracks",
               {"add": [ids[0]], "remove": [ids[0]]})

        assert playlist_by_name(ipod_client, "冲突")["count"] == 1

    @ffmpeg_required
    def test_unknown_track_ids_are_ignored(self, ipod_ctx, ipod_client, music_dir) -> None:
        """库里的不存在的 id 要被忽略，不能把不存在的曲目写进歌单。"""
        import_tracks(ipod_ctx, music_dir, 2)
        submit(ipod_client, "/api/library/playlists/create", {"name": "过滤"})
        target = playlist_by_name(ipod_client, "过滤")

        result = submit(ipod_client,
                        f"/api/library/playlists/{target['playlist_id']}/tracks",
                        {"add": ["999999999", "不是数字", ""]})

        assert result["added"] == 0
        assert playlist_by_name(ipod_client, "过滤")["count"] == 0

    @ffmpeg_required
    def test_no_change_is_reported_not_faked(
        self, ipod_ctx, ipod_client, music_dir
    ) -> None:
        import_tracks(ipod_ctx, music_dir, 2)
        submit(ipod_client, "/api/library/playlists/create", {"name": "空操作"})
        target = playlist_by_name(ipod_client, "空操作")

        # 移出一个不在里面的
        result = submit(ipod_client,
                        f"/api/library/playlists/{target['playlist_id']}/tracks",
                        {"remove": all_ids(ipod_ctx)[:1]})

        assert result["added"] == 0 and result["removed"] == 0
        assert result["note"] == "无变化"


class TestReadOnly:
    """智能 / 播客播放列表只读。

    虚拟 iPod 默认只有主列表，所以这里**直接往 raw 里注入**这两类行——
    走的是跟真机一样的读取路径（`library.raw["mhlp_smart"]`）。
    """

    def _inject(self, ipod_ctx, key: str, playlist_id: int, name: str) -> None:
        library = ipod_ctx.library(force=True)
        row = dict(library.raw[key][0]) if library.raw.get(key) else {}
        row.update({"playlist_id": playlist_id, "Title": name, "items": []})
        library.raw.setdefault(key, [])
        library.raw[key] = [*library.raw[key], row]

    def test_smart_playlist_is_read_only(self, ipod_ctx, ipod_client) -> None:
        self._inject(ipod_ctx, "mhlp_smart", 4242, "最近添加")

        item = playlist_by_name(ipod_client, "最近添加")
        assert item["editable"] is False
        # 必须给中文原因，不能只是"列表里少一个"
        assert "智能播放列表" in item["readonly_reason"]

        resp = ipod_client.post("/api/library/playlists/4242/tracks",
                                json={"add": ["1"]})
        assert resp.status_code == 409
        assert "智能播放列表" in resp.json()["detail"]

    def test_podcast_playlist_is_read_only(self, ipod_ctx, ipod_client) -> None:
        self._inject(ipod_ctx, "mhlp_podcast", 5151, "某播客")

        item = playlist_by_name(ipod_client, "某播客")
        assert item["editable"] is False
        assert "播客" in item["readonly_reason"]

        # 注意路径：删除预览的 id 在 **body** 里（`/delete/preview` 是固定路径），
        # 不是 `/5151/delete/preview`——写错会得到 404 而不是预期的不允许
        resp = ipod_client.post("/api/library/playlists/delete/preview",
                                json={"playlist_id": "5151"})
        assert resp.status_code == 409, resp.text
        assert "播客" in resp.json()["detail"]

    def test_unknown_playlist_is_404(self, ipod_client) -> None:
        resp = ipod_client.get("/api/library/playlists/99999/tracks")
        assert resp.status_code == 404
        assert "没有这个歌单" in resp.json()["detail"]
