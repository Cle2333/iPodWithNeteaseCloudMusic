"""设置 / 缓存 / 作业队列的路由测试。"""

from __future__ import annotations

import time

from ipod_web.context import MIN_INTERVAL_FLOOR


class TestSettings:
    def test_defaults_match_the_measured_plan(self, web_client) -> None:
        """默认值不是随手定的：exhigh 是因为全库无损装不下（144GB > 79.6GB）。"""
        data = web_client.get("/api/settings").json()
        assert data["quality"] == "exhigh"
        assert data["min_interval"] == 0.35

    def test_options_are_shipped_to_the_ui(self, web_client) -> None:
        """档位选项由后端给出，Dart 侧不用再抄一份常量（抄了就迟早不同步）。"""
        data = web_client.get("/api/settings").json()
        values = {o["value"] for o in data["quality_options"]}
        assert values == {"standard", "exhigh", "lossless"}
        assert data["quality_label"] == "极高 (320k)"

    def test_change_quality(self, web_client, web_store) -> None:
        resp = web_client.put("/api/settings", json={"quality": "lossless"})
        assert resp.status_code == 200
        assert web_store.get_setting("quality") == "lossless"
        assert resp.json()["settings"]["quality_label"] == "无损 (FLAC)"

    def test_unknown_quality_is_rejected(self, web_client, web_store) -> None:
        """不能把任意字符串塞进去——后面拼到请求参数里会变成"静默降级"。"""
        resp = web_client.put("/api/settings", json={"quality": "jymaster"})
        assert resp.status_code == 400
        assert "jymaster" in resp.json()["detail"]
        assert web_store.get_setting("quality", "") == ""

    def test_interval_below_floor_is_rejected(self, web_client, web_store) -> None:
        """★ 请求间隔的下限是**安全阀**，不是偏好项。

        调太小 = 高频请求 = 网易云风控封号。后端必须自己拦，不能信界面。
        """
        resp = web_client.put("/api/settings", json={"min_interval": 0.05})
        assert resp.status_code == 400
        assert "风控" in resp.json()["detail"]
        assert web_store.get_setting("min_interval", "") == ""

    def test_interval_at_the_floor_is_accepted(self, web_client) -> None:
        resp = web_client.put(
            "/api/settings", json={"min_interval": MIN_INTERVAL_FLOOR}
        )
        assert resp.status_code == 200

    def test_interval_floor_is_enforced_even_if_db_is_tampered(
        self, web_client, web_store
    ) -> None:
        """★ 设置库里被写歪了（手改数据库 / 别处写的旧值）也不能真放出去。

        这是最后一道闸：所有请求间隔都从 `ctx.min_interval()` 走。
        """
        web_store.set_setting("min_interval", "0.01")

        data = web_client.get("/api/settings").json()
        assert data["min_interval"] == MIN_INTERVAL_FLOOR

    def test_garbage_interval_falls_back_to_default(self, web_store, web_ctx) -> None:
        web_store.set_setting("min_interval", "不是数字")
        assert web_ctx.min_interval() == 0.35

    def test_partial_update_only_touches_what_was_sent(
        self, web_client, web_store
    ) -> None:
        web_client.put("/api/settings", json={"quality": "lossless"})
        web_client.put("/api/settings", json={"min_interval": 0.5})

        assert web_store.get_setting("quality") == "lossless"
        assert web_store.get_setting("min_interval") == "0.50"

    def test_client_uses_the_saved_settings(self, web_ctx, web_store) -> None:
        """设置改了之后新建的客户端要带上——不然改了等于没改。"""
        web_store.set_setting("quality", "lossless")
        web_store.set_setting("min_interval", "0.8")
        web_store.save_account(1, "cookie-1", nickname="号一")
        web_store.set_active_uid(1)

        client = web_ctx.client()
        assert client.min_interval == 0.8
        assert client.cookie == "cookie-1"


