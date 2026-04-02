"""Load and validate platform configuration from TOML files.

A platform defines the hardware-level foundation: kernel, package repos,
bootloader, hardware detection, and base packages. Templates build on top
of the platform to define workload-specific packages and configuration.
Disk layouts are now a separate concept (see ``disk_layout.py``).
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
    hardware_detection: HardwareDetectionConfig
    base_packages: list[str] = field(default_factory=list)
    # Swap strategy — typically "zram" for compressed in-memory swap.
    # Moved here from the removed DiskLayoutConfig.
    swap: str = "zram"
    # CachyOS optimization tier for x86-64 platforms.  Controls which
    # CachyOS repo tier is used for optimized packages (affects the
    # entire package set, not just the kernel).  Valid values:
    #   "x86-64"     -- baseline (no tier-specific repos, kernels only)
    #   "x86-64-v3"  -- AVX2/SSE4.2 (2011+ hardware)
    #   "x86-64-v4"  -- AVX-512 (Zen 4+, Haswell+)
    #   "znver4"     -- AMD Zen 4/5 specific tuning
    # Empty string for non-x86 platforms (CachyOS is x86-64 only).
    cachyos_optimization_tier: str = ""
    # Platform-specific kernel command-line parameters (console, loglevel,
    # video mode, etc.).  Applied to both ISO boot configs and the
    # installed system's bootloader.
    kernel_flags: list[str] = field(default_factory=list)
    # Default template for ISO builds.  The ISO is built as a superset
    # of this template's installed system.  Can be overridden at build
    # time with TEMPLATE=<name>.
    default_template: str = ""
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
        hw = data.get("hardware_detection", {})
        base = data.get("base_packages", {})

        # Parse kernel variants -- supports the variants list format:
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
            # Fallback for empty/missing variants -- use generic defaults
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

        # Swap strategy (was in [disk_layout], now top-level in [platform])
        swap = plat.get("swap", "zram")

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
            hardware_detection=HardwareDetectionConfig(
                enabled=hw.get("enabled", False),
                tool=hw.get("tool", ""),
                args=hw.get("args", []),
                optional=hw.get("optional", True),
            ),
            base_packages=base.get("install", []),
            swap=swap,
            cachyos_optimization_tier=cachyos_tier,
            kernel_flags=kern.get("flags", []),
            default_template=plat.get("default_template", ""),
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
