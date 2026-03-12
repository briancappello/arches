"""Snapper configuration for btrfs snapshot management."""

from __future__ import annotations

import subprocess
from typing import Callable

from arches_installer.core.disk import MOUNT_ROOT
from arches_installer.core.template import InstallTemplate

LogCallback = Callable[[str], None]


def _log(msg: str, callback: LogCallback | None = None) -> None:
    if callback:
        callback(msg)


def chroot_run(
    cmd: list[str],
    log: LogCallback | None = None,
) -> subprocess.CompletedProcess:
    """Run a command inside the target chroot."""
    _log(f"$ {' '.join(cmd)}", log)
    result = subprocess.run(
        ["arch-chroot", str(MOUNT_ROOT)] + cmd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _log(f"ERROR: {result.stderr.strip()}", log)
        result.check_returncode()
    return result


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
    template: InstallTemplate,
    log: LogCallback | None = None,
) -> None:
    """Set up snapper for btrfs snapshot management."""
    if template.disk.filesystem != "btrfs":
        _log("Filesystem is not btrfs, skipping snapper setup.", log)
        return

    _log("Configuring snapper...", log)

    # The .snapshots subvolume should already exist from disk setup.
    # Snapper expects to create its own .snapshots directory, so we
    # need to unmount it, let snapper create-config, then re-mount.

    # Create snapper config for root
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
    template: InstallTemplate,
    log: LogCallback | None = None,
) -> None:
    """Set up limine-snapper-sync for bootable snapshots."""
    if not template.bootloader.snapshot_boot:
        _log("Snapshot boot not enabled, skipping.", log)
        return

    _log("Configuring snapshot boot (limine-snapper-sync)...", log)

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
    template: InstallTemplate,
    log: LogCallback | None = None,
) -> None:
    """Full snapshot setup pipeline."""
    configure_snapper(template, log)
    configure_snapshot_boot(template, log)
