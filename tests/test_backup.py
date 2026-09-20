"""备份与还原的测试。

关键场景：
  * 备份含 Device/iTunes/Artwork 三块，不只是 iTunesDB
  * 校验能发现备份被改动
  * 还原能把设备恢复回去（含播放列表和 iPod 名字）
  * 还原前会另存当前状态（还原本身可逆）
  * 不含 Music 的备份不碰音频文件
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ffmpeg_required, make_mp3

from ipod_cli.backup import (
    BackupError,
    create_backup,
    load_manifest,
    restore_backup,
    verify_backup,
)
from ipod_cli.importer import build_import_plan, execute_import
from ipod_cli.library import read_library

pytestmark = ffmpeg_required


def _seed(ipod_root: Path, music_dir: Path, count: int = 2):
    from ipod_cli.discovery import probe_mount

    device = probe_mount(ipod_root)
    assert device is not None
    device.activate()
    library = read_library(ipod_root)
    files = [
        make_mp3(music_dir / f"t{i}.mp3", title=f"曲目{i}", artist=f"艺人{i}")
        for i in range(1, count + 1)
    ]
    result = execute_import(build_import_plan(device, library, files), progress=None)
    assert result.added == count
    return device


class TestCreateBackup:
    def test_backup_contains_all_metadata_blocks(self, ipod_root, music_dir, tmp_path) -> None:
        """只备份 iTunesDB 是不够的——写入会重写 ArtworkDB 和 ithmb。"""
        device = _seed(ipod_root, music_dir)
        dest = tmp_path / "bk"
        manifest = create_backup(device, dest)

        control = dest / "iPod_Control"
        assert (control / "Device" / "SysInfo").is_file()
        assert (control / "iTunes" / "iTunesDB").is_file()
        # Artwork 目录有没有内容取决于设备；但必须被处理过
        assert (dest / "manifest.json").is_file()
        assert manifest.track_count == 2
        assert len(manifest.metadata_files) >= 3

    def test_manifest_records_hashes(self, ipod_root, music_dir, tmp_path) -> None:
        device = _seed(ipod_root, music_dir)
        dest = tmp_path / "bk"
        create_backup(device, dest)

        manifest = load_manifest(dest)
        assert manifest.metadata_files
        for record in manifest.metadata_files.values():
            assert len(record.sha256) == 64
            assert record.size > 0

    def test_refuses_non_empty_destination(self, ipod_root, music_dir, tmp_path) -> None:
        """不能默默覆盖已有备份。"""
        device = _seed(ipod_root, music_dir)
        dest = tmp_path / "bk"
        dest.mkdir()
        (dest / "已有内容.txt").write_text("别动我", encoding="utf-8")

        with pytest.raises(BackupError, match="已存在且非空"):
            create_backup(device, dest)

    def test_rejects_non_ipod_path(self, tmp_path) -> None:
        from ipod_cli.discovery import IpodDevice

        fake = IpodDevice(root=tmp_path, mount_name="假", db_path=tmp_path / "x")
        with pytest.raises(BackupError, match="不是 iPod"):
            create_backup(fake, tmp_path / "bk")


class TestVerifyBackup:
    def test_fresh_backup_passes(self, ipod_root, music_dir, tmp_path) -> None:
        device = _seed(ipod_root, music_dir)
        dest = tmp_path / "bk"
        create_backup(device, dest)

        consistent, problems = verify_backup(dest)
        assert consistent, problems
        assert problems == []

    def test_detects_tampered_database(self, ipod_root, music_dir, tmp_path) -> None:
        """备份被改动过时必须在还原前就发现。"""
        device = _seed(ipod_root, music_dir)
        dest = tmp_path / "bk"
        create_backup(device, dest)

        target = dest / "iPod_Control" / "iTunes" / "iTunesDB"
        raw = bytearray(target.read_bytes())
        raw[0] = 0x00          # 改一个字节
        target.write_bytes(bytes(raw))

        consistent, problems = verify_backup(dest)
        assert not consistent
        assert any("校验和不符" in p for p in problems)

    def test_detects_missing_file(self, ipod_root, music_dir, tmp_path) -> None:
        device = _seed(ipod_root, music_dir)
        dest = tmp_path / "bk"
        create_backup(device, dest)

        (dest / "iPod_Control" / "Device" / "SysInfo").unlink()

        consistent, problems = verify_backup(dest)
        assert not consistent
        assert any("缺失" in p for p in problems)

    def test_missing_manifest_is_rejected(self, tmp_path) -> None:
        with pytest.raises(BackupError, match="manifest"):
            load_manifest(tmp_path)


class TestRestoreBackup:
    def test_restore_brings_back_deleted_track(self, ipod_root, music_dir, tmp_path) -> None:
        """还原能把曲库恢复回备份时的样子。"""
        device = _seed(ipod_root, music_dir)
        dest = tmp_path / "bk"
        create_backup(device, dest)
        assert len(read_library(ipod_root).tracks) == 2

        # 破坏：把数据库换成一个只有 0 首曲目的假库（模拟"库被清空"）
        db = ipod_root / "iPod_Control" / "iTunes" / "iTunesDB"
        db.write_bytes(b"\x00" * 4096)

        restore_backup(dest, device, backup_current=False)
        assert len(read_library(ipod_root).tracks) == 2

    def test_restore_snapshots_current_state(self, ipod_root, music_dir, tmp_path, monkeypatch) -> None:
        """还原本身要可逆：先把当前状态另存。"""
        device = _seed(ipod_root, music_dir)
        dest = tmp_path / "bk"
        create_backup(device, dest)

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        saved = restore_backup(dest, device, backup_current=True)
        assert saved is not None
        assert (saved / "iPod_Control" / "iTunes" / "iTunesDB").is_file()

    def test_restore_refuses_tampered_backup(self, ipod_root, music_dir, tmp_path) -> None:
        """校验不过就绝不写设备。"""
        device = _seed(ipod_root, music_dir)
        dest = tmp_path / "bk"
        create_backup(device, dest)

        target = dest / "iPod_Control" / "iTunes" / "iTunesDB"
        raw = bytearray(target.read_bytes())
        raw[0] = 0x00
        target.write_bytes(bytes(raw))

        with pytest.raises(BackupError, match="校验未通过"):
            restore_backup(dest, device, backup_current=False)

    def test_restore_does_not_touch_music_without_flag(
        self, ipod_root, music_dir, tmp_path
    ) -> None:
        """不含 Music 的备份，还原时不能动音频文件。"""
        device = _seed(ipod_root, music_dir)
        dest = tmp_path / "bk"
        manifest = create_backup(device, dest)
        assert not manifest.includes_music

        audio_files = sorted(
            p for p in (ipod_root / "iPod_Control" / "Music").rglob("*") if p.is_file()
        )
        before = {p: p.read_bytes() for p in audio_files}
        assert before, "测试前提不成立：没有音频文件"

        restore_backup(dest, device, backup_current=False)

        after = {
            p: p.read_bytes()
            for p in (ipod_root / "iPod_Control" / "Music").rglob("*")
            if p.is_file()
        }
        assert after.keys() == before.keys(), "还原不该增删音频文件"
        for path, data in before.items():
            assert after[path] == data, f"音频文件被改动了：{path}"

    def test_restore_roundtrip_preserves_playlist_name(
        self, ipod_root, music_dir, tmp_path
    ) -> None:
        """还原后 iPod 名字（= 主播放列表标题）要回到备份时的值。"""
        device = _seed(ipod_root, music_dir)
        dest = tmp_path / "bk"
        create_backup(device, dest)

        original_name = read_library(ipod_root).ipod_name

        # 改名
        from ipod_cli.dbwrite import build_track_infos, write_library

        library = read_library(ipod_root)
        infos, _ = build_track_infos(library.track_dicts)
        write_library(
            device, library, infos, pc_file_paths=None,
            master_playlist_name_override="改过的名字",
        )
        assert read_library(ipod_root).ipod_name == "改过的名字"

        restore_backup(dest, device, backup_current=False)
        assert read_library(ipod_root).ipod_name == original_name
