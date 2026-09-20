"""网易云客户端的测试。

## 这些测试一次真网络都不发

注入 ``FakeTransport`` 当传输层——**测试永远不该碰真接口**：
既是为了可重复（不受网络/限速影响），也是为了**不拿真实账号去冒风控的险**。
用户明确提过这个担心。

## 重点守的三件事

1. **限速真的生效**（请求之间确实隔了 min_interval）
2. **空响应被当成"限速"去重试**——而不是当成"歌单是空的"。
   P0 时就是因为这个漏了 2184 条数据，肉眼还看不出问题。
3. **档位阶梯只往下走**，绝不"往上够"到 iPod 播不了的档位。
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import pytest

from ipod_cli.ncm.client import (
    DEFAULT_QUALITY,
    QUALITY_LADDER,
    NcmClient,
    NcmError,
    NotLoggedInError,
    Song,
)


class FakeTransport:
    """假的 API 传输层。记录所有调用，按路径返回预设响应。"""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes: dict[str, Any] = routes or {}
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: int) -> dict[str, Any]:
        self.calls.append(url)
        parsed = urllib.parse.urlparse(url)
        handler = self.routes.get(parsed.path)
        if handler is None:
            raise NcmError(f"测试没给 {parsed.path} 定义响应")
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if callable(handler):
            return handler(params)
        if isinstance(handler, list):
            # 列表 = 依次返回（第 N 次调用取第 N 个），用来模拟"先失败后成功"
            index = min(self._count(parsed.path) - 1, len(handler) - 1)
            return handler[index]
        return handler

    def _count(self, path: str) -> int:
        return sum(1 for u in self.calls if urllib.parse.urlparse(u).path == path)

    def paths(self) -> list[str]:
        return [urllib.parse.urlparse(u).path for u in self.calls]

    def params_for(self, path: str) -> list[dict[str, str]]:
        out = []
        for url in self.calls:
            parsed = urllib.parse.urlparse(url)
            if parsed.path == path:
                out.append(dict(urllib.parse.parse_qsl(parsed.query)))
        return out


class FakeClock:
    """可推进的假时钟 + 记录 sleep 时长。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_client(transport: FakeTransport, **kwargs: Any) -> tuple[NcmClient, FakeClock]:
    fake = FakeClock()
    client = NcmClient(
        cookie="test-cookie",
        transport=transport,
        sleep=fake.sleep,
        clock=fake.clock,
        **kwargs,
    )
    return client, fake


SONG_RAW = {
    "id": 111,
    "name": "歌名",
    "ar": [{"name": "艺人"}],
    "al": {"name": "专辑", "id": 9, "picUrl": "https://p.example/c.jpg"},
    "dt": 200000,
    "no": 3,
    "fee": 1,
}


class TestRateLimiting:
    """★ 保护账号的第一道闸。"""

    def test_requests_are_spaced_out(self) -> None:
        transport = FakeTransport({"/login/qr/key": {"data": {"unikey": "u"}}})
        client, fake = make_client(transport, min_interval=0.5)

        for _ in range(4):
            client.ping()

        # 首次不用等（没有"前一次"），后 3 次每次都要补足间隔
        assert len(fake.slept) == 3
        assert all(s == pytest.approx(0.5) for s in fake.slept), fake.slept

    def test_no_wait_when_interval_disabled(self) -> None:
        transport = FakeTransport({"/login/qr/key": {"data": {"unikey": "u"}}})
        client, fake = make_client(transport, min_interval=0)

        for _ in range(5):
            client.ping()

        assert fake.slept == []

    def test_elapsed_time_is_credited(self) -> None:
        """上一次请求之后已经过了很久，就不该再等。"""
        transport = FakeTransport({"/login/qr/key": {"data": {"unikey": "u"}}})
        client, fake = make_client(transport, min_interval=1.0)

        client.ping()
        fake.now += 10.0          # 中间隔了很久
        client.ping()

        assert fake.slept == [], f"不该等，实际等了 {fake.slept}"

    def test_request_count_is_tracked(self) -> None:
        """看不到的量管不住——调用方要能问"这趟发了多少请求"。"""
        transport = FakeTransport({"/login/qr/key": {"data": {"unikey": "u"}}})
        client, _ = make_client(transport)

        assert client.request_count == 0
        client.ping()
        client.ping()
        assert client.request_count == 2


