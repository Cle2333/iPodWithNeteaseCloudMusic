"""找到并识别 iPod Classic。

刻意只做**基于文件系统**的识别：扫描挂载点 → 看有没有 iPod_Control →
读 SysInfo 拿型号 → 查能力表。不碰 USB VPD / IOCTL / 注册表那套硬件探测，
所以既不需要额外依赖，也不会因为驱动差异在你机器上失灵。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from iopenpod.device import (
    DeviceInfo,
    detect_checksum_type,
    get_current_device_for_path,
    read_sysinfo,
    set_current_device,
)
from iopenpod.device.checksum import ChecksumType
from iopenpod.device.lookup import extract_model_number, get_model_info

ITUNES_SUBDIR = Path("iPod_Control") / "iTunes"
SYSINFO_SUBPATH = Path("iPod_Control") / "Device" / "SysInfo"

# 认得出的数据库文件名（Classic 用未压缩的 iTunesDB）
DATABASE_FILENAMES = ("iTunesDB", "iTunesCDB")


class DeviceNotFoundError(RuntimeError):
    """找不到 iPod。"""


@dataclass
class IpodDevice:
    """一台已识别的 iPod。"""

    root: Path
    mount_name: str
    db_path: Path

    model_number: str = ""
    family: str = ""
    generation: str = ""
    capacity: str = ""
    color: str = ""
    serial: str = ""
    firewire_guid: str = ""
    checksum: str = ""

    total_bytes: int = 0
    free_bytes: int = 0

    @property
    def display_name(self) -> str:
        """给人看的中文友好名称。"""
        parts = [self.family or "iPod"]
        if self.generation:
            parts.append(self.generation)
        if self.capacity:
            parts.append(self.capacity)
        if self.color:
            parts.append(self.color)
        return " ".join(parts)

    @property
    def is_classic(self) -> bool:
        return "Classic" in self.family

    @property
    def is_supported(self) -> bool:
        """Classic 全代走 HASH58，是本工具验证过的路径。"""
        return self.is_classic and self.checksum == "HASH58"

    @property
    def free_text(self) -> str:
        return human_size(self.free_bytes)

    @property
    def total_text(self) -> str:
        return human_size(self.total_bytes)

    @property
    def used_text(self) -> str:
        return human_size(max(self.total_bytes - self.free_bytes, 0))

    # ── 与 vendored 内核的桥接 ────────────────────────────────────────
    def to_device_info(self) -> DeviceInfo:
        """构造内核认识的 ``DeviceInfo``。

        vendored 的封面 / 能力查询代码都通过 ``get_current_device_for_path``
        取设备，所以写库前必须把它注册进去，否则内核会认为"设备未知"而
        拒绝写封面（宁可不写也不猜错格式，这个设计是对的）。
        """
        info = DeviceInfo(path=str(self.root), mount_name=self.mount_name)
        info.model_number = self.model_number
        info.model_family = self.family
        info.generation = self.generation
        info.capacity = self.capacity
        info.color = self.color
        info.serial = self.serial
        info.firewire_guid = self.firewire_guid
        info.identification_method = "sysinfo"

        try:
            info.checksum_type = (
                int(ChecksumType[self.checksum])
                if self.checksum in ChecksumType.__members__
                else 99
            )
        except Exception:
            info.checksum_type = 99

        try:
            info.disk_size_gb = self.total_bytes / (1000 ** 3)
            info.free_space_gb = self.free_bytes / (1000 ** 3)
        except Exception:
            pass

        # 记录字段来源，避免内核把我们的值当成"猜测值"而触发重新探测
        for field in (
            "model_number", "model_family", "generation", "capacity",
            "color", "serial", "firewire_guid",
        ):
            if getattr(info, field, ""):
                info._field_sources[field] = "SysInfo"

        return info

    def activate(self) -> DeviceInfo:
        """把本设备注册为当前活动设备，供内核查询能力与封面格式。

        **幂等**：已经注册过同一台就直接返回，不重复写。

        不幂等的代价实测过：界面状态条每 3 秒轮询一次，每次轮询拿到设备
        就注册一遍，日志里刷出一大串

          Device stored: iPod Classic 6th Gen (MB029) ...

        那份日志正是出问题时「导出诊断记录」给人看的——每 3 秒一条噪音，
        真线索就被淹了。
        """
        current = get_current_device_for_path(str(self.root))
        if current is not None:
            return current

        info = self.to_device_info()
        set_current_device(info)
        return info


def human_size(num_bytes: int) -> str:
    """字节数转人类可读（十进制单位，跟厂商标称一致）。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


# ──────────────────────────────────────────────────────────────────────
# 挂载点枚举
# ──────────────────────────────────────────────────────────────────────

def _windows_drive_roots() -> list[Path]:
    """用 GetLogicalDrives 枚举盘符。

    比逐个试 A:\\~Z:\\ 快得多——不存在的盘符不会触发软驱/网络盘超时。
    """
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
    except Exception:
        return []
    roots = []
    for index in range(26):
        if bitmask & (1 << index):
            roots.append(Path(f"{chr(ord('A') + index)}:\\"))
    return roots


def _posix_mount_roots() -> list[Path]:
    """macOS 看 /Volumes，Linux 看几个常见挂载父目录。

    macOS 走文件系统不需要额外权限（unlike IOKit probing）。
    """
    roots: list[Path] = []
    if sys.platform == "darwin":
        roots.append(Path("/Volumes"))
    else:
        for parent in ("/media", "/run/media", "/mnt"):
            if os.path.isdir(parent):
                roots.append(Path(parent))
        # Linux 可能把 iPod 挂在 /media/<user>/<label>
        for parent in list(roots):
            if parent.name in {"media", "run"} or str(parent).startswith("/run/media"):
                try:
                    for user_dir in parent.iterdir():
                        if user_dir.is_dir():
                            roots.append(user_dir)
                except OSError:
                    pass
    return roots


