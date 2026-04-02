"""Tests for platform configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from arches_installer.core.platform import (
    KernelConfig,
    KernelVariant,
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
        assert len(platform.kernel.variants) == 2
        assert platform.kernel.variants[1].package == "linux-cachyos-lts"
        assert platform.cachyos_optimization_tier == "x86-64-v3"
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
        # x86_64 defaults to baseline cachyos tier
        assert platform.cachyos_optimization_tier == "x86-64"

    def test_load_minimal_platform(self, tmp_path: Path) -> None:
        """A platform with only the required fields should use defaults."""
        p = tmp_path / "minimal.toml"
        p.write_text("""\
[platform]
name = "test"
arch = "aarch64"

[kernel]
variants = [
    { package = "linux-aarch64", headers = "linux-aarch64-headers" },
]
""")
        platform = load_platform(p)
        assert platform.name == "test"
        assert platform.arch == "aarch64"
        assert platform.kernel.package == "linux-aarch64"
        assert len(platform.kernel.variants) == 1
        assert platform.bootloader.type == "limine"
        assert platform.bootloader.supports_bios is True
        assert platform.hardware_detection.enabled is False
        assert platform.base_packages == []
        # aarch64 has no CachyOS tier
        assert platform.cachyos_optimization_tier == ""


class TestPlatformConfigFromDict:
    """Test the from_dict constructor."""

    def test_minimal_dict(self) -> None:
        platform = PlatformConfig.from_dict({})
        assert platform.name == "unknown"
        assert platform.arch == "x86_64"
        assert platform.kernel.package == "linux"
        # x86_64 defaults to baseline
        assert platform.cachyos_optimization_tier == "x86-64"

    def test_full_dict_with_variants(self) -> None:
        data = {
            "platform": {
                "name": "x86-64",
                "description": "Test platform",
                "arch": "x86_64",
                "cachyos_optimization_tier": "x86-64-v3",
            },
            "kernel": {
                "variants": [
                    {
                        "package": "linux-cachyos",
                        "headers": "linux-cachyos-headers",
                    },
                    {
                        "package": "linux-cachyos-lts",
                        "headers": "linux-cachyos-lts-headers",
                    },
                ],
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
        assert platform.kernel.headers == "linux-cachyos-headers"
        assert len(platform.kernel.variants) == 2
        assert platform.kernel.variants[1].package == "linux-cachyos-lts"
        assert platform.cachyos_optimization_tier == "x86-64-v3"
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
                "variants": [
                    {
                        "package": "linux-asahi",
                        "headers": "linux-asahi-headers",
                    },
                ],
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
        assert len(platform.kernel.variants) == 1
        assert platform.bootloader.efi_binary == "BOOTAA64.EFI"
        assert platform.bootloader.supports_bios is False
        assert platform.hardware_detection.enabled is False
        # aarch64 has no CachyOS tier
        assert platform.cachyos_optimization_tier == ""

    def test_unknown_keys_ignored(self) -> None:
        data = {
            "platform": {"name": "test", "unknown": "ignored"},
            "qemu": {"binary": "qemu-system-x86_64"},
        }
        platform = PlatformConfig.from_dict(data)
        assert platform.name == "test"

    def test_fallback_without_variants(self) -> None:
        """Legacy format without variants should fall back gracefully."""
        data = {
            "kernel": {
                "package": "linux-custom",
                "headers": "linux-custom-headers",
            },
        }
        platform = PlatformConfig.from_dict(data)
        assert platform.kernel.package == "linux-custom"
        assert platform.kernel.headers == "linux-custom-headers"
        assert len(platform.kernel.variants) == 1


class TestKernelConfig:
    """Test KernelConfig variant selection logic."""

    def test_first_variant_is_default(self) -> None:
        """First variant is default when none is explicitly marked."""
        kc = KernelConfig(
            variants=[
                KernelVariant(package="linux-a", headers="linux-a-headers"),
                KernelVariant(package="linux-b", headers="linux-b-headers"),
            ]
        )
        assert kc.default_variant.package == "linux-a"
        assert kc.package == "linux-a"
        assert kc.headers == "linux-a-headers"

    def test_explicit_default_override(self) -> None:
        """Explicit default=True overrides position."""
        kc = KernelConfig(
            variants=[
                KernelVariant(package="linux-a", headers="linux-a-headers"),
                KernelVariant(
                    package="linux-b", headers="linux-b-headers", default=True
                ),
            ]
        )
        assert kc.default_variant.package == "linux-b"
        assert kc.package == "linux-b"
        assert kc.headers == "linux-b-headers"

    def test_single_variant(self) -> None:
        """Single variant is always the default."""
        kc = KernelConfig(
            variants=[
                KernelVariant(package="linux-asahi", headers="linux-asahi-headers"),
            ]
        )
        assert kc.default_variant.package == "linux-asahi"
        assert kc.package == "linux-asahi"


class TestCachyosOptimizationTier:
    """Test cachyos_optimization_tier defaulting logic."""

    def test_x86_64_defaults_to_baseline(self) -> None:
        """x86_64 without explicit tier defaults to 'x86-64' (baseline)."""
        data = {"platform": {"arch": "x86_64"}}
        platform = PlatformConfig.from_dict(data)
        assert platform.cachyos_optimization_tier == "x86-64"

    def test_x86_64_explicit_tier(self) -> None:
        """x86_64 with explicit tier uses the specified value."""
        data = {
            "platform": {
                "arch": "x86_64",
                "cachyos_optimization_tier": "znver4",
            },
        }
        platform = PlatformConfig.from_dict(data)
        assert platform.cachyos_optimization_tier == "znver4"

    def test_aarch64_no_tier(self) -> None:
        """aarch64 has no CachyOS tier (empty string)."""
        data = {"platform": {"arch": "aarch64"}}
        platform = PlatformConfig.from_dict(data)
        assert platform.cachyos_optimization_tier == ""

    def test_aarch64_ignores_explicit_tier(self) -> None:
        """If someone sets a tier on aarch64, it's preserved (no validation)."""
        data = {
            "platform": {
                "arch": "aarch64",
                "cachyos_optimization_tier": "x86-64-v3",
            },
        }
        platform = PlatformConfig.from_dict(data)
        # We don't block it — just store what was set
        assert platform.cachyos_optimization_tier == "x86-64-v3"


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
        assert len(platform.kernel.variants) == 1
        assert platform.cachyos_optimization_tier == ""
        assert platform.bootloader.type == "grub"
        assert platform.bootloader.snapshot_boot is True
        assert platform.swap == "zram"

    def test_load_aarch64_apple(self) -> None:
        toml_path = self.PROJECT_ROOT / "platforms" / "aarch64-apple" / "platform.toml"
        platform = load_platform(toml_path)
        assert platform.name == "aarch64-apple"
        assert platform.arch == "aarch64"
        assert platform.kernel.package == "linux-asahi"
        assert platform.kernel.headers == "linux-asahi-headers"
        assert len(platform.kernel.variants) == 1
        assert platform.cachyos_optimization_tier == ""
        assert platform.bootloader.type == "grub"
        assert platform.bootloader.snapshot_boot is False
        assert platform.swap == "zram"
        assert "asahi-alarm-keyring" in platform.base_packages
        assert "asahi-fwextract" in platform.base_packages
        assert "grub" in platform.base_packages
        assert "efibootmgr" in platform.base_packages
        assert "btrfs-progs" in platform.base_packages

    def test_aarch64_generic_swap(self) -> None:
        toml_path = (
            self.PROJECT_ROOT / "platforms" / "aarch64-generic" / "platform.toml"
        )
        platform = load_platform(toml_path)
        assert platform.swap == "zram"

    def test_aarch64_apple_swap(self) -> None:
        toml_path = self.PROJECT_ROOT / "platforms" / "aarch64-apple" / "platform.toml"
        platform = load_platform(toml_path)
        assert platform.swap == "zram"


