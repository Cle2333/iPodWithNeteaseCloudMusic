"""用 ffmpeg 把 iPod 播放不了的格式转成它能播的格式。

遵循 iOpenPod 的做法，并且再往前一步：

* **无损源**（FLAC / APE / WavPack / WAV / AIFF / TAK / TTA）→ 转 **ALAC**
  （Apple 无损），装进 .m4a 容器。不二次损失音质。
* **有损源**（OGG / Opus / WMA / MPC）→ 转 **AAC** 256kbps。

转码失败的条目会被跳过并报错，绝不会留下"文件名是 m4a、内容是 FLAC"的坏文件。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .mediafile import PcTrack, probe_media_file

# 无损源：转 ALAC，保音质
LOSSLESS_SOURCE_EXTENSIONS = {
    ".flac", ".wav", ".aif", ".aiff",
    ".ape", ".wv", ".tak", ".tta", ".dsf", ".dff",
}

# iPod 不支持且源本身有损 → 转 AAC
LOSSY_SOURCE_EXTENSIONS = {".ogg", ".oga", ".opus", ".wma", ".mpc", ".spx"}

DEFAULT_AAC_BITRATE = 256          # kbps
TRANSCODE_TIMEOUT_SECONDS = 3600   # 单曲最长给 1 小时，够转一张超长有声书


class TranscodeError(RuntimeError):
    """转码失败。"""


@dataclass
class TranscodeOutcome:
    source: Path
    dest: Path
    codec: str                 # "alac" / "aac"
    duration_ms: int = 0
    bitrate: int = 0
    sample_rate: int = 44100


def find_ffmpeg() -> str | None:
    """找 ffmpeg 可执行文件。"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    # Windows 上 WinGet 装的 ffmpeg 不一定在 PATH 里，兜一下常见位置
    if os.name == "nt":
        for base in (
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
            Path(os.environ.get("ProgramFiles", "")) / "ffmpeg" / "bin",
        ):
            candidate = base / "ffmpeg.exe"
            if candidate.is_file():
                return str(candidate)
        # WinGet 的 Packages 目录里版本号是目录名，扫一层
        packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        if packages.is_dir():
            for hit in packages.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
                return str(hit)
    return None


def require_ffmpeg() -> str:
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise TranscodeError(
            "找不到 ffmpeg。需要它来把 FLAC/OGG 等格式转成 iPod 能播的格式。\n"
            "  安装方式：\n"
            "    Windows : winget install Gyan.FFmpeg\n"
            "    macOS   : brew install ffmpeg\n"
            "    Linux   : sudo apt install ffmpeg"
        )
    return ffmpeg


def ffmpeg_available() -> bool:
    return find_ffmpeg() is not None


def is_lossless_source(path: Path) -> bool:
    return Path(path).suffix.lower() in LOSSLESS_SOURCE_EXTENSIONS


def target_codec_for(path: Path) -> str:
    """决定用哪种编码：无损源保 ALAC，有损源转 AAC。"""
    return "alac" if is_lossless_source(path) else "aac"


#: iPod Classic 的 ALAC 上限：16bit / 48kHz。
#:
#: 这两个限制不是"建议"——超了就是播不了。实测网易云的 `lossless` 无损里
#: 有相当一部分是 **24bit** 源，直接 `-c:a alac` 会产出 24bit ALAC，
#: 拷进 iPod 之后固件根本不解码。
IPOD_MAX_SAMPLE_RATE = 48000


