"""Load and validate platform configuration from TOML files.

A platform defines the hardware-level foundation: kernel, package repos,
bootloader, disk layout, hardware detection, and base packages. Templates
build on top of the platform to define workload-specific packages and
configuration.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Default path where the platform config is baked into the ISO
ISO_PLATFORM_DIR = Path("/opt/arches/platform")


@dataclass
class KernelVariant:
    """A single kernel variant (package + headers)."""

    package: str
    headers: str
    default: bool = False


@dataclass
class KernelConfig:
    """Kernel configuration supporting multiple variants.

    The default variant is the first one with ``default=True``, or the
    first variant in the list if none is explicitly marked.  Backward-compat
    ``.package`` and ``.headers`` properties delegate to the default variant
    so existing code that references ``platform.kernel.package`` keeps working.
    """

    variants: list[KernelVariant] = field(default_factory=list)

    @property
    def default_variant(self) -> KernelVariant:
        """Return the default kernel variant."""
        for v in self.variants:
            if v.default:
                return v
        return self.variants[0]

    @property
    def package(self) -> str:
        """Default variant's package name (backward compat)."""
        return self.default_variant.package

    @property
    def headers(self) -> str:
        """Default variant's headers package name (backward compat)."""
        return self.default_variant.headers


@dataclass
class BootloaderPlatformConfig:
    type: str = "limine"
    efi_binary: str = "BOOTX64.EFI"
    efi_fallback_path: str = "EFI/BOOT/BOOTX64.EFI"
    supports_bios: bool = True
    snapshot_boot: bool = False


@dataclass
class DiskLayoutConfig:
    """Default disk layout for auto-install.

    On x86-64 (Limine): ESP doubles as /boot, btrfs root with subvolumes.
    On aarch64 (GRUB): ESP at /boot/efi, btrfs root with subvolumes.
        GRUB reads kernels from btrfs natively — no separate /boot needed.
    """

    filesystem: str = "ext4"
    mount_options: str = "noatime"
    subvolumes: list[str] = field(default_factory=list)
    esp_size_mib: int = 512
    boot_size_mib: int = 0  # 0 = no separate /boot (ESP is /boot)
    home_partition: bool = False  # separate /home partition
    swap: str = "zram"


@dataclass
class HardwareDetectionConfig:
    enabled: bool = False
    tool: str = ""
    args: list[str] = field(default_factory=list)
    optional: bool = True


@dataclass
class PlatformConfig:
    """Hardware-level platform configuration."""

    name: str
    description: str
    arch: str
    kernel: KernelConfig
    bootloader: BootloaderPlatformConfig
    disk_layout: DiskLayoutConfig
    hardware_detection: HardwareDetectionConfig
    base_packages: list[str] = field(default_factory=list)
    # CachyOS optimization tier for x86-64 platforms.  Controls which
    # CachyOS repo tier is used for optimized packages (affects the
    # entire package set, not just the kernel).  Valid values:
    #   "x86-64"     — baseline (no tier-specific repos, kernels only)
    #   "x86-64-v3"  — AVX2/SSE4.2 (2011+ hardware)
    #   "x86-64-v4"  — AVX-512 (Zen 4+, Haswell+)
    #   "znver4"     — AMD Zen 4/5 specific tuning
    # Empty string for non-x86 platforms (CachyOS is x86-64 only).
    cachyos_optimization_tier: str = ""
    # When False, auto-install and auto-partition are disabled. This
    # prevents destructive whole-disk wipes on platforms where the
    # partition table is managed externally (e.g. Apple Silicon, where
    # Asahi's m1n1/U-Boot/macOS recovery partitions must not be touched).
    allow_auto_install: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlatformConfig:
        """Build a PlatformConfig from a parsed TOML dict."""
        plat = data.get("platform", {})
        kern = data.get("kernel", {})
        boot = data.get("bootloader", {})
        disk = data.get("disk_layout", {})
        hw = data.get("hardware_detection", {})
        base = data.get("base_packages", {})

        # Parse kernel variants — supports the variants list format:
        #   [kernel]
        #   variants = [
        #       { package = "linux-cachyos", headers = "linux-cachyos-headers" },
        #   ]
        raw_variants = kern.get("variants", [])
        if raw_variants:
            variants = [
                KernelVariant(
                    package=v["package"],
                    headers=v["headers"],
                    default=v.get("default", False),
                )
                for v in raw_variants
            ]
        else:
            # Fallback for empty/missing variants — use generic defaults
            variants = [
                KernelVariant(
                    package=kern.get("package", "linux"),
                    headers=kern.get("headers", "linux-headers"),
                )
            ]

        # CachyOS optimization tier: default to baseline for x86_64,
        # empty string for other architectures (CachyOS is x86-64 only).
        arch = plat.get("arch", "x86_64")
        cachyos_tier = plat.get("cachyos_optimization_tier", "")
        if not cachyos_tier and arch == "x86_64":
            cachyos_tier = "x86-64"

        return cls(
            name=plat.get("name", "unknown"),
            description=plat.get("description", ""),
            arch=arch,
            kernel=KernelConfig(variants=variants),
            bootloader=BootloaderPlatformConfig(
                type=boot.get("type", "limine"),
                efi_binary=boot.get("efi_binary", "BOOTX64.EFI"),
                efi_fallback_path=boot.get("efi_fallback_path", "EFI/BOOT/BOOTX64.EFI"),
                supports_bios=boot.get("supports_bios", True),
                snapshot_boot=boot.get("snapshot_boot", False),
            ),
            disk_layout=DiskLayoutConfig(
                filesystem=disk.get("filesystem", "ext4"),
                mount_options=disk.get("mount_options", "noatime"),
                subvolumes=disk.get("subvolumes", []),
                esp_size_mib=disk.get("esp_size_mib", 512),
                boot_size_mib=disk.get("boot_size_mib", 0),
                home_partition=disk.get("home_partition", False),
                swap=disk.get("swap", "zram"),
            ),
            hardware_detection=HardwareDetectionConfig(
                enabled=hw.get("enabled", False),
                tool=hw.get("tool", ""),
                args=hw.get("args", []),
                optional=hw.get("optional", True),
            ),
            base_packages=base.get("install", []),
            cachyos_optimization_tier=cachyos_tier,
            allow_auto_install=plat.get("allow_auto_install", True),
        )


def load_platform(path: Path) -> PlatformConfig:
    """Load a platform config from a TOML file."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return PlatformConfig.from_dict(data)


def load_platform_from_iso() -> PlatformConfig:
    """Load the platform config baked into the running ISO."""
    return load_platform(ISO_PLATFORM_DIR / "platform.toml")
