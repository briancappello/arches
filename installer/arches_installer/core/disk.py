"""Disk detection, mounting, and mount validation.

This module provides block device detection, mount inspection, and cleanup
utilities.  Destructive partitioning/formatting operations have moved to
``disk_layout.py`` which uses ``core.run.run()`` for full logging.

The ``PartitionMap`` dataclass is the central data structure that carries
partition device paths and filesystem metadata between the disk layer and
the install pipeline.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MOUNT_ROOT = Path("/mnt")


@dataclass
class BlockDevice:
    """Represents a detected block device."""

    name: str  # e.g. "sda", "nvme0n1"
    path: str  # e.g. "/dev/sda"
    size: str  # human-readable e.g. "500G"
    model: str
    removable: bool
    partitions: list[str]

    @property
    def display(self) -> str:
        return f"{self.path}  {self.size}  {self.model}"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, raising on failure with stderr in the exception.

    This is a lightweight wrapper used by disk operations that don't need
    streaming log output. For operations that need real-time log streaming,
    use ``arches_installer.core.run.run()`` instead.
    """
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)
    except subprocess.CalledProcessError as e:
        # Re-raise with stderr visible so failures aren't silently swallowed
        msg = f"Command {e.cmd!r} returned non-zero exit status {e.returncode}."
        if e.stderr:
            msg += f"\nstderr: {e.stderr.strip()}"
        if e.stdout:
            msg += f"\nstdout: {e.stdout.strip()}"
        raise subprocess.CalledProcessError(
            e.returncode, e.cmd, output=msg, stderr=e.stderr
        ) from None


def detect_block_devices() -> list[BlockDevice]:
    """Detect available block devices via lsblk."""
    result = run(
        [
            "lsblk",
            "-J",
            "-d",
            "-o",
            "NAME,PATH,SIZE,MODEL,RM,TYPE",
        ]
    )
    data = json.loads(result.stdout)
    devices = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        # Skip virtual/non-installable devices
        name = dev.get("name", "")
        if name.startswith(("zram", "loop", "sr", "ram")):
            continue
        # Get partitions for this device
        part_result = run(
            [
                "lsblk",
                "-J",
                "-o",
                "NAME,TYPE",
                dev["path"],
            ]
        )
        part_data = json.loads(part_result.stdout)
        partitions = [
            p["name"]
            for p in part_data.get("blockdevices", [{}])[0].get("children", [])
            if p.get("type") == "part"
        ]
        devices.append(
            BlockDevice(
                name=dev["name"],
                path=dev["path"],
                size=dev.get("size", "?"),
                model=dev.get("model", "").strip() if dev.get("model") else "Unknown",
                removable=dev.get("rm", False),
                partitions=partitions,
            )
        )
    return devices


def detect_single_disk() -> BlockDevice:
    """Detect exactly one non-removable disk for unattended install.

    Returns the single non-removable disk.  Raises ``RuntimeError`` if zero
    or more than one non-removable disk is found.
    """
    all_devs = detect_block_devices()
    disks = [d for d in all_devs if not d.removable]
    if len(disks) == 0:
        raise RuntimeError("No non-removable disks detected")
    if len(disks) > 1:
        names = ", ".join(d.path for d in disks)
        raise RuntimeError(
            f"Multiple non-removable disks detected ({names}). "
            "Auto-install requires exactly one disk."
        )
    return disks[0]


def _part_name(device: str, num: int) -> str:
    """Return partition device path (handles nvme/mmcblk 'p' convention)."""
    if "nvme" in device or "mmcblk" in device:
        return f"{device}p{num}"
    return f"{device}{num}"


@dataclass
class PartitionMap:
    """Maps partition roles to device paths.

    Also carries filesystem metadata so downstream phases (bootloader,
    snapper) can make decisions without needing a separate config object.
    """

    esp: str
    root: str
    boot: str = ""  # empty if ESP doubles as /boot
    home: str = ""  # empty if no separate /home
    root_filesystem: str = ""  # "btrfs", "ext4", etc.
    root_subvolumes: list[str] = field(default_factory=list)  # ["@", "@home", "@var"]


def detect_mounts() -> PartitionMap | None:
    """Detect partitions currently mounted under MOUNT_ROOT.

    Inspects /proc/mounts for MOUNT_ROOT, MOUNT_ROOT/boot, MOUNT_ROOT/boot/efi,
    and MOUNT_ROOT/home to build a PartitionMap. Returns None if root is not
    mounted at MOUNT_ROOT.

    This is used by the manual partitioning flow -- the user partitions, formats,
    and mounts everything themselves, then the installer inspects what's there.
    """
    mounts: dict[str, str] = {}  # mountpoint -> device
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    device_path, mountpoint = parts[0], parts[1]
                    mounts[mountpoint] = device_path
    except OSError:
        return None

    root_str = str(MOUNT_ROOT)
    root_dev = mounts.get(root_str)
    if not root_dev:
        return None

    # Detect ESP: could be at /mnt/boot or /mnt/boot/efi
    boot_efi_dev = mounts.get(f"{root_str}/boot/efi", "")
    boot_dev = mounts.get(f"{root_str}/boot", "")
    home_dev = mounts.get(f"{root_str}/home", "")

    if boot_efi_dev:
        # ESP at /boot/efi -- GRUB-style. /boot may be a separate partition
        # (ext4 layout) or a directory on the root filesystem (btrfs layout).
        return PartitionMap(
            esp=boot_efi_dev,
            root=root_dev,
            boot=boot_dev,
            home=home_dev,
        )
    elif boot_dev:
        # ESP mounted at /boot -- Limine-style (x86-64)
        return PartitionMap(
            esp=boot_dev,
            root=root_dev,
            home=home_dev,
        )
    else:
        # No boot mount detected -- return just root
        return PartitionMap(
            esp="",
            root=root_dev,
            home=home_dev,
        )


def validate_mounts(parts: PartitionMap) -> list[str]:
    """Validate a detected PartitionMap, returning a list of error messages.

    Empty list means the mounts are valid.
    """
    errors = []
    if not parts.root:
        errors.append("No root filesystem mounted at /mnt")
    if not parts.esp:
        errors.append(
            "No ESP detected. Mount your EFI System Partition at "
            "/mnt/boot (Limine) or /mnt/boot/efi (GRUB/btrfs)."
        )
    return errors


def cleanup_mounts() -> None:
    """Unmount everything under MOUNT_ROOT in reverse order.

    Safe to call even if nothing is mounted -- ignores errors on individual
    unmounts so the cleanup is best-effort.
    """
    # Read current mounts and filter to those under MOUNT_ROOT
    mounts: list[str] = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1].startswith(str(MOUNT_ROOT)):
                    mounts.append(parts[1])
    except OSError:
        return

    # Unmount in reverse order (deepest first)
    for mount_point in sorted(mounts, reverse=True):
        try:
            run(["umount", mount_point])
        except subprocess.CalledProcessError:
            pass  # best-effort


# TODO(asahi): prepare_subvolume() previously lived here and used
# platform.disk_layout for btrfs layout details.  It needs to be
# refactored to work with the new DiskLayout model once we pick up
# the Apple Silicon host-install work.
