"""用真机**当前**状态搭一个彩排 iPod。

跟 ``rehearsal_from_backup.py`` 的区别：那个用旧备份，这个直接照抄真机
现在的内容——所以能在"几乎就是真机"的环境上先跑一遍写入，
确认播放列表、名字、封面都还在，再去动真机。

彩排目录是个普通文件夹，需要通过 ``iPodInfo.json`` 标记成虚拟设备，
否则内核会以"不是独立挂载卷"为由拒绝写入（这个保护是对的，
只是在彩排场景下得显式绕过）。

用法: uv run python tools/rehearsal_from_device.py <真机盘符> <彩排目录> [--with-music]
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

#: 照抄这三块就够验证写库行为了。Music 默认不抄（几个 GB，慢），
#: 代价是彩排里 verify 会报"库里有记录但磁盘没文件"——那是预期的。
METADATA_DIRS = ("Device", "iTunes", "Artwork")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    with_music = "--with-music" in sys.argv

    if len(args) < 2:
        print(__doc__)
        return 2

    real = Path(args[0])
    reh = Path(args[1])
    control_src = real / "iPod_Control"
    if not control_src.is_dir():
        print(f"❌ {real} 里没有 iPod_Control")
        return 1

    from iopenpod.device import create_virtual_ipod

    if reh.exists():
        shutil.rmtree(reh)

    # 真机型号 + 名字都照抄，保证能力表和播放列表标题一致
    model = "MB029"
    sysinfo = control_src / "Device" / "SysInfo"
    ipod_name = "彩排"
    if sysinfo.is_file():
        from iopenpod.device.lookup import extract_model_number

        for line in sysinfo.read_text(errors="ignore").splitlines():
            if line.strip().startswith("ModelNumStr:"):
                # SysInfo 里是带前缀的（形如 xB029），要按内核的规则剥掉前缀，
                # 否则 create_virtual_ipod 认不出这个型号
                model = extract_model_number(line.split(":", 1)[1].strip()) or model

    create_virtual_ipod(reh, model, ipod_name=ipod_name)
    target_control = reh / "iPod_Control"

    for name in METADATA_DIRS:
        source = control_src / name
        if not source.is_dir():
            continue
        destination = target_control / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        print(f"  已复制 {name}/")

    if with_music:
        source = control_src / "Music"
        if source.is_dir():
            destination = target_control / "Music"
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
            print("  已复制 Music/")

    # 关键：标记为虚拟设备，绕过"必须是独立挂载卷"的写入检查
    marker = reh / "iPodInfo.json"
    marker.write_text(
        json.dumps(
            {
                "virtual": True,
                "note": "ipod-cli 彩排环境，不是真机",
                "model": model,
                "source_device": str(real),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  已标记 {marker.name}（虚拟设备）")

    # 顺手报一下照抄过来的状态，便于跟真机比对
    from ipod_cli.library import read_library

    library = read_library(reh)
    print()
    print(f"彩排 iPod：{reh}")
    print(f"  iPod 名字  {library.ipod_name}")
    print(f"  曲目       {len(library.tracks)} 首")
    print(f"  播放列表   {len(library.playlists)} 个")
    for playlist in library.playlists[:6]:
        title = playlist.get("Title") or playlist.get("Playlist Title") or "?"
        count = playlist.get("Track Count") or playlist.get("Item Count") or 0
        print(f"      {title}（{count} 首）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
