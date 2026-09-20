"""播放列表保全的回归测试。

背景（真实事故）：导入器最初调 ``write_itunesdb()`` 时没传 ``playlists`` 参数，
整库重写后用户的播放列表全部消失，连 iPod 的名字都被改成默认的 "iPod"
（iPod 名字存在主播放列表标题里）。而且**不报错**——静默数据丢失。

这组测试守着这条链路。故意用真机数据的形状（主列表带名字、用户列表、
智能列表、多曲目）而不是最简情形。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ffmpeg_required, make_mp3

from iopenpod.itunesdb_parser.ipod_library import load_ipod_library
from iopenpod.itunesdb_writer import PlaylistInfo, TrackInfo, write_itunesdb
from ipod_cli.importer import build_import_plan, execute_import
from ipod_cli.library import read_library

IPOD_NAME = "我的 iPod"      # 故意用真机上的名字，含中文和空格
USER_PLAYLIST = "跑步歌单"


def _seed_database_with_playlists(ipod_root: Path) -> None:
    """造一个有名字、有播放列表的初始数据库。"""
    tracks = [
        TrackInfo(
            title="第一首",
            location=":iPod_Control:Music:F00:AAAA.mp3",
            artist="艺人甲",
            album="专辑一",
            filetype="mp3",
            size=1000,
            length=1000,
            bitrate=320,
            sample_rate=44100,
        ),
        TrackInfo(
            title="第二首",
            location=":iPod_Control:Music:F00:BBBB.mp3",
            artist="艺人乙",
            album="专辑二",
            filetype="mp3",
            size=1000,
            length=1000,
            bitrate=320,
            sample_rate=44100,
        ),
    ]
    for track in tracks:
        target = ipod_root / track.location.strip(":").replace(":", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00" * 1000)

    # db_track_id 留 0 让写入器生成，写完后读回来拿到真实 id
    assert write_itunesdb(
        str(ipod_root),
        tracks,
        master_playlist_name=IPOD_NAME,
        capabilities=None,
    ) is True

    library = read_library(ipod_root)
    ids = [t.db_track_id for t in library.tracks]

    # 用真实 db_track_id 建用户播放列表
    assert write_itunesdb(
        str(ipod_root),
        [
            TrackInfo(
                **{**_track_kwargs(t), "db_track_id": t.db_track_id}
            )
            for t in library.tracks
        ],
        playlists=[PlaylistInfo(name=USER_PLAYLIST, track_ids=[ids[0]])],
        master_playlist_name=IPOD_NAME,
        capabilities=None,
    ) is True


def _track_kwargs(track) -> dict:
    return {
        "title": track.title,
        "location": track.location,
        "artist": track.artist or None,
        "album": track.album or None,
        "filetype": track.filetype.lower() or "mp3",
        "size": track.size,
        "length": track.length,
        "bitrate": track.bitrate,
        "sample_rate": track.sample_rate or 44100,
    }


def _raw_playlists(ipod_root: Path) -> dict:
    data = load_ipod_library(str(ipod_root / "iPod_Control" / "iTunes" / "iTunesDB"))
    return {
        "mhlp": data.get("mhlp") or [],
        "mhlp_smart": data.get("mhlp_smart") or [],
        "mhlp_podcast": data.get("mhlp_podcast") or [],
    }


class TestSeedDatabase:
    """先确认测试夹具本身是对的，不然后面的断言没有意义。"""

    def test_seed_creates_named_master_playlist(self, ipod_root: Path) -> None:
        _seed_database_with_playlists(ipod_root)
        raw = _raw_playlists(ipod_root)
        titles = [p.get("Title") for p in raw["mhlp"]]
        assert IPOD_NAME in titles, f"主播放列表名字应该是 {IPOD_NAME!r}，实际 {titles}"

    def test_seed_creates_user_playlist(self, ipod_root: Path) -> None:
        _seed_database_with_playlists(ipod_root)
        raw = _raw_playlists(ipod_root)
        titles = [p.get("Title") for p in raw["mhlp"]]
        assert USER_PLAYLIST in titles, f"用户播放列表丢了，实际 {titles}"


@ffmpeg_required
class TestPlaylistPreservationOnImport:
    def test_import_keeps_ipod_name(self, ipod_root: Path, music_dir: Path) -> None:
        """iPod 名字存在主播放列表标题里，重写不能把它变成默认的 "iPod"。"""
        _seed_database_with_playlists(ipod_root)

        device = pytest.importorskip("ipod_cli.discovery").probe_mount(ipod_root)
        device.activate()
        library = read_library(ipod_root)
        track = make_mp3(music_dir / "new.mp3", title="新歌", artist="新艺人")

        execute_import(
            build_import_plan(device, library, [track]), progress=None
        )

        titles = [p.get("Title") for p in _raw_playlists(ipod_root)["mhlp"]]
        assert IPOD_NAME in titles, f"iPod 名字被改了！实际主列表名: {titles}"
        assert "iPod" not in titles, "不该出现默认名 'iPod'"

    def test_import_keeps_user_playlist(self, ipod_root: Path, music_dir: Path) -> None:
        _seed_database_with_playlists(ipod_root)

        device = pytest.importorskip("ipod_cli.discovery").probe_mount(ipod_root)
        device.activate()
        library = read_library(ipod_root)
        track = make_mp3(music_dir / "new.mp3", title="新歌", artist="新艺人")

        execute_import(
            build_import_plan(device, library, [track]), progress=None
        )

        playlists = _raw_playlists(ipod_root)["mhlp"]
        titles = [p.get("Title") for p in playlists]
        assert USER_PLAYLIST in titles, f"用户播放列表丢了！实际: {titles}"

        # 播放列表的成员也要还在
        user = next(p for p in playlists if p.get("Title") == USER_PLAYLIST)
        assert len(user.get("items") or []) == 1, "播放列表成员丢了"

    def test_import_keeps_master_track_membership(
        self, ipod_root: Path, music_dir: Path
    ) -> None:
        """主播放列表要包含全部曲目，包括新导入的。"""
        _seed_database_with_playlists(ipod_root)

        device = pytest.importorskip("ipod_cli.discovery").probe_mount(ipod_root)
        device.activate()
        library = read_library(ipod_root)
        track = make_mp3(music_dir / "new.mp3", title="新歌", artist="新艺人")

        execute_import(
            build_import_plan(device, library, [track]), progress=None
        )

        master = next(
            p for p in _raw_playlists(ipod_root)["mhlp"]
            if p.get("Title") == IPOD_NAME
        )
        assert len(master.get("items") or []) == 3, (
            f"主列表应有 3 首（原 2 + 新 1），实际 {len(master.get('items') or [])}"
        )

    def test_import_on_empty_library_still_works(
        self, ipod_root: Path, music_dir: Path
    ) -> None:
        """没有任何播放列表时不能因为重建逻辑而失败。"""
        device = pytest.importorskip("ipod_cli.discovery").probe_mount(ipod_root)
        device.activate()
        library = read_library(ipod_root)
        assert not library.tracks

        track = make_mp3(music_dir / "new.mp3", title="第一首", artist="艺人")
        result = execute_import(
            build_import_plan(device, library, [track]), progress=None
        )

        assert result.added == 1
        assert result.verified
        assert len(read_library(ipod_root).tracks) == 1


@ffmpeg_required
class TestPlaylistPreservationOnExport:
    def test_playlist_filter_finds_seeded_playlist(
        self, ipod_root: Path, music_dir: Path
    ) -> None:
        """--playlist 能按名字找到播放列表（不是摆设）。"""
        from ipod_cli.exporter import select_tracks

        _seed_database_with_playlists(ipod_root)
        library = read_library(ipod_root)

        selected = select_tracks(library, playlist=USER_PLAYLIST)
        assert len(selected) == 1, "应该只选出播放列表里的那一首"
        assert selected[0].title == "第一首"
