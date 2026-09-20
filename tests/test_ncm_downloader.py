"""下载器的测试。

重点不是"能下载"，而是**下完之后文件是不是真的能用**：

1. 标签要能被项目现有的读取器读到（写错编码/字段名的话读出来是空的）
2. 封面要能被项目现有的**提取器**读到——这是 iPod 封面的唯一来源，
   嵌错了位置（比如 MP3 用 ID3v2.4 的 APIC 而提取器只认别的）就白嵌
3. **格式不符必须报错**，绝不能把 MP3 字节存成 .flac。
   这是本项目真实踩过的坑（当年 FLAC 字节被拷成 .m4a），
   文件能播、元数据全错、极难定位。

网络全部被替掉——套件不碰真接口，也不碰真 CDN。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import ffmpeg_required, make_flac, make_mp3

from ipod_cli.ncm.client import DEFAULT_QUALITY, NcmClient, Song, SongUrl
from ipod_cli.ncm.downloader import (
    DownloadError,
    detect_kind,
    download_song,
    expected_extension,
    safe_filename,
    target_path,
    write_tags,
)

pytestmark = ffmpeg_required


def make_song(**overrides: Any) -> Song:
    base = {
        "id": 111,
        "name": "测试歌名",
        "artists": ["测试艺人"],
        "album": "测试专辑",
        "cover_url": "https://p.example/cover.jpg",
        "duration_ms": 1000,
        "track_no": 5,
        "fee": 0,
    }
    base.update(overrides)
    return Song(**base)


class TestFilenames:
    def test_strips_illegal_characters(self) -> None:
        assert safe_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"

    def test_keeps_chinese_and_spaces(self) -> None:
        """中文和空格要留着——缓存目录是给人看的。"""
        assert safe_filename("周杰伦 - 稻香") == "周杰伦 - 稻香"

    def test_collapses_whitespace(self) -> None:
        assert safe_filename("a   \t  b") == "a b"

    def test_limits_length(self) -> None:
        assert len(safe_filename("字" * 200)) <= 80

    def test_empty_gets_a_placeholder(self) -> None:
        assert safe_filename("") == "未命名"
        assert safe_filename("///") == "未命名"

    def test_trailing_dots_and_spaces_trimmed(self) -> None:
        """Windows 不允许文件名以点或空格结尾。"""
        assert safe_filename("歌名... ") == "歌名"


class TestExtensionsAndPaths:
    def test_lossless_levels_give_flac(self) -> None:
        for level in ("lossless", "hires", "jymaster"):
            assert expected_extension(level) == ".flac"

    def test_lossy_levels_give_mp3(self) -> None:
        for level in ("exhigh", "standard"):
            assert expected_extension(level) == ".mp3"

    def test_path_has_id_prefix_for_uniqueness(self, tmp_path: Path) -> None:
        path = target_path(tmp_path, make_song(), "exhigh")
        assert path.name.startswith("111 ")
        assert path.suffix == ".mp3"

    def test_same_title_different_id_does_not_collide(self, tmp_path: Path) -> None:
        """同名歌曲很常见——靠 ID 前缀区分，别互相覆盖。"""
        a = target_path(tmp_path, make_song(id=1), "exhigh")
        b = target_path(tmp_path, make_song(id=2), "exhigh")
        assert a != b

    def test_extension_follows_the_actual_level(self, tmp_path: Path) -> None:
        """★ 按"实际拿到的"档位定扩展名，不按请求的。

        请求 lossless 被降级成 320k 时，扩展名必须是 .mp3。
        """
        path = target_path(tmp_path, make_song(), "exhigh")
        assert path.suffix == ".mp3"
        path = target_path(tmp_path, make_song(), "lossless")
        assert path.suffix == ".flac"


class TestMagicDetection:
    def test_flac(self) -> None:
        assert detect_kind(b"fLaC\x00\x00\x00\x22") == "flac"

    def test_mp3_with_id3(self) -> None:
        assert detect_kind(b"ID3\x04\x00\x00") == "mp3"

    def test_mp3_bare_frame_sync(self) -> None:
        assert detect_kind(b"\xff\xfb\x90\x00") == "mp3"

    def test_m4a(self) -> None:
        assert detect_kind(b"\x00\x00\x00\x20ftypM4A ") == "m4a"

    def test_garbage(self) -> None:
        assert detect_kind(b"<html>error</html>") == "unknown"
        assert detect_kind(b"") == "unknown"


class TestWriteTagsFlac:
    """FLAC 是**必须要自己写标签**的那种——网易云给的 FLAC 一个字段都没有。"""

    def test_writes_all_fields(self, tmp_path: Path) -> None:
        from ipod_cli.mediafile import read_pc_track

        path = make_flac(tmp_path / "a.flac", title="")

        write_tags(path, make_song())
        track = read_pc_track(path)

        assert track.title == "测试歌名"
        assert track.artist == "测试艺人"
        assert track.album == "测试专辑"

    def test_embeds_cover_the_project_can_extract(self, tmp_path: Path) -> None:
        """★ 嵌进去的封面必须能被项目现有的提取器读到。

        这是 iPod 上封面的唯一来源——嵌在提取器不认识的位置等于没嵌。
        """
        from iopenpod.artworkdb_writer.art_extractor import extract_art

        path = make_flac(tmp_path / "b.flac", title="")
        cover = b"\xff\xd8\xff\xe0" + b"JPEGDATA" * 100

        assert write_tags(path, make_song(), cover) is True

        extracted = extract_art(str(path))
        assert extracted == cover, "提取器读到的封面和写入的不一致"

    def test_no_cover_means_no_picture(self, tmp_path: Path) -> None:
        from mutagen.flac import FLAC

        path = make_flac(tmp_path / "c.flac", title="")
        assert write_tags(path, make_song(), b"") is False
        assert len(FLAC(str(path)).pictures) == 0

    def test_overwrites_existing_tags(self, tmp_path: Path) -> None:
        """文件自带的标签要按接口数据覆盖，保证不同来源进 iPod 后一致。"""
        from ipod_cli.mediafile import read_pc_track

        path = make_flac(tmp_path / "d.flac", title="旧的错误标题", artist="旧的艺人")

        write_tags(path, make_song(name="新的正确标题", artists=["新艺人"]))
        track = read_pc_track(path)

        assert track.title == "新的正确标题"
        assert track.artist == "新艺人"


class TestWriteTagsMp3:
    def test_writes_all_fields(self, tmp_path: Path) -> None:
        from ipod_cli.mediafile import read_pc_track

        path = make_mp3(tmp_path / "a.mp3", title="网易云自带标题")

        write_tags(path, make_song(name="接口标题", artists=["接口艺人"]))
        track = read_pc_track(path)

        assert track.title == "接口标题"
        assert track.artist == "接口艺人"
        assert track.album == "测试专辑"

    def test_embeds_cover_the_project_can_extract(self, tmp_path: Path) -> None:
        from iopenpod.artworkdb_writer.art_extractor import extract_art

        path = make_mp3(tmp_path / "b.mp3", title="")
        cover = b"\xff\xd8\xff\xe0" + b"JPEGDATA" * 100

        assert write_tags(path, make_song(), cover) is True

        extracted = extract_art(str(path))
        assert extracted == cover

    def test_uses_id3v2_3_for_ipod_compatibility(self, tmp_path: Path) -> None:
        """iPod 对 ID3v2.3 的兼容性最好，v2.4 在部分固件上会读不到标签。"""
        from mutagen.id3 import ID3

        path = make_mp3(tmp_path / "c.mp3", title="")
        write_tags(path, make_song())

        assert ID3(str(path)).version[:2] == (2, 3)

    def test_works_on_file_without_existing_tags(self, tmp_path: Path) -> None:
        """裸 MP3（没有 ID3 块）也要能写进去。"""
        import subprocess

        from ipod_cli.mediafile import read_pc_track
        from ipod_cli.transcode import find_ffmpeg

        ffmpeg = find_ffmpeg()
        assert ffmpeg
        path = tmp_path / "bare.mp3"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
             "-map_metadata", "-1", "-c:a", "libmp3lame", str(path)],
            check=True, capture_output=True,
        )

        write_tags(path, make_song())
        assert read_pc_track(path).title == "测试歌名"


class TestDownloadSong:
    """``download_song`` 的端到端行为（网络全替换）。"""

    @pytest.fixture()
    def client(self) -> NcmClient:
        return NcmClient(cookie="test", sleep=lambda _s: None)

    def _stub_source(self, monkeypatch, raw: bytes, level: str,
                     *, trial: bool = False) -> None:
        """把"取链接 + 下载字节 + 下封面"三件网络事全替掉。"""
        import ipod_cli.ncm.downloader as dl

        monkeypatch.setattr(
            NcmClient, "song_url",
            lambda self, song_id, level=DEFAULT_QUALITY, cookie="": SongUrl(
                song_id=song_id, url="https://cdn.example/a", level=level,
                bitrate=320000, size=len(raw), trial=trial,
            ),
        )
        monkeypatch.setattr(dl, "download_audio", lambda url, timeout=60: raw)
        monkeypatch.setattr(
            NcmClient, "cover_bytes", lambda self, url, size=600: b"\xff\xd8\xff\xe0COVER"
        )

    def test_downloads_and_tags(self, tmp_path: Path, client: NcmClient,
                                monkeypatch) -> None:
        real = tmp_path / "src.mp3"
        make_mp3(real, title="原始")
        self._stub_source(monkeypatch, real.read_bytes(), "exhigh")

        result = download_song(client, make_song(), tmp_path / "out", level="exhigh")

        assert result.path.is_file()
        assert result.level == "exhigh"
        assert result.has_cover is True
        assert result.size > 0

        from ipod_cli.mediafile import read_pc_track

        track = read_pc_track(result.path)
        assert track.title == "测试歌名"
        assert track.artist == "测试艺人"

    def test_format_mismatch_is_rejected(self, tmp_path: Path, client: NcmClient,
                                         monkeypatch) -> None:
        """★ 请求无损却拿回 MP3 字节 —— 必须报错，不能默默存成 .flac。

        这正是本项目踩过的坑：字节和扩展名不符，文件"看起来"下好了，
        到 iPod 上才发现播不了/元数据全错。
        """
        real = tmp_path / "src.mp3"
        make_mp3(real, title="其实是 MP3")
        # 声明无损，实际给的却是 MP3 字节
        self._stub_source(monkeypatch, real.read_bytes(), "lossless")

        with pytest.raises(DownloadError, match="格式不符"):
            download_song(client, make_song(), tmp_path / "out", level="lossless")

    def test_garbage_is_rejected(self, tmp_path: Path, client: NcmClient,
                                 monkeypatch) -> None:
        """链接过期时 CDN 会返回 HTML/JSON 错误体，不能当音频存下来。"""
        self._stub_source(monkeypatch, b"<html>403 Forbidden</html>", "exhigh")

        with pytest.raises(DownloadError, match="不是音频"):
            download_song(client, make_song(), tmp_path / "out", level="exhigh")

    def test_unavailable_song_raises(self, tmp_path: Path, client: NcmClient,
                                     monkeypatch) -> None:
        monkeypatch.setattr(
            NcmClient, "song_url",
            lambda self, song_id, level=DEFAULT_QUALITY, cookie="": SongUrl(song_id=song_id),
        )

        with pytest.raises(DownloadError, match="无版权"):
            download_song(client, make_song(), tmp_path / "out", level="exhigh")

    def test_cover_failure_does_not_kill_the_song(self, tmp_path: Path,
                                                  client: NcmClient, monkeypatch) -> None:
        """封面拿不到不该让整首歌白下——没封面照样能播。"""
        real = tmp_path / "src.mp3"
        make_mp3(real, title="原始")
        self._stub_source(monkeypatch, real.read_bytes(), "exhigh")
        monkeypatch.setattr(
            NcmClient, "cover_bytes", lambda self, url, size=600: b""
        )

        result = download_song(client, make_song(), tmp_path / "out", level="exhigh")

        assert result.path.is_file()
        assert result.has_cover is False

    def test_flac_source_works_end_to_end(self, tmp_path: Path, client: NcmClient,
                                          monkeypatch) -> None:
        real = tmp_path / "src.flac"
        make_flac(real, title="")
        self._stub_source(monkeypatch, real.read_bytes(), "lossless")

        result = download_song(client, make_song(), tmp_path / "out", level="lossless")

        assert result.path.suffix == ".flac"
        assert result.has_cover is True

        from ipod_cli.mediafile import read_pc_track

        track = read_pc_track(result.path)
        assert track.title == "测试歌名"

    def test_trial_clip_is_rejected(self, tmp_path: Path, client: NcmClient,
                                    monkeypatch) -> None:
        """★ 试听片段必须被拒绝，绝不能存进缓存。

        这是实测踩过的坑：不带 cookie 请求下载链接，网易云返回 30 秒试听。
        文件能播、标签正常、所有校验都过，只有时长不对——如果不拦，
        整个库会悄无声息地变成一堆 30 秒片段。
        """
        real = tmp_path / "src.mp3"
        make_mp3(real, title="试听")
        self._stub_source(monkeypatch, real.read_bytes(), "standard", trial=True)

        with pytest.raises(DownloadError, match="试听"):
            download_song(client, make_song(), tmp_path / "out", level="exhigh")

    def test_too_short_file_is_rejected_and_cleaned_up(
        self, tmp_path: Path, client: NcmClient, monkeypatch
    ) -> None:
        """★ 时长核对：就算接口没标 freeTrialInfo，偏短的文件也要拦住。

        独立的第二道闸——接口改字段、走别的返回路径时，这条还能兜住。
        元数据说 200 秒、文件只有 1 秒，明显不对。
        """
        real = tmp_path / "src.mp3"
        make_mp3(real, title="短")
        self._stub_source(monkeypatch, real.read_bytes(), "exhigh")

        song = make_song(duration_ms=200_000)     # 元数据说 200 秒
        with pytest.raises(DownloadError, match="偏短"):
            download_song(client, song, tmp_path / "out", level="exhigh")

        # 坏文件不能留在缓存目录里
        leftovers = list((tmp_path / "out").glob("*.mp3"))
        assert leftovers == [], f"偏短的文件没被清掉：{leftovers}"

    def test_duration_close_enough_is_accepted(self, tmp_path: Path, client: NcmClient,
                                               monkeypatch) -> None:
        """元数据和实际时长有点出入是正常的（不同计长方式），不能误杀。"""
        real = tmp_path / "src.mp3"
        make_mp3(real, title="正常")
        self._stub_source(monkeypatch, real.read_bytes(), "exhigh")

        # 实际约 1 秒，元数据说 1.1 秒 —— 差 10%，在容差内
        result = download_song(
            client, make_song(duration_ms=1100), tmp_path / "out", level="exhigh"
        )
        assert result.path.is_file()

    def test_cookie_is_forwarded_to_url_lookup(self, tmp_path: Path, client: NcmClient,
                                               monkeypatch) -> None:
        """★ download_song 必须把 cookie 传到 song_url。

        这里是那个试听 bug 的源头：少传一个 cookie，静默拿到片段。
        """
        from ipod_cli.ncm.client import SongUrl

        real = tmp_path / "src.mp3"
        make_mp3(real, title="原始")

        seen: list[str] = []

        def spy(self, song_id, level="exhigh", cookie=""):  # noqa: ARG001
            seen.append(cookie)
            return SongUrl(song_id=song_id, url="https://cdn/a",
                           level="exhigh", size=1, trial=False)

        monkeypatch.setattr(NcmClient, "song_url", spy)
        import ipod_cli.ncm.downloader as dl

        monkeypatch.setattr(dl, "download_audio", lambda url, timeout=60: real.read_bytes())
        monkeypatch.setattr(NcmClient, "cover_bytes", lambda self, url, size=600: b"")

        download_song(client, make_song(), tmp_path / "out", level="exhigh",
                      cookie="账号cookie")

        assert seen == ["账号cookie"], "cookie 没传到 song_url"


