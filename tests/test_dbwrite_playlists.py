"""``dbwrite._upsert_playlists`` 的测试。

这个函数决定"同步同一个歌单"是覆盖还是堆积。搞错了的表现是：
同步跑十次，iPod 上多出十个同名播放列表，每个的成员还不一样。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ipod_cli.dbwrite import _upsert_playlists


@dataclass
class FakePlaylist:
    name: str
    track_ids: list[int] = field(default_factory=list)


class TestUpsert:
    def test_new_playlist_is_added(self) -> None:
        existing = [FakePlaylist("老列表", [1, 2])]
        incoming = [FakePlaylist("新列表", [3])]

        merged = _upsert_playlists(existing, incoming)

        assert [p.name for p in merged] == ["老列表", "新列表"]

    def test_same_name_replaces_instead_of_duplicating(self) -> None:
        """★ 同名必须替换。不然每同步一次就多一个同名列表。"""
        existing = [FakePlaylist("我喜欢的音乐", [1, 2, 3])]
        incoming = [FakePlaylist("我喜欢的音乐", [4, 5])]

        merged = _upsert_playlists(existing, incoming)

        assert len(merged) == 1, f"堆出了 {len(merged)} 个同名列表"
        assert merged[0].track_ids == [4, 5], "成员没更新成新的"

    def test_other_playlists_are_preserved(self) -> None:
        """用户的其它播放列表不能被碰掉——这是真机踩过的坑。"""
        existing = [
            FakePlaylist("On-The-Go 1", [1]),
            FakePlaylist("我喜欢的音乐", [2]),
            FakePlaylist("别的列表", [3]),
        ]
        incoming = [FakePlaylist("我喜欢的音乐", [9])]

        merged = _upsert_playlists(existing, incoming)

        names = [p.name for p in merged]
        assert "On-The-Go 1" in names
        assert "别的列表" in names
        assert names.count("我喜欢的音乐") == 1
        assert len(merged) == 3

    def test_multiple_incoming(self) -> None:
        existing = [FakePlaylist("A", [1])]
        incoming = [FakePlaylist("B", [2]), FakePlaylist("C", [3])]

        merged = _upsert_playlists(existing, incoming)

        assert [p.name for p in merged] == ["A", "B", "C"]

    def test_empty_existing(self) -> None:
        merged = _upsert_playlists([], [FakePlaylist("新的", [1])])
        assert [p.name for p in merged] == ["新的"]

    def test_empty_incoming_keeps_existing(self) -> None:
        existing = [FakePlaylist("A", [1])]
        assert _upsert_playlists(existing, []) == existing

    def test_repeated_sync_is_idempotent(self) -> None:
        """反复"同步"同一个歌单，列表数量应该稳定。"""
        current: list = []
        for _ in range(5):
            current = _upsert_playlists(current, [FakePlaylist("我喜欢的音乐", [1, 2])])
        assert len(current) == 1
