"""歌单浏览 / 下载 / 同步 的路由测试。

核心要守住的三条：
1. 浏览歌单不该反复打接口（10 分钟缓存 + 显式刷新）
2. 打接口的活全部走作业队列（不能绕过限速）
3. 同步状态不缓存（不然会出现"刚下完还显示未处理"）
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ipod_cli.ncm.downloader import DownloadError

pytestmark = pytest.mark.usefixtures("fast_poll")


def seed_account(store) -> None:
    store.save_account(1, "cookie-1", nickname="测试号", vip_type=11)
    store.set_active_uid(1)


@pytest.fixture
def fake_with_playlists(make_fake_ncm, make_playlist, make_song, web_store):
    """一个有 2 个歌单、歌单 100 有 5 首歌的假客户端。"""
    seed_account(web_store)

    fake = make_fake_ncm()
    fake.playlists = [
        make_playlist(100, "通勤歌单", track_count=5),
        make_playlist(200, "被改过名的喜欢的音乐", track_count=3, liked=True),
    ]
    fake.tracks = {
        100: [
            # 第 3 首换个艺人：测"按艺人搜"要用到（其余都是默认的"测试歌手"）
            *[make_song(1000 + i, f"通勤第{i}首") for i in range(3)],
            make_song(1003, "通勤第3首", artist="陈奕迅"),
            make_song(1004, "通勤第4首"),
        ],
        # 「我喜欢的音乐」的曲目也挂在**它的歌单 id** 下：现在取曲目走
        # /playlist/track/all（App 的顺序），不再走 /likelist。
        200: [make_song(2000 + i, f"喜欢第{i}首") for i in range(3)],
    }
    fake.liked_songs = list(fake.tracks[200])
    # ★ likelist **故意给相反的顺序**。
    #   实测你的 246 首：两份集合一样，同位置零匹配——likelist 的顺序是
    #   网易云内部的、跟 App 显示的对不上。测试这样设，才能守住
    #   "界面用的是歌单那份、不是 likelist 那份"。
    fake.liked_ids = [s.id for s in reversed(fake.liked_songs)]
    return fake


class TestPlaylistList:
    def test_lists_playlists(self, web_ctx, web_client, fake_with_playlists, patch_clients) -> None:
        patch_clients(web_ctx, fake_with_playlists)

        resp = web_client.get("/api/playlists")
        assert resp.status_code == 200

        data = resp.json()
        assert data["loading"] is False
        assert data["cached"] is False
        assert [p["name"] for p in data["playlists"]] == ["通勤歌单", "被改过名的喜欢的音乐"]
        assert data["playlists"][1]["liked"] is True

    def test_second_read_hits_the_cache(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """★ 10 分钟内重复进歌单页**不该再打接口**。

        每次浏览都发请求 = 白白消耗有风控的配额。这是这条规则的全部意义。
        """
        patch_clients(web_ctx, fake_with_playlists)

        web_client.get("/api/playlists")
        assert fake_with_playlists.playlist_calls == 1

        second = web_client.get("/api/playlists")
        assert second.json()["cached"] is True
        assert fake_with_playlists.playlist_calls == 1, "第二次又打了接口"

    def test_refresh_bypasses_the_cache(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """缓存必须能被显式绕过，否则新加的歌要等 10 分钟才出现。"""
        patch_clients(web_ctx, fake_with_playlists)

        web_client.get("/api/playlists")
        web_client.get("/api/playlists?refresh=true")

        assert fake_with_playlists.playlist_calls == 2

    def test_reading_goes_through_the_job_queue(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """★ 读歌单也是打接口，必须在作业线程上跑。

        在路由线程上跑等于绕过队列和限速——那样"绝不并发"就只是句口号。
        """
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        assert fake_with_playlists.threads
        assert all(t.startswith("ipod-job") for t in fake_with_playlists.threads), (
            f"有请求跑在非作业线程上：{fake_with_playlists.threads}"
        )

    def test_not_logged_in_gives_a_usable_message(self, web_ctx, web_client, patch_clients, make_fake_ncm) -> None:
        """没登录时要说"去设置里扫码"，不能甩个 500。"""
        patch_clients(web_ctx, make_fake_ncm())

        resp = web_client.get("/api/playlists")
        assert resp.status_code == 502
        assert "登录" in resp.json()["detail"]

    def test_busy_queue_returns_loading_instead_of_hanging(
        self, web_ctx, web_client, fake_with_playlists, patch_clients, monkeypatch
    ) -> None:
        """★ 队列前面压着活的时候，**先返回"正在加载"而不是干等**。

        真等下去就是请求超时，用户看到的是"歌单页坏了"，
        而实际只是"有个下载在跑，排队中"。
        """
        import ipod_web.routes.playlists as mod

        monkeypatch.setattr(mod, "LOAD_WAIT", 0.2)
        patch_clients(web_ctx, fake_with_playlists)

        # 先塞一个慢作业占住队列
        def slow(handle):
            time.sleep(1.5)

        web_ctx.jobs.submit("test", "占位的慢活", slow)

        data = web_client.get("/api/playlists").json()
        assert data["loading"] is True
        assert data["job_id"]
        assert "排队" in data["message"]
        assert data["playlists"] == []

        web_ctx.jobs.wait_idle(10)



def cache_song(store, cache_dir: Path, song_id: int, name: str = "") -> Path:
    """记一条下载记录，**并把文件真的建出来**。

    必须建文件：`downloaded_song_ids()` 现在会核对文件是否真的在
    （以前只信记录，于是界面标着"已下载"而本地其实没有——那是个 bug）。
    只 `remember_download` 不建文件的写法，测的是已经不存在的旧行为。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / (name or f"{song_id}.mp3")
    path.write_bytes(b"x" * 16)
    store.remember_download(song_id, path, level="exhigh", size=16)
    return path

