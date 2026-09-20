"""账号路由的测试：扫码登录、切换、移除。"""

from __future__ import annotations

import base64

import pytest

pytestmark = pytest.mark.usefixtures("fast_poll")


class TestQrLogin:
    def test_start_returns_a_qr_image(self, web_ctx, web_client, make_fake_ncm, patch_clients) -> None:
        fake = make_fake_ncm(codes=[801])
        patch_clients(web_ctx, fake)

        resp = web_client.post("/api/account/login")
        assert resp.status_code == 200

        data = resp.json()
        assert data["session_id"]
        assert data["state"] in {"waiting", "scanned"}
        # 二维码要以 base64 给出来，界面才能直接 Image.memory 显示
        assert base64.b64decode(data["image_base64"]) == fake.png

    def test_login_runs_on_the_job_thread(self, web_ctx, web_client, make_fake_ncm, patch_clients) -> None:
        """★ 登录的网络请求必须跑在**作业线程**上，不是路由的请求线程。

        跑在请求线程上就等于绕过了队列——那样两个登录、或者登录和下载
        就能同时改同一份 cookie，队列的意义整个没了。
        """
        fake = make_fake_ncm(codes=[801])
        patch_clients(web_ctx, fake)

        web_client.post("/api/account/login")
        web_ctx.jobs.wait_idle(5)

        assert fake.threads, "假客户端根本没被调用"
        assert all(name.startswith("ipod-job") for name in fake.threads), (
            f"有请求跑在非作业线程上：{fake.threads}"
        )

    def test_start_is_idempotent(self, web_ctx, web_client, make_fake_ncm, patch_clients) -> None:
        """连点两次"登录"不该拿到两张互相顶掉的二维码。"""
        fake = make_fake_ncm(codes=[801, 801, 801, 801])
        patch_clients(web_ctx, fake)

        first = web_client.post("/api/account/login").json()
        second = web_client.post("/api/account/login").json()

        assert first["session_id"] == second["session_id"]
        assert fake.start_calls == 1, "起了第二个登录作业（又去取了一张二维码）"

    def test_confirm_saves_account_and_makes_it_active(
        self, web_ctx, web_client, web_store, make_fake_ncm, patch_clients
    ) -> None:
        """扫完码 → 存账号 → 设为当前账号。三步缺一不可。"""
        fake = make_fake_ncm(codes=[801, 802, 803], uid=888, nickname="测试账号")
        patch_clients(web_ctx, fake)

        web_client.post("/api/account/login")
        assert web_ctx.jobs.wait_idle(10), "登录作业没跑完"

        state = web_client.get("/api/account/login").json()
        assert state["state"] == "confirmed"
        assert state["finished"] is True
        assert state["account"]["nickname"] == "测试账号"

        # 账号真的落库了，而且被设成了当前账号
        saved = web_store.get_account(888)
        assert saved is not None
        assert saved.nickname == "测试账号"
        assert saved.cookie == "fake-cookie"
        assert saved.vip_type == 11
        assert web_store.active_account().uid == 888

    def test_scanned_state_is_surfaced(self, web_ctx, web_client, make_fake_ncm, patch_clients) -> None:
        """802 = 已扫描待确认。界面要能显示这一步，用户才知道该去点手机。"""
        fake = make_fake_ncm(codes=[802])
        patch_clients(web_ctx, fake)

        web_client.post("/api/account/login")
        # 让轮询跑一轮，但别让它跑到确认
        web_ctx.jobs.wait_idle(5)

        assert any(
            "已扫描" in line.text
            for job in web_ctx.jobs.list_jobs()
            for line in job.log
        ), "扫描状态没被记录下来"

    def test_expired_qr_is_reported(self, web_ctx, web_client, make_fake_ncm, patch_clients) -> None:
        fake = make_fake_ncm(codes=[800])
        patch_clients(web_ctx, fake)

        web_client.post("/api/account/login")
        web_ctx.jobs.wait_idle(10)

        state = web_client.get("/api/account/login").json()
        assert state["state"] == "expired"
        assert "过期" in state["message"]

    def test_single_poll_failure_does_not_kill_the_login(
        self, web_ctx, web_client, make_fake_ncm, patch_clients
    ) -> None:
        """★ 一次轮询失败（网络抖一下）不该终结整个登录。

        二维码还开在用户手机上，这里却把流程判死，用户只能从头再扫一次。
        """
        fake = make_fake_ncm(codes=[803])
        fake.poll_error = OSError("网络抖了一下")
        patch_clients(web_ctx, fake)

        # 先让它抖几轮
        web_client.post("/api/account/login")
        for _ in range(3):
            fake.poll_error = OSError("还在抖")

        state = web_client.get("/api/account/login").json()
        assert state["state"] not in {"failed"}, "单次失败把登录判死了"

        # 网络恢复后应当能继续走完
        fake.poll_error = None
        assert web_ctx.jobs.wait_idle(10)

    def test_cancel_stops_the_login(self, web_ctx, web_client, make_fake_ncm, patch_clients) -> None:
        """取消后状态要变成"已取消"，不能一直停在"等待扫码"。"""
        fake = make_fake_ncm(codes=[801] * 500)
        patch_clients(web_ctx, fake)

        web_client.post("/api/account/login")
        resp = web_client.post("/api/account/login/cancel")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        assert web_ctx.jobs.wait_idle(10)
        state = web_client.get("/api/account/login").json()
        assert state["state"] == "cancelled"
        assert state["finished"] is True

    def test_cancel_without_login_is_not_an_error(
        self, web_client, web_ctx
    ) -> None:
        resp = web_client.post("/api/account/login/cancel")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_idle_state_when_never_logged_in(self, web_client) -> None:
        state = web_client.get("/api/account/login").json()
        assert state["state"] == "idle"
        assert state["finished"] is True

    def test_qr_failure_gives_chinese_guidance(
        self, web_ctx, web_client, make_fake_ncm, patch_clients
    ) -> None:
        """连不上本地 API 服务时，给的必须是**能照着做**的中文提示，
        不是一句 "502" 或者英文堆栈。"""
        fake = make_fake_ncm()

        def boom() -> tuple[str, bytes]:
            raise OSError("connection refused")

        fake.qr_login_start = boom  # type: ignore[method-assign]
        patch_clients(web_ctx, fake)

        resp = web_client.post("/api/account/login")
        assert resp.status_code in {502, 503}
        assert "网易云" in resp.json()["detail"]