def build_ffmpeg_command(
    ffmpeg: str,
    source: Path,
    dest: Path,
    *,
    codec: str,
    aac_bitrate: int = DEFAULT_AAC_BITRATE,
    sample_rate_cap: int | None = None,
) -> list[str]:
    """拼出转码命令。

    ``-vn`` 丢掉视频/封面流：封面走 ArtworkDB 那条链路（从源文件直接提取），
    不需要塞进音频文件里，避免把 .m4a 撑大。

    ALAC 分支强制 ``-sample_fmt s16p``：iPod Classic 只支持 16bit ALAC，
    而 24bit 源（网易云无损里很常见）不转换就会产出播不了的 24bit ALAC。
    对 16bit 源这是空操作，所以可以无条件加。

    ``sample_rate_cap`` 只在源采样率确实超过它时由调用方传入，
    免得把 44.1kHz 的源无谓地重采样到 48kHz。
    """
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-y",
        "-i", str(source),
        "-vn",
        "-map_metadata", "0",
    ]
    if codec == "alac":
        command += ["-c:a", "alac", "-sample_fmt", "s16p"]
        if sample_rate_cap:
            command += ["-ar", str(sample_rate_cap)]
    else:
        command += ["-c:a", "aac", "-b:a", f"{aac_bitrate}k"]
    command.append(str(dest))
    return command


def transcode_file(
    source: Path,
    dest: Path,
    *,
    aac_bitrate: int = DEFAULT_AAC_BITRATE,
) -> TranscodeOutcome:
    """把 source 转码到 dest，返回产物信息。"""
    source = Path(source)
    dest = Path(dest)
    ffmpeg = require_ffmpeg()
    codec = target_codec_for(source)

    dest.parent.mkdir(parents=True, exist_ok=True)

    # ALAC 需要知道源的采样率，才能决定要不要降到 iPod 支持的上限。
    # 探测失败就交给 ffmpeg 原样处理（44.1/48kHz 是绝大多数情况，够用）。
    sample_rate_cap: int | None = None
    if codec == "alac":
        try:
            _, _, source_rate = probe_media_file(source)
        except Exception:
            source_rate = 0
        if source_rate and source_rate > IPOD_MAX_SAMPLE_RATE:
            sample_rate_cap = IPOD_MAX_SAMPLE_RATE

    command = build_ffmpeg_command(
        ffmpeg,
        source,
        dest,
        codec=codec,
        aac_bitrate=aac_bitrate,
        sample_rate_cap=sample_rate_cap,
    )

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=TRANSCODE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _cleanup(dest)
        raise TranscodeError(
            f"转码超时（超过 {TRANSCODE_TIMEOUT_SECONDS // 60} 分钟）：{source.name}"
        ) from exc
    except OSError as exc:
        _cleanup(dest)
        raise TranscodeError(f"无法执行 ffmpeg：{exc}") from exc

    if completed.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        _cleanup(dest)
        detail = (completed.stderr or "").strip().splitlines()
        tail = " / ".join(detail[-3:]) if detail else f"退出码 {completed.returncode}"
        raise TranscodeError(f"转码失败：{source.name}\n  ffmpeg: {tail}")

    outcome = TranscodeOutcome(source=source, dest=dest, codec=codec)
    duration_ms, bitrate, sample_rate = probe_media_file(dest)
    outcome.duration_ms = duration_ms
    outcome.bitrate = bitrate
    outcome.sample_rate = sample_rate
    return outcome


def transcode_for_import(pc: PcTrack, dest: Path, **kwargs) -> Path:
    """给 importer 用的转码回调：``(PcTrack, 目标路径) -> 产物路径``。

    顺手把产物真实属性写回 ``pc``，这样 iTunesDB 里记的时长/码率跟文件对得上
    （源文件是 FLAC 时，源码率跟转出来的 ALAC 码率完全是两回事）。
    """
    outcome = transcode_file(pc.source_path, dest, **kwargs)
    if outcome.duration_ms:
        pc.duration_ms = outcome.duration_ms
    if outcome.bitrate:
        pc.bitrate = outcome.bitrate
    if outcome.sample_rate:
        pc.sample_rate = outcome.sample_rate
    return outcome.dest


def _cleanup(path: Path) -> None:
    """转码失败时清掉半成品，避免留下坏文件。"""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
