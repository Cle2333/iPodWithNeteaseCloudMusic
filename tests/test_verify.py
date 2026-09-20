"""健康检查的测试。

重点不是"健康设备能通过"（那是最容易写的测试），而是**各种坏掉的情况能不能被抓到**。
这次真机测试的教训就是："导入成功"的提示说明不了任何事，必须有一双专门挑毛病的眼睛。

所以每个检查项都造一个对应的坏状态，确认它确实报错。
"""

from __future__ import annotations

import struct
from pathlib import Path

from conftest import ffmpeg_required, make_mp3

from iopenpod.itunesdb_shared.mhbd_defs import (
    MHBD_OFFSET_HASH58,
    MHBD_OFFSET_HASHING_SCHEME,
)
from ipod_cli.importer import build_import_plan, execute_import
from ipod_cli.library import read_library
from ipod_cli.verify import FAIL, OK, WARN, check_device

pytestmark = ffmpeg_required


def _import_tracks(
    ipod_root: Path, music_dir: Path, count: int = 2, *, with_cover: bool = False
):
    """往虚拟 iPod 里导入若干曲目，作为检查的基准状态。"""
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
    return device


def _find(report, name: str):
    for check in report.checks:
        if check.name == name:
            return check
    raise AssertionError(f"报告里没有名为 {name} 的检查项")


def _detail_value(check, label: str) -> int:
    """从 details 里取出 ``悬空引用    3`` 这种行的数值。

    比字符串全等匹配稳——详情行的对齐空格数量会随文案调整。
    """
    for line in check.details:
        stripped = line.strip()
        if stripped.startswith(label):
            tail = stripped[len(label):].strip().split()
            if tail and tail[0].lstrip("-").isdigit():
                return int(tail[0])
    raise AssertionError(f"{check.name} 的详情里找不到 {label!r}：{check.details}")


class TestHealthyDevice:
    def test_freshly_imported_device_is_healthy(self, ipod_root, music_dir) -> None:
        device = _import_tracks(ipod_root, music_dir)
        report = check_device(device)
        assert not report.failures, [c.summary for c in report.failures]
        assert all(c.status in {OK, WARN} for c in report.checks)

    def test_reports_track_count_and_playlist_name(self, ipod_root, music_dir) -> None:
        device = _import_tracks(ipod_root, music_dir)
        report = check_device(device)
        assert _find(report, "数据库可读").status == OK
        # 主列表名会作为"iPod 的名字"报出来
        playlists = _find(report, "播放列表")
        assert any("主列表名" in line for line in playlists.details)

    def test_json_shape(self, ipod_root, music_dir) -> None:
        from ipod_cli.verify import report_as_dict

        device = _import_tracks(ipod_root, music_dir)
        payload = report_as_dict(check_device(device))
        assert payload["ok"] is True
        assert payload["track_count"] == 2
        assert payload["device"]["model"]
        assert isinstance(payload["checks"], list)

    def test_skip_files_omits_that_check(self, ipod_root, music_dir) -> None:
        device = _import_tracks(ipod_root, music_dir)
        report = check_device(device, skip_files=True)
        assert not any(c.name == "文件对应" for c in report.checks)


class TestDetectsMissingFiles:
    def test_deleted_music_file_is_a_failure(self, ipod_root, music_dir) -> None:
        """库里有记录、磁盘上文件没了 —— 这是最典型的坏状态。"""
        device = _import_tracks(ipod_root, music_dir)
        library = read_library(ipod_root)
        victim = ipod_root / library.tracks[0].relative_path
        victim.unlink()

        report = check_device(device)
        check = _find(report, "文件对应")
        assert check.status == FAIL
        assert "缺失" in check.summary or "没文件" in check.summary
        assert not report.ok


class TestDetectsOrphanFiles:
    def test_stray_music_file_is_a_warning(self, ipod_root, music_dir) -> None:
        """磁盘上有文件、库里没记录 —— 占空间但不致命，所以是警告不是失败。"""
        device = _import_tracks(ipod_root, music_dir)
        stray = ipod_root / "iPod_Control" / "Music" / "F00" / "ORPH.mp3"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"\x00" * 128)

        report = check_device(device)
        check = _find(report, "文件对应")
        assert check.status == WARN
        assert "孤儿" in check.summary
        # 警告不算失败：设备还能用
        assert report.ok


class TestDetectsBadSignature:
    def test_zeroed_signature_is_a_failure(self, ipod_root, music_dir) -> None:
        """签名字段被清零 —— 固件会判定数据库损坏。"""
        device = _import_tracks(ipod_root, music_dir)
        db = ipod_root / "iPod_Control" / "iTunes" / "iTunesDB"
        raw = bytearray(db.read_bytes())
        raw[MHBD_OFFSET_HASH58 : MHBD_OFFSET_HASH58 + 20] = bytes(20)
        db.write_bytes(bytes(raw))

        report = check_device(device)
        check = _find(report, "签名")
        assert check.status == FAIL
        assert "全零" in check.summary

    def test_truncated_database_is_a_failure(self, ipod_root, music_dir) -> None:
        device = _import_tracks(ipod_root, music_dir)
        db = ipod_root / "iPod_Control" / "iTunes" / "iTunesDB"
        db.write_bytes(db.read_bytes()[:16])

        report = check_device(device)
        assert not report.ok

    def test_signature_scheme_is_reported(self, ipod_root, music_dir) -> None:
        device = _import_tracks(ipod_root, music_dir)
        check = _find(check_device(device), "签名")
        assert check.status == OK
        assert any("hashing_scheme" in line for line in check.details)
        assert any("HASH58" in line for line in check.details)


