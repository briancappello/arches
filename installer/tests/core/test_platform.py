"""Tests for platform configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from arches_installer.core.platform import (
    PlatformConfig,
    load_platform,
)


class TestLoadPlatform:
    """Test loading platform configs from TOML files."""

    def test_load_x86_64_platform(self, platform_toml_file: Path) -> None:
        platform = load_platform(platform_toml_file)
        assert platform.name == "x86-64"
        assert platform.arch == "x86_64"
        assert platform.kernel.package == "linux-cachyos"
        assert platform.kernel.headers == "linux-cachyos-headers"
        assert platform.bootloader.efi_binary == "BOOTX64.EFI"
        assert platform.bootloader.supports_bios is True
        assert platform.hardware_detection.enabled is True
        assert platform.hardware_detection.tool == "chwd"
        assert "cachyos-keyring" in platform.base_packages

    def test_load_nonexistent_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_platform(tmp_path / "nope.toml")

    def test_load_empty_toml(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.toml"
        empty.write_text("")
        platform = load_platform(empty)
        # Should use defaults
        assert platform.name == "unknown"
        assert platform.arch == "x86_64"
        assert platform.kernel.package == "linux"
        assert platform.hardware_detection.enabled is False

    def test_load_minimal_platform(self, tmp_path: Path) -> None:
        """A platform with only the required fields should use defaults."""
        p = tmp_path / "minimal.toml"
        p.write_text("""\
[platform]
name = "test"
arch = "aarch64"

[kernel]
package = "linux-aarch64"
headers = "linux-aarch64-headers"
""")
        platform = load_platform(p)
        assert platform.name == "test"
        assert platform.arch == "aarch64"
        assert platform.kernel.package == "linux-aarch64"
        assert platform.bootloader.type == "limine"
        assert platform.bootloader.supports_bios is True
        assert platform.hardware_detection.enabled is False
        assert platform.base_packages == []


class TestPlatformConfigFromDict:
    """Test the from_dict constructor."""

    def test_minimal_dict(self) -> None:
        platform = PlatformConfig.from_dict({})
        assert platform.name == "unknown"
        assert platform.arch == "x86_64"
        assert platform.kernel.package == "linux"

    def test_full_dict(self) -> None:
        data = {
            "platform": {
                "name": "x86-64",
                "description": "Test platform",
                "arch": "x86_64",
            },
            "kernel": {
                "package": "linux-cachyos",
                "headers": "linux-cachyos-headers",
            },
            "bootloader": {
                "type": "limine",
                "efi_binary": "BOOTX64.EFI",
                "efi_fallback_path": "EFI/BOOT/BOOTX64.EFI",
                "supports_bios": True,
            },
            "hardware_detection": {
                "enabled": True,
                "tool": "chwd",
                "args": ["-a"],
                "optional": True,
            },
            "base_packages": {
                "install": ["cachyos-keyring", "cachyos-settings"],
            },
        }
        platform = PlatformConfig.from_dict(data)
        assert platform.name == "x86-64"
        assert platform.kernel.package == "linux-cachyos"
        assert platform.bootloader.efi_binary == "BOOTX64.EFI"
        assert platform.hardware_detection.enabled is True
        assert platform.hardware_detection.tool == "chwd"
        assert "cachyos-keyring" in platform.base_packages

    def test_aarch64_platform(self) -> None:
        """Test an arm64 platform config."""
        data = {
            "platform": {
                "name": "aarch64-apple",
                "description": "Apple Silicon",
                "arch": "aarch64",
            },
            "kernel": {
                "package": "linux-asahi",
                "headers": "linux-asahi-headers",
            },
            "bootloader": {
                "type": "grub",
                "efi_binary": "BOOTAA64.EFI",
                "efi_fallback_path": "EFI/BOOT/BOOTAA64.EFI",
                "supports_bios": False,
            },
            "hardware_detection": {
                "enabled": False,
            },
            "base_packages": {
                "install": ["asahi-fwextract"],
            },
        }
        platform = PlatformConfig.from_dict(data)
        assert platform.name == "aarch64-apple"
        assert platform.arch == "aarch64"
        assert platform.kernel.package == "linux-asahi"
        assert platform.bootloader.efi_binary == "BOOTAA64.EFI"
        assert platform.bootloader.supports_bios is False
        assert platform.hardware_detection.enabled is False

    def test_unknown_keys_ignored(self) -> None:
        data = {
            "platform": {"name": "test", "unknown": "ignored"},
            "qemu": {"binary": "qemu-system-x86_64"},
        }
        platform = PlatformConfig.from_dict(data)
        assert platform.name == "test"


class TestLoadAarch64Platforms:
    """Test loading the real aarch64 platform TOML files from the repo."""

    # Project root: installer/tests/core/test_platform.py -> parents[3] = repo root
    PROJECT_ROOT = Path(__file__).resolve().parents[3]

    def test_load_aarch64_generic(self) -> None:
        toml_path = (
            self.PROJECT_ROOT / "platforms" / "aarch64-generic" / "platform.toml"
        )
        platform = load_platform(toml_path)
        assert platform.name == "aarch64-generic"
        assert platform.arch == "aarch64"
        assert platform.kernel.package == "linux-aarch64"
        assert platform.kernel.headers == "linux-aarch64-headers"
        assert platform.bootloader.type == "grub"
        assert platform.bootloader.snapshot_boot is True
        assert platform.disk_layout.filesystem == "btrfs"
        assert platform.disk_layout.boot_size_mib == 0
        assert platform.disk_layout.home_partition is False
        assert platform.disk_layout.subvolumes == ["@", "@home", "@var"]

    def test_load_aarch64_apple(self) -> None:
        toml_path = self.PROJECT_ROOT / "platforms" / "aarch64-apple" / "platform.toml"
        platform = load_platform(toml_path)
        assert platform.name == "aarch64-apple"
        assert platform.arch == "aarch64"
        assert platform.kernel.package == "linux-asahi"
        assert platform.kernel.headers == "linux-asahi-headers"
        assert platform.bootloader.type == "grub"
        assert platform.bootloader.snapshot_boot is False
        assert "asahi-fwextract" in platform.base_packages

    def test_aarch64_generic_disk_layout(self) -> None:
        toml_path = (
            self.PROJECT_ROOT / "platforms" / "aarch64-generic" / "platform.toml"
        )
        platform = load_platform(toml_path)
        dl = platform.disk_layout
        assert dl.filesystem == "btrfs"
        assert dl.esp_size_mib == 512
        assert dl.boot_size_mib == 0
        assert dl.home_partition is False
        assert dl.swap == "zram"
        assert dl.subvolumes == ["@", "@home", "@var"]

    def test_aarch64_apple_disk_layout(self) -> None:
        toml_path = self.PROJECT_ROOT / "platforms" / "aarch64-apple" / "platform.toml"
        platform = load_platform(toml_path)
        dl = platform.disk_layout
        assert dl.filesystem == "ext4"
        assert dl.esp_size_mib == 512
        assert dl.boot_size_mib == 1024
        assert dl.home_partition is True
        assert dl.swap == "zram"
        assert dl.subvolumes == []
