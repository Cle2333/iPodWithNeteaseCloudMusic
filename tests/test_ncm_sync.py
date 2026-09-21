"""同步规划与执行的测试。

规划是"要下什么"的唯一判据来源，判错的两个方向代价不对称：

* 判成"要下"但实际已有 → 白下一次，浪费带宽，但不出错
* 判成"已有"但实际没有 → **歌永远进不了 iPod，而且不报错**

所以这里重点测后者：**状态库说"有了"时到底信不信**、
**缓存文件没了会不会被当成已有**。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ipod_cli.ncm.client import NcmClient, Playlist, Song, SongUrl
from ipod_cli.ncm.state import StateStore
from ipod_cli.ncm.sync import (
    ACTION_DOWNLOAD,
    ACTION_SKIP,
    ACTION_UNAVAILABLE,
    TARGET_IPOD,
    TARGET_LOCAL,
    SyncSource,
    execute_downloads,
    fetch_source_songs,
    plan_sync,
)


def make_song(song_id: int, **overrides: Any) -> Song:
    base: dict[str, Any] = {
        "id": song_id,
        "name": f"歌{song_id}",
        "artists": ["艺人"],
        "album": "专辑",
        "cover_url": "",
        "duration_ms": 1000,
        "track_no": 1,
        "fee": 0,
    }
    base.update(overrides)
    return Song(**base)


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "ncm.db")


@pytest.fixture()
def client() -> NcmClient:
    """不带传输层的客户端——本组测试全都把网络方法替换掉。"""
    return NcmClient(cookie="test", sleep=lambda _s: None)


def stub_liked(monkeypatch, songs: list[Song], playlist_id: int = 500) -> None:
    """让"我喜欢的音乐"返回指定歌曲，不碰网络。

    ★ 走的是**歌单接口**（``/playlist/track/all``），不是 ``/likelist``：
    likelist 只保证"有哪些歌"，顺序是网易云内部的、跟 App 显示的对不上，
    而且不带元数据还得再补一轮 song/detail。

    四个方法都打桩：前两个是主路径，后两个是"拿不到歌单 id"时的兜底，
    两条路都得能跑通。
    """
    monkeypatch.setattr(
        NcmClient, "liked_playlist_id", lambda self, uid, cookie="": playlist_id
    )
    monkeypatch.setattr(
        NcmClient, "playlist_tracks", lambda self, pid, expected=0, cookie="": list(songs)
    )
    monkeypatch.setattr(
        NcmClient, "liked_song_ids", lambda self, uid, cookie="": [s.id for s in songs]
    )
    monkeypatch.setattr(
        NcmClient, "song_details", lambda self, ids, cookie="": [
            s for s in songs if s.id in ids
        ]
    )


class TestSyncSourceFactories:
    def test_liked(self) -> None:
        source = SyncSource.liked()
        assert source.kind == "liked"
        assert source.name == "我喜欢的音乐"

    def test_playlist(self) -> None:
        source = SyncSource.playlist(777, "我的歌单")
        assert source.kind == "playlist"
        assert source.playlist_id == 777
        assert source.name == "我的歌单"


class TestPlanSync:
    def test_all_new_songs_are_to_download(self, client, store, monkeypatch) -> None:
        stub_liked(monkeypatch, [make_song(1), make_song(2)])

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")

        assert len(plan.to_download) == 2
        assert plan.to_skip == []
        assert all(i.action == ACTION_DOWNLOAD for i in plan.items)

    def test_already_synced_songs_are_skipped(self, client, store, monkeypatch) -> None:
        """★ 幂等的基础：已经在 iPod 上的不再下一次。"""
        stub_liked(monkeypatch, [make_song(1), make_song(2)])
        store.mark_synced(1, ipod_location="F00/a.mp3")

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")

        assert [i.song.id for i in plan.to_download] == [2]
        assert [i.song.id for i in plan.to_skip] == [1]
        assert plan.to_skip[0].action == ACTION_SKIP
        assert plan.to_skip[0].reason == "已同步"

    def test_cached_download_still_needs_to_reach_the_ipod(
        self, client, store, monkeypatch, tmp_path: Path
    ) -> None:
        """★ 本地缓存命中**不等于**不用管了。

        歌还在本地、没进 iPod，就必须继续处理。早先的版本把它算作"跳过"，
        于是 push 会先显示"要写入 0 首"、然后又真的写了 3 首——自相矛盾，
        而且如果用户只看汇总就会以为没事。
        """
        stub_liked(monkeypatch, [make_song(1)])
        cached = tmp_path / "1.mp3"
        cached.write_bytes(b"ID3 fake")
        store.remember_download(1, cached, size=100)

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")

        assert len(plan.to_download) == 1, "缓存命中被误判成'不用处理'了"
        assert plan.to_download[0].cached is True
        # 但**不用重新下载**——这是 cached 标记的真正作用
        assert plan.needs_fetch == []
        assert plan.to_skip == []

    def test_uncached_song_needs_fetch(self, client, store, monkeypatch) -> None:
        stub_liked(monkeypatch, [make_song(1)])

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")

        assert len(plan.needs_fetch) == 1
        assert plan.needs_fetch[0].cached is False

    def test_mixed_cached_and_not(self, client, store, monkeypatch,
                                 tmp_path: Path) -> None:
        """一批里有的已缓存、有的没有，要能分开数。"""
        stub_liked(monkeypatch, [make_song(1), make_song(2), make_song(3)])
        cached = tmp_path / "1.mp3"
        cached.write_bytes(b"ID3")
        store.remember_download(1, cached)

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")

        assert len(plan.to_download) == 3
        assert len(plan.needs_fetch) == 2
        assert {i.song.id for i in plan.needs_fetch} == {2, 3}

    def test_stale_cache_does_not_hide_a_song(self, client, store, monkeypatch,
                                             tmp_path: Path) -> None:
        """★ 缓存记录还在但文件被删了 —— 必须重新下，不能当成已有。

        信了它，这首歌既不在本地也不在 iPod 上，而且永远不会被处理。
        """
        stub_liked(monkeypatch, [make_song(1)])
        cached = tmp_path / "1.mp3"
        cached.write_bytes(b"ID3")
        store.remember_download(1, cached)
        cached.unlink()

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")

        assert len(plan.to_download) == 1, "文件没了却当成已有，歌就再也进不去了"

    def test_limit_reduces_scope(self, client, store, monkeypatch) -> None:
        """--limit 是"先试几首"的保险，必须真的限制住。"""
        stub_liked(monkeypatch, [make_song(i) for i in range(1, 11)])

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh", limit=3)

        assert len(plan.items) == 3

    def test_only_ids_plans_just_those_songs(self, client, store, monkeypatch) -> None:
        """★ only_ids 是界面上的"下载选中的 N 首"，只该规划选中的那些。"""
        stub_liked(monkeypatch, [make_song(i) for i in range(1, 11)])

        plan = plan_sync(
            client, store, SyncSource.liked(), level="exhigh", only_ids={2, 5, 9}
        )

        assert sorted(i.song.id for i in plan.items) == [2, 5, 9]

    def test_only_ids_empty_means_empty_not_everything(
        self, client, store, monkeypatch
    ) -> None:
        """★ 空集合 = 一首都不选，**不是**"没限制"。

        这个区别要紧：把空集当"全部"的话，用户取消所有勾选后一点下载，
        几百首就开始跑了。
        """
        stub_liked(monkeypatch, [make_song(i) for i in range(1, 11)])

        plan = plan_sync(
            client, store, SyncSource.liked(), level="exhigh", only_ids=set()
        )

        assert plan.items == []

    def test_only_ids_none_means_no_filter(self, client, store, monkeypatch) -> None:
        """不传 only_ids = 整单，行为跟以前一样。"""
        stub_liked(monkeypatch, [make_song(i) for i in range(1, 6)])

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")

        assert len(plan.items) == 5

    def test_only_ids_keeps_original_order(self, client, store, monkeypatch) -> None:
        """选中集是 set，但没有顺序——顺序必须跟着**歌单原序**走。

        否则界面上勾了第 1、3、5 首，下出来的顺序是随机的。
        """
        stub_liked(monkeypatch, [make_song(i) for i in range(1, 11)])

        plan = plan_sync(
            client, store, SyncSource.liked(), level="exhigh",
            only_ids={9, 1, 5},
        )

        assert [i.song.id for i in plan.items] == [1, 5, 9]

    def test_only_ids_on_playlist_source(self, client, store, monkeypatch) -> None:
        """歌单源也要支持选中子集（歌单接口自带元数据，走的是另一条分支）。"""
        songs = [make_song(i) for i in range(1, 8)]
        monkeypatch.setattr(
            NcmClient, "playlist_tracks",
            lambda self, pid, cookie="": list(songs),
        )

        plan = plan_sync(
            client, store, SyncSource.playlist(100, "通勤歌单"),
            level="exhigh", only_ids={3, 4},
        )

        assert [i.song.id for i in plan.items] == [3, 4]

    def test_estimate_uses_level_typical_size(self, client, store, monkeypatch) -> None:
        stub_liked(monkeypatch, [make_song(i) for i in range(1, 5)])

        lossless = plan_sync(client, store, SyncSource.liked(), level="lossless")
        exhigh = plan_sync(client, store, SyncSource.liked(), level="exhigh")

        assert lossless.estimated_mb > exhigh.estimated_mb
        assert lossless.estimated_mb == pytest.approx(100.0, abs=1.0)   # 4 × 25
        assert exhigh.estimated_mb == pytest.approx(28.0, abs=1.0)      # 4 × 7

    def test_check_urls_marks_unavailable(self, client, store, monkeypatch) -> None:
        stub_liked(monkeypatch, [make_song(1), make_song(2)])
        monkeypatch.setattr(
            NcmClient, "song_url",
            lambda self, sid, level="exhigh", cookie="": (
                SongUrl(song_id=sid) if sid == 2
                else SongUrl(song_id=sid, url="http://cdn/a", level="exhigh",
                             size=12345678)
            ),
        )

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh",
                         check_urls=True)

        assert [i.song.id for i in plan.to_download] == [1]
        assert [i.song.id for i in plan.unavailable] == [2]
        assert plan.unavailable[0].action == ACTION_UNAVAILABLE
        assert "无版权" in plan.unavailable[0].reason

    def test_check_urls_uses_actual_size(self, client, store, monkeypatch) -> None:
        stub_liked(monkeypatch, [make_song(1)])
        monkeypatch.setattr(
            NcmClient, "song_url",
            lambda self, sid, level="exhigh", cookie="": SongUrl(
                song_id=sid, url="http://cdn/a", level="lossless", size=10485760
            ),
        )

        plan = plan_sync(client, store, SyncSource.liked(), level="lossless",
                         check_urls=True)

        assert plan.estimated_bytes == 10485760
        assert plan.to_download[0].actual_level == "lossless"

    def test_no_check_urls_means_no_url_requests(self, client, store, monkeypatch) -> None:
        """★ 默认不查链接——这是"别把号搞封"的关键。

        246 首红心歌逐首查链接要几百次请求，而规划阶段根本不需要。
        """
        stub_liked(monkeypatch, [make_song(i) for i in range(1, 51)])
        calls = {"n": 0}

        def spy(*_a, **_k):
            calls["n"] += 1
            return SongUrl(song_id=0)

        monkeypatch.setattr(NcmClient, "song_url", spy)

        plan_sync(client, store, SyncSource.liked(), level="exhigh")

        assert calls["n"] == 0, "默认路径下不该有任何链接查询"

    def test_playlist_source_does_not_refetch_details(self, client, store,
                                                     monkeypatch) -> None:
        """歌单接口自带元数据，别再补一轮 song_detail（省请求）。"""
        monkeypatch.setattr(
            NcmClient, "playlist_tracks",
            lambda self, pid, expected=0, cookie="": [make_song(1), make_song(2)],
        )
        called = {"details": 0}

        def spy_details(*_a, **_k):
            called["details"] += 1
            return []

        monkeypatch.setattr(NcmClient, "song_details", spy_details)

        plan_sync(client, store, SyncSource.playlist(7, "歌单"), level="exhigh")

        assert called["details"] == 0


class TestSyncedOnDevice:
    """★ 状态库说"已同步"不等于**这台设备上**真有。

    彩排环境实测踩到：拿真机状态搭的彩排把 3 首歌记成"已同步"，真机同步时
    被误判成"已在设备上"，汇总显示"要写入 0 首"（其实要写 3 首），
    播放列表还被算成 6 个成员（3 个是指向不存在曲目的幽灵条目）。
    """

    def test_location_must_match_a_real_track(self, store) -> None:
        from ipod_cli.ncm.sync import synced_on_device

        library = SimpleNamespace(tracks=[
            SimpleNamespace(location=":iPod_Control:Music:F00:AAAA.mp3"),
        ])
        # 状态库说两首都同步过，但只有一手的 location 在设备上
        store.mark_synced(1, ipod_location=":iPod_Control:Music:F00:AAAA.mp3")
        store.mark_synced(2, ipod_location=":iPod_Control:Music:F09:ZZZZ.mp3")

        assert synced_on_device(store, library) == {1}

    def test_empty_state(self, store) -> None:
        from ipod_cli.ncm.sync import synced_on_device

        library = SimpleNamespace(tracks=[])
        assert synced_on_device(store, library) == set()

    def test_empty_library(self, store) -> None:
        """设备被还原空了 —— 状态库里所有记录都不算数。"""
        from ipod_cli.ncm.sync import synced_on_device

        store.mark_synced(1, ipod_location=":iPod_Control:Music:F00:AAAA.mp3")
        library = SimpleNamespace(tracks=[])
        assert synced_on_device(store, library) == set()

    def test_plan_uses_device_truth(self, client, store, monkeypatch) -> None:
        """plan 拿到 library 时，必须以设备内容为准重新判断。"""
        from ipod_cli.ncm.sync import synced_on_device  # noqa: F401

        stub_liked(monkeypatch, [make_song(1)])
        # 状态库说同步过了，但设备的 location 对不上（比如被还原过）
        store.mark_synced(1, ipod_location=":iPod_Control:Music:F00:GONE.mp3")

        library = SimpleNamespace(tracks=[
            SimpleNamespace(location=":iPod_Control:Music:F00:OTHER.mp3"),
        ])
        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh",
                         library=library)

        assert len(plan.to_download) == 1, "被状态库的陈旧记录骗了"

    def test_no_library_falls_back_to_state(self, client, store, monkeypatch) -> None:
        """只做下载规划时没有设备库，退回信状态库（那是唯一依据）。"""
        stub_liked(monkeypatch, [make_song(1)])
        store.mark_synced(1, ipod_location=":iPod_Control:Music:F00:AAAA.mp3")

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")

        assert plan.to_download == []
        assert len(plan.to_skip) == 1


class TestExecuteDownloads:
    def test_successful_download_is_recorded(self, client, store, monkeypatch,
                                             tmp_path: Path) -> None:
        stub_liked(monkeypatch, [make_song(1)])
        _stub_download(monkeypatch, tmp_path)

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")
        outcome = execute_downloads(client, store, plan, tmp_path / "cache")

        assert len(outcome.downloaded) == 1
        assert outcome.total_bytes > 0
        # 记进缓存，下次规划就能跳过
        assert store.cached_download(1) is not None
        assert store.stats()["downloaded"] == 1

    def test_one_failure_does_not_stop_the_rest(self, client, store, monkeypatch,
                                                tmp_path: Path) -> None:
        """一首歌失败不该中断整批——其余的照下，失败的记下来。"""
        stub_liked(monkeypatch, [make_song(1), make_song(2), make_song(3)])
        _stub_download(monkeypatch, tmp_path, fail_ids={2})

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")
        outcome = execute_downloads(client, store, plan, tmp_path / "cache")

        assert len(outcome.downloaded) == 2
        assert len(outcome.failed) == 1
        assert outcome.failed[0][0].id == 2
        # 失败的不该被记进缓存（否则下次会被跳过）
        assert store.cached_download(2) is None

    def test_downloads_are_sequential(self, client, store, monkeypatch,
                                      tmp_path: Path) -> None:
        """★ 必须串行。并发会触发网易云风控——用户明确提过这个担心。"""
        import ipod_cli.ncm.sync as sync_mod
        from ipod_cli.ncm.downloader import DownloadResult

        stub_liked(monkeypatch, [make_song(i) for i in range(1, 6)])
        active = {"now": 0, "max": 0}

        def tracker(*_a, **_k):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            active["now"] -= 1        # 串行完成，瞬时并发应该是 1
            return DownloadResult(
                song_id=1, path=tmp_path / "x.mp3", level="exhigh",
                size=100, has_cover=False,
            )

        monkeypatch.setattr(sync_mod, "download_song", tracker)

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")
        execute_downloads(client, store, plan, tmp_path / "cache")

        assert active["max"] == 1

    def test_on_item_reports_structured_progress(self, client, store, monkeypatch,
                                                 tmp_path: Path) -> None:
        """★ 结构化进度：界面靠它驱动进度条。

        ``progress`` 是**给人看的文本**（措辞随时会改），``on_item`` 是
        **给程序用的数字**。让界面去 parse ``✓ [3/246]`` 的话，改一句文案
        就把进度条弄坏了，而且歌名里恰好出现的数字（比如《7/11》）会被
        认成进度。
        """
        stub_liked(monkeypatch, [make_song(1), make_song(2), make_song(3)])
        _stub_download(monkeypatch, tmp_path)

        calls: list[tuple[int, int]] = []
        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")
        execute_downloads(client, store, plan, tmp_path / "cache",
                          on_item=lambda d, t: calls.append((d, t)))

        assert calls[0] == (0, 3), (
            "没先报一次总数——界面要等第一首下完才知道总共几首"
        )
        assert calls[-1] == (3, 3)
        assert calls == [(0, 3), (1, 3), (2, 3), (3, 3)]

    def test_on_item_counts_failures_too(self, client, store, monkeypatch,
                                         tmp_path: Path) -> None:
        """失败的也要计数——否则进度条卡住不动，看起来像死机。"""
        stub_liked(monkeypatch, [make_song(1), make_song(2), make_song(3)])
        _stub_download(monkeypatch, tmp_path, fail_ids={2})

        calls: list[tuple[int, int]] = []
        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")
        execute_downloads(client, store, plan, tmp_path / "cache",
                          on_item=lambda d, t: calls.append((d, t)))

        assert calls[-1] == (3, 3), f"失败的那首没被计数：{calls}"

    def test_on_item_is_optional(self, client, store, monkeypatch,
                                 tmp_path: Path) -> None:
        """不传 on_item 也要能跑——CLI 那条路径就不传。"""
        stub_liked(monkeypatch, [make_song(1)])
        _stub_download(monkeypatch, tmp_path)

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")
        outcome = execute_downloads(client, store, plan, tmp_path / "cache")

        assert len(outcome.downloaded) == 1

    def test_nothing_to_do_is_fine(self, client, store, monkeypatch) -> None:
        stub_liked(monkeypatch, [])

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")
        outcome = execute_downloads(client, store, plan, Path("."))

        assert outcome.downloaded == []
        assert outcome.failed == []


def _stub_download(monkeypatch, tmp_path: Path, fail_ids: set[int] | None = None) -> None:
    """替换掉真下载，只验证编排逻辑。"""
    import ipod_cli.ncm.sync as sync_mod
    from ipod_cli.ncm.downloader import DownloadError, DownloadResult

    fail_ids = fail_ids or set()

    def fake(client, song, dest_dir, *, level, cookie="", with_cover=True,
             progress=None):
        if song.id in fail_ids:
            raise DownloadError("模拟失败")
        dest = Path(dest_dir) / f"{song.id}.mp3"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ID3 fake audio")
        return DownloadResult(
            song_id=song.id, path=dest, level=level,
            size=dest.stat().st_size, has_cover=False,
        )

    monkeypatch.setattr(sync_mod, "download_song", fake)


class TestPlanTarget:
    """「同步到 iPod」和「下载到本地」的跳过条件**不一样**。

    用户报的 bug："我把本地的歌删了重新下载，它提示已准备就绪。"
    根因就是这里——只要歌在 iPod 上就无条件跳过，于是本地文件没了
    也不去下。
    """

    def test_local_target_redownloads_when_the_file_is_gone(
        self, client, store, monkeypatch, tmp_path
    ) -> None:
        """★ 核心回归：歌在 iPod 上、本地文件没了、用户要"下载到本地"
        → **必须去下载**，不能跳过。"""
        stub_liked(monkeypatch, [make_song(1)])
        store.mark_synced(1, ipod_location="F00/a.mp3")

        plan = plan_sync(
            client, store, SyncSource.liked(), level="exhigh", target=TARGET_LOCAL
        )

        assert [i.song.id for i in plan.needs_fetch] == [1], (
            "本地文件没了却不去下载——用户看到的就是「都已经就绪」"
        )
        assert plan.to_skip == []

    def test_local_target_skips_when_the_file_is_there(
        self, client, store, monkeypatch, tmp_path
    ) -> None:
        """本地已经有了 → 这才是真的没事可做。"""
        stub_liked(monkeypatch, [make_song(1)])
        cached = tmp_path / "a.mp3"
        cached.write_bytes(b"x")
        store.remember_download(1, cached, size=100)

        plan = plan_sync(
            client, store, SyncSource.liked(), level="exhigh", target=TARGET_LOCAL
        )

        assert plan.needs_fetch == []
        assert [i.song.id for i in plan.to_skip] == [1]
        assert plan.to_skip[0].reason == "已下载"

    def test_local_target_ignores_the_state_db(
        self, client, store, monkeypatch
    ) -> None:
        """★ 本地下载**不查状态库**。

        `store.synced_ids()` 不分设备——彩排或另一台机器写过的记录照样算数。
        本地下载本来就不关心 iPod，不该让一份不可靠的记录决定要不要下载。
        """
        stub_liked(monkeypatch, [make_song(1)])
        store.mark_synced(1, ipod_location="F00/另一台机器.mp3")

        plan = plan_sync(
            client, store, SyncSource.liked(), level="exhigh", target=TARGET_LOCAL
        )

        assert [i.song.id for i in plan.needs_fetch] == [1]

    def test_ipod_target_still_skips_what_is_on_the_device(
        self, client, store, monkeypatch
    ) -> None:
        """既有行为不能变：要同步到 iPod，设备上有了就跳过。"""
        stub_liked(monkeypatch, [make_song(1), make_song(2)])
        store.mark_synced(1, ipod_location="F00/a.mp3")

        plan = plan_sync(
            client, store, SyncSource.liked(), level="exhigh", target=TARGET_IPOD
        )

        assert [i.song.id for i in plan.to_skip] == [1]
        assert [i.song.id for i in plan.to_download] == [2]

    def test_default_target_is_ipod(self, client, store, monkeypatch) -> None:
        """默认保持原行为——CLI 和既有调用方不受影响。"""
        stub_liked(monkeypatch, [make_song(1)])
        store.mark_synced(1, ipod_location="F00/a.mp3")

        plan = plan_sync(client, store, SyncSource.liked(), level="exhigh")
        assert [i.song.id for i in plan.to_skip] == [1]

    def test_ipod_target_resends_a_song_whose_file_is_gone(
        self, client, store, monkeypatch, tmp_path
    ) -> None:
        """要同步到 iPod、歌还没进设备、本地文件没了 → 得重新下。

        （这是原本就对的：缓存命中只打标记不跳过。）
        """
        stub_liked(monkeypatch, [make_song(1)])
        gone = tmp_path / "gone.mp3"
        store.remember_download(1, gone)      # 记录在，文件不在

        plan = plan_sync(
            client, store, SyncSource.liked(), level="exhigh", target=TARGET_IPOD
        )

        assert [i.song.id for i in plan.needs_fetch] == [1]
        assert not plan.to_download[0].cached


class TestLikedOrder:
    """「我喜欢的音乐」取歌走的路径 + 顺序。

    `/likelist` 只保证"有哪些歌"，顺序是网易云内部存的、跟 App 显示的对不上
    （实测 246 首：集合完全一样，同位置零匹配）。要用 App 的顺序就得走
    歌单接口 `/playlist/track/all`，顺带把元数据也带回来，还更省请求。
    """

    def test_order_comes_from_the_playlist_not_likelist(
        self, client, store, monkeypatch
    ) -> None:
        """★ 顺序必须来自歌单接口。likelist 故意给相反的顺序，结果不能被它带偏。"""
        monkeypatch.setattr(
            NcmClient, "liked_song_ids", lambda self, uid, cookie="": [3, 2, 1]
        )
        monkeypatch.setattr(
            NcmClient, "liked_playlist_id", lambda self, uid, cookie="": 777
        )
        seen: dict[str, int] = {}

        def fake_tracks(self, pid, expected=0, cookie=""):
            seen["pid"] = pid
            return [make_song(i) for i in (1, 2, 3)]

        monkeypatch.setattr(NcmClient, "playlist_tracks", fake_tracks)

        songs = fetch_source_songs(client, SyncSource.liked(), uid=1)

        assert [s.id for s in songs] == [1, 2, 3], (
            "顺序该跟 App 一致（歌单接口），不是 likelist 那个内部顺序"
        )
        assert seen["pid"] == 777

    def test_falls_back_to_likelist_when_the_id_is_unknown(
        self, client, store, monkeypatch
    ) -> None:
        """★ 拿不到歌单 id 要退回 likelist。

        顺序会不对，但**有歌**远好过整个同步失败——"读不到我喜欢的音乐"
        不该是硬故障。
        """
        stub_liked(monkeypatch, [make_song(1), make_song(2)], playlist_id=0)

        songs = fetch_source_songs(client, SyncSource.liked(), uid=1)

        assert [s.id for s in songs] == [1, 2]

    def test_a_supplied_playlist_id_skips_the_lookup(
        self, client, store, monkeypatch
    ) -> None:
        """界面把歌单 id 带过来了（歌单列表里本来就有），就别再发请求解析它。"""

        def boom(self, uid, cookie=""):
            raise AssertionError("界面已经给了歌单 id，不该再去 /user/playlist 解析")

        monkeypatch.setattr(NcmClient, "liked_playlist_id", boom)
        monkeypatch.setattr(
            NcmClient, "playlist_tracks",
            lambda self, pid, expected=0, cookie="": [make_song(9)],
        )

        songs = fetch_source_songs(
            client, SyncSource.liked(playlist_id=888), uid=1
        )

        assert [s.id for s in songs] == [9]

    def test_no_extra_metadata_round_trip(self, client, store, monkeypatch) -> None:
        """★ 歌单接口自带元数据，不该再补一轮 song/detail（这正是省下来的请求）。"""

        def boom(self, ids, cookie=""):
            raise AssertionError("曲目已经带元数据了，不该再问一次 song/detail")

        stub_liked(monkeypatch, [make_song(1), make_song(2)])
        monkeypatch.setattr(NcmClient, "song_details", boom)

        songs = fetch_source_songs(client, SyncSource.liked(), uid=1)

        assert len(songs) == 2


class TestLikedPlaylistId:
    def test_found_by_special_type_not_by_name(self, client, monkeypatch) -> None:
        """★ 只能认 specialType，不能按名字找——用户把名字改过（实测改成了
        "被改过名的喜欢的音乐"），按名字找必然落空。"""
        monkeypatch.setattr(
            NcmClient, "user_playlists",
            lambda self, uid, cookie="": [
                Playlist(id=1, name="我喜欢的音乐", track_count=1),          # 名字像，但不是
                Playlist(id=2, name="被改过名的喜欢的音乐", track_count=9,
                         special_type=5),                                     # 就是它
            ],
        )
        assert client.liked_playlist_id(uid=1) == 2

    def test_returns_zero_when_absent(self, client, monkeypatch) -> None:
        """没有那种歌单就返回 0（调用方据此走兜底），不抛异常。"""
        monkeypatch.setattr(
            NcmClient, "user_playlists",
            lambda self, uid, cookie="": [Playlist(id=1, name="随便", track_count=1)],
        )
        assert client.liked_playlist_id(uid=1) == 0

class Test设备核对按歌名兜底:
    """★ 回归：数据库被重建后，同一个曲目会换一个文件位置。

    iPod 上的文件名是随机的（``F01:CQRR.mp3``）。iTunes 同步过、设备恢复过
    备份、或别的工具重导过之后，记录里的位置就失效了——**歌还是那首歌**。

    只比位置的话，那些歌会被判成"没同步过"，下次同步再导一遍，
    **歌就重复了**。实测踩到：真机 10 条记录只有 1 条位置对得上，而按
    歌名 + 艺人查有 6 首确实在设备上。
    """

    def _track(self, location: str, title: str = "", artist: str = ""):
        return SimpleNamespace(location=location, title=title, artist=artist)

    def test_位置变了但歌名艺人对得上_仍算在设备上(self, store) -> None:
        from ipod_cli.ncm.sync import synced_on_device

        # 记录里的位置是旧的（数据库重建前），设备上现在叫别的名字
        store.mark_synced(
            1,
            ipod_location=":iPod_Control:Music:F01:CQRR.mp3",
            name="甲乙丙丁",
            artist="李佳薇",
        )

        library = SimpleNamespace(tracks=[
            self._track(":iPod_Control:Music:F07:ZZZZ.mp3", "甲乙丙丁", "李佳薇"),
        ])

        assert synced_on_device(store, library) == {1}, (
            "位置对不上就判成没同步过——下次同步会把这首歌再导一遍，歌就重复了"
        )

    def test_艺人不同就不算同一首(self, store) -> None:
        """保守一点：同名不同艺人不能当成同一首。"""
        from ipod_cli.ncm.sync import synced_on_device

        store.mark_synced(
            1,
            ipod_location=":iPod_Control:Music:F01:CQRR.mp3",
            name="告别",
            artist="甲",
        )

        library = SimpleNamespace(tracks=[
            self._track(":iPod_Control:Music:F07:ZZZZ.mp3", "告别", "乙"),
        ])

        assert synced_on_device(store, library) == set()

    def test_设备上真没有的仍然不算(self, store) -> None:
        """兜底不能变成"什么都算有"——那会让该同步的歌永远进不去。"""
        from ipod_cli.ncm.sync import synced_on_device

        store.mark_synced(
            1,
            ipod_location=":iPod_Control:Music:F01:CQRR.mp3",
            name="红色高跟鞋",
            artist="某人",
        )

        library = SimpleNamespace(tracks=[
            self._track(":iPod_Control:Music:F07:ZZZZ.mp3", "完全不同的歌", "别人"),
        ])

        assert synced_on_device(store, library) == set()

    def test_没存歌名的老记录退回到只看位置(self, store) -> None:
        """老记录里 name/artist 是空的，不能因此把设备上的歌全认成"同一首"。"""
        from ipod_cli.ncm.sync import synced_on_device

        store.mark_synced(1, ipod_location=":iPod_Control:Music:F01:CQRR.mp3")
        library = SimpleNamespace(tracks=[
            self._track(":iPod_Control:Music:F07:ZZZZ.mp3", "随便什么", "随便谁"),
        ])

        assert synced_on_device(store, library) == set()

class Test从设备库出发判断:
    """★ 回归：**设备上有多少歌，不能取决于状态库里记了多少条。**

    以前走的是 ``synced_on_device``（遍历状态库的同步记录逐条核对设备位置）。
    方向反了：设备上有多少歌，取决于我们记过多少条。

    实测：用户前一天导进 130 首，状态库里只有 10 条记录，界面上另外 120 首
    全部显示"iPod 上没有"——而设备库里有。用户的原话是：

        "这个状态更新难道不是应该扫描 iPod 的数据库吗"

    对，就该那样。所以改成 ``songs_on_device``：拿这批歌去问设备库。
    """

    def _track(self, title: str, artist: str, db_id: int = 1):
        return SimpleNamespace(
            title=title, artist=artist, db_track_id=db_id,
            location=f":iPod_Control:Music:F00:{db_id}.mp3",
        )

    def _song(self, sid: int, name: str, artist: str):
        return SimpleNamespace(id=sid, name=name, artist_text=artist)

    def test_状态库里没有记录的歌_只要在设备上就算(self) -> None:
        """★ 核心：没记过 ≠ 不在设备上。"""
        from ipod_cli.ncm.sync import songs_on_device

        library = SimpleNamespace(tracks=[
            self._track("虚拟", "陈粒", db_id=7),
            self._track("别的歌", "别人", db_id=8),
        ])
        songs = [self._song(1, "虚拟", "陈粒")]

        # 注意：**不传 store**——状态库完全是空的，照样能判
        assert songs_on_device(library, songs) == {1}

    def test_同名不同艺人_算两首不同的歌(self) -> None:
        """歌名一样但是翻唱，不能混为一谈。

        实测数据里这种很多：歌单里"普通朋友"是宋雨琦，"天天"是刘大拿，
        而设备上是陶喆的——只比歌名会把它们判成同一首，结果是
        "该同步的歌被当成已在设备上，永远进不去"。
        """
        from ipod_cli.ncm.sync import songs_on_device

        library = SimpleNamespace(tracks=[self._track("普通朋友", "陶喆")])
        songs = [self._song(1, "普通朋友", "宋雨琦")]

        assert songs_on_device(library, songs) == set()

    def test_设备上没有的不会被算进来(self) -> None:
        """兜底不能变成"什么都算有"——那会让该同步的歌永远进不去。"""
        from ipod_cli.ncm.sync import songs_on_device

        library = SimpleNamespace(tracks=[self._track("在的", "甲")])
        songs = [self._song(1, "在的", "甲"), self._song(2, "不在的", "乙")]

        assert songs_on_device(library, songs) == {1}

    def test_空歌单和空设备都不炸(self) -> None:
        from ipod_cli.ncm.sync import songs_on_device

        empty = SimpleNamespace(tracks=[])
        assert songs_on_device(empty, []) == set()
        assert songs_on_device(empty, [self._song(1, "x", "y")]) == set()
        assert songs_on_device(SimpleNamespace(tracks=[self._track("x", "y")]), []) == set()

    def test_歌名被改过时靠状态库的位置认出来(self, store) -> None:
        """位置是更确凿的证据：设备上那首的标签被改过，但文件是同一个。"""
        from ipod_cli.ncm.sync import songs_on_device

        store.mark_synced(
            1, ipod_location=":iPod_Control:Music:F00:9.mp3",
            name="旧名字", artist="旧艺人",
        )
        library = SimpleNamespace(tracks=[
            self._track("新名字", "新艺人", db_id=9),   # location 是 ...:9.mp3
        ])
        songs = [self._song(1, "旧名字", "旧艺人")]

        assert songs_on_device(library, songs, store=store) == {1}

    def test_设备曲目自带_db_track_id_能直接拿来写歌单(self) -> None:
        """写播放列表成员时用设备曲目自己的 id，不必绕状态库。"""
        from ipod_cli.ncm.sync import device_track_index

        library = SimpleNamespace(tracks=[self._track("虚拟", "陈粒", db_id=42)])

        index = device_track_index(library)
        assert index[("虚拟", "陈粒")].db_track_id == 42