class TestRetryOnEmpty:
    """★ 限速在网易云那边的表现是"200 + 空数据"，必须重试而不是当真。"""

    def test_empty_then_data_retries_and_succeeds(self) -> None:
        transport = FakeTransport({
            "/user/playlist": [
                {"playlist": []},                                   # 被限速
                {"playlist": [{"id": 1, "name": "歌单", "trackCount": 2}]},
            ]
        })
        client, fake = make_client(transport)

        playlists = client.user_playlists(uid=1001)

        assert len(playlists) == 1
        assert playlists[0].name == "歌单"
        assert len(transport.calls) == 2, "应该重试了一次"
        assert fake.slept, "重试之间应该有退避等待"

    def test_permanently_empty_raises_instead_of_reporting_empty(self) -> None:
        """一直空就要报错。**绝不能返回空列表**——那会伪装成"歌单是空的"。"""
        transport = FakeTransport({"/user/playlist": {"playlist": []}})
        client, _ = make_client(transport)

        with pytest.raises(NcmError, match="限速"):
            client.user_playlists(uid=1001)

    def test_backoff_grows(self) -> None:
        transport = FakeTransport({"/user/playlist": {"playlist": []}})
        client, fake = make_client(transport)

        with pytest.raises(NcmError):
            client.user_playlists(uid=1001)

        assert len(fake.slept) >= 2
        # 退避应该是递增的（0.8, 1.6, 3.2…）
        assert fake.slept[1] > fake.slept[0], fake.slept

    def test_network_error_retries(self) -> None:
        calls = {"n": 0}

        def flaky(_params: dict[str, str]) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise NcmError("网络抖了一下")
            return {"playlist": [{"id": 1, "name": "歌单", "trackCount": 1}]}

        transport = FakeTransport({"/user/playlist": flaky})
        client, _ = make_client(transport)

        assert len(client.user_playlists(uid=1)) == 1