class TestCache:
    def test_empty_cache(self, web_client, web_ctx) -> None:
        web_ctx.cache_dir.mkdir(parents=True, exist_ok=True)
        data = web_client.get("/api/cache").json()
        assert data["files"] == 0
        assert data["size_text"] == "0 B"
        assert data["consistent"] is True

    def test_counts_files_and_bytes(self, web_client, web_ctx, web_store) -> None:
        web_ctx.cache_dir.mkdir(parents=True, exist_ok=True)
        (web_ctx.cache_dir / "a.mp3").write_bytes(b"x" * 1500)
        (web_ctx.cache_dir / "b.mp3").write_bytes(b"y" * 500)
        web_store.remember_download(1, web_ctx.cache_dir / "a.mp3")
        web_store.remember_download(2, web_ctx.cache_dir / "b.mp3")

        data = web_client.get("/api/cache").json()
        assert data["files"] == 2
        assert data["bytes"] == 2000
        assert data["records"] == 2
        assert data["consistent"] is True
        assert "KB" in data["size_text"]

    def test_inconsistency_is_reported(self, web_client, web_ctx, web_store) -> None:
        """★ 文件数和记录数对不上时要如实报出来。

        用户手删过缓存、或者从别处拷来的话，界面继续显示"一致"就是在骗人。
        """
        web_ctx.cache_dir.mkdir(parents=True, exist_ok=True)
        (web_ctx.cache_dir / "a.mp3").write_bytes(b"x" * 10)
        # 记录有两条，文件只有一个
        web_store.remember_download(1, web_ctx.cache_dir / "a.mp3")
        web_store.remember_download(2, web_ctx.cache_dir / "gone.mp3")

        data = web_client.get("/api/cache").json()
        assert data["files"] == 1
        assert data["records"] == 2
        assert data["consistent"] is False

    def test_clear_removes_files_and_records(self, web_client, web_ctx, web_store) -> None:
        """★ 清缓存要**同时**清下载记录。

        只删文件的话记录还在，界面会显示"已下载"而文件其实不在——
        虽然 `cached_download` 下次访问会自我修正，中间那段时间是在骗用户。
        """
        web_ctx.cache_dir.mkdir(parents=True, exist_ok=True)
        path = web_ctx.cache_dir / "a.mp3"
        path.write_bytes(b"x" * 2048)
        web_store.remember_download(1, path)

        resp = web_client.post("/api/cache/clear")
        assert resp.status_code == 200

        data = resp.json()
        assert data["removed_files"] == 1
        assert data["removed_records"] == 1
        assert data["freed_bytes"] == 2048
        assert not path.exists()
        assert web_store.count_downloads() == 0
        assert web_store.cached_download(1) is None

    def test_clear_on_missing_dir_is_not_an_error(self, web_client, web_ctx) -> None:
        assert not web_ctx.cache_dir.exists()
        resp = web_client.post("/api/cache/clear")
        assert resp.status_code == 200
        assert resp.json()["removed_files"] == 0

    def test_clear_keeps_accounts(self, web_client, web_ctx, web_store) -> None:
        """清缓存不该把登录状态一起清了——那是两码事。"""
        web_store.save_account(1, "cookie-1", nickname="号一")
        web_client.post("/api/cache/clear")
        assert web_store.get_account(1) is not None


class TestRemoveCacheEntries:
    """删本地缓存的**指定几首**（歌单页右键「删除本地那份」/ 多选走这里）。

    这一组是真的会删文件的，所以守得细：删错歌、或者把没勾的也删了，
    用户的下载就白下了——还得重新下，而重下意味着又去碰网易云接口。
    """

    @staticmethod
    def _seed(store, cache_dir, entries) -> None:
        """往状态库塞记录，**并把文件真的建出来**。

        必须建真文件：`cached_download` / `downloaded_song_ids` 都会核对
        文件是否真的在，只塞记录等于在测已经不存在的旧行为。
        """
        cache_dir.mkdir(parents=True, exist_ok=True)
        for song_id, name in entries:
            path = cache_dir / f"{song_id}-{name}.mp3"
            path.write_bytes(b"x" * 16)
            store.remember_download(
                song_id, path, level="exhigh", size=16, name=name, artist=""
            )

    def test_empty_selection_is_rejected(
        self, web_ctx, web_client, web_store
    ) -> None:
        """★ 空列表**报错**，不是"全删"。

        当成全删的话：用户点了个没勾选的删除，40 GB 缓存就没了。
        清空有专门的按钮（/cache/clear），不该靠"空集=全部"这种隐式约定。
        """
        self._seed(web_store, web_ctx.cache_dir, [(1, "a")])

        resp = web_client.post("/api/cache/remove", json={"song_ids": []})

        assert resp.status_code == 400
        assert "没有选中任何歌曲" in resp.json()["detail"]
        assert len(web_store.list_downloads()) == 1, "什么都没删的请求把东西删了"
        assert len(list(web_ctx.cache_dir.glob("*.mp3"))) == 1

    def test_removes_only_the_selected(
        self, web_ctx, web_client, web_store
    ) -> None:
        """★ 只删勾中的那些。没勾的被误删 = 用户的下载白下了。"""
        self._seed(
            web_store,
            web_ctx.cache_dir,
            [(1, "留着"), (2, "删掉"), (3, "也留着")],
        )

        web_client.post("/api/cache/remove", json={"song_ids": [2]})
        assert web_ctx.jobs.wait_idle(20)

        assert {d.song_id for d in web_store.list_downloads()} == {1, 3}
        assert {p.name for p in web_ctx.cache_dir.glob("*.mp3")} == {
            "1-留着.mp3",
            "3-也留着.mp3",
        }

    def test_deletes_files_and_records_together(
        self, web_ctx, web_client, web_store
    ) -> None:
        """记录和文件要一起没。只删一头会留下幽灵条目或孤儿文件。"""
        self._seed(web_store, web_ctx.cache_dir, [(1, "a")])

        web_client.post("/api/cache/remove", json={"song_ids": [1]})
        assert web_ctx.jobs.wait_idle(20)

        assert web_store.list_downloads() == []
        assert list(web_ctx.cache_dir.glob("*.mp3")) == []

    def test_keep_files_option_only_drops_records(
        self, web_ctx, web_client, web_store
    ) -> None:
        """``delete_files=False`` = 只清记录，留着文件。

        缓存被外部动过、想重新对一遍的时候用得上。
        """
        self._seed(web_store, web_ctx.cache_dir, [(1, "a")])

        web_client.post(
            "/api/cache/remove",
            json={"song_ids": [1], "delete_files": False},
        )
        assert web_ctx.jobs.wait_idle(20)

        assert web_store.list_downloads() == []
        assert len(list(web_ctx.cache_dir.glob("*.mp3"))) == 1, "说了保留文件却删了"

    def test_reports_freed_space(self, web_ctx, web_client, web_store) -> None:
        self._seed(web_store, web_ctx.cache_dir, [(1, "a")])

        job_id = web_client.post(
            "/api/cache/remove", json={"song_ids": [1]}
        ).json()["job_id"]
        assert web_ctx.jobs.wait_idle(20)

        result = web_client.get(f"/api/jobs/{job_id}").json()["result"]
        assert result["records_removed"] == 1
        assert result["files_deleted"] == 1

    def test_removing_something_already_gone_is_not_an_error(
        self, web_ctx, web_client, web_store
    ) -> None:
        """文件已经被手工删过：记录清掉就行，不该报错。"""
        self._seed(web_store, web_ctx.cache_dir, [(1, "a")])
        (web_ctx.cache_dir / "1-a.mp3").unlink()

        resp = web_client.post("/api/cache/remove", json={"song_ids": [1]})

        assert resp.status_code == 200
        assert web_ctx.jobs.wait_idle(20)
        assert web_store.list_downloads() == []


