"""设备识别与路径枚举的测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ipod_cli.discovery import (
    DeviceNotFoundError,
    human_size,
    probe_mount,
    require_ipod,
)


class TestProbeMount:
    def test_identifies_classic_from_virtual_ipod(self, ipod_root: Path) -> None:
        device = probe_mount(ipod_root)
        assert device is not None
        assert device.family == "iPod Classic"
        assert device.generation == "6th Gen"
        assert device.capacity == "80GB"
        assert device.color == "Black"
        assert device.model_number == "MB147"

    def test_classic_uses_hash58(self, ipod_root: Path) -> None:
        device = probe_mount(ipod_root)
        assert device is not None
        assert device.checksum == "HASH58"
        assert device.is_classic
        assert device.is_supported

    def test_reads_firewire_guid_from_sysinfo(self, ipod_root: Path) -> None:
        device = probe_mount(ipod_root)
        assert device is not None
        assert device.firewire_guid, "HASH58 签名需要 FireWire GUID"
        # GUID 是十六进制串
        int(device.firewire_guid, 16)

    def test_plain_directory_is_not_an_ipod(self, tmp_path: Path) -> None:
        plain = tmp_path / "not-an-ipod"
        plain.mkdir()
        assert probe_mount(plain) is None

    def test_ipod_control_without_database_is_not_probed(
        self, tmp_path: Path
    ) -> None:
        """有 iPod_Control 但没有 iTunesDB —— 不能当成可用设备。"""
        root = tmp_path / "IPOD"
        (root / "iPod_Control" / "iTunes").mkdir(parents=True)
        assert probe_mount(root) is None

    def test_display_name_is_chinese_friendly(self, ipod_root: Path) -> None:
        device = probe_mount(ipod_root)
        assert device is not None
        # 部件顺序：家族 → 代数 → 容量 → 颜色
        assert device.display_name == "iPod Classic 6th Gen 80GB Black"


class TestRequireIpod:
    def test_explicit_path_works(self, ipod_root: Path) -> None:
        device = require_ipod(ipod_root)
        assert device.model_number == "MB147"

    def test_missing_path_raises_with_chinese_message(self, tmp_path: Path) -> None:
        with pytest.raises(DeviceNotFoundError) as excinfo:
            require_ipod(tmp_path / "does-not-exist")
        assert "路径不存在" in str(excinfo.value)

    def test_non_ipod_path_raises_with_hint(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(DeviceNotFoundError) as excinfo:
            require_ipod(plain)
        message = str(excinfo.value)
        assert "iPod_Control" in message
        assert "盘符" in message


class TestDeviceInfoBridge:
    """与 vendored 内核的桥接：内核靠 DeviceInfo 查能力和封面格式。"""

    def test_to_device_info_carries_identity(self, device) -> None:
        info = device.to_device_info()
        assert info.model_family == "iPod Classic"
        assert info.generation == "6th Gen"
        assert info.model_number == "MB147"
        assert info.capacity == "80GB"

    def test_activate_passes_kernel_validation(self, device) -> None:
        """set_current_device 会校验"能不能选出唯一安全写入档"，不能抛异常。"""
        info = device.activate()
        assert info.model_family == "iPod Classic"

        from iopenpod.device import get_current_device_for_path

        resolved = get_current_device_for_path(str(device.root))
        assert resolved is not None, "注册后内核必须能按路径找回这台设备"

    def test_activate_enables_artwork_formats(self, device) -> None:
        """注册后内核才敢写封面（否则宁可不写也不猜错格式）。"""
        device.activate()
        from iopenpod.device import get_current_device_for_path
        from iopenpod.device.artwork import resolve_cover_art_format_definitions_for_device

        info = get_current_device_for_path(str(device.root))
        formats = resolve_cover_art_format_definitions_for_device(info)
        assert formats, "注册后应该能解析出封面格式表"


class TestHumanSize:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (0, "0 B"),
            (999, "999 B"),
            (1000, "1.0 KB"),
            (1536, "1.5 KB"),
            (80_000_000_000, "80.0 GB"),
            (160_000_000_000, "160.0 GB"),
        ],
    )
    def test_formats_decimal_units(self, value: int, expected: str) -> None:
        assert human_size(value) == expected
