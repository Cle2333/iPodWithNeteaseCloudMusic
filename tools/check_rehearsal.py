"""在彩排环境上跑导入，然后核对播放列表、封面、签名、曲目保全。

用法: uv run python tools/check_rehearsal.py <彩排目录> [待导入路径...]
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def show_playlists(label: str, library) -> dict:
    raw = library.raw or {}
    counts = {}
    print(f"  [{label}]")
    for key in ("mhlp", "mhlp_smart", "mhlp_podcast"):
        items = raw.get(key) or []
        counts[key] = len(items)
        names = [
            f"{p.get('Title')!r}({len(p.get('items') or [])})"
            for p in items
        ]
        print(f"      {key}: {len(items)} 个  {' '.join(names)}")
    return counts


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    reh = Path(sys.argv[1])
    sources = [Path(p) for p in sys.argv[2:]] or [
        Path.home() / "AppData/Local/Temp/ipod-realtest"
    ]

    from iopenpod.artworkdb_writer.artworkdb_chunks import read_existing_artwork
    from iopenpod.itunesdb_shared.mhbd_defs import MHBD_OFFSET_HASHING_SCHEME
    from ipod_cli.discovery import probe_mount
    from ipod_cli.importer import build_import_plan, execute_import
    from ipod_cli.library import read_library
    from ipod_cli.mediafile import collect_audio_files
    from ipod_cli.transcode import transcode_for_import

    device = probe_mount(reh)
    assert device is not None
    device.activate()
    print(f"设备: {device.display_name} | {device.checksum}\n")

    before = read_library(reh)
    print(f"写入前: {len(before.tracks)} 首")
    before_pl = show_playlists("写入前播放列表", before)

    files: list[Path] = []
    for source in sources:
        files.extend(collect_audio_files(source))

    plan = build_import_plan(device, before, files)
    print(f"\n导入计划: {len(plan.to_add)} 首\n")

    result = execute_import(plan, transcode=transcode_for_import, progress=None)
    print(f"写入: 新增 {result.added} | 数据库 {result.database_written} | 校验 {result.verified}")
    print(f"  {result.verification_note}\n")

    after = read_library(reh)
    print(f"写入后: {len(after.tracks)} 首")
    after_pl = show_playlists("写入后播放列表", after)

    # ── 封面 ──
    art = read_existing_artwork(
        str(reh / "iPod_Control" / "Artwork" / "ArtworkDB"),
        str(reh / "iPod_Control" / "Artwork"),
    )
    ids = set(art.keys())
    by_song = {e.get("song_id"): i for i, e in art.items() if e.get("song_id")}
    linked = dangling = 0
    for t in after.track_dicts:
        ref = t.get("artwork_id_ref")
        sid = t.get("db_track_id")
        if sid in by_song or (ref and ref in ids):
            linked += 1
        elif ref:
            dangling += 1
    print(f"\n封面: {len(art)} 条 | 有可用封面 {linked}/{len(after.tracks)} | 悬空 {dangling}")

    # ── 签名 ──
    raw = (reh / "iPod_Control" / "iTunes" / "iTunesDB").read_bytes()
    scheme = struct.unpack_from("<I", raw, MHBD_OFFSET_HASHING_SCHEME)[0]
    print(f"签名方案: {scheme} ({'HASH58' if scheme == 1 else '异常'})")

    # ── 曲目保全 ──
    before_titles = {t.title for t in before.tracks}
    after_titles = {t.title for t in after.tracks}
    lost = before_titles - after_titles
    print(f"原有曲目保留: {len(before_titles - lost)}/{len(before_titles)}")

    # ── 判定 ──
    ok = (
        result.verified
        and not lost
        and scheme == 1
        and after_pl["mhlp"] >= before_pl["mhlp"]
        and after_pl["mhlp_smart"] >= before_pl["mhlp_smart"]
        and after_pl["mhlp_podcast"] >= before_pl["mhlp_podcast"]
    )
    print()
    print("=" * 56)
    print(f"彩排{'通过 ✅' if ok else '失败 ❌'}")
    if not ok:
        if lost:
            print(f"  ❌ 丢失曲目: {sorted(lost)[:5]}")
        for key in before_pl:
            if after_pl[key] < before_pl[key]:
                print(f"  ❌ 播放列表丢失: {key} {before_pl[key]} -> {after_pl[key]}")
    print("=" * 56)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
