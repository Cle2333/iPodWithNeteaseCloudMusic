"""CLI 层测试：子命令、退出码、中文输出、以及"不该动手时绝不动手"。"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ffmpeg_required, make_mp3

from ipod_cli.cli import EXIT_ERROR, EXIT_OK, main
from ipod_cli.library import read_library
from ipod_cli.transcode import ffmpeg_available


@pytest.fixture()
def cli_ipod(ipod_root: Path) -> str:
    return str(ipod_root)


class TestArgumentHandling:
    def test_no_command_prints_help(self, capsys) -> None:
        assert main([]) == EXIT_OK
        assert "iPod Classic" in capsys.readouterr().out

    def test_bad_command_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            main(["这不是命令"])

    def test_version_flag(self, capsys) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])
        assert excinfo.value.code == 0


class TestDoctor:
    def test_reports_environment_and_device(self, cli_ipod, capsys) -> None:
        assert main(["doctor", "--ipod", cli_ipod]) == EXIT_OK
        out = capsys.readouterr().out
        assert "环境自检" in out
        assert "iPod Classic" in out
        assert "HASH58" in out
        assert "内核" in out

    def test_reports_ffmpeg_state(self, cli_ipod, capsys) -> None:
        main(["doctor", "--ipod", cli_ipod])
        out = capsys.readouterr().out
        assert ("已找到" in out) if ffmpeg_available() else ("未找到" in out)

    def test_missing_device_is_an_error(self, tmp_path: Path, capsys) -> None:
        code = main(["doctor", "--ipod", str(tmp_path / "nope")])
        assert code == EXIT_ERROR
        assert "路径不存在" in capsys.readouterr().err


class TestInfo:
    def test_shows_device_and_library_summary(self, cli_ipod, capsys) -> None:
        assert main(["info", "--ipod", cli_ipod]) == EXIT_OK
        out = capsys.readouterr().out
        assert "设备信息" in out
        assert "曲目数" in out
        assert "签名方案" in out

    def test_warns_when_not_classic(self, cli_ipod, capsys, monkeypatch) -> None:
        """非 HASH58 设备必须给出警告，让人知道风险。"""
        from ipod_cli import discovery

        original = discovery.probe_mount

        def fake(mount):
            device = original(mount)
            if device is not None:
                device.checksum = "HASHAB"
            return device

        monkeypatch.setattr(discovery, "probe_mount", fake)
        main(["info", "--ipod", cli_ipod])
        out = capsys.readouterr().out
        assert "⚠" in out
        assert "HASH58" in out


class TestList:
    def test_empty_library(self, cli_ipod, capsys) -> None:
        assert main(["list", "--ipod", cli_ipod]) == EXIT_OK
        assert "没有符合条件的曲目" in capsys.readouterr().out

    @ffmpeg_required
    def test_lists_imported_tracks(
        self, cli_ipod, tmp_path: Path, capsys
    ) -> None:
        music = tmp_path / "music"
        music.mkdir()
        make_mp3(music / "a.mp3", title="晴天", artist="周杰伦", album="叶惠美")
        main(["import", str(music), "--ipod", cli_ipod, "-y"])
        capsys.readouterr()

        assert main(["list", "--ipod", cli_ipod]) == EXIT_OK
        out = capsys.readouterr().out
        assert "晴天" in out
        assert "周杰伦" in out

    @ffmpeg_required
    def test_by_album_groups(self, cli_ipod, tmp_path: Path, capsys) -> None:
        music = tmp_path / "music"
        music.mkdir()
        make_mp3(music / "a.mp3", title="晴天", artist="周杰伦", album="叶惠美")
        main(["import", str(music), "--ipod", cli_ipod, "-y"])
        capsys.readouterr()

        assert main(["list", "--ipod", cli_ipod, "--by-album"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "叶惠美" in out
        assert "▸" in out

    @ffmpeg_required
    def test_json_output_is_valid(self, cli_ipod, tmp_path: Path, capsys) -> None:
        import json

        music = tmp_path / "music"
        music.mkdir()
        make_mp3(music / "a.mp3", title="晴天", artist="周杰伦")
        main(["import", str(music), "--ipod", cli_ipod, "-y"])
        capsys.readouterr()

        main(["list", "--ipod", cli_ipod, "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["title"] == "晴天"


class TestImportCommand:
    @ffmpeg_required
    def test_dry_run_changes_nothing(self, cli_ipod, tmp_path: Path, capsys) -> None:
        music = tmp_path / "music"
        music.mkdir()
        make_mp3(music / "a.mp3", title="晴天", artist="周杰伦")

        assert main(["import", str(music), "--ipod", cli_ipod, "--dry-run"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "预览" in out
        assert "未做任何修改" in out

        # iPod 必须原封不动
        assert len(read_library(Path(cli_ipod)).tracks) == 0

    @ffmpeg_required
    def test_yes_flag_skips_confirmation(self, cli_ipod, tmp_path: Path, capsys) -> None:
        music = tmp_path / "music"
        music.mkdir()
        make_mp3(music / "a.mp3", title="晴天", artist="周杰伦")

        assert main(["import", str(music), "--ipod", cli_ipod, "-y"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "校验通过" in out
        assert len(read_library(Path(cli_ipod)).tracks) == 1

    @ffmpeg_required
    def test_confirmation_prompt_aborts_on_no(
        self, cli_ipod, tmp_path: Path, capsys, monkeypatch
    ) -> None:
        music = tmp_path / "music"
        music.mkdir()
        make_mp3(music / "a.mp3", title="晴天", artist="周杰伦")

        monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
        code = main(["import", str(music), "--ipod", cli_ipod])
        assert code != EXIT_OK
        assert "已取消" in capsys.readouterr().out
        assert len(read_library(Path(cli_ipod)).tracks) == 0

    def test_no_audio_found_is_an_error(self, cli_ipod, tmp_path: Path, capsys) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert main(["import", str(empty), "--ipod", cli_ipod, "-y"]) == EXIT_ERROR
        assert "没有找到可导入的音频文件" in capsys.readouterr().err

    @ffmpeg_required
    def test_second_import_reports_skipped(
        self, cli_ipod, tmp_path: Path, capsys
    ) -> None:
        music = tmp_path / "music"
        music.mkdir()
        make_mp3(music / "a.mp3", title="重复", artist="艺人")
        main(["import", str(music), "--ipod", cli_ipod, "-y"])
        capsys.readouterr()

        main(["import", str(music), "--ipod", cli_ipod, "-y"])
        out = capsys.readouterr().out
        assert "跳过 1 首" in out
        assert "没有需要导入的曲目" in out


class TestExportCommand:
    @ffmpeg_required
    def test_dry_run_then_real_export(
        self, cli_ipod, tmp_path: Path, capsys
    ) -> None:
        music = tmp_path / "music"
        music.mkdir()
        make_mp3(music / "a.mp3", title="晴天", artist="周杰伦", album="叶惠美")
        main(["import", str(music), "--ipod", cli_ipod, "-y"])
        capsys.readouterr()

        out_dir = tmp_path / "backup"
        assert main(["export", str(out_dir), "--ipod", cli_ipod, "--dry-run"]) == EXIT_OK
        assert not out_dir.exists()
        capsys.readouterr()

        assert main(["export", str(out_dir), "--ipod", cli_ipod, "--csv",
                     str(out_dir / "清单.csv")]) == EXIT_OK
        out = capsys.readouterr().out
        assert "已导出 1 / 1 首" in out

        exported = [p for p in out_dir.rglob("*.mp3")]
        assert len(exported) == 1
        assert exported[0].parent.name == "周杰伦"
        assert (out_dir / "清单.csv").is_file()

    @ffmpeg_required
    def test_filter_by_artist(self, cli_ipod, tmp_path: Path, capsys) -> None:
        music = tmp_path / "music"
        music.mkdir()
        make_mp3(music / "a.mp3", title="A", artist="周杰伦")
        make_mp3(music / "b.mp3", title="B", artist="陈奕迅")
        main(["import", str(music), "--ipod", cli_ipod, "-y"])
        capsys.readouterr()

        out_dir = tmp_path / "backup"
        main(["export", str(out_dir), "--ipod", cli_ipod, "--artist", "周杰伦"])
        assert "已导出 1 / 1 首" in capsys.readouterr().out

    def test_empty_library_is_not_an_error(self, cli_ipod, tmp_path: Path, capsys) -> None:
        assert main(["export", str(tmp_path / "out"), "--ipod", cli_ipod]) == EXIT_OK
        assert "没有符合条件的曲目" in capsys.readouterr().out

    def test_all_conflicts_with_filters(self, cli_ipod, tmp_path: Path, capsys) -> None:
        """--all 与筛选条件同时用是理解错误，必须拦下来而不是悄悄忽略筛选。"""
        code = main([
            "export", str(tmp_path / "out"), "--ipod", cli_ipod,
            "--all", "--artist", "周杰伦",
        ])
        assert code == EXIT_ERROR
        assert "不能与" in capsys.readouterr().err

    @ffmpeg_required
    def test_unknown_playlist_reports_available(
        self, cli_ipod, tmp_path: Path, capsys
    ) -> None:
        music = tmp_path / "music"
        music.mkdir()
        make_mp3(music / "a.mp3", title="A", artist="艺人")
        main(["import", str(music), "--ipod", cli_ipod, "-y"])
        capsys.readouterr()

        code = main([
            "export", str(tmp_path / "out"), "--ipod", cli_ipod,
            "--playlist", "不存在的歌单",
        ])
        assert code == EXIT_ERROR
        assert "找不到播放列表" in capsys.readouterr().err