class TestQualityLadder:
    """★ 档位只能往下走。"""

    def test_requested_level_is_used_first(self) -> None:
        transport = FakeTransport({
            "/song/url/v1": {"data": [{"url": "http://cdn/a.mp3",
                                       "level": "exhigh", "br": 320000}]}
        })
        client, _ = make_client(transport)

        result = client.song_url(111, "exhigh")

        assert result.available
        assert result.level == "exhigh"
        assert transport.params_for("/song/url/v1")[0]["level"] == "exhigh"

    def test_falls_back_down_when_unavailable(self) -> None:
        """请求无损拿不到 → 退 320k。"""
        transport = FakeTransport({
            "/song/url/v1": lambda p: (
                {"data": [{"url": "http://cdn/a.mp3", "level": "exhigh", "br": 320000}]}
                if p["level"] == "exhigh"
                else {"data": [{"url": None, "level": p["level"]}]}
            )
        })
        client, _ = make_client(transport)

        result = client.song_url(111, "lossless")

        assert result.level == "exhigh"
        tried = [p["level"] for p in transport.params_for("/song/url/v1")]
        assert tried == ["lossless", "exhigh"], tried

    def test_never_tries_levels_above_the_request(self) -> None:
        """★ 请求 exhigh 就只能试 exhigh 及以下。

        `hires`/`jymaster`/`jyeffect` 是 24bit+ 或空间音频，iPod Classic
        播不了——拿到了也是废文件，还白占带宽和磁盘。
        """
        transport = FakeTransport({
            "/song/url/v1": {"data": [{"url": None}]}
        })
        client, _ = make_client(transport)

        client.song_url(111, "exhigh")

        tried = [p["level"] for p in transport.params_for("/song/url/v1")]
        assert tried == ["exhigh", "standard"], tried
        for bad in ("hires", "jymaster", "jyeffect"):
            assert bad not in tried

    def test_unavailable_song_reports_unavailable(self) -> None:
        transport = FakeTransport({"/song/url/v1": {"data": [{"url": None}]}})
        client, _ = make_client(transport)

        result = client.song_url(111, "lossless")

        assert not result.available
        assert len(transport.params_for("/song/url/v1")) == len(QUALITY_LADDER)

    def test_empty_response_does_not_silently_downgrade(self) -> None:
        """★ 瞬时空响应**不能**被当成"这个档位拿不到"。

        实测在批量下载时命中过：某次 song_url 拿到空 data，
        于是那首歌静默落到 128k——唯一的症状只是"文件比预期小"，
        从日志里根本看不出来。
        """
        calls = {"n": 0}

        def handler(params: dict[str, str]) -> dict[str, Any]:
            if params["level"] == "exhigh":
                calls["n"] += 1
                if calls["n"] == 1:
                    return {"data": []}              # 瞬时失败
                return {"data": [{"url": "http://cdn/a.mp3",
                                  "level": "exhigh", "br": 320000}]}
            return {"data": [{"url": "http://cdn/b.mp3",
                              "level": "standard", "br": 128000}]}

        transport = FakeTransport({"/song/url/v1": handler})
        client, _ = make_client(transport)

        result = client.song_url(111, "exhigh")

        assert result.level == "exhigh", "瞬时失败被当成'拿不到'，静默降级了"
        tried = [p["level"] for p in transport.params_for("/song/url/v1")]
        assert tried == ["exhigh", "exhigh"], tried

    def test_genuinely_unavailable_is_not_retried(self) -> None:
        """"data 里有条目但 url 是 null" = 这首歌确实没有。

        这种情况重试多少次都一样，应该直接交给下一档，
        别浪费时间在注定失败的请求上。
        """
        transport = FakeTransport({"/song/url/v1": {"data": [{"url": None}]}})
        client, _ = make_client(transport)

        client.song_url(111, "exhigh")

        tried = [p["level"] for p in transport.params_for("/song/url/v1")]
        assert tried == ["exhigh", "standard"], tried

    def test_invalid_level_is_rejected(self) -> None:
        """对着 iPod 播不了的档位请求，直接报错，别默默给个坏文件。"""
        transport = FakeTransport({})
        client, _ = make_client(transport)

        with pytest.raises(ValueError, match="音质"):
            client.song_url(111, "jyeffect")

    def test_trial_clip_is_flagged(self) -> None:
        """★ 试听片段必须被标出来。

        网易云在"没带 cookie"或"你只能试听"时返回 30 秒片段，
        `freeTrialInfo` 里有 end=30。**不能被当成完整歌曲**。
        """
        transport = FakeTransport({
            "/song/url/v1": {"data": [{
                "url": "http://cdn/trial.mp3",
                "level": "standard",
                "br": 128000,
                "size": 480000,
                "freeTrialInfo": {"fragmentType": -1, "start": 0, "end": 30},
            }]}
        })
        client, _ = make_client(transport)

        result = client.song_url(111, "lossless")

        assert result.url, "URL 是有的"
        assert result.trial is True
        assert result.available is False, "试听片段不能算'可下载'"

    def test_full_song_is_not_flagged_as_trial(self) -> None:
        transport = FakeTransport({
            "/song/url/v1": {"data": [{
                "url": "http://cdn/a.mp3", "level": "exhigh",
                "br": 320000, "size": 5000000,
            }]}
        })
        client, _ = make_client(transport)

        result = client.song_url(111, "lossless")

        assert result.trial is False
        assert result.available is True

    def test_trial_at_top_level_still_tries_lower(self) -> None:
        """高档位只有试听时，继续往下试——万一低档位给的是完整版。"""
        def handler(params: dict[str, str]) -> dict[str, Any]:
            if params["level"] == "lossless":
                return {"data": [{"url": "http://cdn/t.flac", "level": "lossless",
                                  "freeTrialInfo": {"end": 30}}]}
            return {"data": [{"url": "http://cdn/a.mp3", "level": "exhigh",
                              "br": 320000}]}

        transport = FakeTransport({"/song/url/v1": handler})
        client, _ = make_client(transport)

        result = client.song_url(111, "lossless")

        assert result.trial is False
        assert result.level == "exhigh"

    def test_all_levels_trial_returns_the_trial(self) -> None:
        """全是试听就把试听结果返回出去，让调用方拒绝——不能返回"没有"。"""
        transport = FakeTransport({
            "/song/url/v1": {"data": [{
                "url": "http://cdn/t.mp3", "level": "standard",
                "freeTrialInfo": {"end": 30},
            }]}
        })
        client, _ = make_client(transport)

        result = client.song_url(111, "exhigh")

        assert result.trial is True
        assert not result.available

    def test_cookie_is_sent_when_requesting_url(self) -> None:
        """★ 请求下载链接必须带 cookie。

        不带的话网易云返回的试听片段——曾经的 bug 就是这么来的。
        """
        transport = FakeTransport({"/song/url/v1": {"data": [{"url": "http://cdn/a"}]}})
        client, _ = make_client(transport)

        client.song_url(111, "exhigh", cookie="账号cookie")

        assert transport.params_for("/song/url/v1")[0]["cookie"] == "账号cookie"

    def test_instance_cookie_used_when_not_passed(self) -> None:
        transport = FakeTransport({"/song/url/v1": {"data": [{"url": "http://cdn/a"}]}})
        client, _ = make_client(transport)

        client.song_url(111, "exhigh")

        assert transport.params_for("/song/url/v1")[0]["cookie"] == "test-cookie"

    def test_default_quality_is_exhigh(self) -> None:
        assert DEFAULT_QUALITY == "exhigh"
        assert QUALITY_LADDER == ("lossless", "exhigh", "standard")


