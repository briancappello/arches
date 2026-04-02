"""Automated (non-interactive) install runner.

Used by `arches-install --auto <config.toml>` to run the full install
pipeline without the TUI. The config file specifies the template,
hostname, username, and password — everything the TUI would collect
interactively. The target disk is auto-detected (must be exactly one
non-removable disk). Disk layout and bootloader come from the platform
config.

Optional ``[wifi]`` and ``[network]`` tables allow unattended WiFi
connection and/or wired static IP configuration before the install
begins.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arches_installer.core.disk_layout import (
    DiskLayout,
    load_disk_layout,
    resolve_disk_layout,
)
from arches_installer.core.template import (
    InstallTemplate,
    load_template,
    resolve_template,
)


@dataclass
class WifiConfig:
    """WiFi connection settings for auto-install."""

    ssid: str
    psk: str | None = None  # None for open networks
    static_ip: str | None = None  # e.g. "192.168.1.50/24"
    gateway: str | None = None
    dns: list[str] = field(default_factory=list)


@dataclass
class WiredConfig:
    """Wired static IP settings for auto-install."""

    interface: str  # e.g. "eth0"
    static_ip: str  # e.g. "192.168.1.50/24"
    gateway: str
    dns: list[str] = field(default_factory=list)


@dataclass
class AutoInstallConfig:
    """Configuration for an unattended install."""

    template: InstallTemplate
    disk_layout: DiskLayout
    hostname: str
    username: str
    password: str
    reboot: bool
    shutdown: bool
    wifi: WifiConfig | None = None
    wired: WiredConfig | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutoInstallConfig:
        """Build config from a parsed TOML dict."""
        install = data.get("install", {})

        template_name = install.get("template")
        if not template_name:
            raise ValueError("install.template is required")

        template = load_template(resolve_template(template_name))

        # Disk layout — defaults to "basic.toml" if not specified
        disk_layout_name = install.get("disk_layout", "basic.toml")
        if not disk_layout_name.endswith(".toml"):
            disk_layout_name = f"{disk_layout_name}.toml"
        disk_layout = load_disk_layout(resolve_disk_layout(disk_layout_name))

        hostname = install.get("hostname", "arches")
        username = install.get("username")
        if not username:
            raise ValueError("install.username is required")

        password = install.get("password")
        if not password:
            raise ValueError("install.password is required")

        reboot = install.get("reboot", False)
        shutdown = install.get("shutdown", False)

        # Optional WiFi configuration
        wifi = None
        wifi_data = data.get("wifi")
        if wifi_data:
            wifi_ssid = wifi_data.get("ssid")
            if not wifi_ssid:
                raise ValueError("[wifi] table requires ssid")
            wifi = WifiConfig(
                ssid=wifi_ssid,
                psk=wifi_data.get("psk"),
                static_ip=wifi_data.get("static_ip"),
                gateway=wifi_data.get("gateway"),
                dns=wifi_data.get("dns", []),
            )

        # Optional wired static IP configuration
        wired = None
        wired_data = data.get("network")
        if wired_data:
            iface = wired_data.get("interface")
            if not iface:
                raise ValueError("[network] table requires interface")
            static = wired_data.get("static_ip")
            if not static:
                raise ValueError("[network] table requires static_ip")
            gw = wired_data.get("gateway")
            if not gw:
                raise ValueError("[network] table requires gateway")
            wired = WiredConfig(
                interface=iface,
                static_ip=static,
                gateway=gw,
                dns=wired_data.get("dns", []),
            )

        return cls(
            template=template,
            disk_layout=disk_layout,
            hostname=hostname,
            username=username,
            password=password,
            reboot=reboot,
            shutdown=shutdown,
            wifi=wifi,
            wired=wired,
        )

    @classmethod
    def from_file(cls, path: Path) -> AutoInstallConfig:
        """Load config from a TOML file."""
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls.from_dict(data)