class TestAccounts:
    def _seed(self, store, uid: int, nickname: str, *, vip: int = 0) -> None:
        store.save_account(uid, f"cookie-{uid}", nickname=nickname, vip_type=vip)

    def test_list_marks_the_active_one(self, web_client, web_store) -> None:
        self._seed(web_store, 1, "号一", vip=11)
        self._seed(web_store, 2, "号二")
        web_store.set_active_uid(2)

        data = web_client.get("/api/account/list").json()
        assert data["active_uid"] == 2
        by_uid = {a["uid"]: a for a in data["accounts"]}
        assert by_uid[2]["active"] is True
        assert by_uid[1]["active"] is False
        assert by_uid[1]["vip"] is True
        assert len(data["accounts"]) == 2

    def test_switch_account(self, web_client, web_store) -> None:
        self._seed(web_store, 1, "号一")
        self._seed(web_store, 2, "号二")
        web_store.set_active_uid(1)

        resp = web_client.post("/api/account/use", json={"uid": 2})
        assert resp.status_code == 200
        assert resp.json()["nickname"] == "号二"
        assert web_store.active_account().uid == 2

    def test_switch_to_unknown_uid_is_404(self, web_client) -> None:
        resp = web_client.post("/api/account/use", json={"uid": 999})
        assert resp.status_code == 404
        assert "999" in resp.json()["detail"]

    def test_remove_non_active_account_keeps_active(
        self, web_client, web_store
    ) -> None:
        self._seed(web_store, 1, "号一")
        self._seed(web_store, 2, "号二")
        web_store.set_active_uid(1)

        resp = web_client.post("/api/account/remove", json={"uid": 2})
        assert resp.status_code == 200
        assert resp.json()["was_active"] is False
        assert web_store.active_account().uid == 1

    def test_removing_active_account_does_not_leave_a_dangling_uid(
        self, web_client, web_store
    ) -> None:
        """★ 删掉当前账号后，**绝不能留下一个指向不存在账号的当前 uid**。

        留着的话之后所有请求都会带着空 cookie 跑，症状是"莫名拿不到歌"，
        而且从界面上完全看不出原因。

        这里期望落到"剩下那个账号"而不是"没有账号"：`active_account()`
        有个有意的兜底——只剩一个账号时直接用它（多账号才需要手动切）。
        所以正确结果是"自动切到号二"，而不是空。
        """
        self._seed(web_store, 1, "号一")
        self._seed(web_store, 2, "号二")
        web_store.set_active_uid(1)

        resp = web_client.post("/api/account/remove", json={"uid": 1})
        assert resp.json()["was_active"] is True

        active = web_store.active_account()
        assert active is not None, "没有兜底到剩下的账号"
        assert active.uid == 2, f"当前账号指向了已被删除的 uid：{active.uid}"

    def test_removing_the_last_account_leaves_nothing_active(
        self, web_client, web_store
    ) -> None:
        """账号全删光之后就该是"没账号"，不能还指着谁。"""
        self._seed(web_store, 7, "独苗")
        web_store.set_active_uid(7)

        web_client.post("/api/account/remove", json={"uid": 7})
        assert web_store.active_account() is None

    def test_cookies_are_kept_separate(self, web_store) -> None:
        """多账号 cookie 分开存——切账号不该把另一个号的凭据冲掉。"""
        self._seed(web_store, 1, "号一")
        self._seed(web_store, 2, "号二")
        assert web_store.get_account(1).cookie == "cookie-1"
        assert web_store.get_account(2).cookie == "cookie-2"