class TestLikedSongs:
    def test_returns_ids(self) -> None:
        transport = FakeTransport({"/likelist": {"ids": [1, 2, 3]}})
        client, _ = make_client(transport)
        assert client.liked_song_ids(uid=1001) == [1, 2, 3]

    def test_empty_list_is_legitimate_here(self) -> None:
        """likelist 返回空列表是正常的（真的没红心歌），不该被当成限速。"""
        transport = FakeTransport({"/likelist": {"ids": []}})
        client, _ = make_client(transport)
        assert client.liked_song_ids(uid=1001) == []


class TestSongDetails:
    def test_batches_of_100(self) -> None:
        ids = list(range(1, 251))

        def handler(params: dict[str, str]) -> dict[str, Any]:
            wanted = params["ids"].split(",")
            return {"songs": [dict(SONG_RAW, id=int(i)) for i in wanted]}

        transport = FakeTransport({"/song/detail": handler})
        client, _ = make_client(transport)

        songs = client.song_details(ids)

        assert len(songs) == 250
        assert len(transport.params_for("/song/detail")) == 3   # 100+100+50

    def test_null_artist_name_does_not_crash(self) -> None:
        """★ P0 实测踩到：ar[].name 可能是显式 null。"""
        bad = dict(SONG_RAW, ar=[{"name": None}, {"name": "某某"}])
        transport = FakeTransport({"/song/detail": {"songs": [bad]}})
        client, _ = make_client(transport)

        songs = client.song_details([111])

        assert songs[0].artist_text == "某某", songs[0].artist_text
        assert songs[0].label == "歌名 - 某某"

    def test_all_null_artists_gives_empty_string(self) -> None:
        bad = dict(SONG_RAW, ar=[{"name": None}])
        transport = FakeTransport({"/song/detail": {"songs": [bad]}})
        client, _ = make_client(transport)

        song = client.song_details([111])[0]
        assert song.artist_text == ""
        assert song.label == "歌名"

    def test_missing_artist_list(self) -> None:
        bad = dict(SONG_RAW)
        bad.pop("ar")
        transport = FakeTransport({"/song/detail": {"songs": [bad]}})
        client, _ = make_client(transport)

        assert client.song_details([111])[0].artist_text == ""

    def test_partial_response_is_accepted(self) -> None:
        """下架的歌不会返回，导致数量对不上——**不能因此判定成限速**去重试。

        要求数量完全相等的话会白等 4 轮然后报错，而其实数据已经拿到了。
        """
        transport = FakeTransport({
            "/song/detail": {"songs": [SONG_RAW]},   # 要了 3 首只回 1 首
        })
        client, _ = make_client(transport)

        songs = client.song_details([111, 222, 333])

        assert len(songs) == 1
        assert len(transport.params_for("/song/detail")) == 1, "不该重试"


