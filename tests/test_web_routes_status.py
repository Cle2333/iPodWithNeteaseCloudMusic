"""后端路由的测试。

**全程不碰真网络、不碰真设备**：网卡用假传输层，设备用临时目录搭的
虚拟 iPod。跑一百遍也不会有副作用，更不会消耗网易云的请求配额。
"""

from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ipod_cli.ncm.state import StateStore
from ipod_web.app import create_app
from ipod_web.context import WebContext
from ipod_web.jobs import JobManager


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "web-state.db")


@pytest.fixture
def ctx(store: StateStore) -> WebContext:
    return WebContext(store=store, jobs=JobManager())


@pytest.fixture
def client(ctx: WebContext):
    app = create_app(ctx)
    with TestClient(app) as c:
        yield c


# ──────────────────────────────────────────────────────────────────────
# 就绪探测
# ──────────────────────────────────────────────────────────────────────


class TestHealth:
    def test_health_ok(self, client) -> None:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "就绪" in data["message"]

    def test_health_does_not_touch_device_or_network(self, ctx, client) -> None:
        """★ 就绪探测必须极轻量。

        启动时设备可能还没插——那时候也该让界面起来，否则用户看到的是
        "启动失败"，而真实情况只是"没插 iPod"，排查方向全错了。
        """
        calls: list[str] = []

        def explode(*_a, **_k):
            calls.append("碰了不该碰的东西")
            raise AssertionError("就绪探测不该读设备或探测服务")

        ctx.device = explode          # type: ignore[method-assign]
        ctx.netease_service = explode  # type: ignore[method-assign]

        resp = client.get("/api/health")

        assert resp.status_code == 200
        assert calls == []


# ──────────────────────────────────────────────────────────────────────
# 状态条
# ──────────────────────────────────────────────────────────────────────


class TestStatus:
    def test_no_device_is_not_an_error(self, ctx, client, monkeypatch) -> None:
        """没插设备是常态，不是错误——返回 200 + 中文引导。"""
        from ipod_cli.discovery import DeviceNotFoundError

        def no_device():
            raise DeviceNotFoundError("没找到 iPod")

        monkeypatch.setattr(ctx, "device", no_device)

        resp = client.get("/api/status")
        assert resp.status_code == 200

        data = resp.json()
        assert data["ipod"]["connected"] is False
        assert "iPod" in data["ipod"]["hint"]

    def test_reports_account_state(self, client, store: StateStore) -> None:
        resp = client.get("/api/status")
        data = resp.json()

        assert data["account"]["logged_in"] is False
        assert data["account"]["account_count"] == 0

    def test_reports_service_unreachable(self, client, ctx: WebContext) -> None:
        """服务没起时要能看出来——端口随便挑个没人听的。"""
        ctx.base_url = "http://127.0.0.1:1"       # 1 号端口不可能有人听
        resp = client.get("/api/status")
        data = resp.json()

        assert data["netease_service"]["reachable"] is False
        assert "连不上" in data["netease_service"]["detail"]

    def test_service_probe_only_does_tcp(self, ctx, monkeypatch) -> None:
        """★ 服务探测**只做 TCP 连接**。

        状态条每几秒轮询一次。如果这里打真实 API，光挂着界面就会持续
        消耗网易云的请求配额——而那条配额是有风控的。
        """
        created: list[tuple[str, int]] = []

        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_create_connection(address, timeout=None):
            created.append(address)
            return FakeSocket()

        monkeypatch.setattr(socket, "create_connection", fake_create_connection)

        result = ctx.netease_service()

        assert result.reachable is True
        assert created == [("127.0.0.1", 4000)]

    def test_reports_running_job(self, ctx, client) -> None:
        import threading
        import time

        release = threading.Event()
        job = ctx.jobs.submit("t", "正在跑的作业", lambda h: release.wait(5))
        time.sleep(0.1)

        data = client.get("/api/status").json()
        assert data["jobs"]["has_work"] is True
        assert data["jobs"]["running"] is not None
        assert data["jobs"]["running"]["title"] == "正在跑的作业"

        release.set()
        ctx.jobs.wait_idle(10)
        assert ctx.jobs.get(job.id) is not None


# ──────────────────────────────────────────────────────────────────────
# 设备信息
# ──────────────────────────────────────────────────────────────────────


