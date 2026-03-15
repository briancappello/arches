"""Load and validate install templates from TOML files.

Templates define the userspace workload: packages, services, Ansible roles,
timezone, and locale. They are platform-independent — disk layout, bootloader,
kernel, and base packages all come from the platform config.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any


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
    system: SystemConfig
    services: list[str] = field(default_factory=list)
    ansible: AnsibleConfig = field(default_factory=AnsibleConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstallTemplate:
        """Build an InstallTemplate from a parsed TOML dict."""
        meta = data.get("meta", {})
        sys_raw = data.get("system", {})
        svc_raw = data.get("services", {})
        ans_raw = data.get("ansible", {})

        return cls(
            name=meta.get("name", "Unknown"),
            description=meta.get("description", ""),
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
