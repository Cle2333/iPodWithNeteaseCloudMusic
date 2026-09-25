"""真机受控写入测试：验证修复功能在**真实设备**上的删除路径与报数。

## 为什么这样测是安全的

`clean_orphans` / `clean_stray_temp` **不重写数据库**（只有 clean_broken_records 才
重写），所以这条路径在真机上是可逆的：我先放几个**自己造的**文件，再让清理删掉，
净效果为零。数据库一个字都不动。

## 验证的正是这次改的东西

* 真孤儿（音频、库不认）→ 认出并删掉
* `desktop.ini`（非音频）→ **不许碰**（新增的音频扩展名白名单）
* `*.tmp` / `*.partial` → 只算临时文件，**不重复计入孤儿**（新增的共用判据）
* 报出的释放量 = 实际删掉的字节，不是数据库里记的大小
* 库里引用的 148 个文件**一个都不能少**

用法：uv run python <本文件> [盘符，默认 D:/]
"""
import sys
from pathlib import Path

REPO = Path(r"C:/Users/ROG/source/repos/iPodWithNeteaseCloudMusic")
sys.path.insert(0, str(REPO / "src"))

from ipod_cli.discovery import require_ipod  # noqa: E402
from ipod_cli.library import read_library  # noqa: E402
from ipod_cli.repair import (  # noqa: E402
    clean_orphans,
    clean_stray_temp,
    scan_device,
)

DEVICE = sys.argv[1] if len(sys.argv) > 1 else "D:/"
MARK = "ZZTEST"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"   {'✅' if ok else '❌'} {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def norm(rel: str) -> str:
    return rel.strip(":").replace(":", "/").replace("\\", "/").strip("/").casefold()


device = require_ipod(DEVICE)
root = Path(device.root)
print(f"真机：{device.display_name}  {root}")

# ── 基线 ─────────────────────────────────────────────────────────────────
lib0 = read_library(root)
db_refs = {norm(str(t.location)) for t in lib0.tracks if t.location}
music = root / "iPod_Control" / "Music"
subdirs = sorted(p.name for p in music.iterdir() if p.is_dir())
print(f"基线：数据库 {len(lib0.tracks)} 首，Music 子目录 {len(subdirs)} 个")

# 挑一个已存在的子目录放测试文件（没有就用 F00，用完撤掉）
target_sub = subdirs[0] if subdirs else "F00"
target = music / target_sub
target.mkdir(parents=True, exist_ok=True)
itunes = root / "iPod_Control" / "iTunes"
print(f"测试文件放在：{target}")

# ── 造测试素材（全部带 ZZTEST 前缀，便于自查）──────────────────────────
ORPHAN = target / f"{MARK}_orphan.m4a"          # 真孤儿：音频、库不认
NONAUDIO = target / "desktop.ini"                # 非音频：不许删
TEMPMUSIC = target / f"{MARK}.tmp"               # 临时（在 Music 下，易被重复计数）
TEMPTITUNES = itunes / f"{MARK}.partial"         # 临时（在 iTunes 下）

created: list[Path] = []


def snapshot() -> set[str]:
    """清点设备上所有音频文件的归一化相对路径。"""
    out = set()
    for p in music.rglob("*"):
        if p.is_file() and not p.name.startswith("."):
            out.add(norm(p.relative_to(root).as_posix()))
    return out