class TestPlaylistTracks:
    def test_single_page(self) -> None:
        transport = FakeTransport({
            "/playlist/track/all": {"songs": [SONG_RAW, dict(SONG_RAW, id=222)]},
        })
        client, _ = make_client(transport)

        songs = client.playlist_tracks(777, expected=2)

        assert [s.id for s in songs] == [111, 222]
        assert len(transport.params_for("/playlist/track/all")) == 1

    def test_paginates_when_full_page(self) -> None:
        """满 1000 首说明还有下一页。"""
        page1 = [dict(SONG_RAW, id=i) for i in range(1, 1001)]
        page2 = [dict(SONG_RAW, id=i) for i in range(1001, 1201)]

        def handler(params: dict[str, str]) -> dict[str, Any]:
            return {"songs": page1 if params["offset"] == "0" else page2}

        transport = FakeTransport({"/playlist/track/all": handler})
        client, _ = make_client(transport)

        songs = client.playlist_tracks(777, expected=1200)

        assert len(songs) == 1200
        offsets = [p["offset"] for p in transport.params_for("/playlist/track/all")]
        assert offsets == ["0", "1000"], offsets

    def test_rate_limited_page_is_retried_not_swallowed(self) -> None:
        """★ 第二页被限速返回空 —— 不能就此收工，否则静默丢数据。

        这条是 P0 最值钱的教训：40 个歌单里 3 个返回空，
        结果唯一歌曲数从 5906 变成了 4468，而且**看不出任何异常**。
        """
        page1 = [dict(SONG_RAW, id=i) for i in range(1, 1001)]
        page2 = [dict(SONG_RAW, id=i) for i in range(1001, 1101)]
        state = {"page2_calls": 0}

        def handler(params: dict[str, str]) -> dict[str, Any]:
            if params["offset"] == "0":
                return {"songs": page1}
            state["page2_calls"] += 1
            if state["page2_calls"] == 1:
                return {"songs": []}          # 被限速
            return {"songs": page2}

        transport = FakeTransport({"/playlist/track/all": handler})
        client, _ = make_client(transport)

        songs = client.playlist_tracks(777, expected=1100)

        assert len(songs) == 1100, f"第二页被吞了，只拿到 {len(songs)} 首"
        assert state["page2_calls"] == 2, "应该重试过"

    def test_unavailable_playlist_raises(self) -> None:
        transport = FakeTransport({"/playlist/track/all": {"songs": []}})
        client, _ = make_client(transport)

        with pytest.raises(NcmError):
            client.playlist_tracks(777, expected=50)


class TestPing:
    def test_true_when_service_alive(self) -> None:
        transport = FakeTransport({"/login/qr/key": {"data": {"unikey": "u"}}})
        client, _ = make_client(transport)
        assert client.ping() is True

    def test_false_when_service_down(self) -> None:
        def boom(_url: str, _timeout: int) -> dict[str, Any]:
            raise NcmError("连接被拒绝")

        client = NcmClient(transport=boom, sleep=lambda _s: None)
        assert client.ping() is False


