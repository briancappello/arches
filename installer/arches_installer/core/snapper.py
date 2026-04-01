"""Snapper configuration for btrfs snapshot management."""

from __future__ import annotations

import re
import subprocess

from arches_installer.core.disk import MOUNT_ROOT
from arches_installer.core.platform import PlatformConfig
from arches_installer.core.run import LogCallback, _log, chroot_run


# Default snapper config for root subvolume
ROOT_SNAPPER_CONFIG = """\
# Arches snapper config for root
SUBVOLUME="/"
FSTYPE="btrfs"

# Snapshot retention
TIMELINE_CREATE="yes"
TIMELINE_CLEANUP="yes"
TIMELINE_MIN_AGE="1800"
TIMELINE_LIMIT_HOURLY="5"
TIMELINE_LIMIT_DAILY="7"
TIMELINE_LIMIT_WEEKLY="4"
TIMELINE_LIMIT_MONTHLY="3"
TIMELINE_LIMIT_YEARLY="0"

# Number-based cleanup
NUMBER_CLEANUP="yes"
NUMBER_MIN_AGE="1800"
NUMBER_LIMIT="50"
NUMBER_LIMIT_IMPORTANT="10"

# Users allowed to interact with snapshots
ALLOW_USERS=""
ALLOW_GROUPS="wheel"
"""


def configure_snapper(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Set up snapper for btrfs snapshot management."""
    if platform.disk_layout.filesystem != "btrfs":
        _log("Filesystem is not btrfs, skipping snapper setup.", log)
        return

    _log("Configuring snapper...", log)

    # Let snapper create its own .snapshots nested subvolume under @.
    # This is the standard layout expected by snapper and limine-snapper-sync.
    try:
        chroot_run(["snapper", "--no-dbus", "create-config", "/"], log=log)
    except subprocess.CalledProcessError:
        _log("snapper create-config failed, config may already exist.", log)

    # Write our custom snapper config
    snapper_conf = MOUNT_ROOT / "etc" / "snapper" / "configs" / "root"
    snapper_conf.parent.mkdir(parents=True, exist_ok=True)
    snapper_conf.write_text(ROOT_SNAPPER_CONFIG)
    _log("Wrote snapper root config.", log)

    # Ensure snapper is listed in /etc/conf.d/snapper
    snapper_confd = MOUNT_ROOT / "etc" / "conf.d" / "snapper"
    snapper_confd.parent.mkdir(parents=True, exist_ok=True)
    snapper_confd.write_text('SNAPPER_CONFIGS="root"\n')

    _log("Snapper configured.", log)


def configure_snapshot_boot(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Set up bootable snapshot entries.

    Dispatches based on bootloader type:
    - Limine: uses limine-snapper-sync to copy kernels and write boot entries
    - GRUB: uses grub-btrfs to auto-regenerate grub.cfg with snapshot entries
    """
    if not platform.bootloader.snapshot_boot:
        _log("Snapshot boot not enabled, skipping.", log)
        return

    if platform.bootloader.type == "grub":
        _configure_snapshot_boot_grub(platform, log)
    else:
        _configure_snapshot_boot_limine(platform, log)


def _configure_snapshot_boot_grub(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Set up grub-btrfs for bootable snapshots via GRUB.

    grub-btrfs watches /.snapshots for new snapper snapshots and
    auto-regenerates grub.cfg to include snapshot boot entries.
    """
    _log("Configuring snapshot boot (grub-btrfs)...", log)

    # Enable grub-btrfsd — the daemon that watches for snapshot changes
    # and regenerates grub.cfg automatically.
    try:
        chroot_run(
            ["systemctl", "enable", "grub-btrfsd.service"],
            log=log,
        )
        _log("Enabled grub-btrfsd service.", log)
    except subprocess.CalledProcessError:
        _log("WARNING: Could not enable grub-btrfsd service.", log)
        _log("You may need to install grub-btrfs and enable it manually.", log)


def _configure_snapshot_boot_limine(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Set up limine-snapper-sync for bootable snapshots via Limine."""
    _log("Configuring snapshot boot (limine-snapper-sync)...", log)

    # Update limine-snapper-sync config for our OS name.
    # The snapshot path defaults to /@/.snapshots which matches our nested layout.
    conf = MOUNT_ROOT / "etc" / "limine-snapper-sync.conf"
    if conf.exists():
        content = conf.read_text()
        # Replace TARGET_OS_NAME regardless of its current value
        content = re.sub(
            r"^#?TARGET_OS_NAME=.*$",
            'TARGET_OS_NAME="Arches Linux"',
            content,
            flags=re.MULTILINE,
        )
        conf.write_text(content)
        _log("Updated limine-snapper-sync.conf.", log)

    # limine-snapper-sync should be pre-built in the arches-local repo
    # and installed via pacstrap. Enable its service.
    try:
        chroot_run(
            [
                "systemctl",
                "enable",
                "limine-snapper-sync.service",
            ],
            log=log,
        )
        _log("Enabled limine-snapper-sync service.", log)
    except subprocess.CalledProcessError:
        _log("WARNING: Could not enable limine-snapper-sync service.", log)
        _log("You may need to install and enable it manually.", log)


def setup_snapshots(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Full snapshot setup pipeline."""
    configure_snapper(platform, log)
    configure_snapshot_boot(platform, log)
