"""Load and validate platform configuration from TOML files.

A platform defines the hardware-level foundation: kernel, package repos,
bootloader, hardware detection, and base packages. Templates build on top
of the platform to define workload-specific packages and configuration.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Default path where the platform config is baked into the ISO
ISO_PLATFORM_DIR = Path("/opt/arches/platform")


@dataclass
class KernelConfig:
    package: str
    headers: str


@dataclass
class BootloaderPlatformConfig:
    type: str = "limine"
    efi_binary: str = "BOOTX64.EFI"
    efi_fallback_path: str = "EFI/BOOT/BOOTX64.EFI"
    supports_bios: bool = True


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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlatformConfig:
        """Build a PlatformConfig from a parsed TOML dict."""
        plat = data.get("platform", {})
        kern = data.get("kernel", {})
        boot = data.get("bootloader", {})
        hw = data.get("hardware_detection", {})
        base = data.get("base_packages", {})

        return cls(
            name=plat.get("name", "unknown"),
            description=plat.get("description", ""),
            arch=plat.get("arch", "x86_64"),
            kernel=KernelConfig(
                package=kern.get("package", "linux"),
                headers=kern.get("headers", "linux-headers"),
            ),
            bootloader=BootloaderPlatformConfig(
                type=boot.get("type", "limine"),
                efi_binary=boot.get("efi_binary", "BOOTX64.EFI"),
                efi_fallback_path=boot.get("efi_fallback_path", "EFI/BOOT/BOOTX64.EFI"),
                supports_bios=boot.get("supports_bios", True),
            ),
            hardware_detection=HardwareDetectionConfig(
                enabled=hw.get("enabled", False),
                tool=hw.get("tool", ""),
                args=hw.get("args", []),
                optional=hw.get("optional", True),
            ),
            base_packages=base.get("install", []),
        )


def load_platform(path: Path) -> PlatformConfig:
    """Load a platform config from a TOML file."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return PlatformConfig.from_dict(data)


def load_platform_from_iso() -> PlatformConfig:
    """Load the platform config baked into the running ISO."""
    return load_platform(ISO_PLATFORM_DIR / "platform.toml")
