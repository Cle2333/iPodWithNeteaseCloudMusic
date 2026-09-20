"""整机备份与还原。

**为什么必须备份整个 `iPod_Control` 而不只是 `iTunesDB`**：

真机测试时我只备份了数据库，结果发现写入会**同时重写** `Artwork/ArtworkDB`
和 `.ithmb` 文件。只还原数据库的话，曲目里的 `artwork_id_ref` 会全部指向
已不存在的封面条目——实测 **100% 悬空**，封面全丢。

所以备份范围必须是 `Device/` + `iTunes/` + `Artwork/` 三块
（不含 `Music/` 时约 60MB，很快）。要连音乐一起备份就加 ``include_music``——
那会变成几十 GB，而且不做逐文件哈希（太慢），只记录文件数。

备份目录里放一份 ``manifest.json``：记录每块的 SHA256、设备信息、曲目数。
还原前可以先用它校验备份有没有损坏。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .discovery import IpodDevice, human_size
from .library import read_library

MANIFEST_NAME = "manifest.json"
# 元数据三块：缺任何一块都无法完整还原
METADATA_DIRS = ("Device", "iTunes", "Artwork")
MUSIC_DIR = "Music"
_CHUNK = 1024 * 1024

ProgressCallback = Callable[[str], None]


class BackupError(RuntimeError):
    """备份或还原无法安全进行时抛出。"""


@dataclass
class FileRecord:
    sha256: str
    size: int


@dataclass
class BackupManifest:
    """备份目录的清单。"""

    created: str
    ipod_root: str
    device: dict = field(default_factory=dict)
    track_count: int = 0
    includes_music: bool = False
    metadata_files: dict[str, FileRecord] = field(default_factory=dict)
    music_file_count: int = 0
    music_total_bytes: int = 0
    total_bytes: int = 0

    def to_json(self) -> dict:
        return {
            "created": self.created,
            "ipod_root": self.ipod_root,
            "device": self.device,
            "track_count": self.track_count,
            "includes_music": self.includes_music,
            "metadata_files": {
                rel: {"sha256": rec.sha256, "size": rec.size}
                for rel, rec in sorted(self.metadata_files.items())
            },
            "music_file_count": self.music_file_count,
            "music_total_bytes": self.music_total_bytes,
            "total_bytes": self.total_bytes,
        }

    @classmethod
    def from_json(cls, data: dict) -> BackupManifest:
        return cls(
            created=data.get("created", ""),
            ipod_root=data.get("ipod_root", ""),
            device=data.get("device", {}) or {},
            track_count=int(data.get("track_count", 0) or 0),
            includes_music=bool(data.get("includes_music")),
            metadata_files={
                rel: FileRecord(sha256=v.get("sha256", ""), size=int(v.get("size", 0)))
                for rel, v in (data.get("metadata_files") or {}).items()
            },
            music_file_count=int(data.get("music_file_count", 0) or 0),
            music_total_bytes=int(data.get("music_total_bytes", 0) or 0),
            total_bytes=int(data.get("total_bytes", 0) or 0),
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def default_backup_dir(device: IpodDevice, base: Path | None = None) -> Path:
    """生成带时间戳的备份目录名。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = base or Path.home() / "Documents"
    return base / f"iPod备份-{stamp}"


