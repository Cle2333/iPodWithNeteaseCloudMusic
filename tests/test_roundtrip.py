"""往返测试：写 iTunesDB → 签名 → 读回。

证明两件事：
  1. 裁剪后的 vendored 引擎仍能正确写入并签名（HASH58）
  2. 中文元数据能完整穿过二进制读写循环（中文版工具的编码风险点）

全部跑在 tmp_path 下的**虚拟 iPod**上，绝不碰真实设备。
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from iopenpod.device import create_virtual_ipod, detect_checksum_type
from iopenpod.itunesdb_shared.mhbd_defs import MHBD_OFFSET_HASHING_SCHEME
from iopenpod.itunesdb_writer import TrackInfo, write_itunesdb
from ipod_cli.library import read_library

# MB147 = iPod Classic 6th Gen 80GB Black（零售型号 A1238）
CLASSIC_MODEL = "MB147"

CHINESE_TRACKS = [
    TrackInfo(
        title="晴天",
        location=":iPod_Control:Music:F00:QINGTIAN.mp3",
        artist="周杰伦",
        album="叶惠美",
        album_artist="周杰伦",
        genre="华语流行",
        composer="周杰伦",
        year=2003,
        track_number=1,
        total_tracks=11,
        filetype="mp3",
        size=123456,
        length=269000,
        bitrate=320,
        sample_rate=44100,
    ),
    TrackInfo(
        title="富士山下",
        location=":iPod_Control:Music:F00:FUSHISHANXIA.m4a",
        artist="陈奕迅",
        album="认了吧",
        album_artist="陈奕迅",
        genre="粤语流行",
        year=2007,
        track_number=2,
        total_tracks=10,
        filetype="m4a",
        size=234567,
        length=259000,
        bitrate=256,
        sample_rate=44100,
    ),
    TrackInfo(
        title="Plain ASCII Title",
        location=":iPod_Control:Music:F01:PLAIN.mp3",
        artist="Some Artist",
        album="Some Album",
        filetype="mp3",
        size=345678,
        length=180000,
        bitrate=192,
        sample_rate=44100,
    ),
]

# 容易让粗糙编码器翻车的标点/符号边界
EDGE_TITLES = [
    '引号"双"与「单」',
    "破折号——与省略号……",
    "emoji 🎵 音乐",
    "括号（全角）与(半角)",
    "百分号 100% & 与号",
    "空格   与\t制表",
]


@pytest.fixture()
def ipod(tmp_path: Path) -> Path:
    root = tmp_path / "ipod"
    create_virtual_ipod(root, CLASSIC_MODEL, ipod_name="测试用 iPod")
    return root


def _materialise_audio(ipod_root: Path, tracks: list[TrackInfo]) -> None:
    """把数据库要指向的音频文件真实建出来。"""
    for track in tracks:
        target = ipod_root / track.location.strip(":").replace(":", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00" * 1024)


def test_virtual_ipod_requires_hash58(ipod: Path) -> None:
    """Classic 必须走 HASH58（而非 HASH72/HASHAB）。"""
    assert detect_checksum_type(str(ipod)).name == "HASH58"


def test_written_database_carries_hash58_scheme(ipod: Path) -> None:
    """写出的库头部签名方案必须是 1（HASH58）。"""
    _materialise_audio(ipod, CHINESE_TRACKS)
    assert write_itunesdb(str(ipod), CHINESE_TRACKS, capabilities=None) is True

    raw = (ipod / "iPod_Control" / "iTunes" / "iTunesDB").read_bytes()
    assert raw[:4] == b"mhbd"
    scheme = struct.unpack_from("<I", raw, MHBD_OFFSET_HASHING_SCHEME)[0]
    assert scheme == 1, f"签名方案应为 1 (HASH58)，实际为 {scheme}"


def test_hash58_field_is_populated(ipod: Path) -> None:
    """HASH58 签名字段必须写入非零值，否则 iPod 会判定数据库损坏。"""
    _materialise_audio(ipod, CHINESE_TRACKS)
    assert write_itunesdb(str(ipod), CHINESE_TRACKS, capabilities=None) is True

    from iopenpod.itunesdb_parser import parse_itunesdb

    db = parse_itunesdb(str(ipod / "iPod_Control" / "iTunes" / "iTunesDB"))
    signature = db.get("hash58")
    assert isinstance(signature, bytes) and len(signature) >= 8
    assert signature != b"\x00" * len(signature), "HASH58 签名为全零 = 未签名"


def test_roundtrip_chinese_metadata(ipod: Path) -> None:
    """中文标题/艺人/专辑必须原样读回。"""
    _materialise_audio(ipod, CHINESE_TRACKS)
    assert write_itunesdb(str(ipod), CHINESE_TRACKS, capabilities=None) is True

    tracks = read_library(ipod).tracks
    assert len(tracks) == len(CHINESE_TRACKS)

    by_title = {t.title: t for t in tracks}
    for expected in CHINESE_TRACKS:
        got = by_title.get(expected.title)
        assert got is not None, f"曲目丢失：{expected.title!r}"
        assert got.artist == expected.artist, f"{expected.title} 的艺人串码"
        assert got.album == expected.album, f"{expected.title} 的专辑串码"
        assert got.location == expected.location, f"{expected.title} 的路径串码"
        assert got.length == expected.length, f"{expected.title} 的时长串码"
        assert got.bitrate == expected.bitrate, f"{expected.title} 的码率串码"


def test_roundtrip_unicode_edge_cases(ipod: Path) -> None:
    """标点/emoji/空白等边界字符不能丢字或串码。"""
    tracks = [
        TrackInfo(
            title=title,
            location=f":iPod_Control:Music:F00:EDGE{i}.mp3",
            artist="测试艺人",
            album="边界测试",
            filetype="mp3",
            size=1000,
            length=1000,
            bitrate=128,
            sample_rate=44100,
        )
        for i, title in enumerate(EDGE_TITLES)
    ]

    assert write_itunesdb(str(ipod), tracks, capabilities=None) is True
    got_titles = {t.title for t in read_library(ipod).tracks}

    for title in EDGE_TITLES:
        assert title in got_titles, f"边界标题丢失或串码：{title!r}"


def test_read_library_derives_relative_path(ipod: Path) -> None:
    """冒号路径要能正确还原成可用的相对路径。"""
    _materialise_audio(ipod, CHINESE_TRACKS)
    assert write_itunesdb(str(ipod), CHINESE_TRACKS, capabilities=None) is True

    by_title = {t.title: t for t in read_library(ipod).tracks}
    track = by_title["晴天"]
    assert track.relative_path == "iPod_Control/Music/F00/QINGTIAN.mp3"
    assert track.duration_text == "4:29"


# ── 播放次数增量文件（"Play Counts"）──────────────────────────────────────
#
# 它是"iPod 上自上次同步以来积累的播放/跳过次数"的**增量**文件：读库时被
# 合并进曲目（libgpod 的 get_mhit 逻辑），随写库落盘。所以**写完必须清掉**
# ——iTunes 就是这么做的。
#
# 不清的后果（真机实测）：那份残留是 2023-10-08 的，库里早换过好几轮，
# 于是每读一次库就重新合并一遍（日志里每 6 秒一条警告），而且合并是
# **按下标配对**的（entries[i] ↔ tracks[i]）——旧歌的播放次数会算到新歌
# 头上，再随写库固化下来。


def _write_play_counts(root: Path, entries: list[tuple[int, int, int]]) -> Path:
    """造一个真实的 Play Counts 文件：96 字节头 + 28 字节/条。

    每条布局：play_count(+0) / last_played(+4) / bookmark(+8) / rating(+12)
    / unk16(+16) / skip_count(+20) / last_skipped(+24)。
    """
    header_len, entry_len = 96, 28
    buf = bytearray(header_len)
    buf[0:4] = b"mhdp"
    struct.pack_into("<III", buf, 4, header_len, entry_len, len(entries))
    for play, rating, skip in entries:
        entry = bytearray(entry_len)
        struct.pack_into("<I", entry, 0, play)
        struct.pack_into("<I", entry, 12, rating)
        struct.pack_into("<I", entry, 20, skip)
        buf += entry

    path = root / "iPod_Control" / "iTunes" / "Play Counts"
    path.write_bytes(bytes(buf))
    return path


def test_play_counts_are_absorbed_then_cleared(ipod: Path) -> None:
    """★ 先把增量吸收进曲目并落盘，然后才清掉文件——不能直接丢数据。"""
    from ipod_cli.dbwrite import build_track_infos, write_library
    from ipod_cli.discovery import probe_mount

    _materialise_audio(ipod, CHINESE_TRACKS)
    write_itunesdb(str(ipod), CHINESE_TRACKS, capabilities=None)

    pc = _write_play_counts(
        ipod, [(7, 0, 2)] + [(0, 0, 0)] * (len(CHINESE_TRACKS) - 1)
    )

    # ① 读库时增量被合并进来
    merged = read_library(ipod)
    assert merged.track_dicts[0]["play_count_1"] >= 7
    assert merged.track_dicts[0]["skip_count"] >= 2

    # ② 写库（含读回校验）之后，增量文件要被清掉
    device = probe_mount(ipod)
    infos, _ = build_track_infos(merged.track_dicts)
    result = write_library(device, merged, infos, pc_file_paths=None)

    assert result.play_counts_cleared, (
        "没清掉增量文件——下次读库会再合并一遍，而且按下标配对会把"
        "旧歌的播放次数算到新歌头上"
    )
    assert not pc.exists()

    # ③ 关键是"吸收后清掉"，不是"直接丢掉"
    after = read_library(ipod)
    assert after.track_dicts[0]["play_count_1"] >= 7, "播放次数丢了"
    assert after.track_dicts[0]["skip_count"] >= 2, "跳过次数丢了"


def test_no_play_counts_file_is_not_an_error(ipod: Path) -> None:
    """本来就没有增量文件（正常情况）→ 写库照常成功，不报错。"""
    from ipod_cli.dbwrite import build_track_infos, write_library
    from ipod_cli.discovery import probe_mount

    _materialise_audio(ipod, CHINESE_TRACKS)
    write_itunesdb(str(ipod), CHINESE_TRACKS, capabilities=None)
    assert not (ipod / "iPod_Control" / "iTunes" / "Play Counts").exists()

    library = read_library(ipod)
    infos, _ = build_track_infos(library.track_dicts)
    result = write_library(probe_mount(ipod), library, infos, pc_file_paths=None)

    assert result.play_counts_cleared is False
    assert result.verified is True, "没有增量文件不该影响写库结果"


def test_stale_play_counts_do_not_leak_onto_the_wrong_track(ipod: Path) -> None:
    """★ 增量条数比曲目多时（残留文件），不能张冠李戴。

    实测：库里 2 首、增量文件 3 条。合并按下标配对，第 3 条无处可去
    （被丢弃），前两条可能压到**不同的歌**上。清掉文件是根治办法——
    这里守住"清掉之后写库、再读不会带出任何残留"。
    """
    from ipod_cli.dbwrite import build_track_infos, write_library
    from ipod_cli.discovery import probe_mount

    _materialise_audio(ipod, CHINESE_TRACKS)
    write_itunesdb(str(ipod), CHINESE_TRACKS, capabilities=None)

    # 比曲目多两条的残留
    pc = _write_play_counts(
        ipod, [(5, 0, 0)] * (len(CHINESE_TRACKS) + 2)
    )
    device = probe_mount(ipod)
    library = read_library(ipod)
    infos, _ = build_track_infos(library.track_dicts)
    write_library(device, library, infos, pc_file_paths=None)

    assert not pc.exists(), "残留没清掉，下次读库还会再按位置错配一遍"
    assert len(read_library(ipod).tracks) == len(CHINESE_TRACKS)
