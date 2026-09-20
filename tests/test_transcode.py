"""转码的测试。

重点守一个**真机才会暴露的 bug**：iPod Classic 只支持 **16bit / 48kHz** 的
ALAC，而网易云的 `lossless` 无损里有相当一部分是 **24bit** 源。
原来直接 `-c:a alac` 会产出 24bit ALAC——文件本身没问题、ffmpeg 也不报错、
我们的读回校验也通不过（它只看曲目数），**但拷进 iPod 就是播不出来**。

这类"本地一切正常、到设备上才炸"的问题只能靠断言产物的真实位深来挡。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import ffmpeg_required, make_flac

from ipod_cli.transcode import (
    IPOD_MAX_SAMPLE_RATE,
    build_ffmpeg_command,
    find_ffmpeg,
    transcode_file,
)

pytestmark = ffmpeg_required


def probe_audio(path: Path) -> dict:
    """用 ffprobe 读产物的真实编码参数。"""
    ffmpeg = find_ffmpeg()
    assert ffmpeg
    ffprobe = str(Path(ffmpeg).with_name("ffprobe"))
    out = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "stream=codec_name,sample_rate,bits_per_raw_sample",
            "-of", "default=noprint_wrappers=1:nokey=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result: dict = {}
    for line in out.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


class TestCommandConstruction:
    """命令拼装要能被单测覆盖，不必真跑 ffmpeg。"""

    def test_alac_forces_16bit(self, tmp_path: Path) -> None:
        """核心断言：ALAC 一律带 -sample_fmt s16p。"""
        command = build_ffmpeg_command(
            "ffmpeg", tmp_path / "a.flac", tmp_path / "b.m4a", codec="alac"
        )
        assert "-c:a" in command
        assert command[command.index("-c:a") + 1] == "alac"
        assert "-sample_fmt" in command
        assert command[command.index("-sample_fmt") + 1] == "s16p"

    def test_aac_does_not_get_sample_fmt(self, tmp_path: Path) -> None:
        """AAC 分支不该被 ALAC 的规则污染。"""
        command = build_ffmpeg_command(
            "ffmpeg", tmp_path / "a.ogg", tmp_path / "b.m4a", codec="aac"
        )
        assert "-sample_fmt" not in command
        assert "-ar" not in command

    def test_no_rate_cap_by_default(self, tmp_path: Path) -> None:
        """不传 cap 就不能加 -ar，否则 44.1kHz 会被无谓重采样。"""
        command = build_ffmpeg_command(
            "ffmpeg", tmp_path / "a.flac", tmp_path / "b.m4a", codec="alac"
        )
        assert "-ar" not in command

    def test_rate_cap_applied_when_asked(self, tmp_path: Path) -> None:
        command = build_ffmpeg_command(
            "ffmpeg", tmp_path / "a.flac", tmp_path / "b.m4a",
            codec="alac", sample_rate_cap=48000,
        )
        assert "-ar" in command
        assert command[command.index("-ar") + 1] == "48000"


class TestRealTranscode:
    """真跑 ffmpeg，断言产物的真实参数。"""

    def test_16bit_source_stays_16bit(self, tmp_path: Path) -> None:
        source = make_flac(tmp_path / "cd.flac", title="CD 音质", bits=16)
        dest = tmp_path / "cd.m4a"
        transcode_file(source, dest)

        info = probe_audio(dest)
        assert info["codec_name"] == "alac"
        assert int(info["bits_per_raw_sample"]) == 16

    def test_24bit_source_is_downsampled_to_16bit(self, tmp_path: Path) -> None:
        """★ 这就是那个 bug：24bit 源必须落成 16bit ALAC。"""
        source = make_flac(tmp_path / "hi.flac", title="24bit 源", bits=24)
        assert int(probe_audio(source)["bits_per_raw_sample"]) == 24, (
            "测试前提不成立：源不是 24bit"
        )

        dest = tmp_path / "hi.m4a"
        transcode_file(source, dest)

        info = probe_audio(dest)
        assert info["codec_name"] == "alac"
        assert int(info["bits_per_raw_sample"]) == 16, (
            f"产出 {info['bits_per_raw_sample']}bit ALAC，iPod Classic 播不了"
        )

    def test_96khz_source_is_capped(self, tmp_path: Path) -> None:
        source = make_flac(tmp_path / "hr.flac", title="96k 源", sample_rate=96000)
        dest = tmp_path / "hr.m4a"
        transcode_file(source, dest)

        info = probe_audio(dest)
        assert int(info["sample_rate"]) <= IPOD_MAX_SAMPLE_RATE

    def test_44100_source_is_not_resampled(self, tmp_path: Path) -> None:
        """44.1kHz 是绝大多数情况，不能被无谓地重采样。"""
        source = make_flac(tmp_path / "n.flac", title="常规", sample_rate=44100)
        dest = tmp_path / "n.m4a"
        transcode_file(source, dest)
        assert int(probe_audio(dest)["sample_rate"]) == 44100

    def test_48000_source_kept_as_is(self, tmp_path: Path) -> None:
        """48kHz 正好在支持范围内，不该降。"""
        source = make_flac(tmp_path / "s.flac", title="48k", sample_rate=48000)
        dest = tmp_path / "s.m4a"
        transcode_file(source, dest)
        assert int(probe_audio(dest)["sample_rate"]) == 48000

    def test_metadata_is_carried_over(self, tmp_path: Path) -> None:
        """转码不能把标题艺人弄丢——iTunesDB 里的标签靠它。"""
        from ipod_cli.mediafile import read_pc_track

        source = make_flac(
            tmp_path / "tag.flac", title="曲目标题", artist="某艺人", bits=24
        )
        dest = tmp_path / "tag.m4a"
        transcode_file(source, dest)

        track = read_pc_track(dest)
        assert track.title == "曲目标题"
        assert track.artist == "某艺人"

    def test_outcome_reports_real_properties(self, tmp_path: Path) -> None:
        """产物属性要重新探测——源码率跟转出来的完全是两回事。"""
        source = make_flac(tmp_path / "p.flac", title="属性", bits=24)
        dest = tmp_path / "p.m4a"
        outcome = transcode_file(source, dest)

        assert outcome.codec == "alac"
        assert outcome.duration_ms > 0
        assert outcome.bitrate > 0
        assert outcome.sample_rate > 0