def create_backup(
    device: IpodDevice,
    dest: Path,
    *,
    include_music: bool = False,
    progress: ProgressCallback | None = None,
) -> BackupManifest:
    """把 ``iPod_Control`` 备份到 ``dest``，并写入 manifest.json。"""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise BackupError(
            f"备份目录已存在且非空：{dest}\n"
            f"  换一个目录名，或先把它移走——不覆盖已有备份。"
        )
    dest.mkdir(parents=True, exist_ok=True)

    src_control = Path(device.root) / "iPod_Control"
    if not src_control.is_dir():
        raise BackupError(f"选中的位置不是 iPod：找不到 {src_control}")

    manifest = BackupManifest(
        created=datetime.now().isoformat(timespec="seconds"),
        ipod_root=str(device.root),
        device={
            "name": device.display_name,
            "model": device.model_number,
            "serial": device.serial,
            "checksum": device.checksum,
        },
    )

    try:
        manifest.track_count = len(read_library(device.root).tracks)
    except Exception:
        manifest.track_count = 0

    # ── 元数据三块（逐文件哈希，用于还原前校验）──────────────────────
    total = 0
    for name in METADATA_DIRS:
        src = src_control / name
        if not src.is_dir():
            if progress is not None:
                progress(f"跳过 {name}/（不存在）")
            continue
        if progress is not None:
            progress(f"正在备份 {name}/…")
        target = dest / "iPod_Control" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, target, dirs_exist_ok=True)

        for path in sorted(target.rglob("*")):
            if not path.is_file() or path.name.startswith(".iop-"):
                continue
            rel = path.relative_to(dest).as_posix()
            digest = _sha256(path)
            manifest.metadata_files[rel] = FileRecord(digest, path.stat().st_size)
            total += path.stat().st_size

    # ── Music（可选；只统计不哈希，几十 GB 哈希太慢）──────────────────
    if include_music:
        src_music = src_control / MUSIC_DIR
        if src_music.is_dir():
            if progress is not None:
                progress("正在备份 Music/…（文件多，需要一会儿）")
            target_music = dest / "iPod_Control" / MUSIC_DIR
            shutil.copytree(src_music, target_music, dirs_exist_ok=True)
            for path in target_music.rglob("*"):
                if path.is_file():
                    manifest.music_file_count += 1
                    manifest.music_total_bytes += path.stat().st_size
            manifest.includes_music = True
            total += manifest.music_total_bytes

    manifest.total_bytes = total
    (dest / MANIFEST_NAME).write_text(
        json.dumps(manifest.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if progress is not None:
        progress(f"备份完成：{human_size(total)}")
    return manifest


def load_manifest(backup_dir: Path) -> BackupManifest:
    path = Path(backup_dir) / MANIFEST_NAME
    if not path.is_file():
        raise BackupError(
            f"找不到 {MANIFEST_NAME}：{backup_dir}\n"
            f"  这个目录不是用本工具创建的备份。"
        )
    return BackupManifest.from_json(json.loads(path.read_text(encoding="utf-8")))


def verify_backup(
    backup_dir: Path, *, progress: ProgressCallback | None = None
) -> tuple[bool, list[str]]:
    """逐文件比对备份与 manifest 的哈希。返回 (是否全部一致, 问题列表)。"""
    backup_dir = Path(backup_dir)
    manifest = load_manifest(backup_dir)
    problems: list[str] = []

    for index, (rel, record) in enumerate(sorted(manifest.metadata_files.items()), 1):
        path = backup_dir / rel
        if not path.is_file():
            problems.append(f"缺失  {rel}")
            continue
        size = path.stat().st_size
        if size != record.size:
            problems.append(f"大小不符  {rel}  备份记 {record.size}，实际 {size}")
            continue
        if _sha256(path) != record.sha256:
            problems.append(f"校验和不符  {rel}")
        if progress is not None and index % 50 == 0:
            progress(f"已校验 {index}/{len(manifest.metadata_files)}")
    return not problems, problems


def restore_backup(
    backup_dir: Path,
    device: IpodDevice,
    *,
    backup_current: bool = True,
    progress: ProgressCallback | None = None,
) -> Path | None:
    """把备份写回设备。

    默认**先把设备当前状态另存一份**——还原本身也应该是可逆的，
    免得"想恢复结果覆盖掉了现在还能用的数据"。

    返回当前状态的临时备份目录（若已创建），否则 None。
    """
    backup_dir = Path(backup_dir)
    manifest = load_manifest(backup_dir)

    src_control = backup_dir / "iPod_Control"
    if not src_control.is_dir():
        raise BackupError(f"备份不完整：找不到 {src_control}")

    consistent, problems = verify_backup(backup_dir, progress=progress)
    if not consistent:
        listing = "\n".join(f"    {p}" for p in problems[:10])
        raise BackupError(
            f"备份校验未通过（{len(problems)} 个问题），已放弃还原：\n{listing}"
        )
    if progress is not None:
        progress("备份校验通过。")

    saved: Path | None = None
    if backup_current:
        # 进度消息不在这里发：调用方会在操作结束后报告保存位置，
        # 而进度行在终端里会被 _end_progress() 抹掉。发两遍等于什么都没说。
        saved = _snapshot_current(device, progress)

    dest_control = Path(device.root) / "iPod_Control"
    names = list(METADATA_DIRS) + ([MUSIC_DIR] if manifest.includes_music else [])
    for name in names:
        src = src_control / name
        if not src.is_dir():
            continue
        if progress is not None:
            progress(f"正在还原 {name}/…")
        target = dest_control / name
        if target.is_dir():
            shutil.rmtree(target)
        shutil.copytree(src, target)

    if progress is not None:
        progress("还原完成。")

    # 读回确认
    try:
        count = len(read_library(device.root).tracks)
    except Exception as exc:
        raise BackupError(f"还原后无法读取数据库：{exc}") from exc
    if manifest.track_count and count != manifest.track_count:
        raise BackupError(
            f"还原后曲目数不符：备份记 {manifest.track_count} 首，实际读到 {count} 首。\n"
            f"  设备未被破坏，可重试或手工拷贝。"
        )
    if progress is not None:
        progress(f"读回确认：{count} 首曲目。")
    return saved


def _snapshot_current(device: IpodDevice, progress: ProgressCallback | None) -> Path | None:
    """把设备当前元数据三块另存一份（用于还原操作本身可回退）。

    失败时返回 None——调用方据此决定是否继续（继续的话还原不可逆，要提示）。
    """
    src_control = Path(device.root) / "iPod_Control"
    if not src_control.is_dir():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = Path.home() / "Documents" / f"iPod还原前快照-{stamp}"
    try:
        saved.mkdir(parents=True, exist_ok=True)
        for name in METADATA_DIRS:
            src = src_control / name
            if src.is_dir():
                shutil.copytree(src, saved / "iPod_Control" / name)
    except OSError:
        return None
    return saved