class TestLoginAndSwitching:
    def test_status_returns_account(self) -> None:
        transport = FakeTransport({
            "/login/status": {
                "data": {"account": {"id": 1001, "userName": "甲", "vipType": 11}}
            }
        })
        client, _ = make_client(transport)

        account = client.login_status("cookie-A")

        assert account.uid == 1001
        assert account.nickname == "甲"
        assert account.vip_type == 11
        assert account.cookie == "cookie-A"

    def test_invalid_cookie_raises_not_logged_in(self) -> None:
        transport = FakeTransport({"/login/status": {"data": {}}})
        client, _ = make_client(transport)

        with pytest.raises(NotLoggedInError, match="失效"):
            client.login_status("过期cookie")

    def test_no_cookie_at_all_raises(self) -> None:
        client = NcmClient(transport=FakeTransport({}), cookie="")
        with pytest.raises(NotLoggedInError, match="先登录"):
            client.login_status()

    def test_qr_login_start_returns_png(self) -> None:
        png = b"\x89PNG fake"
        transport = FakeTransport({
            "/login/qr/key": {"data": {"unikey": "KEY-123"}},
            "/login/qr/create": {
                "data": {"qrimg": "data:image/png;base64," + __import__("base64")
                         .b64encode(png).decode()}
            },
        })
        client, _ = make_client(transport)

        unikey, image = client.qr_login_start()

        assert unikey == "KEY-123"
        assert image == png

    def test_qr_create_without_image_raises(self) -> None:
        transport = FakeTransport({
            "/login/qr/key": {"data": {"unikey": "K"}},
            "/login/qr/create": {"data": {}},
        })
        client, _ = make_client(transport)

        with pytest.raises(NcmError):
            client.qr_login_start()

    def test_qr_poll_states(self) -> None:
        transport = FakeTransport({
            "/login/qr/check": [
                {"code": 801},
                {"code": 802},
                {"code": 803, "cookie": "MUSIC_U=abc"},
            ]
        })
        client, _ = make_client(transport)

        assert client.qr_login_poll("K") == (801, "")
        assert client.qr_login_poll("K") == (802, "")
        code, cookie = client.qr_login_poll("K")
        assert code == 803
        assert cookie == "MUSIC_U=abc"


class TestCookieHandling:
    def test_cookie_is_attached_to_requests(self) -> None:
        transport = FakeTransport({"/likelist": {"ids": []}})
        client, _ = make_client(transport)

        client.liked_song_ids(uid=1)

        assert transport.params_for("/likelist")[0]["cookie"] == "test-cookie"

    def test_per_call_cookie_overrides_instance(self) -> None:
        """切账号靠这个：不重建客户端也能换 cookie。"""
        transport = FakeTransport({"/likelist": {"ids": []}})
        client, _ = make_client(transport)

        client.liked_song_ids(uid=1, cookie="另一个账号的cookie")

        assert transport.params_for("/likelist")[0]["cookie"] == "另一个账号的cookie"

    def test_timestamp_is_attached(self) -> None:
        transport = FakeTransport({"/likelist": {"ids": []}})
        client, _ = make_client(transport)

        client.liked_song_ids(uid=1)

        assert "timestamp" in transport.params_for("/likelist")[0]


class TestSongModel:
    def test_from_api_full(self) -> None:
        song = Song.from_api(SONG_RAW)
        assert song.id == 111
        assert song.album == "专辑"
        assert song.cover_url == "https://p.example/c.jpg"
        assert song.duration_ms == 200000
        assert song.track_no == 3

    def test_from_api_empty(self) -> None:
        song = Song.from_api({})
        assert song.id == 0
        assert song.artist_text == ""
        assert song.label == ""

    def test_fee_zero_is_preserved(self) -> None:
        """fee=0 是"免费"，不能被 `or` 当成假值吃掉。"""
        assert Song.from_api(dict(SONG_RAW, fee=0)).fee == 0

    def test_missing_fee_defaults_to_zero(self) -> None:
        raw = dict(SONG_RAW)
        raw.pop("fee")
        assert Song.from_api(raw).fee == 0
