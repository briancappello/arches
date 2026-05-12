"""Shared install pipeline -- the single source of truth for the install sequence.

Both the TUI progress screen and auto-install mode call ``run_install_pipeline``
to execute the install.  This eliminates duplication and ensures logging,
error handling, and phase ordering are consistent regardless of the entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from arches_installer.core.bootloader import install_bootloader
from arches_installer.core.disk import PartitionMap
from arches_installer.core.disk_layout import (
    DiskLayout,
    RaidBackend,
    RaidConfig,
    RaidLevel,
    ResolvedDiskRoles,
    apply_disk_layout,
    apply_disk_layout_resolved,
    mirror_esp,
    setup_raid_mdadm,
    write_disk_roles_state,
    _part_name,
)
from arches_installer.core.firstboot import inject_firstboot_service
from arches_installer.core.hardware import HardwareConfig
from arches_installer.core.install import append_swap_fstab_entries, install_system
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
    # Used by the legacy single-disk-or-homogeneous-RAID flow only.
    # Multi-disk role-based layouts use ``resolved_disk_roles`` instead.
    raid_config: RaidConfig | None = None
    # Resolved multi-disk role assignments. When set, the disk layout
    # is applied via :func:`apply_disk_layout_resolved` and the legacy
    # ``device`` + ``raid_config`` parameters are ignored.
    resolved_disk_roles: ResolvedDiskRoles | None = None
    # Hardware configuration (machine profile + auto-detected quirks).
    # When set, hardware-specific packages, services, modprobe/udev
    # configs, and Ansible roles are merged into the install.
    hardware: HardwareConfig | None = None
    # Extra Ansible variables from auto-install config [ansible_vars].
    # Forwarded as -e key=value to the firstboot ansible-playbook run.
    ansible_vars: dict[str, str] | None = None


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
        if params.resolved_disk_roles is not None:
            # Multi-disk role-based path: a separate resolver step
            # already mapped each layout role to physical disks.
            log(
                "[bold cyan]Applying multi-disk layout with "
                f"{len(params.resolved_disk_roles.assignments)} role(s)...[/bold cyan]"
            )
            for role, devs in params.resolved_disk_roles.assignments.items():
                paths = ", ".join(d.path for d in devs)
                log(f"  {role}: {paths}")
            parts = apply_disk_layout_resolved(
                params.disk_layout,
                params.resolved_disk_roles,
                log=log,
            )
            log("[green]Disk prepared successfully.[/green]")
        else:
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
        hardware=params.hardware,
        log=log,
    )
    # genfstab runs inside install_system before chroot config; swap
    # partitions need to be appended afterward because genfstab reads
    # /proc/swaps which is empty during install.
    if parts.swap_partitions:
        append_swap_fstab_entries(parts.swap_partitions, log=log)
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
        platform=platform,
        hardware=params.hardware,
        extra_vars=params.ansible_vars,
        log=log,
    )

    # Persist resolved disk-roles state so arches-hardware-rescan can
    # validate disk presence on every subsequent boot. Only meaningful
    # when the multi-disk role path was used; harmless otherwise.
    if params.resolved_disk_roles is not None:
        from arches_installer.core.disk import MOUNT_ROOT

        write_disk_roles_state(params.resolved_disk_roles, MOUNT_ROOT, log=log)

    # Copy install log to the installed system for post-mortem debugging
    _persist_install_log(log)

    log("")
    log("== Installation complete ==")

    return parts


def _persist_install_log(log: LogCallback | None = None) -> None:
    """Copy the install log from the live ISO tmpfs into the installed system."""
    from arches_installer.core.disk import MOUNT_ROOT
    from arches_installer.core.run import _log

    src = Path("/var/log/arches-install.log")
    if not src.exists():
        return

    target_log = MOUNT_ROOT / "var" / "log" / "arches-install.log"
    try:
        target_log.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.copy2(src, target_log)
        _log(f"Install log saved to {target_log}.", log)
    except OSError as e:
        _log(f"WARNING: Could not save install log: {e}", log)