class TestPlaylistSongs:
    def _load(self, web_client, web_ctx, fake, patch_clients) -> None:
        patch_clients(web_ctx, fake)
        web_client.get("/api/playlists")   # 先让歌单列表进缓存
        web_client.get("/api/playlists/100/songs")

    def test_paginates(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        self._load(web_client, web_ctx, fake_with_playlists, patch_clients)

        data = web_client.get("/api/playlists/100/songs?page=2&size=2").json()
        assert data["total"] == 5
        assert data["pages"] == 3
        assert len(data["songs"]) == 2
        assert data["songs"][0]["name"] == "通勤第2首"
        assert data["songs"][0]["artist"] == "测试歌手"

    def test_last_page_is_partial(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        self._load(web_client, web_ctx, fake_with_playlists, patch_clients)
        data = web_client.get("/api/playlists/100/songs?page=3&size=2").json()
        assert len(data["songs"]) == 1
        assert data["songs"][0]["name"] == "通勤第4首"

    def test_status_marks_downloaded_and_pending(
        self, web_ctx, web_client, web_store, fake_with_playlists, patch_clients
    ) -> None:
        """已下载 / 未处理要分清楚。"""
        self._load(web_client, web_ctx, fake_with_playlists, patch_clients)
        cache_song(web_store, web_ctx.cache_dir, 1000)
        cache_song(web_store, web_ctx.cache_dir, 1001)

        data = web_client.get("/api/playlists/100/songs").json()
        by_name = {s["name"]: s["status"] for s in data["songs"]}
        assert by_name["通勤第0首"] == "downloaded"
        assert by_name["通勤第1首"] == "downloaded"
        assert by_name["通勤第2首"] == "pending"
        # 只断言关心的键：counts 以后还会长（比如"在 iPod 上但本地没留"），
        # 整字典精确比对会一直脆
        counts = data["counts"]
        assert counts["on_ipod"] == 0
        assert counts["downloaded"] == 2
        assert counts["pending"] == 3
        assert counts["all"] == 5

    def test_status_is_not_cached(
        self, web_ctx, web_client, web_store, fake_with_playlists, patch_clients
    ) -> None:
        """★ 同步状态必须现算。

        缓存了的话会出现"刚刚下完，界面还显示未处理"——用户会以为下载失败，
        然后再点一次。
        """
        self._load(web_client, web_ctx, fake_with_playlists, patch_clients)

        before = web_client.get("/api/playlists/100/songs").json()["counts"]
        assert before["downloaded"] == 0

        cache_song(web_store, web_ctx.cache_dir, 1002)

        after = web_client.get("/api/playlists/100/songs").json()["counts"]
        assert after["downloaded"] == 1, "状态走了缓存，没跟上"

    def test_unknown_playlist_is_404(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")
        resp = web_client.get("/api/playlists/999/songs")
        assert resp.status_code == 404
        assert "999" in resp.json()["detail"]

    def test_without_playlist_cache_asks_to_open_the_page_first(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """没有歌单列表缓存时给明确的引导，而不是编一个"歌单999"的名字。"""
        patch_clients(web_ctx, fake_with_playlists)
        resp = web_client.get("/api/playlists/100/songs")
        assert resp.status_code == 409
        assert "歌单页" in resp.json()["detail"]

    def test_liked_playlist_uses_the_liked_endpoint(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """「我喜欢的音乐」走 likelist + song_detail，不是 playlist_tracks。"""
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        data = web_client.get("/api/playlists/200/songs").json()
        assert data["total"] == 3
        assert {s["id"] for s in data["songs"]} == {2000, 2001, 2002}


class TestSongFilterAndIds:
    """★ 状态筛选 + 「一键选中所有未处理的」用的轻量 ID 列表。"""

    def _load(self, web_client, web_ctx, fake, patch_clients):
        patch_clients(web_ctx, fake)
        web_client.get("/api/playlists")
        return web_client.get("/api/playlists/100/songs").json()

    def test_filter_pending_only(
        self, web_ctx, web_client, web_store, fake_with_playlists, patch_clients
    ) -> None:
        self._load(web_client, web_ctx, fake_with_playlists, patch_clients)
        cache_song(web_store, web_ctx.cache_dir, 1000)

        data = web_client.get("/api/playlists/100/songs?status=pending").json()
        assert data["filtered"] == 4
        assert data["total"] == 5, "筛选不该改变整单的总数"
        assert all(s["status"] == "pending" for s in data["songs"])

    def test_filter_downloaded_only(
        self, web_ctx, web_client, web_store, fake_with_playlists, patch_clients
    ) -> None:
        self._load(web_client, web_ctx, fake_with_playlists, patch_clients)
        cache_song(web_store, web_ctx.cache_dir, 1000)

        data = web_client.get("/api/playlists/100/songs?status=downloaded").json()
        assert data["filtered"] == 1
        assert data["songs"][0]["id"] == 1000

    def test_unknown_filter_is_rejected(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        self._load(web_client, web_ctx, fake_with_playlists, patch_clients)
        resp = web_client.get("/api/playlists/100/songs?status=乱填的")
        assert resp.status_code == 400
        assert "不认识的筛选" in resp.json()["detail"]

    def test_ids_returns_every_pending_across_all_pages(
        self, web_ctx, web_client, web_store, fake_with_playlists, patch_clients
    ) -> None:
        """★ 关键点：ID 列表要覆盖**整单**，不是当前这一页。

        只给当前页的话，用户点"全选未处理的"以为选了 243 首、
        实际只选了 50 首。
        """
        self._load(web_client, web_ctx, fake_with_playlists, patch_clients)
        cache_song(web_store, web_ctx.cache_dir, 1000)

        data = web_client.get("/api/playlists/100/ids?status=pending").json()
        assert data["count"] == 4
        assert 1000 not in data["ids"]
        assert set(data["ids"]) == {1001, 1002, 1003, 1004}

    def test_ids_all_returns_everything(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        self._load(web_client, web_ctx, fake_with_playlists, patch_clients)
        data = web_client.get("/api/playlists/100/ids").json()
        assert data["count"] == 5
        assert data["ids"] == [1000, 1001, 1002, 1003, 1004]

    def test_ids_does_not_hit_the_network_again(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """曲目本来就缓存着——这个接口不该产生任何网易云请求。"""
        self._load(web_client, web_ctx, fake_with_playlists, patch_clients)
        before = fake_with_playlists.request_count

        web_client.get("/api/playlists/100/ids?status=pending")
        web_client.get("/api/playlists/100/ids?status=pending")

        assert fake_with_playlists.request_count == before


class TestPlan:
    def test_plan_previews_before_downloading(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """★ 下载前先给预览。不然一句"下载整个歌单"点了就是几百首几个 G。"""
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        data = web_client.post("/api/playlists/100/plan", json={}).json()
        assert data["total"] == 5
        assert data["to_download"] == 5
        assert data["needs_fetch"] == 5
        assert len(data["preview"]) == 5

    def test_plan_counts_cached_as_nothing_to_do_for_local(
        self, web_ctx, web_client, web_store, fake_with_playlists, patch_clients
    ) -> None:
        """只想下载到本地时，本地已有的就是**没事可做**（跳过）。

        （要同步到 iPod 时不一样：缓存命中仍然要写进设备，所以算进
        "要处理"。那套语义在 test_ncm_sync.py 里守着。）
        """
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")
        cache_song(web_store, web_ctx.cache_dir, 1000)

        data = web_client.post("/api/playlists/100/plan", json={}).json()

        assert data["target"] == "local"
        assert data["needs_fetch"] == 4, "本地已有的还被算成要下载"
        assert data["to_download"] == 4, "本地已有的不该再算进要处理"
        assert data["skip_reasons"] == {"已下载": 1}

    def test_plan_does_not_skip_local_songs_because_they_are_on_the_ipod(
        self, web_ctx, web_client, web_store, fake_with_playlists, patch_clients
    ) -> None:
        """★ 用户报的那个 bug：歌在 iPod 上、本地文件没了，点「下载」必须去下。

        不能因为"它已经在 iPod 上了"就跳过——用户要的是**本地那份**。
        """
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")
        # 状态库里记着"同步过"（模拟"这首歌早就进 iPod 了"），但本地没有文件
        web_store.mark_synced(
            1000, ipod_location="iPod_Control/Music/F00/x.mp3"
        )

        data = web_client.post("/api/playlists/100/plan", json={}).json()

        assert data["needs_fetch"] >= 1, (
            "本地文件没了却因为「已在 iPod」被跳过——"
            "这正是用户看到「都已经就绪」的原因"
        )
        assert data.get("skip_reasons", {}) == {}

    def test_plan_honours_limit(self, web_ctx, web_client, fake_with_playlists, patch_clients) -> None:
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        data = web_client.post("/api/playlists/100/plan", json={"limit": 2}).json()
        assert data["total"] == 2

    def test_plan_counts_only_the_selected(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """★ 只规划选中的那几首——预览说"要下 5 首"实际只下 2 首就是假预览。"""
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        data = web_client.post(
            "/api/playlists/100/plan", json={"song_ids": [1000, 1002]}
        ).json()
        assert data["total"] == 2
        assert data["needs_fetch"] == 2
        assert len(data["preview"]) == 2

    def test_empty_selection_is_an_error_not_everything(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """★ 空选中集**报错**，不能默默变成"整个歌单"。

        用户把勾全取消之后点下载，结果几百首开始跑——配额和磁盘都真花了。
        """
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        resp = web_client.post("/api/playlists/100/plan", json={"song_ids": []})
        assert resp.status_code == 400
        assert "没有选中任何歌曲" in resp.json()["detail"]

        resp = web_client.post(
            "/api/playlists/100/download", json={"song_ids": [], "push": False}
        )
        assert resp.status_code == 400

    def test_selection_that_matches_nothing_fails_the_job(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """选的曲子一首都没匹配上时，作业要失败并说清楚，不能假装在跑。"""
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        web_client.post(
            "/api/playlists/100/download", json={"song_ids": [999999]}
        )
        assert web_ctx.jobs.wait_idle(20)

        job = web_ctx.jobs.list_jobs()[0]
        assert job.state == "failed"
        assert "匹配" in job.error


class TestSelectedDownload:
    """★ 只下选中的几首。"""

    def test_download_only_touches_the_selected(
        self, web_ctx, web_client, fake_with_playlists, patch_clients, monkeypatch
    ) -> None:
        """★ 选了哪几首，下载器就只能被问到哪几首。

        这里不去查磁盘（假客户端根本没实现下载），而是记录**下载器被
        问了哪些歌**——这正是"选中集有没有真的传到下载环节"的直接证据。
        """
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        asked: list[int] = []

        def fake_download(client, song, dest, **kwargs):
            asked.append(song.id)
            raise DownloadError("测试用：不真下")

        monkeypatch.setattr("ipod_cli.ncm.sync.download_song", fake_download)

        web_client.post(
            "/api/playlists/100/download", json={"song_ids": [1001, 1003]}
        )
        assert web_ctx.jobs.wait_idle(20)

        assert sorted(asked) == [1001, 1003], (
            f"下载器被问到的不是选中的那两首：{asked}"
        )

    def test_whole_playlist_still_asks_for_everything(
        self, web_ctx, web_client, fake_with_playlists, patch_clients, monkeypatch
    ) -> None:
        """不传 song_ids = 整单。这条要跟"选中"区分清楚。"""
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        asked: list[int] = []

        def fake_download(client, song, dest, **kwargs):
            asked.append(song.id)
            raise DownloadError("测试用：不真下")

        monkeypatch.setattr("ipod_cli.ncm.sync.download_song", fake_download)

        web_client.post("/api/playlists/100/download", json={})
        assert web_ctx.jobs.wait_idle(20)

        assert len(asked) == 5, f"整单应该问到 5 首，实际 {sorted(asked)}"

    def test_job_title_says_how_many_selected(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """标题要写清"选中的 N 首"——不然用户不知道这一下会动多少。"""
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        data = web_client.post(
            "/api/playlists/100/download", json={"song_ids": [1000, 1001]}
        ).json()
        assert "选中的 2 首" in data["message"]
        assert "通勤歌单" in data["message"]

        web_ctx.jobs.wait_idle(20)


class TestDownloadJob:
    def test_download_creates_a_job(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """点"下载"要立刻返回一个作业号，不能把请求卡在那儿等下载完。"""
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        resp = web_client.post("/api/playlists/100/download", json={"limit": 3})
        assert resp.status_code == 200

        data = resp.json()
        assert data["ok"] is True
        assert data["job_id"]
        assert "通勤歌单" in data["message"]

        web_ctx.jobs.wait_idle(20)

    def test_sync_job_title_says_sync(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """push=true 的标题要写"同步"，不然用户分不清自己点的是哪个。"""
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        resp = web_client.post(
            "/api/playlists/100/download", json={"push": True, "limit": 1}
        )
        assert "同步" in resp.json()["message"]

        web_ctx.jobs.wait_idle(20)

    def test_download_reports_failures_individually(
        self, web_ctx, web_client, web_store, fake_with_playlists, patch_clients, monkeypatch
    ) -> None:
        """★ 失败的歌要逐首列出。

        "有 3 首失败"对排查毫无帮助——用户想知道是哪 3 首、为什么。
        """
        from ipod_cli.ncm.downloader import DownloadError

        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        def fake_download(client, song, dest, **kwargs):
            raise DownloadError(f"拿不到「{song.name}」的下载链接")

        # 打在 sync 模块上：execute_downloads 是在那里调 download_song 的
        monkeypatch.setattr("ipod_cli.ncm.sync.download_song", fake_download)

        job_id = web_client.post(
            "/api/playlists/100/download", json={"limit": 2}
        ).json()["job_id"]
        web_ctx.jobs.wait_idle(20)

        detail = web_client.get(f"/api/jobs/{job_id}").json()
        texts = "\n".join(line["text"] for line in detail["log"])
        assert "通勤第0首" in texts
        assert "通勤第1首" in texts
        assert detail["result"]["failed"] == 2


class TestLikedTracksOrder:
    """★ 界面上「我喜欢的音乐」的曲目顺序。

    这条是用户反馈补上的："我喜欢歌单的顺序我这里还是乱的"。

    根因是**同一套取曲目的逻辑写了两份**：引擎里那份（`fetch_source_songs`）
    改成走歌单接口了，而路由里 `_load_tracks` 那份副本没跟上——于是
    **下载的顺序对了、界面上看到的还是乱的**。两份实现必然有一份掉队，
    所以合并成了一份。

    这里把 likelist 的顺序**故意设成相反的**（见 fake_with_playlists），
    所以一旦哪边又走回 likelist，这条断言会立刻红。
    """

    def test_song_list_follows_the_playlist_order(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        patch_clients(web_ctx, fake_with_playlists)
        # 先读歌单列表：路由靠它把 playlist_id 认成同步源（没读会 409）
        web_client.get("/api/playlists")

        data = web_client.get("/api/playlists/200/songs").json()

        assert [s["name"] for s in data["songs"]] == [
            "喜欢第0首",
            "喜欢第1首",
            "喜欢第2首",
        ], "界面顺序跟 App 不一致——多半又走回 /likelist 了"

    def test_no_extra_requests_for_the_liked_list(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """★ 歌单接口自带元数据，不该再补一轮 song/detail。

        顺带证明没走 likelist：那条路是 likelist + 每 100 首一次 song/detail。
        """
        patch_clients(web_ctx, fake_with_playlists)
        list_calls_before = fake_with_playlists.request_count
        web_client.get("/api/playlists")            # 1 次：读歌单列表
        web_client.get("/api/playlists/200/songs")  # 应该只有 1 次：读曲目

        extra = fake_with_playlists.request_count - list_calls_before
        # 歌单列表 1 次 + 曲目 1 次。走 likelist 的话还要多出 likelist + 
        # song/detail，这里会明显超。
        assert extra <= 2, f"多发了请求（{extra} 次），多半绕回了 likelist"


class TestSongSearch:
    """★ 按歌名/艺人搜当前歌单。

    **在服务端筛是有意的**：客户端只能筛当前页那 50 首，246 首的歌单
    "搜了跟没搜一样"——用户明明看得见那首歌，却搜不出来。
    """

    def test_search_by_name(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        data = web_client.get("/api/playlists/100/songs?search=第3首").json()

        assert [s["name"] for s in data["songs"]] == ["通勤第3首"]
        assert data["filtered"] == 1

    def test_search_by_artist(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        data = web_client.get("/api/playlists/100/songs?search=陈奕迅").json()

        assert [s["name"] for s in data["songs"]] == ["通勤第3首"]

    def test_search_is_case_insensitive_for_latin(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """拉丁字母不分大小写。中文没大小写，这条只对英文名有意义。"""
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")
        # 假数据里有个大写的（"Episode 33" 那种场景）
        fake_with_playlists.tracks[100][0].name = "Goodbye & Hello"

        data = web_client.get("/api/playlists/100/songs?search=goodbye").json()

        assert [s["name"] for s in data["songs"]] == ["Goodbye & Hello"]

    def test_search_combines_with_status(
        self, web_ctx, web_client, web_store, fake_with_playlists, patch_clients
    ) -> None:
        """搜索和状态筛选是**叠加**的，不是二选一。"""
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")
        cache_song(web_store, web_ctx.cache_dir, 1003)

        data = web_client.get(
            "/api/playlists/100/songs?search=通勤&status=downloaded"
        ).json()

        assert [s["name"] for s in data["songs"]] == ["通勤第3首"]

    def test_blank_search_returns_everything(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """空串/纯空格 = 不筛，别把空搜索当"啥都不匹配"。"""
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        assert len(
            web_client.get("/api/playlists/100/songs?search=").json()["songs"]
        ) == 5
        assert len(
            web_client.get("/api/playlists/100/songs?search=%20%20").json()["songs"]
        ) == 5

    def test_counts_stay_whole_playlist(
        self, web_ctx, web_client, fake_with_playlists, patch_clients
    ) -> None:
        """★ 搜出来的 counts 仍是**整单**的概况。

        搜"第3首"时把 counts 也算成 1 首的话，顶上那行"共 246 首 ·
        未下载 243"就会跟着搜索乱跳——那是概况，不是筛选结果。
        """
        patch_clients(web_ctx, fake_with_playlists)
        web_client.get("/api/playlists")

        data = web_client.get("/api/playlists/100/songs?search=第3首").json()

        assert data["total"] == 5
        assert data["counts"]["all"] == 5
