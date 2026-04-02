"""Shared install pipeline -- the single source of truth for the install sequence.

Both the TUI progress screen and auto-install mode call ``run_install_pipeline``
to execute the install.  This eliminates duplication and ensures logging,
error handling, and phase ordering are consistent regardless of the entry point.
"""

from __future__ import annotations

from dataclasses import dataclass

from arches_installer.core.bootloader import install_bootloader
from arches_installer.core.disk import PartitionMap
from arches_installer.core.disk_layout import (
    DiskLayout,
    RaidBackend,
    RaidConfig,
    RaidLevel,
    apply_disk_layout,
    mirror_esp,
    setup_raid_mdadm,
    _part_name,
)
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
    # If set, disk_layout is ignored and these mounts are used as-is.
    partition_map: PartitionMap | None = None
    # Disk layout to apply (layout-based partitioning).
    # Used when partition_map is None.
    disk_layout: DiskLayout | None = None
    # Optional RAID configuration (btrfs multi-device or mdadm).
    raid_config: RaidConfig | None = None


def run_install_pipeline(
    params: InstallParams,
    log: LogCallback,
) -> PartitionMap:
    """Run the full install pipeline.

    Executes all 5 phases in order:
      1. Disk setup (manual map, layout-based, or RAID + layout)
      2. System install (pacstrap + chroot configuration)
      3. Bootloader (Limine or GRUB) + optional ESP mirroring
      4. Snapshots (if btrfs root)
      5. First-boot service (Ansible + firstboot packages)

    Returns the partition map (useful for the caller to store).
    Raises on failure -- the caller should catch and handle.
    """
    platform = params.platform
    template = params.template

    # Phase 1: Disk
    log("[bold cyan]== Phase 1: Disk Setup ==[/bold cyan]")
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
    elif params.disk_layout is not None:
        primary_device = params.device
        extra_devices: list[str] = []

        if params.raid_config is not None:
            if params.raid_config.backend == RaidBackend.MDADM:
                # mdadm: assemble array first, then apply layout to /dev/md0
                log(
                    f"[bold cyan]Setting up mdadm "
                    f"RAID{params.raid_config.level.value}...[/bold cyan]"
                )
                primary_device = setup_raid_mdadm(params.raid_config, log=log)
            elif params.raid_config.backend == RaidBackend.BTRFS:
                # btrfs RAID: apply layout to all disks simultaneously
                log(
                    f"[bold cyan]Preparing btrfs "
                    f"RAID{params.raid_config.level.value} across "
                    f"{len(params.raid_config.devices)} devices...[/bold cyan]"
                )
                primary_device = params.raid_config.devices[0]
                extra_devices = params.raid_config.devices[1:]

        parts = apply_disk_layout(
            primary_device,
            params.disk_layout,
            extra_devices=extra_devices or None,
            raid_config=params.raid_config,
            log=log,
        )
        log("[green]Disk prepared successfully.[/green]")
    else:
        raise RuntimeError(
            "No partition_map or disk_layout provided. "
            "Either manually partition and mount, or select a disk layout."
        )

    # Phase 2: System install
    log("[bold cyan]== Phase 2: System Install ==[/bold cyan]")
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
    log("[bold cyan]== Phase 3: Bootloader ==[/bold cyan]")
    install_bootloader(
        platform,
        params.device,
        parts.esp,
        parts.root,
        parts=parts,
        log=log,
    )
    log("[green]Bootloader installed.[/green]")

    # Phase 3 epilogue: mirror ESP for btrfs RAID 1/10
    if (
        params.raid_config is not None
        and params.raid_config.backend == RaidBackend.BTRFS
        and params.raid_config.level in (RaidLevel.RAID1, RaidLevel.RAID10)
    ):
        log("[bold cyan]-- ESP Mirroring --[/bold cyan]")
        secondary_esps = [_part_name(d, 1) for d in params.raid_config.devices[1:]]
        mirror_esp(parts.esp, secondary_esps, log=log)
    elif (
        params.raid_config is not None
        and params.raid_config.backend == RaidBackend.BTRFS
        and params.raid_config.level == RaidLevel.RAID0
    ):
        log(
            "[yellow]RAID 0: ESP is on primary disk only. "
            "No mirroring (RAID 0 has no redundancy).[/yellow]"
        )

    # Phase 4: Snapshots (if btrfs root)
    if parts.root_filesystem == "btrfs":
        log("[bold cyan]== Phase 4: Snapshots ==[/bold cyan]")
        setup_snapshots(platform, parts=parts, log=log)
        log("[green]Snapshot support configured.[/green]")

    # Phase 5: First-boot service
    log("[bold cyan]== Phase 5: First-Boot ==[/bold cyan]")
    inject_firstboot_service(
        template,
        params.username,
        log=log,
    )

    log("")
    log("== Installation complete ==")

    return parts
