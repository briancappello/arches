"""Automated (non-interactive) install runner.

Used by `arches-install --auto <config.toml>` to run the full install
pipeline without the TUI. The config file specifies the device, template,
hostname, username, and password — everything the TUI would collect
interactively.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arches_installer.core.bootloader import install_bootloader
from arches_installer.core.disk import prepare_disk
from arches_installer.core.firstboot import inject_firstboot_service
from arches_installer.core.install import install_system
from arches_installer.core.snapper import setup_snapshots
from arches_installer.core.template import InstallTemplate, load_template


@dataclass
class AutoInstallConfig:
    """Configuration for an unattended install."""

    device: str
    template: InstallTemplate
    hostname: str
    username: str
    password: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutoInstallConfig:
        """Build config from a parsed TOML dict."""
        install = data.get("install", {})

        device = install.get("device")
        if not device:
            raise ValueError("install.device is required")

        template_path = install.get("template")
        if not template_path:
            raise ValueError("install.template is required")

        template = load_template(Path(template_path))

        hostname = install.get("hostname", "arches")
        username = install.get("username")
        if not username:
            raise ValueError("install.username is required")

        password = install.get("password")
        if not password:
            raise ValueError("install.password is required")

        return cls(
            device=device,
            template=template,
            hostname=hostname,
            username=username,
            password=password,
        )

    @classmethod
    def from_file(cls, path: Path) -> AutoInstallConfig:
        """Load config from a TOML file."""
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls.from_dict(data)


def log_stdout(msg: str) -> None:
    """Log to stdout, stripping Rich markup for plain text output."""
    # Strip common Rich markup tags for clean CLI output
    clean = msg
    for tag in (
        "[bold cyan]",
        "[/bold cyan]",
        "[bold green]",
        "[/bold green]",
        "[bold red]",
        "[/bold red]",
        "[green]",
        "[/green]",
        "[red]",
        "[/red]",
    ):
        clean = clean.replace(tag, "")
    print(clean, flush=True)


def run_auto_install(config: AutoInstallConfig) -> int:
    """Run the full install pipeline without TUI. Returns exit code."""
    log = log_stdout

    log("== Arches Auto Install ==")
    log(f"  Device:   {config.device}")
    log(f"  Template: {config.template.name}")
    log(f"  Hostname: {config.hostname}")
    log(f"  User:     {config.username}")
    log("")

    try:
        # Phase 1: Disk
        log("-- Phase 1: Disk Setup --")
        esp_part, root_part = prepare_disk(config.device, config.template.disk)
        log("Disk prepared successfully.")

        # Phase 2: System install
        log("-- Phase 2: System Install --")
        install_system(
            config.template,
            config.hostname,
            config.username,
            config.password,
            log=log,
        )
        log("System installed successfully.")

        # Phase 3: Bootloader
        log("-- Phase 3: Bootloader --")
        install_bootloader(
            config.template,
            config.device,
            esp_part,
            root_part,
            log=log,
        )
        log("Bootloader installed.")

        # Phase 4: Snapshots
        if config.template.disk.filesystem == "btrfs":
            log("-- Phase 4: Snapshots --")
            setup_snapshots(config.template, log=log)
            log("Snapshot support configured.")

        # Phase 5: First-boot service
        log("-- Phase 5: First-Boot --")
        inject_firstboot_service(
            config.template,
            config.username,
            log=log,
        )

        log("")
        log("== Installation complete ==")
        return 0

    except Exception as e:
        log(f"\nINSTALL FAILED: {e}")
        import traceback

        traceback.print_exc(file=sys.stderr)
        return 1
