"""真机彩排：用真机数据在目录副本上跑完整写入链路。

为什么需要这个：
  内核的写入前检查会拒绝"不是独立挂载卷"的路径（防止往没挂载成功的空目录写）。
  但根目录有 iPodInfo.json 标记的目录被认作"虚拟 iPod"，可以绕过该检查——
  这正是 iOpenPod 自己跑测试的方式。

关键保真点：
  必须用**真机的 SysInfo 和 FireWire GUID**，否则 HASH58 会用一个错误的 GUID
  计算签名，彩排就变成自欺欺人。

用法：
    uv run python tools/dress_rehearsal.py <真机盘符> <彩排目录> [待导入文件...]
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def setup(real_ipod: Path, rehearsal: Path) -> None:
    """搭彩排环境：真机数据 + 虚拟 iPod 标记。"""
    from iopenpod.device import create_virtual_ipod

    if rehearsal.exists():
        shutil.rmtree(rehearsal)
    rehearsal.mkdir(parents=True)

    # 1) 先用内核造一个合法的虚拟 iPod（生成 iPodInfo.json 标记）
    create_virtual_ipod(rehearsal, "MB029", ipod_name="彩排")

    # 2) 用真机的 Device/ 覆盖（SysInfo 里有真 GUID）
    real_device = real_ipod / "iPod_Control" / "Device"
    target_device = rehearsal / "iPod_Control" / "Device"
    if target_device.exists():
        shutil.rmtree(target_device)
    shutil.copytree(real_device, target_device)

    # 3) 用真机的 iTunesDB 覆盖
    real_db = real_ipod / "iPod_Control" / "iTunes" / "iTunesDB"
    target_db = rehearsal / "iPod_Control" / "iTunes" / "iTunesDB"
    shutil.copy2(real_db, target_db)

    # 4) 用真机的 ArtworkDB + ithmb 覆盖
    real_art = real_ipod / "iPod_Control" / "Artwork"
    target_art = rehearsal / "iPod_Control" / "Artwork"
    if target_art.exists():
        shutil.rmtree(target_art)
    shutil.copytree(real_art, target_art)

    # 5) 让虚拟元数据里的关键身份字段和真机一致
    sysinfo = {}
    sysinfo_path = target_device / "SysInfo"
    for raw in sysinfo_path.read_text(errors="ignore").splitlines():
        line = raw.strip().replace("\x00", "")
        if ":" in line:
            key, _, value = line.partition(":")
            sysinfo[key.strip()] = value.strip()

    info_path = rehearsal / "iPodInfo.json"
    payload = json.loads(info_path.read_text(encoding="utf-8"))
    guid = sysinfo.get("FirewireGuid", "")
    if guid.lower().startswith("0x"):
        guid = guid[2:]
    payload["firewire_guid"] = guid
    payload["model_number"] = "MB029"
    payload["model_family"] = "iPod Classic"
    payload["generation"] = "6th Gen"
    payload["capacity"] = "80GB"
    payload["color"] = "Silver"
    payload["serial"] = sysinfo.get("pszSerialNumber", payload.get("serial", ""))
    info_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"彩排环境已就绪: {rehearsal}")
    print(f"  真机 GUID : {guid}")
    print(f"  SysInfo   : {sysinfo_path}")
    print(f"  iTunesDB  : {target_db.stat().st_size:,} 字节")
    print(f"  ArtworkDB : {(target_art / 'ArtworkDB').stat().st_size:,} 字节")
    print(f"  ithmb     : {len(list(target_art.glob('*.ithmb')))} 个")


def run_import(rehearsal: Path, sources: list[Path]) -> int:
    from iopenpod.artworkdb_writer.artworkdb_chunks import read_existing_artwork
    from ipod_cli.discovery import probe_mount
    from ipod_cli.importer import build_import_plan, execute_import
    from ipod_cli.library import read_library
    from ipod_cli.mediafile import collect_audio_files
    from ipod_cli.transcode import transcode_for_import

    device = probe_mount(rehearsal)
    if device is None:
        print("❌ 彩排目录没被识别成 iPod")
        return 1
    print(f"\n设备识别: {device.display_name} | {device.checksum}")

    before = read_library(rehearsal)
    before_art = read_existing_artwork(
        str(rehearsal / "iPod_Control" / "Artwork" / "ArtworkDB"),
        str(rehearsal / "iPod_Control" / "Artwork"),
    )
    print(f"写入前: {len(before.tracks)} 首曲目, {len(before_art)} 条封面")

    device.activate()

    files: list[Path] = []
    for source in sources:
        files.extend(collect_audio_files(source))

    plan = build_import_plan(device, before, files)
    print(f"导入计划: 新增 {len(plan.to_add)} 首")

    result = execute_import(plan, transcode=transcode_for_import, progress=None)
    print(f"\n写入结果: 新增 {result.added} | 数据库 {result.database_written} | 校验 {result.verified}")
    print(f"  {result.verification_note}")
    for src, reason in result.failed:
        print(f"  失败: {src} — {reason}")

    # ── 写入后核对 ──
    after = read_library(rehearsal)
    after_art = read_existing_artwork(
        str(rehearsal / "iPod_Control" / "Artwork" / "ArtworkDB"),
        str(rehearsal / "iPod_Control" / "Artwork"),
    )
    print(f"\n写入后: {len(after.tracks)} 首曲目, {len(after_art)} 条封面")

    import struct

    from iopenpod.itunesdb_shared.mhbd_defs import MHBD_OFFSET_HASHING_SCHEME

    raw = (rehearsal / "iPod_Control" / "iTunes" / "iTunesDB").read_bytes()
    scheme = struct.unpack_from("<I", raw, MHBD_OFFSET_HASHING_SCHEME)[0]

    print(f"\n签名方案: {scheme} ({'HASH58 ✓' if scheme == 1 else '异常'})")

    # 已有曲目是否还在（按标题集合比对）
    before_titles = {t.title for t in before.tracks}
    after_titles = {t.title for t in after.tracks}
    lost = before_titles - after_titles
    print(f"原有曲目保留: {len(before_titles - lost)}/{len(before_titles)}"
          f"{'  ✓' if not lost else '  ❌ 丢失: ' + str(sorted(lost)[:5])}")

    ok = (
        result.verified
        and not lost
        and scheme == 1
        and len(after.tracks) == len(before.tracks) + result.added
    )
    print(f"\n{'=' * 50}\n彩排{'通过 ✅' if ok else '失败 ❌'}\n{'=' * 50}")
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    real_ipod = Path(sys.argv[1])
    rehearsal = Path(sys.argv[2])
    sources = [Path(p) for p in sys.argv[3:]] or [
        Path(tempfile.gettempdir()) / "ipod-realtest"
    ]

    setup(real_ipod, rehearsal)
    return run_import(rehearsal, sources)


if __name__ == "__main__":
    sys.exit(main())
