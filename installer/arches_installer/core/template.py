"""Load and validate install templates from TOML files.

Templates define the userspace workload: packages, services, Ansible roles,
timezone, and locale. They are platform-independent — disk layout, bootloader,
kernel, and base packages all come from the platform config.

Templates live in ``<project>/templates/`` (development) or
``/opt/arches/templates/`` (live ISO).  Install-specific templates
(auto-install.toml, host-install.toml) and system templates
(dev-workstation.toml, vm-server.toml) all live in the same directory.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Search paths for the templates directory, checked in order.
# On the live ISO, templates are staged at /opt/arches/templates/.
# In development, they're at <project>/templates/ (relative to the
# installer package: installer/arches_installer/core/template.py -> ../../../../templates).
_TEMPLATES_SEARCH = [
    Path("/opt/arches/templates"),
    Path(__file__).resolve().parents[3] / "templates",
]


@dataclass
class SystemConfig:
    timezone: str = "America/Denver"
    locale: str = "en_US.UTF-8"


@dataclass
class InstallPhases:
    """Package lists separated by installation phase."""

    pacstrap: list[str] = field(default_factory=list)
    override: list[str] = field(default_factory=list)
    firstboot: list[str] = field(default_factory=list)

    @property
    def all_packages(self) -> list[str]:
        """All packages across all phases (for caching, display, etc.)."""
        return self.pacstrap + self.override + self.firstboot


@dataclass
class AnsibleConfig:
    firstboot_roles: list[str] = field(default_factory=list)


@dataclass
class InstallTemplate:
    name: str
    description: str
    system: SystemConfig
    install: InstallPhases
    services: list[str] = field(default_factory=list)
    ansible: AnsibleConfig = field(default_factory=AnsibleConfig)
    # When True, the ISO boots into a graphical desktop (SDDM + Plasma)
    # with a liveuser autologin. When False, the ISO boots to a text
    # console with the TUI installer on tty1.
    graphical: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstallTemplate:
        """Build an InstallTemplate from a parsed TOML dict."""
        meta = data.get("meta", {})
        sys_raw = data.get("system", {})
        svc_raw = data.get("services", {})
        ans_raw = data.get("ansible", {})
        install_raw = data.get("install", {})

        # Support old format: [system] packages = [...]
        # as well as new format: [install.pacstrap] packages = [...]
        old_packages = sys_raw.get("packages", [])
        pacstrap_packages = install_raw.get("pacstrap", {}).get(
            "packages", old_packages
        )

        return cls(
            name=meta.get("name", "Unknown"),
            description=meta.get("description", ""),
            system=SystemConfig(
                timezone=sys_raw.get("timezone", "America/Denver"),
                locale=sys_raw.get("locale", "en_US.UTF-8"),
            ),
            install=InstallPhases(
                pacstrap=pacstrap_packages,
                override=install_raw.get("override", {}).get("packages", []),
                firstboot=install_raw.get("firstboot", {}).get("packages", []),
            ),
            services=svc_raw.get("enable", []),
            ansible=AnsibleConfig(
                firstboot_roles=ans_raw.get("firstboot_roles", []),
            ),
            graphical=meta.get("graphical", False),
        )


def load_template(path: Path) -> InstallTemplate:
    """Load a single template from a TOML file."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return InstallTemplate.from_dict(data)


def _find_templates_dir() -> Path:
    """Locate the templates directory from the search path."""
    for d in _TEMPLATES_SEARCH:
        if d.is_dir():
            return d
    searched = ", ".join(str(d) for d in _TEMPLATES_SEARCH)
    raise FileNotFoundError(f"Templates directory not found (searched: {searched})")


def resolve_template(filename: str) -> Path:
    """Resolve a template filename to its full path in the templates directory.

    Accepts a bare filename like ``"dev-workstation.toml"`` and returns the
    absolute path.  Raises ``FileNotFoundError`` if the file does not exist.
    """
    templates_dir = _find_templates_dir()
    path = templates_dir / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"Template not found: {filename} (looked in {templates_dir})"
        )
    return path


def discover_templates() -> list[InstallTemplate]:
    """Discover all install templates (system templates only).

    Returns templates that define a system workload (have ``[meta]`` with a
    name).  Config files like auto-install.toml and host-install.toml are
    excluded — they reference templates by filename, not define them.
    """
    templates_dir = _find_templates_dir()
    templates: list[InstallTemplate] = []

    for item in templates_dir.iterdir():
        if item.name.endswith(".toml"):
            try:
                tmpl = load_template(item)
                # Only include system templates (have a meaningful name)
                if tmpl.name != "Unknown":
                    templates.append(tmpl)
            except Exception:
                pass  # Skip non-template TOML files (auto-install, etc.)

    return sorted(templates, key=lambda t: t.name)
