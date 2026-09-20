"""用真机备份的数据搭一个彩排 iPod。

比 create_virtual_ipod 造的空白设备真实得多：真机的 118 首曲目、中文标签、
117 条封面条目、2+5+1 个播放列表都能原样拿来测新命令。

用法: uv run python tools/rehearsal_from_backup.py <备份目录> <彩排目录>
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def read_sysinfo(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip().replace("\x00", "")
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    backup = Path(sys.argv[1])
    target = Path(sys.argv[2])

    # 备份目录可能有两种布局：
    #   <备份>/iPod_Control/{Device,iTunes,Artwork}     ← ipod backup 的产物
    #   <备份>/{Device,iTunes,Artwork}                  ← 手工 cp 的产物
    def looks_like_control(path: Path) -> bool:
        return any((path / name).is_dir() for name in ("Device", "iTunes"))

    control = backup / "iPod_Control"
    if not looks_like_control(control):
        if looks_like_control(backup):
            control = backup
        else:
            candidates = [p for p in backup.glob("*/iPod_Control") if p.is_dir()]
            candidates += [p for p in backup.glob("*") if p.is_dir() and looks_like_control(p)]
            if not candidates:
                print(f"❌ 在 {backup} 里找不到 iPod_Control（或其内容）")
                return 1
            control = candidates[0]

    print(f"备份来源: {control}")

    from iopenpod.device import create_virtual_ipod

    if target.exists():
        shutil.rmtree(target)
    create_virtual_ipod(target, "MB029", ipod_name="彩排")

    for name in ("Device", "iTunes", "Artwork"):
        src = control / name
        if not src.is_dir():
            print(f"  跳过 {name}/（备份里没有）")
            continue
        dst = target / "iPod_Control" / name
        if dst.is_dir():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        print(f"  ✓ 复制 {name}/")

    # 让虚拟元数据的关键身份字段与真机一致（否则签名会用错 GUID）
    sysinfo = read_sysinfo(target / "iPod_Control" / "Device" / "SysInfo")
    guid = sysinfo.get("FirewireGuid", "")
    if guid.lower().startswith("0x"):
        guid = guid[2:]

    marker = target / "iPodInfo.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload.update(
        firewire_guid=guid,
        model_number="MB029",
        model_family="iPod Classic",
        generation="6th Gen",
        capacity="80GB",
        color="Silver",
        serial=sysinfo.get("pszSerialNumber", payload.get("serial", "")),
    )
    marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n彩排环境已就绪：{target}")
    print(f"  真机 GUID: {guid}")
    print(f"  SysInfo  : {sysinfo.get('ModelNumStr', '?')}")

    from ipod_cli.discovery import probe_mount
    from ipod_cli.library import read_library

    device = probe_mount(target)
    if device is None:
        print("❌ 彩排目录没被识别成 iPod")
        return 1
    library = read_library(target)
    print(f"  曲目     : {len(library.tracks)} 首")
    print(f"  iPod 名字: {library.ipod_name!r}")
    print(f"  播放列表 : {len(library.playlists)} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
