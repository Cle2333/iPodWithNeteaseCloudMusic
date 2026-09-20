"""删除曲目的测试。

删除是破坏性操作，所以测试重点在**边界和拒止**：

  * 空筛选结果要报错而不是"成功地什么都没做"
  * 删光整个库要被拦住
  * 删除后播放列表的幽灵条目要被清掉
  * 删除后剩下的曲目和封面要完好
  * 文件要真的从磁盘上消失
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ffmpeg_required, make_mp3

from iopenpod.itunesdb_parser.ipod_library import load_ipod_library
from iopenpod.itunesdb_writer import PlaylistInfo, write_itunesdb
from ipod_cli.exporter import select_tracks
from ipod_cli.importer import build_import_plan, execute_import
from ipod_cli.library import read_library
from ipod_cli.remover import (
    RemoveError,
    build_remove_plan,
    execute_remove,
)

pytestmark = ffmpeg_required

IPOD_NAME = "铁头的 IPO"
USER_PLAYLIST = "精选"


def _seed(ipod_root: Path, music_dir: Path, count: int = 3, *, with_cover: bool = False):
    """导入曲目，并建一个用户播放列表（含第一首），构成真实形状的基准状态。

    ``with_cover`` 会导入带内嵌封面的曲目，从而生成 ArtworkDB——
    验证"删除不动封面库"的测试需要它。
    """
    from ipod_cli.discovery import probe_mount

    device = probe_mount(ipod_root)
    assert device is not None
    device.activate()
    library = read_library(ipod_root)
    files = [
        make_mp3(
            music_dir / f"t{i}.mp3",
            title=f"曲目{i}",
            artist=f"艺人{i}",
            with_cover=with_cover,
        )
        for i in range(1, count + 1)
    ]
    result = execute_import(build_import_plan(device, library, files), progress=None)
    assert result.added == count

    # 加上名字和播放列表
    library = read_library(ipod_root)
    from ipod_cli.dbwrite import build_track_infos

    infos, _ = build_track_infos(library.track_dicts)
    first_id = infos[0].db_track_id
    write_itunesdb(
        str(ipod_root),
        infos,
        playlists=[PlaylistInfo(name=USER_PLAYLIST, track_ids=[first_id])],
        master_playlist_name=IPOD_NAME,
        capabilities=None,
    )
    return device


def _playlist_titles(ipod_root: Path) -> dict[str, list[str]]:
    data = load_ipod_library(str(ipod_root / "iPod_Control" / "iTunes" / "iTunesDB"))
    return {
        "standard": [
            str(p.get("Title")) for p in (data.get("mhlp") or [])
        ],
        "smart": [str(p.get("Title")) for p in (data.get("mhlp_smart") or [])],
    }


def _playlist_item_count(ipod_root: Path, title: str) -> int:
    data = load_ipod_library(str(ipod_root / "iPod_Control" / "iTunes" / "iTunesDB"))
    for group in ("mhlp", "mhlp_podcast", "mhlp_smart"):
        for playlist in data.get(group) or []:
            if str(playlist.get("Title")) == title:
                return len(playlist.get("items") or [])
    raise AssertionError(f"找不到播放列表 {title!r}")


class TestPlanGuardRails:
    def test_empty_selection_is_an_error(self, ipod_root, music_dir) -> None:
        """筛选不到东西时必须报错，不能静默成功。"""
        device = _seed(ipod_root, music_dir)
        library = read_library(ipod_root)
        with pytest.raises(RemoveError, match="没有匹配到"):
            build_remove_plan(device, library, [])

    def test_refuses_to_empty_the_library(self, ipod_root, music_dir) -> None:
        """删光整个库要拦下来——太容易误用。"""
        device = _seed(ipod_root, music_dir)
        library = read_library(ipod_root)
        with pytest.raises(RemoveError, match="清空"):
            build_remove_plan(device, library, list(library.tracks))

    def test_plan_reports_remaining_and_size(self, ipod_root, music_dir) -> None:
        device = _seed(ipod_root, music_dir)
        library = read_library(ipod_root)
        targets = select_tracks(library, artist="艺人2")
        plan = build_remove_plan(device, library, targets)

        assert plan.count == 1
        assert plan.remaining == 2
        assert plan.bytes_freed > 0
        assert len(plan.files_to_delete) == 1


class TestExecuteRemove:
    def test_removes_track_and_file(self, ipod_root, music_dir) -> None:
        device = _seed(ipod_root, music_dir)
        library = read_library(ipod_root)
        victim = [t for t in library.tracks if t.title == "曲目2"][0]
        victim_path = ipod_root / victim.relative_path
        assert victim_path.is_file()

        plan = build_remove_plan(device, library, [victim])
        result = execute_remove(plan, progress=None)

        assert result.files_deleted == 1
        assert result.database_written
        assert result.verified
        assert not victim_path.exists(), "文件应该从磁盘上删掉"

        after = read_library(ipod_root)
        assert len(after.tracks) == 2
        assert "曲目2" not in {t.title for t in after.tracks}

    def test_removes_from_user_playlist(self, ipod_root, music_dir) -> None:
        """被删的歌如果是用户播放列表成员，要从列表里移除——不能留幽灵条目。"""
        device = _seed(ipod_root, music_dir)
        assert _playlist_item_count(ipod_root, USER_PLAYLIST) == 1

        library = read_library(ipod_root)
        # 播放列表里那首是第一首（曲目1）
        victim = [t for t in library.tracks if t.title == "曲目1"][0]
        plan = build_remove_plan(device, library, [victim])
        execute_remove(plan, progress=None)

        assert _playlist_item_count(ipod_root, USER_PLAYLIST) == 0, "幽灵条目没清掉"

    def test_keeps_other_playlists_and_ipod_name(self, ipod_root, music_dir) -> None:
        device = _seed(ipod_root, music_dir)
        library = read_library(ipod_root)
        victim = [t for t in library.tracks if t.title == "曲目2"][0]

        execute_remove(build_remove_plan(device, library, [victim]), progress=None)

        titles = _playlist_titles(ipod_root)
        assert IPOD_NAME in titles["standard"], "iPod 名字被改了"
        assert USER_PLAYLIST in titles["standard"], "用户播放列表丢了"

    def test_master_playlist_shrinks(self, ipod_root, music_dir) -> None:
        device = _seed(ipod_root, music_dir)
        library = read_library(ipod_root)
        assert _playlist_item_count(ipod_root, IPOD_NAME) == 3

        victim = [t for t in library.tracks if t.title == "曲目3"][0]
        execute_remove(build_remove_plan(device, library, [victim]), progress=None)

        assert _playlist_item_count(ipod_root, IPOD_NAME) == 2

    def test_remaining_tracks_are_intact(self, ipod_root, music_dir) -> None:
        """删除不能顺手改动没被删的曲目。"""
        device = _seed(ipod_root, music_dir)
        before = {t.title: t.raw for t in read_library(ipod_root).tracks}

        library = read_library(ipod_root)
        victim = [t for t in library.tracks if t.title == "曲目2"][0]
        execute_remove(build_remove_plan(device, library, [victim]), progress=None)

        after = {t.title: t.raw for t in read_library(ipod_root).tracks}
        assert set(after) == {"曲目1", "曲目3"}
        for title, raw in after.items():
            for field in ("Artist", "Album", "Location", "size", "filetype"):
                assert raw.get(field) == before[title].get(field), (
                    f"{title} 的 {field} 被改动了"
                )

    def test_artwork_database_untouched_by_default(self, ipod_root, music_dir) -> None:
        """默认传 pc_file_paths=None，写入器完全不碰 ArtworkDB。

        这条很重要：封面库重写有风险（id 会重新编号、需要 57MB 写入），
        删除曲目时干脆别碰它——剩余曲目的引用原样有效。
        """
        device = _seed(ipod_root, music_dir, with_cover=True)
        artdb = ipod_root / "iPod_Control" / "Artwork" / "ArtworkDB"
        assert artdb.is_file(), "测试前提不成立：封面库没生成"

        before = artdb.read_bytes()
        library = read_library(ipod_root)
        victim = [t for t in library.tracks if t.title == "曲目2"][0]
        execute_remove(build_remove_plan(device, library, [victim]), progress=None)

        assert artdb.read_bytes() == before, "默认不该动 ArtworkDB"

    def test_remaining_tracks_keep_working_artwork(self, ipod_root, music_dir) -> None:
        """删掉一首带封面的歌，剩下的歌封面不能跟着坏。"""
        from ipod_cli.verify import OK, _check_artwork

        device = _seed(ipod_root, music_dir, with_cover=True)
        library = read_library(ipod_root)
        victim = [t for t in library.tracks if t.title == "曲目2"][0]
        execute_remove(build_remove_plan(device, library, [victim]), progress=None)

        check = _check_artwork(device, read_library(ipod_root))
        assert check.status == OK, check.summary
        assert any("悬空引用    0" in line for line in check.details), check.details

    def test_result_is_verified(self, ipod_root, music_dir) -> None:
        device = _seed(ipod_root, music_dir)
        library = read_library(ipod_root)
        victim = [t for t in library.tracks if t.title == "曲目2"][0]

        result = execute_remove(build_remove_plan(device, library, [victim]), progress=None)
        assert result.verified, result.verification_note
        assert "2 首" in result.verification_note

    def test_usage_after_remove_is_consistent(self, ipod_root, music_dir) -> None:
        """删除后跑健康检查，除了备份项之外不该有失败。"""
        from ipod_cli.verify import FAIL, check_device

        device = _seed(ipod_root, music_dir)
        library = read_library(ipod_root)
        victim = [t for t in library.tracks if t.title == "曲目2"][0]
        execute_remove(build_remove_plan(device, library, [victim]), progress=None)

        report = check_device(device)
        assert not [c for c in report.checks if c.status == FAIL], (
            [c.summary for c in report.failures]
        )


class TestRemoveMultiple:
    def test_remove_by_artist_filters_all_matches(self, ipod_root, music_dir) -> None:
        device = _seed(ipod_root, music_dir, count=4)
        library = read_library(ipod_root)

        # 艺人3 和 艺人4 都匹配 "艺人" 的话会删光；用精确前缀筛选
        targets = [
            t for t in library.tracks if t.artist in {"艺人3", "艺人4"}
        ]
        assert len(targets) == 2

        plan = build_remove_plan(device, library, targets)
        assert plan.count == 2
        assert plan.remaining == 2

        result = execute_remove(plan, progress=None)
        assert result.files_deleted == 2
        assert len(read_library(ipod_root).tracks) == 2
