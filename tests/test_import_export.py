"""导入与导出的端到端测试。

这些都是**真跑**的：真的生成音频文件、真的写 iTunesDB 并签名、真的读回来比对。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ffmpeg_required, make_flac, make_mp3

from iopenpod.itunesdb_parser.ipod_library import load_ipod_library
from ipod_cli.exporter import (
    build_export_items,
    execute_export,
    sanitize_filename,
    select_tracks,
    write_csv_manifest,
)
from ipod_cli.importer import (
    ImportError_,
    build_import_plan,
    cleanup_stray_temp_files,
    execute_import,
)
from ipod_cli.library import read_library
from ipod_cli.mediafile import collect_audio_files, read_pc_track
from ipod_cli.transcode import transcode_for_import

pytestmark = ffmpeg_required


# ──────────────────────────────────────────────────────────────────────
# 导入
# ──────────────────────────────────────────────────────────────────────

class TestImportPlan:
    def test_plans_native_files_without_touching_ipod(
        self, active_device, music_dir: Path
    ) -> None:
        make_mp3(music_dir / "a.mp3", title="晴天", artist="周杰伦", album="叶惠美")
        library = read_library(active_device.root)
        assert not library.tracks

        plan = build_import_plan(active_device, library, collect_audio_files(music_dir))

        assert len(plan.to_add) == 1
        item = plan.to_add[0]
        assert item.action == "add"
        assert item.ipod_location.startswith(":iPod_Control:Music:F")
        # 规划阶段不能碰 iPod
        assert not item.dest_path.exists()
        assert not read_library(active_device.root).tracks

    def test_flac_is_flagged_for_transcode(self, active_device, music_dir: Path) -> None:
        make_flac(music_dir / "x.flac", title="无损", artist="艺人")
        library = read_library(active_device.root)

        plan = build_import_plan(active_device, library, collect_audio_files(music_dir))

        assert len(plan.items) == 1
        assert plan.items[0].action == "transcode"
        assert plan.items[0].dest_path.suffix == ".m4a", "转码产物应是 m4a"

    def test_no_transcode_marks_flac_as_error(
        self, active_device, music_dir: Path
    ) -> None:
        make_flac(music_dir / "x.flac", title="无损", artist="艺人")
        library = read_library(active_device.root)

        plan = build_import_plan(
            active_device, library, collect_audio_files(music_dir),
            allow_transcode=False,
        )

        assert plan.items[0].action == "error"
        assert "转码" in plan.items[0].reason
        assert not plan.to_add

    def test_duplicate_against_ipod_is_skipped(
        self, active_device, music_dir: Path
    ) -> None:
        track = make_mp3(music_dir / "a.mp3", title="重复歌", artist="艺人", album="专辑")
        library = read_library(active_device.root)
        plan = build_import_plan(active_device, library, [track])
        execute_import(plan, progress=None)

        relibrary = read_library(active_device.root)
        assert len(relibrary.tracks) == 1

        replan = build_import_plan(active_device, relibrary, [track])
        assert len(replan.to_add) == 0
        assert replan.items[0].action == "skip"
        assert "已有" in replan.items[0].reason

    def test_force_allows_duplicate(self, active_device, music_dir: Path) -> None:
        track = make_mp3(music_dir / "a.mp3", title="重复歌", artist="艺人")
        library = read_library(active_device.root)
        execute_import(build_import_plan(active_device, library, [track]), progress=None)

        relibrary = read_library(active_device.root)
        replan = build_import_plan(active_device, relibrary, [track], force=True)
        assert len(replan.to_add) == 1

    def test_duplicate_within_one_batch_is_skipped(
        self, active_device, music_dir: Path
    ) -> None:
        track = make_mp3(music_dir / "a.mp3", title="同一首", artist="艺人")
        library = read_library(active_device.root)
        # 同一个文件传两次
        plan = build_import_plan(active_device, library, [track, track])
        assert len(plan.to_add) == 1
        assert sum(1 for i in plan.items if i.action == "skip") == 1

    def test_paths_are_unique_across_batch(
        self, active_device, music_dir: Path
    ) -> None:
        files = [
            make_mp3(music_dir / f"{i}.mp3", title=f"歌{i}", artist="艺人")
            for i in range(6)
        ]
        library = read_library(active_device.root)
        plan = build_import_plan(active_device, library, files)

        locations = [i.ipod_location for i in plan.to_add]
        assert len(locations) == len(set(locations)), "路径不能撞车"
        folders = {loc.split(":")[3] for loc in locations}
        assert len(folders) > 1, "应该轮转到多个 Fxx 目录"

    def test_unreadable_file_becomes_error_not_crash(
        self, active_device, music_dir: Path
    ) -> None:
        broken = music_dir / "broken.mp3"
        broken.write_bytes(b"this is not audio at all")
        library = read_library(active_device.root)

        plan = build_import_plan(active_device, library, [broken])
        assert plan.items[0].action == "error"
        assert plan.items[0].reason


class TestImportExecute:
    def test_import_writes_signed_database(
        self, active_device, music_dir: Path
    ) -> None:
        track = make_mp3(
            music_dir / "a.mp3", title="晴天", artist="周杰伦",
            album="叶惠美", genre="华语流行", year="2003", track="1/11",
        )
        library = read_library(active_device.root)
        plan = build_import_plan(active_device, library, [track])
        result = execute_import(plan, progress=None)

        assert result.added == 1
        assert result.database_written
        assert result.verified, result.verification_note

        # 签名方案必须是 HASH58
        raw = (active_device.db_path).read_bytes()
        import struct

        from iopenpod.itunesdb_shared.mhbd_defs import MHBD_OFFSET_HASHING_SCHEME

        assert struct.unpack_from("<I", raw, MHBD_OFFSET_HASHING_SCHEME)[0] == 1

    def test_chinese_metadata_survives_import(
        self, active_device, music_dir: Path
    ) -> None:
        make_mp3(
            music_dir / "a.mp3", title="富士山下", artist="陈奕迅",
            album="认了吧", genre="粤语流行",
        )
        library = read_library(active_device.root)
        execute_import(
            build_import_plan(active_device, library, collect_audio_files(music_dir)),
            progress=None,
        )

        tracks = read_library(active_device.root).tracks
        assert len(tracks) == 1
        assert tracks[0].title == "富士山下"
        assert tracks[0].artist == "陈奕迅"
        assert tracks[0].album == "认了吧"
        assert tracks[0].genre == "粤语流行"

    def test_existing_tracks_are_preserved_on_second_import(
        self, active_device, music_dir: Path
    ) -> None:
        """这是最要命的场景：第二次导入绝不能把第一次的歌抹掉。"""
        first = make_mp3(music_dir / "a.mp3", title="第一首", artist="甲")
        library = read_library(active_device.root)
        execute_import(build_import_plan(active_device, library, [first]), progress=None)
        assert len(read_library(active_device.root).tracks) == 1

        second = make_mp3(music_dir / "b.mp3", title="第二首", artist="乙")
        library2 = read_library(active_device.root)
        execute_import(build_import_plan(active_device, library2, [second]), progress=None)

        titles = {t.title for t in read_library(active_device.root).tracks}
        assert titles == {"第一首", "第二首"}

    def test_import_creates_backup_before_writing(
        self, active_device, music_dir: Path
    ) -> None:
        """写坏数据库时的唯一退路。"""
        make_mp3(music_dir / "a.mp3", title="歌", artist="艺人")
        library = read_library(active_device.root)
        execute_import(
            build_import_plan(active_device, library, collect_audio_files(music_dir)),
            progress=None,
        )
        assert (active_device.root / "iPod_Control" / "iTunes" / "iTunesDB.backup").is_file()

    def test_transcode_path_produces_alac(
        self, active_device, music_dir: Path
    ) -> None:
        make_flac(music_dir / "x.flac", title="无损测试", artist="艺人")
        library = read_library(active_device.root)
        plan = build_import_plan(active_device, library, collect_audio_files(music_dir))
        result = execute_import(plan, progress=None, transcode=transcode_for_import)

        assert result.added == 1
        assert result.verified

        # 产物必须是 ALAC，不是改了扩展名的 FLAC
        import subprocess

        produced = plan.to_add[0].dest_path
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1",
             str(produced)],
            capture_output=True, text=True, check=False,
        )
        assert probe.stdout.strip() == "alac"

    def test_transcode_without_transcoder_refuses_to_copy_raw(
        self, active_device, music_dir: Path
    ) -> None:
        """没转码器时必须报错，不能把 FLAC 字节拷成 .m4a（静默损坏）。"""
        make_flac(music_dir / "x.flac", title="无损", artist="艺人")
        library = read_library(active_device.root)
        plan = build_import_plan(active_device, library, collect_audio_files(music_dir))

        with pytest.raises(ImportError_) as excinfo:
            execute_import(plan, progress=None, transcode=None)
        assert "转码" in str(excinfo.value)
        # 不能留下任何垃圾文件
        assert not plan.to_add[0].dest_path.exists()

    def test_artwork_is_written_when_cover_present(
        self, active_device, music_dir: Path
    ) -> None:
        make_mp3(music_dir / "a.mp3", title="有封面", artist="艺人", with_cover=True)
        library = read_library(active_device.root)
        execute_import(
            build_import_plan(active_device, library, collect_audio_files(music_dir)),
            progress=None,
        )

        artwork_dir = active_device.root / "iPod_Control" / "Artwork"
        assert (artwork_dir / "ArtworkDB").is_file()
        assert list(artwork_dir.glob("*.ithmb")), "应该生成 ithmb 封面数据"

        # 数据库里必须真的关联到封面
        data = load_ipod_library(str(active_device.db_path))
        track = data["mhlt"][0]
        assert track.get("has_artwork") == 1
        assert track.get("artwork_count", 0) >= 1

    def test_no_artwork_flag_skips_artwork_db(
        self, active_device, music_dir: Path
    ) -> None:
        make_mp3(music_dir / "a.mp3", title="有封面", artist="艺人", with_cover=True)
        library = read_library(active_device.root)
        plan = build_import_plan(active_device, library, collect_audio_files(music_dir))
        execute_import(plan, progress=None, write_artwork=False)

        assert not (active_device.root / "iPod_Control" / "Artwork" / "ArtworkDB").exists()

    def test_no_stray_temp_files_left_behind(
        self, active_device, music_dir: Path
    ) -> None:
        """没有封面时不能留下 .iop-*.tmp 残留。"""
        make_mp3(music_dir / "a.mp3", title="无封面", artist="艺人")
        library = read_library(active_device.root)
        execute_import(
            build_import_plan(active_device, library, collect_audio_files(music_dir)),
            progress=None,
        )
        assert cleanup_stray_temp_files(active_device.root) == 0, "不应有残留临时文件"

    def test_import_multiple_formats_together(
        self, active_device, music_dir: Path
    ) -> None:
        make_mp3(music_dir / "a.mp3", title="MP3歌", artist="甲")
        make_flac(music_dir / "b.flac", title="FLAC歌", artist="乙")
        library = read_library(active_device.root)
        plan = build_import_plan(active_device, library, collect_audio_files(music_dir))
        result = execute_import(plan, progress=None, transcode=transcode_for_import)

        assert result.added == 2
        assert result.verified
        titles = {t.title for t in read_library(active_device.root).tracks}
        assert titles == {"MP3歌", "FLAC歌"}

    def test_copied_file_size_matches_database(
        self, active_device, music_dir: Path
    ) -> None:
        """数据库里记的大小必须跟磁盘上的文件一致，否则 iPod 会判为损坏。"""
        make_mp3(music_dir / "a.mp3", title="大小校验", artist="艺人")
        library = read_library(active_device.root)
        execute_import(
            build_import_plan(active_device, library, collect_audio_files(music_dir)),
            progress=None,
        )

        track = read_library(active_device.root).tracks[0]
        on_disk = (active_device.root / track.relative_path).stat().st_size
        assert track.size == on_disk


# ──────────────────────────────────────────────────────────────────────
# 导出
# ──────────────────────────────────────────────────────────────────────

class TestExport:
    def _import_one(self, device, music_dir: Path, **kwargs) -> None:
        track = make_mp3(music_dir / "a.mp3", **kwargs)
        library = read_library(device.root)
        execute_import(build_import_plan(device, library, [track]), progress=None)

    def test_export_roundtrip_bytes_identical(
        self, active_device, music_dir: Path, tmp_path: Path
    ) -> None:
        source = make_mp3(music_dir / "a.mp3", title="往返测试", artist="艺人")
        library = read_library(active_device.root)
        execute_import(build_import_plan(active_device, library, [source]), progress=None)

        out_dir = tmp_path / "out"
        tracks = read_library(active_device.root).tracks
        items = build_export_items(active_device, tracks, out_dir)
        result = execute_export(items, progress=None)

        assert result.copied == 1
        exported = items[0].dest
        assert exported.is_file()
        # 导出的是原始字节，不是重新编码过的东西
        assert exported.read_bytes() == source.read_bytes()

    def test_export_preserves_chinese_metadata(
        self, active_device, music_dir: Path, tmp_path: Path
    ) -> None:
        self._import_one(active_device, music_dir, title="中文标题", artist="中文艺人", album="中文专辑")

        out_dir = tmp_path / "out"
        tracks = read_library(active_device.root).tracks
        items = build_export_items(active_device, tracks, out_dir)
        execute_export(items, progress=None)

        exported = read_pc_track(items[0].dest)
        assert exported.title == "中文标题"
        assert exported.artist == "中文艺人"
        assert exported.album == "中文专辑"

    def test_export_creates_chinese_named_paths(
        self, active_device, music_dir: Path, tmp_path: Path
    ) -> None:
        self._import_one(active_device, music_dir, title="晴天", artist="周杰伦", album="叶惠美")

        out_dir = tmp_path / "out"
        tracks = read_library(active_device.root).tracks
        items = build_export_items(active_device, tracks, out_dir)

        relative = items[0].dest.relative_to(out_dir)
        assert relative.parts[0] == "周杰伦"
        assert "晴天" in relative.name

    def test_organize_by_album(
        self, active_device, music_dir: Path, tmp_path: Path
    ) -> None:
        self._import_one(active_device, music_dir, title="歌", artist="艺人", album="专辑名")

        out_dir = tmp_path / "out"
        tracks = read_library(active_device.root).tracks
        items = build_export_items(active_device, tracks, out_dir, organize_by="album")

        relative = items[0].dest.relative_to(out_dir)
        assert relative.parts[0] == "艺人"
        assert relative.parts[1] == "专辑名"

    def test_flat_mode_has_no_subdirs(
        self, active_device, music_dir: Path, tmp_path: Path
    ) -> None:
        self._import_one(active_device, music_dir, title="歌", artist="艺人")

        out_dir = tmp_path / "out"
        tracks = read_library(active_device.root).tracks
        items = build_export_items(active_device, tracks, out_dir, flat=True)
        assert items[0].dest.parent == out_dir

    def test_missing_source_file_is_reported_not_crashed(
        self, active_device, music_dir: Path, tmp_path: Path
    ) -> None:
        self._import_one(active_device, music_dir, title="幽灵", artist="艺人")

        # 手动删掉 iPod 上的文件，模拟数据库有记录但文件不在了
        track = read_library(active_device.root).tracks[0]
        (active_device.root / track.relative_path).unlink()

        out_dir = tmp_path / "out"
        items = build_export_items(active_device, [track], out_dir)
        result = execute_export(items, progress=None)

        assert result.copied == 0
        assert len(items[0:1]) == 1
        assert not items[0].exists

    def test_no_overwrite_by_default(
        self, active_device, music_dir: Path, tmp_path: Path
    ) -> None:
        self._import_one(active_device, music_dir, title="歌", artist="艺人")

        out_dir = tmp_path / "out"
        tracks = read_library(active_device.root).tracks
        items = build_export_items(active_device, tracks, out_dir)
        execute_export(items, progress=None)

        items2 = build_export_items(active_device, tracks, out_dir)
        result2 = execute_export(items2, progress=None)
        assert result2.copied == 0
        assert "已存在" in items2[0].error

    def test_csv_manifest_has_bom_for_excel(
        self, active_device, music_dir: Path, tmp_path: Path
    ) -> None:
        self._import_one(active_device, music_dir, title="晴天", artist="周杰伦")

        out_dir = tmp_path / "out"
        tracks = read_library(active_device.root).tracks
        items = build_export_items(active_device, tracks, out_dir)
        execute_export(items, progress=None)

        csv_path = write_csv_manifest(items, out_dir / "清单.csv")
        raw = csv_path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), "Excel 打开中文不乱码需要 BOM"
        text = raw.decode("utf-8-sig")
        assert "晴天" in text
        assert "标题" in text


class TestSelectTracksKeepPlaylist:
    """``--keep-playlist``：全删只留某个歌单。

    语义容易搞反，所以这里把方向钉死：``keep_playlists`` 是**排除**，
    返回值始终是"要处理的那批"。反了的话后果是**把要留的歌删光**。
    """

    @staticmethod
    def _fake_library(keep_ids: set[int], all_ids=(1, 2, 3, 4)):
        from types import SimpleNamespace

        tracks = [
            SimpleNamespace(
                db_track_id=i, title=f"歌{i}", artist="", album="",
                album_artist="", genre="", raw={"track_persistent_id": i},
            )
            for i in all_ids
        ]
        playlists = [
            {
                "Title": "要保留的",
                "items": [
                    {"track_id": i, "track_persistent_id": i} for i in keep_ids
                ],
            }
        ]
        return SimpleNamespace(tracks=tracks, playlists=playlists)

    def test_returns_everything_except_the_kept_playlist(self) -> None:
        library = self._fake_library({1, 2})

        picked = select_tracks(library, keep_playlists=["要保留的"])

        assert {t.db_track_id for t in picked} == {3, 4}, (
            "方向反了——这会把要保留的歌选中并删掉"
        )

    def test_without_keep_returns_all(self) -> None:
        library = self._fake_library({1, 2})
        assert len(select_tracks(library)) == 4

    def test_keeping_everything_selects_nothing(self) -> None:
        library = self._fake_library({1, 2, 3, 4})
        assert select_tracks(library, keep_playlists=["要保留的"]) == []

    def test_unknown_playlist_raises(self) -> None:
        library = self._fake_library({1})
        with pytest.raises(ValueError, match="找不到播放列表"):
            select_tracks(library, keep_playlists=["不存在的"])

    def test_multiple_keep_playlists(self) -> None:

        library = self._fake_library({1})
        library.playlists.append({
            "Title": "另一个",
            "items": [{"track_id": 2, "track_persistent_id": 2}],
        })

        picked = select_tracks(library, keep_playlists=["要保留的", "另一个"])

        assert {t.db_track_id for t in picked} == {3, 4}

    def test_excludes_from_a_narrowed_selection(self) -> None:
        """先按条件缩小范围，再从结果里排除保留项。"""
        library = self._fake_library({1})
        for track in library.tracks:
            track.artist = "周杰伦"

        picked = select_tracks(
            library, artist="周杰伦", keep_playlists=["要保留的"]
        )

        assert {t.db_track_id for t in picked} == {2, 3, 4}


class TestSelectTracks:
    def test_filter_by_artist(self, active_device, music_dir: Path) -> None:
        for title, artist in (("A", "周杰伦"), ("B", "陈奕迅")):
            make_mp3(music_dir / f"{title}.mp3", title=title, artist=artist)
        library = read_library(active_device.root)
        execute_import(
            build_import_plan(active_device, library, collect_audio_files(music_dir)),
            progress=None,
        )

        relibrary = read_library(active_device.root)
        assert len(select_tracks(relibrary, artist="周杰伦")) == 1
        assert len(select_tracks(relibrary)) == 2

    def test_search_matches_title(self, active_device, music_dir: Path) -> None:
        make_mp3(music_dir / "a.mp3", title="富士山下", artist="陈奕迅")
        make_mp3(music_dir / "b.mp3", title="晴天", artist="周杰伦")
        library = read_library(active_device.root)
        execute_import(
            build_import_plan(active_device, library, collect_audio_files(music_dir)),
            progress=None,
        )

        relibrary = read_library(active_device.root)
        assert len(select_tracks(relibrary, search="晴天")) == 1
        assert len(select_tracks(relibrary, search="不存在的歌")) == 0


class TestSanitizeFilename:
    @pytest.mark.parametrize(
        "raw,forbidden",
        [
            ("a/b", "/"),
            ("a\\b", "\\"),
            ("a:b", ":"),
            ("a*b", "*"),
            ("a?b", "?"),
        ],
    )
    def test_strips_windows_illegal_chars(self, raw: str, forbidden: str) -> None:
        assert forbidden not in sanitize_filename(raw)

    def test_keeps_chinese(self) -> None:
        assert sanitize_filename("晴天") == "晴天"

    def test_windows_reserved_names_are_escaped(self) -> None:
        assert sanitize_filename("CON") != "CON"

    def test_empty_name_falls_back(self) -> None:
        assert sanitize_filename("") == "未命名"