class TestDefaultTemplate:
    """Test default_template field."""

    def test_x86_64_has_default_template(self) -> None:
        data = {
            "platform": {
                "arch": "x86_64",
                "default_template": "dev-workstation",
            },
        }
        platform = PlatformConfig.from_dict(data)
        assert platform.default_template == "dev-workstation"

    def test_default_template_empty_when_omitted(self) -> None:
        data = {"platform": {"arch": "x86_64"}}
        platform = PlatformConfig.from_dict(data)
        assert platform.default_template == ""


class TestLoadX86Platform:
    """Test loading the real x86-64 platform TOML file from the repo."""

    PROJECT_ROOT = Path(__file__).resolve().parents[3]

    def test_load_x86_64(self) -> None:
        toml_path = self.PROJECT_ROOT / "platforms" / "x86-64" / "platform.toml"
        platform = load_platform(toml_path)
        assert platform.name == "x86-64"
        assert platform.arch == "x86_64"
        assert platform.cachyos_optimization_tier == "x86-64-v3"
        assert platform.default_template == "dev-workstation"
        assert len(platform.kernel.variants) == 2
        assert platform.kernel.package == "linux-cachyos"
        assert platform.kernel.variants[0].package == "linux-cachyos"
        assert platform.kernel.variants[1].package == "linux-cachyos-lts"
        assert platform.bootloader.type == "limine"
        assert platform.bootloader.snapshot_boot is True
