"""Load and validate install templates from TOML files.

Templates define the workload built on top of a platform: filesystem layout,
desktop/server packages, services, and Ansible roles. The kernel and base
platform packages come from the platform config, not the template.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any


@dataclass
class DiskConfig:
    filesystem: str  # "btrfs" or "ext4"
    mount_options: str = "noatime"
    subvolumes: list[str] = field(default_factory=list)
    esp_size_mib: int = 512
    swap: str = "zram"


@dataclass
class BootloaderConfig:
    type: str = "limine"
    snapshot_boot: bool = False


@dataclass
class SystemConfig:
    timezone: str = "America/New_York"
    locale: str = "en_US.UTF-8"
    packages: list[str] = field(default_factory=list)


@dataclass
class AnsibleConfig:
    chroot_roles: list[str] = field(default_factory=list)
    firstboot_roles: list[str] = field(default_factory=list)


@dataclass
class InstallTemplate:
    name: str
    description: str
    disk: DiskConfig
    bootloader: BootloaderConfig
    system: SystemConfig
    services: list[str] = field(default_factory=list)
    ansible: AnsibleConfig = field(default_factory=AnsibleConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstallTemplate:
        """Build an InstallTemplate from a parsed TOML dict."""
        meta = data.get("meta", {})
        disk_raw = data.get("disk", {})
        boot_raw = data.get("bootloader", {})
        sys_raw = data.get("system", {})
        svc_raw = data.get("services", {})
        ans_raw = data.get("ansible", {})

        return cls(
            name=meta.get("name", "Unknown"),
            description=meta.get("description", ""),
            disk=DiskConfig(
                filesystem=disk_raw.get("filesystem", "ext4"),
                mount_options=disk_raw.get("mount_options", "noatime"),
                subvolumes=disk_raw.get("subvolumes", []),
                esp_size_mib=disk_raw.get("esp_size_mib", 512),
                swap=disk_raw.get("swap", "zram"),
            ),
            bootloader=BootloaderConfig(
                type=boot_raw.get("type", "limine"),
                snapshot_boot=boot_raw.get("snapshot_boot", False),
            ),
            system=SystemConfig(
                timezone=sys_raw.get("timezone", "America/New_York"),
                locale=sys_raw.get("locale", "en_US.UTF-8"),
                packages=sys_raw.get("packages", []),
            ),
            services=svc_raw.get("enable", []),
            ansible=AnsibleConfig(
                chroot_roles=ans_raw.get("chroot_roles", []),
                firstboot_roles=ans_raw.get("firstboot_roles", []),
            ),
        )


def load_template(path: Path) -> InstallTemplate:
    """Load a single template from a TOML file."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return InstallTemplate.from_dict(data)


def discover_templates() -> list[InstallTemplate]:
    """Discover all .toml templates in the templates directory."""
    templates_dir = resources.files("arches_installer") / "templates"
    templates: list[InstallTemplate] = []

    for item in templates_dir.iterdir():
        if hasattr(item, "name") and item.name.endswith(".toml"):
            path = Path(str(item))
            templates.append(load_template(path))

    return sorted(templates, key=lambda t: t.name)
