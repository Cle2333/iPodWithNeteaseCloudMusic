"""搭一个"还原后的彩排环境"：备份的原始数据库 + 真机当前的封面。

用途：验证修好播放列表后的导入链路——先还原原始库（含播放列表），
再跑一次导入，看播放列表和封面是否都还在。
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
        print("用法: build_rehearsal.py <真机盘符> <彩排目录>")
        return 2
    real = Path(sys.argv[1])
    reh = Path(sys.argv[2])

    from iopenpod.device import create_virtual_ipod

    backup = next(
        Path.home().joinpath("Documents").glob("iPod备份-*/iTunesDB")
    )

    if reh.exists():
        shutil.rmtree(reh)
    create_virtual_ipod(reh, "MB029", ipod_name="彩排")

    # 真机 Device/（SysInfo 里的 FireWire GUID 必须是真的）
    shutil.rmtree(reh / "iPod_Control" / "Device")
    shutil.copytree(real / "iPod_Control" / "Device", reh / "iPod_Control" / "Device")

    # 备份的原始数据库（含播放列表 + 原始 iPod 名字）
    shutil.copy2(backup, reh / "iPod_Control" / "iTunes" / "iTunesDB")

    # 真机当前的 Artwork（条目带 song_id，封面可按 db_track_id 解析回来）
    shutil.rmtree(reh / "iPod_Control" / "Artwork")
    shutil.copytree(real / "iPod_Control" / "Artwork", reh / "iPod_Control" / "Artwork")

    sysinfo = read_sysinfo(reh / "iPod_Control" / "Device" / "SysInfo")
    guid = sysinfo.get("FirewireGuid", "")
    if guid.lower().startswith("0x"):
        guid = guid[2:]

    marker = reh / "iPodInfo.json"
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

    print(f"彩排环境已就绪: {reh}")
    print(f"  原始 iTunesDB（含播放列表）: {backup}")
    print(f"  真机 Artwork 副本 + 真机 GUID: {guid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
