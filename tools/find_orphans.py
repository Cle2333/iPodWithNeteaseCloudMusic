"""找出 iPod 上的**孤儿文件**：躺在 Music/ 里、但数据库里没有任何曲目引用它。

为什么会有孤儿文件：同步是「先拷文件、再写数据库」两步。写数据库那一步失败
（异常、或者进程被杀），文件已经在设备上了，而 iPod 的界面只认数据库——于是
这些文件**既看不见、又占着空间**，而且下次同步会重新分配文件名再拷一遍，
失败一次多一份。实测用户的机器上就这样堆到了 295 个文件（数据库只认 1 首），
白占 4 GB。

用法:
    uv run python tools/find_orphans.py --ipod D:\\            # 只看，不动
    uv run python tools/find_orphans.py --ipod D:\\ --delete   # 真删（会再确认一次）
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from ipod_cli.discovery import require_ipod  # noqa: E402
from ipod_cli.library import read_library  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    ipod_arg = None
    if "--ipod" in args:
        ipod_arg = args[args.index("--ipod") + 1]
    do_delete = "--delete" in args

    device = require_ipod(ipod_arg)
    print(f"设备：{device.root}")

    library = read_library(device.root)
    # 数据库里的 location 形如 ':iPod_Control:Music:F00:XXXX.mp3'
    known = {
        str(track.location).replace(":", "/").lstrip("/").casefold()
        for track in library.tracks
    }
    print(f"数据库里的曲目：{len(library.tracks)} 首")

    music = Path(device.root) / "iPod_Control" / "Music"
    orphans: list[Path] = []
    total_bytes = 0
    on_disk = 0
    for path in music.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        on_disk += 1
        rel = str(path.relative_to(Path(device.root))).replace("\\", "/").casefold()
        if rel not in known:
            orphans.append(path)
            total_bytes += path.stat().st_size

    print(f"磁盘上的音频文件：{on_disk} 个")
    print(f"其中**数据库不认的（孤儿）**：{len(orphans)} 个，"
          f"{total_bytes / 1024 / 1024:.0f} MB")

    if not orphans:
        print("\n没有孤儿文件 ✓")
        return 0

    print("\n前 10 个：")
    for path in orphans[:10]:
        print(f"   {path.stat().st_size / 1024 / 1024:7.1f} MB  {path.name}")
    if len(orphans) > 10:
        print(f"   …… 还有 {len(orphans) - 10} 个")

    if not do_delete:
        print("\n（只列不删。确认列表没问题之后加 --delete 真的删掉。）")
        print(f"  uv run python tools/find_orphans.py --ipod {device.root} --delete")
        return 0

    print(f"\n正在删除 {len(orphans)} 个文件……")
    removed = 0
    freed = 0
    for path in orphans:
        try:
            size = path.stat().st_size
            path.unlink()
            removed += 1
            freed += size
        except OSError as exc:
            print(f"   删不掉 {path.name}：{exc}")
    print(f"已删除 {removed} 个，释放 {freed / 1024 / 1024:.0f} MB")
    print("\n注意：iPod 上的**空文件夹**留着无害（固件不看它）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