def _fake_device(root: Path):
    return SimpleNamespace(
        root=root,
        display_name="iPod Classic 6th Gen 80GB Silver",
        model_number="MB029",
        family="iPod Classic",
        generation="6th Gen",
        capacity="80GB",
        color="Silver",
        serial="8K8097CUY5N",
        firewire_guid="0x000A270013473EA3",
        checksum="HASH58",
        total_bytes=79_800_000_000,
        free_bytes=79_600_000_000,
        free_text="79.6 GB",
        total_text="79.8 GB",
        is_supported=True,
    )


class TestDeviceInfo:
    def test_no_device_gives_guidance(self, ctx, client, monkeypatch) -> None:
        from ipod_cli.discovery import DeviceNotFoundError

        monkeypatch.setattr(
            ctx, "device",
            lambda: (_ for _ in ()).throw(DeviceNotFoundError("没找到")),
        )

        resp = client.get("/api/device")
        assert resp.status_code == 200

        data = resp.json()
        assert data["connected"] is False
        assert data["error"] == "未检测到 iPod"
        # 引导要具体到"先查什么"，不能只说一句"失败了"
        assert "数据线" in data["hint"]

    def test_reports_identity_and_storage(self, ctx, client, monkeypatch) -> None:
        monkeypatch.setattr(ctx, "device", lambda: _fake_device(Path("D:/")))
        monkeypatch.setattr(ctx, "library", lambda **kw: SimpleNamespace(
            ipod_name="我的iPod",
            tracks=[1, 2, 3],
            playlists=[
                {"Title": "我的iPod"},       # 主列表 = 设备名，不该当普通列表列出
                {"Title": "我喜欢的音乐"},
                {"Title": "On-The-Go 2"},
            ],
            summary=lambda: {
                "track_count": 3, "album_count": 3, "artist_count": 3,
                "total_bytes": 22_700_000, "total_ms": 480_000,
            },
        ))

        data = client.get("/api/device").json()

        assert data["connected"] is True
        ident = data["identity"]
        assert ident["model_number"] == "MB029"
        assert ident["serial"] == "8K8097CUY5N"
        assert ident["firewire_guid"] == "0x000A270013473EA3"
        assert ident["checksum"] == "HASH58"
        assert ident["name"] == "我的iPod"

        storage = data["storage"]
        assert storage["total_bytes"] == 79_800_000_000
        assert storage["free_bytes"] == 79_600_000_000
        assert "GB" in storage["total_text"]
        assert 0 <= storage["used_percent"] <= 100

        assert data["content"]["tracks"] == 3
        assert data["content"]["duration_text"] == "8 分"

        # 主播放列表不能混进普通列表
        assert data["content"]["playlist_names"] == ["我喜欢的音乐", "On-The-Go 2"]
        assert data["content"]["playlists"] == 2

    def test_marks_only_name_as_editable(self, ctx, client, monkeypatch) -> None:
        """★ 必须区分"硬件只读"和"数据库可改"。

        设备名存在 iTunesDB 的主播放列表标题里，能改；序列号/GUID 来自
        Device/SysInfo，是硬件信息。界面不标清楚的话用户会以为都能改，
        然后到处找不到入口。
        """
        monkeypatch.setattr(ctx, "device", lambda: _fake_device(Path("D:/")))
        monkeypatch.setattr(ctx, "library", lambda **kw: SimpleNamespace(
            ipod_name="我的iPod", tracks=[], playlists=[],
            summary=lambda: {"track_count": 0, "album_count": 0,
                             "artist_count": 0, "total_bytes": 0, "total_ms": 0},
        ))

        data = client.get("/api/device").json()

        assert data["editable_fields"] == ["name"]
        # 序列号这类绝对不能出现在可改清单里
        assert "serial" not in data["editable_fields"]
        assert "firewire_guid" not in data["editable_fields"]

    def test_broken_database_does_not_500(self, ctx, client, monkeypatch) -> None:
        """库损坏时给中文说明，不要甩一个 500 出去。"""
        monkeypatch.setattr(ctx, "device", lambda: _fake_device(Path("D:/")))

        def broken(**kwargs):
            raise ValueError("mhbd 头不对")

        monkeypatch.setattr(ctx, "library", broken)

        resp = client.get("/api/device")
        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is True
        assert "读不出来" in data["error"]
        assert "ipod verify" in data["hint"]


class TestDurationText:
    @pytest.mark.parametrize(("ms", "expected"), [
        (0, "0 分"),
        (60_000, "1 分"),
        (480_000, "8 分"),
        (3_600_000, "1 小时 0 分"),
        (28_320_000, "7 小时 52 分"),
    ])
    def test_formatting(self, ms: int, expected: str) -> None:
        from ipod_web.routes.status import _duration_text

        assert _duration_text(ms) == expected
