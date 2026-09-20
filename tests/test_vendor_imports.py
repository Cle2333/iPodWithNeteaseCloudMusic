"""Smoke test: does the trimmed vendored tree import and work end-to-end?

This is the gate that proves the trim did not break the engine.
"""

from __future__ import annotations

import sys

MODULES = [
    "iopenpod",
    "iopenpod.device",
    "iopenpod.itunesdb_shared",
    "iopenpod.itunesdb_parser",
    "iopenpod.itunesdb_writer",
    "iopenpod.artworkdb_shared",
    "iopenpod.artworkdb_parser",
    "iopenpod.artworkdb_writer",
]

SYMBOLS = [
    ("iopenpod.device", ["ChecksumType", "detect_checksum_type", "get_firewire_id",
                         "resolve_itdb_path", "read_sysinfo", "capabilities_for_family_gen",
                         "create_virtual_ipod", "ITHMB_FORMAT_MAP", "DeviceWriteGuard",
                         "flush_filesystem", "DeviceInfo"]),
    ("iopenpod.itunesdb_parser", ["parse_itunesdb", "parse_playcounts"]),
    ("iopenpod.itunesdb_writer", ["write_checksum", "detect_checksum_type", "TrackInfo",
                                 "PlaylistInfo", "extract_db_info", "write_itunesdb"]),
    ("iopenpod.artworkdb_writer", ["write_artworkdb"]),
]

failures: list[str] = []

print("=" * 68)
print("[1] 导入所有 vendored 模块")
print("=" * 68)
for mod in MODULES:
    try:
        __import__(mod)
        print(f"  ✓ {mod}")
    except Exception as exc:
        failures.append(f"{mod}: {type(exc).__name__}: {exc}")
        print(f"  ✗ {mod}  -> {type(exc).__name__}: {exc}")

print()
print("=" * 68)
print("[2] 检查关键符号可访问")
print("=" * 68)
for mod, names in SYMBOLS:
    try:
        m = __import__(mod, fromlist=["*"])
    except Exception as exc:
        failures.append(f"import {mod}: {exc}")
        print(f"  ✗ {mod} 导入失败: {exc}")
        continue
    for name in names:
        if hasattr(m, name):
            print(f"  ✓ {mod}.{name}")
        else:
            failures.append(f"{mod}.{name} 不存在")
            print(f"  ✗ {mod}.{name} 不存在")

print()
if failures:
    print(f"❌ {len(failures)} 项失败：")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全部通过")