class TestJobs:
    def test_empty_queue(self, web_client) -> None:
        data = web_client.get("/api/jobs").json()
        assert data["running"] is None
        assert data["queued"] == 0
        assert data["has_work"] is False
        assert data["jobs"] == []

    def test_running_job_shows_up(self, web_ctx, web_client) -> None:
        started = []

        def body(handle):
            handle.set_total(3)
            started.append(True)
            time.sleep(0.5)

        web_ctx.jobs.submit("test", "测试作业", body)
        for _ in range(50):
            if started:
                break
            time.sleep(0.02)

        data = web_client.get("/api/jobs").json()
        assert data["running"] is not None
        assert data["running"]["title"] == "测试作业"
        assert data["running"]["state_text"] == "进行中"
        assert data["has_work"] is True

        web_ctx.jobs.wait_idle(5)

    def test_queued_count(self, web_ctx, web_client) -> None:
        def body(handle):
            time.sleep(0.4)

        for i in range(3):
            web_ctx.jobs.submit("test", f"作业{i}", body)

        data = web_client.get("/api/jobs").json()
        assert data["queued"] >= 1, "排队数没统计到"

        web_ctx.jobs.wait_idle(10)

    def test_job_detail_log_is_incremental(self, web_ctx, web_client) -> None:
        """★ 日志按 seq 增量拉取。

        不做增量的话，跑一次几百首的下载之后，每轮轮询都要重传几千行，
        界面会越来越卡。
        """
        def body(handle):
            for i in range(5):
                handle.log(f"第 {i} 步")

        job = web_ctx.jobs.submit("test", "写日志", body)
        web_ctx.jobs.wait_idle(5)

        first = web_client.get(f"/api/jobs/{job.id}").json()
        # 作业体 5 行 + 管理器的"开始执行"/"完成" 2 行
        assert len(first["log"]) == 7
        cursor = first["latest_seq"]

        second = web_client.get(f"/api/jobs/{job.id}?since={cursor}").json()
        assert second["log"] == [], "增量拉取又重传了旧日志"

    def test_job_detail_404(self, web_client) -> None:
        resp = web_client.get("/api/jobs/不存在的作业")
        assert resp.status_code == 404
        assert "没有这个作业" in resp.json()["detail"]

    def test_cancel_running_job(self, web_ctx, web_client) -> None:
        def body(handle):
            for _ in range(200):
                handle.check_cancelled()
                time.sleep(0.01)

        job = web_ctx.jobs.submit("test", "可取消的活", body)
        time.sleep(0.05)

        resp = web_client.post(f"/api/jobs/{job.id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        assert web_ctx.jobs.wait_idle(5)
        assert web_client.get(f"/api/jobs/{job.id}").json()["state"] == "cancelled"

    def test_cancel_finished_job_says_so(self, web_ctx, web_client) -> None:
        """对已经结束的作业点取消，要明说"结束不了"，不能假装成功。"""
        job = web_ctx.jobs.submit("test", "很快的活", lambda handle: None)
        web_ctx.jobs.wait_idle(5)

        resp = web_client.post(f"/api/jobs/{job.id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "已经结束" in resp.json()["message"]

    def test_cancel_unknown_job_is_404(self, web_client) -> None:
        resp = web_client.post("/api/jobs/nope/cancel")
        assert resp.status_code == 404