class TestDetectsDanglingArtwork:
    def test_cover_import_produces_no_dangling(self, ipod_root, music_dir) -> None:
        """带封面导入后封面必须是健康的——这是基准，也是后面反例的对照。"""
        device = _import_tracks(ipod_root, music_dir, with_cover=True)
        check = _find(check_device(device), "封面")
        assert check.status == OK, check.summary
        assert _detail_value(check, "悬空引用") == 0, check.details
        # "有封面曲目  2/2" 这种是分数，用文本匹配
        assert any("有封面曲目" in line and "2/2" in line for line in check.details), (
            check.details
        )

    def test_dangling_artwork_reference_is_a_failure(self, ipod_root, music_dir) -> None:
        """曲目的 artwork_id_ref 指向不存在的条目 —— 封面显示不出来。

        直接用构造出来的 LibraryData 打这个检查函数，比去改数据库二进制可靠：
        真实故障形态是"重写后 img_id 变了但引用没跟着更新"，
        在数据层表达就是"引用指向的 id 不在封面库的条目集合里"。
        """
        from ipod_cli.library import LibraryData, Track
        from ipod_cli.verify import _check_artwork

        device = _import_tracks(ipod_root, music_dir, with_cover=True)
        artdb = ipod_root / "iPod_Control" / "Artwork" / "ArtworkDB"
        assert artdb.is_file(), "测试前提不成立：封面库没生成"

        bogus = Track(
            title="悬空封面",
            location=":iPod_Control:Music:F00:ZZZZ.mp3",
            db_track_id=999_999,
            raw={"artwork_id_ref": 999_999},
        )
        fake_library = LibraryData(
            root=ipod_root, db_path=artdb.parent.parent / "iTunes" / "iTunesDB",
            tracks=[bogus],
        )

        check = _check_artwork(device, fake_library)
        assert check.status == FAIL
        assert "不存在" in check.summary, check.summary
        assert _detail_value(check, "悬空引用") == 1, check.details

    def test_healthy_tracks_are_not_flagged(self, ipod_root, music_dir) -> None:
        """对照：真有封面的曲目不能被误报成悬空。"""
        from ipod_cli.verify import _check_artwork

        device = _import_tracks(ipod_root, music_dir, with_cover=True)
        check = _check_artwork(device, read_library(ipod_root))
        assert check.status == OK, check.summary


class TestDetectsBadTrackFields:
    def test_track_without_location_is_a_failure(self, ipod_root, music_dir) -> None:
        _import_tracks(ipod_root, music_dir)
        library = read_library(ipod_root)
        # 直接构造一个缺路径的 Track 检查判定逻辑
        from ipod_cli.library import Track

        broken = Track(title="没路径的歌", location="")
        library.tracks.append(broken)

        from ipod_cli.verify import _check_track_fields

        check = _check_track_fields(library)
        assert check.status == FAIL
        assert "缺路径" in check.summary

    def test_track_without_title_is_a_warning(self, ipod_root, music_dir) -> None:
        from ipod_cli.library import LibraryData, Track
        from ipod_cli.verify import _check_track_fields

        library = LibraryData(
            root=ipod_root,
            db_path=ipod_root / "x",
            tracks=[Track(title="", location=":iPod_Control:Music:F00:AAAA.mp3")],
        )
        check = _check_track_fields(library)
        assert check.status == WARN
        assert "缺标题" in check.summary


class TestUnreadableDatabase:
    def test_garbage_database_fails_gracefully(self, ipod_root) -> None:
        """数据库整个是垃圾时不能抛异常，要报成检查项失败。"""
        db = ipod_root / "iPod_Control" / "iTunes" / "iTunesDB"
        db.write_bytes(b"not a database at all" * 10)

        from ipod_cli.discovery import probe_mount

        device = probe_mount(ipod_root)
        report = check_device(device)
        assert not report.ok
        # 解析器对垃圾是宽容的（会"成功"解析成 0 首曲目），
        # 所以必须靠文件头校验识破——这条就是守这个盲区
        check = _find(report, "数据库文件")
        assert check.status == FAIL
        assert "mhbd" in check.summary


class TestSignatureOffsetSanity:
    def test_offset_constant_matches_writer(self, ipod_root, music_dir) -> None:
        """检查用的偏移必须和写入器一致，否则"签名有效"是假结论。"""
        _import_tracks(ipod_root, music_dir)
        db = ipod_root / "iPod_Control" / "iTunes" / "iTunesDB"
        raw = db.read_bytes()

        scheme = struct.unpack_from("<H", raw, MHBD_OFFSET_HASHING_SCHEME)[0]
        assert scheme == 1, "Classic 应该用 HASH58"

        signature = raw[MHBD_OFFSET_HASH58 : MHBD_OFFSET_HASH58 + 20]
        assert signature != bytes(20), "写入器应该已经把签名写进去了"