def iter_candidate_mounts() -> list[Path]:
    """返回所有可能挂着 iPod 的根路径。"""
    candidates: list[Path] = []

    if sys.platform == "win32":
        candidates.extend(_windows_drive_roots())
    else:
        for parent in _posix_mount_roots():
            if _looks_like_ipod_root(parent):
                candidates.append(parent)
            else:
                try:
                    for child in sorted(parent.iterdir()):
                        if child.is_dir():
                            candidates.append(child)
                except OSError:
                    continue

    # 去重，保持顺序
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _looks_like_ipod_root(path: Path) -> bool:
    """路径下是否有 iPod_Control 目录。"""
    try:
        return (path / "iPod_Control").is_dir()
    except OSError:
        return False


def _find_database(root: Path) -> Path | None:
    itunes_dir = root / ITUNES_SUBDIR
    for name in DATABASE_FILENAMES:
        candidate = itunes_dir / name
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


# ──────────────────────────────────────────────────────────────────────
# 设备识别
# ──────────────────────────────────────────────────────────────────────

def probe_mount(mount: Path) -> IpodDevice | None:
    """检查一个挂载点是不是 iPod。是就返回识别结果，不是就返回 None。"""
    if not _looks_like_ipod_root(mount):
        return None

    db_path = _find_database(mount)
    if db_path is None:
        # 有 iPod_Control 但没有数据库——可能是刚格式化过，也可能是别的设备
        return None

    device = IpodDevice(
        root=mount,
        mount_name=str(mount),
        db_path=db_path,
    )

    # ── SysInfo → 型号 ────────────────────────────────────────────────
    try:
        sysinfo = read_sysinfo(str(mount))
    except (FileNotFoundError, OSError):
        sysinfo = {}

    device.serial = str(sysinfo.get("pszSerialNumber") or sysinfo.get("SerialNumber") or "")
    device.firewire_guid = str(sysinfo.get("FirewireGuid") or "")

    raw_model = str(sysinfo.get("ModelNumStr") or "")
    model_number = extract_model_number(raw_model) if raw_model else None
    if model_number:
        device.model_number = model_number
        info = get_model_info(model_number)
        if info:
            family, generation, capacity, color = info
            device.family = family
            device.generation = generation
            device.capacity = capacity
            device.color = color

    # ── 签名方案 ─────────────────────────────────────────────────────
    try:
        device.checksum = detect_checksum_type(str(mount)).name
    except Exception:
        device.checksum = "UNKNOWN"

    # ── 容量 ─────────────────────────────────────────────────────────
    try:
        usage = shutil.disk_usage(str(mount))
        device.total_bytes = usage.total
        device.free_bytes = usage.free
    except OSError:
        pass

    return device


def find_ipods() -> list[IpodDevice]:
    """扫描所有挂载点，返回找到的 iPod。"""
    found: list[IpodDevice] = []
    for mount in iter_candidate_mounts():
        device = probe_mount(mount)
        if device is not None:
            found.append(device)
    return found


def require_ipod(path: str | Path | None = None) -> IpodDevice:
    """解析用户指定的路径，或自动找唯一的一台 iPod。

    路径不合法／找不到／有多台却没说用哪台时，抛出带中文说明的错误。
    """
    if path is not None:
        root = Path(str(path)).expanduser()
        if not root.exists():
            raise DeviceNotFoundError(f"路径不存在：{root}")
        if not _looks_like_ipod_root(root):
            raise DeviceNotFoundError(
                f"这个路径不像 iPod 根目录：{root}\n"
                f"  期望在里面能找到 iPod_Control 文件夹。\n"
                f"  请指向 iPod 的盘符或挂载点（例如 D:\\ 或 /Volumes/IPOD）。"
            )
        device = probe_mount(root)
        if device is None:
            raise DeviceNotFoundError(
                f"在 {root} 里找到了 iPod_Control，但没有可用的 iTunesDB。\n"
                f"  如果这台 iPod 从未被 iTunes 同步过，请先用 iTunes 同步一次以生成数据库。"
            )
        return device

    devices = find_ipods()
    if not devices:
        raise DeviceNotFoundError(
            "没有找到 iPod。请检查：\n"
            "  1. iPod 已连接并已挂载（在「此电脑」里能看到它）\n"
            "  2. iPod 不是处于磁盘模式锁定状态\n"
            "  3. 或者直接用 --ipod 参数指定盘符，例如：--ipod D:\\"
        )
    if len(devices) > 1:
        listing = "\n".join(f"    {d.mount_name}  ({d.display_name})" for d in devices)
        raise DeviceNotFoundError(
            f"找到多台 iPod，请用 --ipod 指定要用哪一台：\n{listing}"
        )
    return devices[0]


def describe(device: IpodDevice) -> str:
    """输出一段中文设备描述。"""
    lines = [
        f"设备      : {device.display_name}",
        f"挂载点    : {device.mount_name}",
    ]
    if device.model_number:
        lines.append(f"型号      : {device.model_number}")
    if device.serial:
        lines.append(f"序列号    : {device.serial}")
    if device.firewire_guid:
        lines.append(f"FireWire  : {device.firewire_guid}")
    lines.append(f"签名方案  : {device.checksum}")
    if device.total_bytes:
        lines.append(
            f"容量      : {device.used_text} 已用 / {device.total_text} 共 "
            f"（剩余 {device.free_text}）"
        )
    return "\n".join(lines)
