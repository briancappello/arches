"""Shared install pipeline — the single source of truth for the install sequence.

Both the TUI progress screen and auto-install mode call ``run_install_pipeline``
to execute the install.  This eliminates duplication and ensures logging,
error handling, and phase ordering are consistent regardless of the entry point.
"""

from __future__ import annotations

from dataclasses import dataclass

from arches_installer.core.bootloader import install_bootloader
from arches_installer.core.disk import PartitionMap, prepare_disk
from arches_installer.core.firstboot import inject_firstboot_service
from arches_installer.core.install import install_system
from arches_installer.core.platform import PlatformConfig
from arches_installer.core.run import LogCallback
from arches_installer.core.snapper import setup_snapshots
from arches_installer.core.template import InstallTemplate


@dataclass
class InstallParams:
    """All parameters needed to run the install pipeline."""

    platform: PlatformConfig
    template: InstallTemplate
    device: str
    hostname: str
    username: str
    password: str
    # Pre-existing partition map (manual partitioning).
    # If None, prepare_disk() auto-partitions the device.
    partition_map: PartitionMap | None = None


def run_install_pipeline(
    params: InstallParams,
    log: LogCallback,
) -> PartitionMap:
    """Run the full install pipeline.

    Executes all 5 phases in order:
      1. Disk setup (auto-partition or use manual map)
      2. System install (pacstrap + chroot configuration)
      3. Bootloader (Limine or GRUB)
      4. Snapshots (if btrfs)
      5. First-boot service (Ansible + firstboot packages)

    Returns the partition map (useful for the caller to store).
    Raises on failure — the caller should catch and handle.
    """
    platform = params.platform
    template = params.template

    # Phase 1: Disk
    log("[bold cyan]── Phase 1: Disk Setup ──[/bold cyan]")
    if params.partition_map is not None:
        parts = params.partition_map
        log("Using manually prepared mounts:")
        log(f"  Root: {parts.root}")
        log(f"  ESP:  {parts.esp}")
        if parts.boot:
            log(f"  Boot: {parts.boot}")
        if parts.home:
            log(f"  Home: {parts.home}")
        log("[green]Manual mounts verified.[/green]")
    else:
        parts = prepare_disk(params.device, platform)
        log("[green]Disk prepared successfully.[/green]")

    # Phase 2: System install
    log("[bold cyan]── Phase 2: System Install ──[/bold cyan]")
    install_system(
        platform,
        template,
        params.hostname,
        params.username,
        params.password,
        log=log,
    )
    log("[green]System installed successfully.[/green]")

    # Phase 3: Bootloader
    log("[bold cyan]── Phase 3: Bootloader ──[/bold cyan]")
    install_bootloader(
        platform,
        params.device,
        parts.esp,
        parts.root,
        log=log,
    )
    log("[green]Bootloader installed.[/green]")

    # Phase 4: Snapshots (if btrfs platform)
    if platform.disk_layout.filesystem == "btrfs":
        log("[bold cyan]── Phase 4: Snapshots ──[/bold cyan]")
        setup_snapshots(platform, log=log)
        log("[green]Snapshot support configured.[/green]")

    # Phase 5: First-boot service
    log("[bold cyan]── Phase 5: First-Boot ──[/bold cyan]")
    inject_firstboot_service(
        template,
        params.username,
        log=log,
    )

    log("")
    log("== Installation complete ==")

    return parts