try:
    ORPHAN.write_bytes(b"\0" * 4096)
    created.append(ORPHAN)
    NONAUDIO.write_bytes(b"[.ShellClassInfo]\r\nIconResource=x\r\n")
    created.append(NONAUDIO)
    TEMPMUSIC.write_bytes(b"x" * 1024)
    created.append(TEMPMUSIC)
    TEMPTITUNES.write_bytes(b"x" * 2048)
    created.append(TEMPTITUNES)

    before = snapshot()
    before_all = sorted(
        p.name for p in target.iterdir() if p.is_file()
    )

    # ── 扫描 ─────────────────────────────────────────────────────────────
    print()
    print("── 扫描 ──")
    scan = scan_device(device)
    orphan_names = [o.name for o in scan.orphans]
    temp_names = sorted(Path(r).name for r in scan.stray_temp)
    print(f"   孤儿 {orphan_names}")
    print(f"   临时 {temp_names}")
    print(f"   磁盘文件 {scan.disk_files}（数据库 {scan.db_tracks}）")

    check("真孤儿被认出", orphan_names == [f"{MARK}_orphan.m4a"], str(orphan_names))
    check(
        "desktop.ini 没被判成孤儿",
        "desktop.ini" not in orphan_names,
        "非音频文件不该被当同步残留",
    )
    check(
        "临时文件没被重复计入孤儿",
        not any(n.endswith((".tmp", ".partial")) for n in orphan_names),
        str(orphan_names),
    )
    check(
        "两个临时文件都被认出",
        temp_names == sorted([f"{MARK}.tmp", f"{MARK}.partial"]),
        str(temp_names),
    )
    check("磁盘文件数 = 库内 + 1 个真孤儿", scan.disk_files == scan.db_tracks + 1,
          f"{scan.disk_files} vs {scan.db_tracks}+1")
    check("孤儿字节只算真孤儿", scan.orphan_bytes == 4096, str(scan.orphan_bytes))

    # ── 清理（不动数据库）──────────────────────────────────────────────
    print()
    print("── 清理孤儿 + 临时文件 ──")
    r_orphan = clean_orphans(device, scan.orphans)
    r_temp = clean_stray_temp(device, scan.stray_temp)
    print(f"   孤儿：removed={r_orphan.removed} 释放={r_orphan.bytes_freed}")
    print(f"   临时：removed={r_temp.removed} 释放={r_temp.bytes_freed}")

    check("孤儿删了 1 个", r_orphan.removed == 1)
    check("孤儿释放量 = 4096（实际字节，不是库里记的大小）",
          r_orphan.bytes_freed == 4096, str(r_orphan.bytes_freed))
    check("临时删了 2 个", r_temp.removed == 2)
    check("临时报错为空", r_orphan.errors == [] and r_temp.errors == [],
          f"{r_orphan.errors} {r_temp.errors}")

    # ── 清理后核对 ───────────────────────────────────────────────────────
    print()
    print("── 清理后核对 ──")
    check("真孤儿文件已消失", not ORPHAN.exists())
    check("desktop.ini 还在（没被顺手删掉）", NONAUDIO.exists())
    check("两个临时文件已消失", not TEMPMUSIC.exists() and not TEMPTITUNES.exists())

    after = snapshot()
    missing = db_refs & (before - after)
    check("★ 库里引用的 148 个文件一个都没少", not missing,
          f"被误删：{sorted(missing)[:5]}")
    print(f"   删掉的文件数：{len(before) - len(after)}")

    lib1 = read_library(root)
    check("数据库曲目数没变（清理不碰库）", len(lib1.tracks) == len(lib0.tracks),
          f"{len(lib0.tracks)} → {len(lib1.tracks)}")

    rescan = scan_device(device)
    check("再扫一次：设备回到干净状态", rescan.is_clean, rescan.summary_text())

finally:
    # 兜底：任何残留的 ZZTEST 文件都清掉（净效果必须为零）
    leftovers = [p for p in created if p.exists()]
    if leftovers:
        print()
        print("── 清理测试残留 ──")
        for p in leftovers:
            p.unlink()
            print(f"   已删 {p.name}")
    # 顺手收掉可能出现的空目录（只在自己造过文件的目录上）
    if not any(target.iterdir()):
        try:
            target.rmdir()
            print(f"   已删空目录 {target.name}")
        except OSError:
            pass

print()
if failures:
    print(f"❌ {len(failures)} 项未通过：{failures}")
    raise SystemExit(1)
print("✅ 真机受控写入测试全部通过 —— 设备已回到测试前状态")
