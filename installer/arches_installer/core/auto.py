"""Automated (non-interactive) install runner.

Used by `arches-install --auto <config.toml>` to run the full install
pipeline without the TUI. The config file specifies the template,
hostname, username, and password — everything the TUI would collect
interactively. The target disk is auto-detected (must be exactly one
non-removable disk). Disk layout and bootloader come from the platform
config.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arches_installer.core.template import (
    InstallTemplate,
    load_template,
    resolve_template,
)


@dataclass
class AutoInstallConfig:
    """Configuration for an unattended install."""

    template: InstallTemplate
    hostname: str
    username: str
    password: str
    reboot: bool
    shutdown: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutoInstallConfig:
        """Build config from a parsed TOML dict."""
        install = data.get("install", {})

        template_name = install.get("template")
        if not template_name:
            raise ValueError("install.template is required")

        template = load_template(resolve_template(template_name))

        hostname = install.get("hostname", "arches")
        username = install.get("username")
        if not username:
            raise ValueError("install.username is required")

        password = install.get("password")
        if not password:
            raise ValueError("install.password is required")

        reboot = install.get("reboot", False)
        shutdown = install.get("shutdown", False)

        return cls(
            template=template,
            hostname=hostname,
            username=username,
            password=password,
            reboot=reboot,
            shutdown=shutdown,
        )

    @classmethod
    def from_file(cls, path: Path) -> AutoInstallConfig:
        """Load config from a TOML file."""
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls.from_dict(data)
