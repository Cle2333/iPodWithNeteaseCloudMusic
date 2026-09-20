"""核对彩排结果：新歌进去了没、老的有没有被碰坏。

真机测试的教训是"写入成功"这四个字说明不了任何事——必须逐项核对：
播放列表、iPod 名字、已有曲目的封面、新曲目的封面、播放列表成员。
"""

import os
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from ipod_cli.library import read_library  # noqa: E402

EXPECTED_OLD = 118
EXPECTED_NEW = 3


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    reh = Path(args[0] if args else
               str(Path(os.environ.get("TEMP", ".")) / "reh-p2"))
    # 期望的 iPod 名字可选——改名之后名字就变了，写死会误报
    expect_name = args[1] if len(args) > 1 else ""
    library = read_library(reh)

    print("=" * 70)
    print("彩排结果核对")
    print("=" * 70)

    ok = True

    # 1. 曲目总数
    total = len(library.tracks)
    want = EXPECTED_OLD + EXPECTED_NEW
    mark = "✅" if total == want else "❌"
    ok &= total == want
    print(f"  {mark} 曲目总数       {total}（期望 {want}）")

    # 2. iPod 名字（写在主播放列表标题里，最容易静默丢失）
    name = library.ipod_name
    if expect_name:
        mark = "✅" if name == expect_name else "❌"
        ok &= name == expect_name
        print(f"  {mark} iPod 名字      {name!r}（期望 {expect_name!r}）")
    else:
        print(f"  ·  iPod 名字      {name!r}（未指定期望值，跳过判定）")

    # 3. 播放列表
    print()
    print("  播放列表：")
    names = []
    for playlist in library.playlists:
        title = (playlist.get("Title") or playlist.get("Playlist Title")
                 or playlist.get("name") or "?")
        names.append(title)
        print(f"      {title}")

    # 4. 新歌的播放列表成员
    target = None
    for playlist in library.playlists:
        title = (playlist.get("Title") or playlist.get("Playlist Title")
                 or playlist.get("name") or "")
        if title == "我喜欢的音乐":
            target = playlist
            break

    print()
    if target is None:
        print("  ❌ 没找到「我喜欢的音乐」播放列表")
        ok = False
    else:
        # 播放列表成员数存在不同字段名里，逐个试
        count = None
        for key in ("Track Count", "Item Count", "Num Tracks", "track_count"):
            if key in target:
                count = target[key]
                break
        print(f"  {'✅' if count == EXPECTED_NEW else '⚠ '} 「我喜欢的音乐」"
              f"成员数：{count}（期望 {EXPECTED_NEW}）")
        if count is not None:
            ok &= count == EXPECTED_NEW
        # 把能看到的键打出来，便于判断真正的字段名
        print(f"      可用字段: {sorted(target.keys())[:14]}")

    # 5. 曲目封面。注意 artwork_id_ref 在 raw 字典里，不在 Track 对象上——
    #    第一版核对我就是用 getattr 取的，结果把"有封面"读成了 0/121。
    print()

    def artwork_ref(track) -> int:
        return int((track.raw or {}).get("artwork_id_ref") or 0)

    with_art = [t for t in library.tracks if artwork_ref(t)]
    print(f"  {'✅' if len(with_art) == total else '⚠ '} 有封面的曲目 "
          f"{len(with_art)}/{total}")
    for track in library.tracks:
        if not artwork_ref(track):
            # 不判失败：上报"哪一首没有"比一个数字有用。
            # 真机上那首是我早先测试留下的残留曲目，本来就没封面。
            print(f"      无封面：{track.title!r} / {track.artist!r}  "
                  f"{track.location}")

    # 6. 新加的三首
    print()
    print("  新增曲目：")
    new_tracks = [
        t for t in library.tracks
        if t.title in ("STAY", "green to blue", "红色高跟鞋")
    ]
    if len(new_tracks) == EXPECTED_NEW:
        print("      ✅ 三首都找到了")
    else:
        print(f"      ❌ 只找到 {len(new_tracks)} 首")
        ok = False
    for track in new_tracks:
        art = artwork_ref(track)
        mark = "✅" if art else "❌"
        print(f"      {mark} {track.title[:22]:<24} {track.artist[:16]:<18} "
              f"{track.length / 1000:6.1f}s  {track.location}")
        if not art:
            ok = False

    # 7. 「我喜欢的音乐」的实际成员列表。
    #    条目用的是 ``track_id``（iTunesDB 里的曲目唯一号），**不是** ``db_track_id``
    #    （那是持久 ID，两套东西）。第一版核对读错了字段，误报"找不到"。
    print()
    if target is not None:
        items = target.get("items") or []
        print(f"  {'✅' if len(items) == EXPECTED_NEW else '❌'} "
              f"「我喜欢的音乐」实际成员 {len(items)} 个（期望 {EXPECTED_NEW}）")
        if len(items) != EXPECTED_NEW:
            ok = False
        by_track_id = {
            int((t.raw or {}).get("track_id") or 0): t for t in library.tracks
        }
        for entry in items[:5]:
            tid = int(entry.get("track_id") or 0)
            track = by_track_id.get(tid)
            if track is None:
                print(f"      ❌ track_id={tid} 指向不存在的曲目")
                ok = False
                continue
            art_ok = "✅" if artwork_ref(track) else "❌"
            print(f"      {art_ok} {track.title[:24]:<26} {track.artist[:16]:<18} "
                  f"{track.length / 1000:6.1f}s")
            if not artwork_ref(track):
                ok = False

    # 8. 已有曲目的封面引用不能被写坏。
    #    复用 P1 写的健康检查——它专门查"悬空引用"（引用指向不存在的封面条目），
    #    这正是当年"只还原 iTunesDB 导致封面 100% 悬空"那次的判据。
    print()
    print("  封面完整性（复用 ipod verify 的检查）：")
    import contextlib
    import io as _io

    from ipod_cli.discovery import probe_mount
    from ipod_cli.verify import check_device

    device = probe_mount(reh)
    buffer = _io.StringIO()
    with contextlib.redirect_stdout(buffer):
        report = check_device(device)
    for check in report.checks:
        if "封面" in check.name:
            icon = {"ok": "✅", "warn": "⚠ ", "fail": "❌"}.get(check.status, "?")
            print(f"      {icon} {check.name}：{check.summary}")
            for line in check.details or []:
                print(f"          {line}")
            if check.status == "fail":
                ok = False

    print()
    print("=" * 70)
    print("  结论：全部通过 ✅" if ok else "  结论：有问题 ❌ 见上面标 ❌/⚠ 的项")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
