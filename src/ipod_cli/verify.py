"""设备健康检查：写入之后真正能发现问题的那个东西。

为什么需要它：写库函数返回 True **不代表固件能读**。这次真机测试踩到的两个
静默数据丢失缺陷（播放列表被抹、`--playlist` 失效）都不是靠命令返回码发现的，
而是靠"写入后逐项核对"。这套核对逻辑本该是命令，而不是每次临时写脚本。

检查项：

  1. 设备识别     型号/序列号/签名方案
  2. 数据库可读   曲目数、总时长
  3. 签名         方案与设备匹配，签名字段非零
  4. 曲目字段     缺标题/缺路径/路径格式异常
  5. 文件对应     库里有但磁盘没有（缺失）、磁盘有但库里没有（孤儿）
  6. 封面         条目数、逐曲可用性、悬空引用
  7. 播放列表     数量、主列表名（= iPod 名字）、幽灵条目
  8. 数据库备份   iTunesDB.backup 是否存在
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from iopenpod.artworkdb_writer.artworkdb_chunks import read_existing_artwork
from iopenpod.itunesdb_shared.mhbd_defs import (
    MHBD_OFFSET_HASH58,
    MHBD_OFFSET_HASH72,
    MHBD_OFFSET_HASHAB,
    MHBD_OFFSET_HASHING_SCHEME,
)

from .discovery import IpodDevice
from .library import LibraryData, read_library

OK = "ok"
WARN = "warn"
FAIL = "fail"

ProgressCallback = Callable[[str], None]

# 签名方案编号 → 名称（内核 hashing_scheme 字段）
_SCHEME_NAMES = {0: "无", 1: "HASH58", 2: "HASH72", 3: "HASHAB"}
_SIGNATURE_FIELDS = {
    1: ("hash58", MHBD_OFFSET_HASH58, 20),
    2: ("hash72", MHBD_OFFSET_HASH72, 46),
    3: ("hashab", MHBD_OFFSET_HASHAB, 57),
}


@dataclass
class Check:
    """单项检查结果。"""

    name: str
    status: str
    summary: str
    details: list[str] = field(default_factory=list)


@dataclass
class HealthReport:
    device: IpodDevice
    library: LibraryData | None
    checks: list[Check]

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def ok(self) -> bool:
        return not self.failures


def _db_path(device: IpodDevice) -> Path:
    return Path(device.root) / "iPod_Control" / "iTunes" / "iTunesDB"


#: iTunesDB 的文件头魔数。整个数据库以这 4 字节 ASCII 开头。
DB_MAGIC = b"mhbd"


def _check_header(device: IpodDevice) -> Check:
    """校验数据库文件头。

    这条单看起来多余，但它挡住了一个真实盲区：解析器对垃圾文件是**宽容**的
    ——遇到不认识的 chunk 会跳过，于是一个全是垃圾的 iTunesDB 也能"成功解析"
    成 0 首曲目，报告一片绿。只有直接看文件头才能识破。
    """
    path = _db_path(device)
    if not path.is_file():
        return Check("数据库文件", FAIL, "找不到 iTunesDB")

    size = path.stat().st_size
    if size < 8:
        return Check("数据库文件", FAIL, f"文件只有 {size} 字节，已被截断", [])

    with path.open("rb") as handle:
        head = handle.read(8)

    if head[:4] != DB_MAGIC:
        return Check(
            "数据库文件", FAIL,
            f"文件头不是 mhbd（实际 {head[:4]!r}）——这不是有效的 iTunesDB",
            [f"文件大小    {size:,} 字节"],
        )
    return Check("数据库文件", OK, f"mhbd 文件头正常（{size:,} 字节）", [])


# ──────────────────────────────────────────────────────────────────────
# 各项检查
# ──────────────────────────────────────────────────────────────────────

def _check_device(device: IpodDevice) -> Check:
    details = [
        f"型号        {device.model_number or '未知'}",
        f"序列号      {device.serial or '未知'}",
        f"FireWire    {device.firewire_guid or '未知'}",
        f"签名方案    {device.checksum}",
        f"容量        {device.used_text} 已用 / {device.total_text} 共"
        f"（剩余 {device.free_text}）",
    ]
    status = OK if device.model_number else WARN
    return Check("设备识别", status, device.display_name, details)


def _check_library(device: IpodDevice, library: LibraryData | None) -> Check:
    if library is None:
        return Check("数据库可读", FAIL, "无法读取 iTunesDB")
    tracks = library.tracks
    details = [
        f"曲目        {len(tracks)} 首",
        f"专辑        {len({t.album for t in tracks if t.album})} 个",
        f"艺人        {len({t.artist for t in tracks if t.artist})} 个",
        f"总时长      {_duration_text(sum(t.length for t in tracks))}",
    ]
    return Check("数据库可读", OK, f"{len(tracks)} 首曲目", details)


def _duration_text(total_ms: int) -> str:
    """毫秒 → 中文时长。不足一分钟就显示秒，免得全是"0 小时 0 分"。"""
    total_seconds = total_ms // 1000
    if total_seconds < 60:
        return f"{total_seconds} 秒"
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分"


def _check_signature(device: IpodDevice) -> Check:
    path = _db_path(device)
    if not path.is_file():
        return Check("签名", FAIL, "找不到 iTunesDB")
    raw = path.read_bytes()
    if len(raw) < MHBD_OFFSET_HASHING_SCHEME + 2:
        return Check("签名", FAIL, "数据库文件过短，可能已损坏")

    scheme = struct.unpack_from("<H", raw, MHBD_OFFSET_HASHING_SCHEME)[0]
    name = _SCHEME_NAMES.get(scheme, f"未知({scheme})")
    details = [f"hashing_scheme  {scheme} ({name})"]

    field = _SIGNATURE_FIELDS.get(scheme)
    if field is None:
        if scheme == 0:
            details.append("签名字段      该机型不需要签名")
            return Check("签名", OK, name, details)
        return Check(
            "签名", FAIL, f"未知的签名方案 {scheme}（固件可能拒绝这个库）", details
        )

    label, offset, size = field
    if offset + size > len(raw):
        return Check("签名", FAIL, "数据库被截断，签名字段读取越界", details)
    value = raw[offset : offset + size]
    details.append(f"{label}  {value.hex()[:40]}")
    if value == bytes(size):
        return Check("签名", FAIL, f"{name} 字段全零 —— 数据库未签名", details)
    return Check("签名", OK, f"{name} 已签名", details)


def _check_track_fields(library: LibraryData | None) -> Check:
    if library is None:
        return Check("曲目字段", FAIL, "无法读取曲目")
    no_title, no_location, bad_location = [], [], []
    for t in library.tracks:
        if not t.title.strip():
            no_title.append(t)
        if not t.location.strip():
            no_location.append(t)
        elif not t.location.startswith(":"):
            bad_location.append(t)

    details = []
    for label, items in (
        ("缺标题", no_title),
        ("缺路径", no_location),
        ("路径格式异常", bad_location),
    ):
        if items:
            sample = "、".join((t.title or "（无标题）")[:18] for t in items[:3])
            details.append(f"{label}  {len(items)} 首  例：{sample}")

    if no_location:
        return Check("曲目字段", FAIL, f"{len(no_location)} 首缺路径（播不出来）", details)
    if bad_location:
        return Check("曲目字段", WARN, f"{len(bad_location)} 首路径格式异常", details)
    if no_title:
        return Check("曲目字段", WARN, f"{len(no_title)} 首缺标题", details)
    return Check("曲目字段", OK, "全部完整", details)


def _check_files(
    device: IpodDevice,
    library: LibraryData | None,
    progress: ProgressCallback | None,
) -> Check:
    """库与磁盘的文件对应关系。大库会慢（每首一次 stat）。"""
    if library is None:
        return Check("文件对应", FAIL, "无法读取曲目")

    root = Path(device.root)
    referenced = {
        t.location.strip(":").replace(":", "/") for t in library.tracks if t.location
    }

    if progress is not None:
        progress("正在核对磁盘文件…")

    music_root = root / "iPod_Control" / "Music"
    on_disk: set[str] = set()
    if music_root.is_dir():
        for path in music_root.rglob("*"):
            if path.is_file() and not path.name.startswith("."):
                on_disk.add(path.relative_to(root).as_posix())

    missing = sorted(referenced - on_disk)
    orphans = sorted(on_disk - referenced)

    details = [
        f"库内引用    {len(referenced)} 个",
        f"磁盘文件    {len(on_disk)} 个",
    ]
    for path in missing[:5]:
        details.append(f"缺失  {path}")
    for path in orphans[:5]:
        details.append(f"孤儿  {path}")
    if len(missing) > 5:
        details.append(f"…还有 {len(missing) - 5} 个缺失")
    if len(orphans) > 5:
        details.append(f"…还有 {len(orphans) - 5} 个孤儿")

    if missing:
        return Check(
            "文件对应", FAIL,
            f"{len(missing)} 首库里有记录但磁盘没文件（会显示但播不出来）",
            details,
        )
    if orphans:
        return Check(
            "文件对应", WARN,
            f"{len(orphans)} 个孤儿文件（占空间，可用 verify --fix 清理）",
            details,
        )
    return Check("文件对应", OK, "库与磁盘完全一致", details)


def _check_artwork(
    device: IpodDevice, library: LibraryData | None
) -> Check:
    if library is None:
        return Check("封面", FAIL, "无法读取曲目")

    art_dir = Path(device.root) / "iPod_Control" / "Artwork"
    artdb = art_dir / "ArtworkDB"
    if not artdb.is_file():
        if any(t.raw.get("artwork_id_ref") for t in library.tracks):
            return Check("封面", FAIL, "曲目有封面引用，但 ArtworkDB 不存在")
        return Check("封面", OK, "没有封面数据（设备不需要）", [])

    try:
        entries = read_existing_artwork(str(artdb), str(art_dir))
    except Exception as exc:
        return Check("封面", FAIL, f"ArtworkDB 无法解析：{exc}")

    ids = set(entries.keys())
    by_song = {
        e.get("song_id"): i for i, e in entries.items() if e.get("song_id")
    }

    linked = dangling = 0
    for t in library.tracks:
        ref = t.raw.get("artwork_id_ref")
        sid = t.db_track_id
        if sid in by_song or (ref and ref in ids):
            linked += 1
        elif ref:
            dangling += 1

    # 没有任何曲目引用的封面条目（删除曲目后的残留）
    used = set(by_song.values()) | {
        t.raw.get("artwork_id_ref") for t in library.tracks if t.raw.get("artwork_id_ref")
    }
    orphan_entries = len(ids - used)

    ithmb = list(art_dir.glob("*.ithmb"))
    missing_ithmb = [
        name
        for name in ("F1055_1.ithmb", "F1060_1.ithmb", "F1061_1.ithmb", "F1068_1.ithmb")
        if not (art_dir / name).is_file()
    ]

    details = [
        f"封面条目    {len(entries)} 条",
        f"ithmb 文件  {len(ithmb)} 个",
        f"有封面曲目  {linked}/{len(library.tracks)}",
        f"悬空引用    {dangling}",
        f"无引用条目  {orphan_entries}",
    ]
    if missing_ithmb:
        details.append(f"缺少 ithmb：{'、'.join(missing_ithmb)}")

    if dangling:
        return Check(
            "封面", FAIL,
            f"{dangling} 首曲目的封面引用指向不存在的条目（封面不显示）",
            details,
        )
    if missing_ithmb:
        return Check("封面", FAIL, "ithmb 文件缺失，封面无法渲染", details)
    if orphan_entries > len(entries) * 0.3:
        return Check(
            "封面", WARN,
            f"{orphan_entries} 条封面无人引用（删除曲目后的残留，可用 --compact-artwork 整理）",
            details,
        )
    return Check("封面", OK, f"{linked}/{len(library.tracks)} 首有可用封面", details)


def _check_playlists(device: IpodDevice, library: LibraryData | None) -> Check:
    if library is None:
        return Check("播放列表", FAIL, "无法读取曲目")

    raw = library.raw or {}
    playlists = raw.get("mhlp") or []
    smart = raw.get("mhlp_smart") or []
    podcast = raw.get("mhlp_podcast") or []

    total = len(library.tracks)
    # 主播放列表 = 常规列表里包含全部曲目的那个；它的标题就是 iPod 的名字
    master = None
    for p in playlists:
        if len(p.get("items") or []) == total and total:
            master = p
            break
    if master is None and playlists:
        master = playlists[0]

    details = [f"普通        {len(playlists)} 个"]
    for p in playlists:
        details.append(f"    {p.get('Title')!r}  {len(p.get('items') or [])} 首")
    details.append(f"智能        {len(smart)} 个")
    for p in smart:
        details.append(f"    {p.get('Title')!r}")
    details.append(f"播客        {len(podcast)} 个")
    if master is not None:
        details.append(f"主列表名    {master.get('Title')!r}  ← 这就是 iPod 的名字")

    # 幽灵条目：指向已不存在曲目的播放列表项
    valid_ids = {t.db_track_id for t in library.tracks}
    valid_tids = {t.track_id for t in library.tracks}
    valid_persistent = {
        t.raw.get("track_persistent_id") for t in library.tracks
        if t.raw.get("track_persistent_id")
    }
    ghosts = 0
    for group in (playlists, smart, podcast):
        for p in group:
            for item in p.get("items") or []:
                if not isinstance(item, dict):
                    continue
                tid = item.get("track_id")
                pid = item.get("track_persistent_id")
                if pid and valid_persistent and pid in valid_persistent:
                    continue
                if tid and tid in valid_tids:
                    continue
                if not tid and not pid:
                    continue
                if tid and valid_ids and tid in valid_ids:
                    continue
                ghosts += 1

    if ghosts:
        details.append(f"幽灵条目    {ghosts} 个")
        return Check(
            "播放列表", WARN,
            f"{ghosts} 个播放列表条目指向已不存在的曲目",
            details,
        )
    if not playlists:
        return Check("播放列表", WARN, "没有任何播放列表", details)
    return Check(
        "播放列表", OK,
        f"普通 {len(playlists)} + 智能 {len(smart)} + 播客 {len(podcast)}，无幽灵条目",
        details,
    )


def _check_backup(device: IpodDevice) -> Check:
    itunes = Path(device.root) / "iPod_Control" / "iTunes"
    db = itunes / "iTunesDB"
    backup = itunes / "iTunesDB.backup"

    if not backup.is_file():
        return Check(
            "数据库备份", WARN,
            "设备上没有 iTunesDB.backup（下次写入时会自动生成）",
            [],
        )

    size = backup.stat().st_size
    details = [f"备份大小    {size:,} 字节"]
    if db.is_file():
        details.append(f"当前大小    {db.stat().st_size:,} 字节")
    return Check("数据库备份", OK, f"{size:,} 字节", details)


# ──────────────────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────────────────

def check_device(
    device: IpodDevice,
    *,
    progress: ProgressCallback | None = None,
    skip_files: bool = False,
) -> HealthReport:
    """跑完整健康检查。"""
    library: LibraryData | None
    try:
        library = read_library(device.root)
    except Exception:
        library = None

    checks = [
        _check_device(device),
        _check_header(device),
        _check_library(device, library),
        _check_signature(device),
        _check_track_fields(library),
    ]
    if not skip_files:
        checks.append(_check_files(device, library, progress))
    checks.append(_check_artwork(device, library))
    checks.append(_check_playlists(device, library))
    checks.append(_check_backup(device))
    return HealthReport(device=device, library=library, checks=checks)


def report_as_dict(report: HealthReport) -> dict:
    """给 ``--json`` 用的结构化输出。"""
    return {
        "device": {
            "name": report.device.display_name,
            "model": report.device.model_number,
            "serial": report.device.serial,
            "checksum": report.device.checksum,
            "root": str(report.device.root),
        },
        "track_count": len(report.library.tracks) if report.library else 0,
        "ok": report.ok,
        "checks": [
            {
                "name": c.name,
                "status": c.status,
                "summary": c.summary,
                "details": c.details,
            }
            for c in report.checks
        ],
    }
