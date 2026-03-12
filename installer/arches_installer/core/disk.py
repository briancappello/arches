"""Disk partitioning, formatting, and mounting."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from arches_installer.core.template import DiskConfig

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
    """Run a command, raising on failure."""
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


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


def wipe_disk(device: str) -> None:
    """Wipe partition table and create a fresh GPT label."""
    run(["wipefs", "--all", "--force", device])
    run(["sgdisk", "--zap-all", device])
    run(["sgdisk", "--clear", device])


def partition_disk(device: str, config: DiskConfig) -> tuple[str, str]:
    """Partition a disk with ESP + root. Returns (esp_part, root_part).

    Handles both /dev/sdX and /dev/nvmeXnY naming conventions.
    """
    esp_size = config.esp_size_mib

    # Create ESP partition
    run(["sgdisk", "-n", f"1:0:+{esp_size}M", "-t", "1:EF00", device])
    # Create root partition (rest of disk)
    run(["sgdisk", "-n", "2:0:0", "-t", "2:8300", device])

    # Determine partition naming convention
    if "nvme" in device or "mmcblk" in device:
        esp_part = f"{device}p1"
        root_part = f"{device}p2"
    else:
        esp_part = f"{device}1"
        root_part = f"{device}2"

    return esp_part, root_part


def format_esp(partition: str) -> None:
    """Format the ESP as FAT32."""
    run(["mkfs.fat", "-F", "32", "-n", "ESP", partition])


def format_root_ext4(partition: str) -> None:
    """Format root as ext4."""
    run(["mkfs.ext4", "-L", "archroot", partition])


def format_root_btrfs(partition: str) -> None:
    """Format root as btrfs."""
    run(["mkfs.btrfs", "-f", "-L", "archroot", partition])


def create_btrfs_subvolumes(
    partition: str,
    subvolumes: list[str],
) -> None:
    """Create btrfs subvolumes on the given partition.

    Temporarily mounts the partition, creates subvolumes, then unmounts.
    """
    tmp_mount = Path("/mnt/btrfs-setup")
    tmp_mount.mkdir(parents=True, exist_ok=True)

    run(["mount", partition, str(tmp_mount)])
    try:
        for subvol in subvolumes:
            run(["btrfs", "subvolume", "create", str(tmp_mount / subvol)])
    finally:
        run(["umount", str(tmp_mount)])


def mount_btrfs(
    partition: str,
    subvolumes: list[str],
    mount_options: str,
) -> None:
    """Mount btrfs subvolumes in the correct order at MOUNT_ROOT."""
    # Mount @ as root
    opts = f"subvol=@,{mount_options}"
    MOUNT_ROOT.mkdir(parents=True, exist_ok=True)
    run(["mount", "-o", opts, partition, str(MOUNT_ROOT)])

    # Mount remaining subvolumes
    subvol_mounts = {
        "@home": MOUNT_ROOT / "home",
        "@var": MOUNT_ROOT / "var",
        "@snapshots": MOUNT_ROOT / ".snapshots",
    }

    for subvol in subvolumes:
        if subvol == "@":
            continue
        mount_point = subvol_mounts.get(subvol)
        if mount_point is None:
            # Fallback: mount at /<subvol_name_without_@>
            mount_point = MOUNT_ROOT / subvol.lstrip("@")
        mount_point.mkdir(parents=True, exist_ok=True)
        opts = f"subvol={subvol},{mount_options}"
        run(["mount", "-o", opts, partition, str(mount_point)])


def mount_ext4(partition: str, mount_options: str) -> None:
    """Mount ext4 root at MOUNT_ROOT."""
    MOUNT_ROOT.mkdir(parents=True, exist_ok=True)
    run(["mount", "-o", mount_options, partition, str(MOUNT_ROOT)])


def mount_esp(partition: str) -> None:
    """Mount ESP at MOUNT_ROOT/boot."""
    boot = MOUNT_ROOT / "boot"
    boot.mkdir(parents=True, exist_ok=True)
    run(["mount", partition, str(boot)])


def prepare_disk(device: str, config: DiskConfig) -> tuple[str, str]:
    """Full disk preparation pipeline. Returns (esp_part, root_part)."""
    wipe_disk(device)
    esp_part, root_part = partition_disk(device, config)

    format_esp(esp_part)

    if config.filesystem == "btrfs":
        format_root_btrfs(root_part)
        if config.subvolumes:
            create_btrfs_subvolumes(root_part, config.subvolumes)
            mount_btrfs(root_part, config.subvolumes, config.mount_options)
        else:
            MOUNT_ROOT.mkdir(parents=True, exist_ok=True)
            run(["mount", "-o", config.mount_options, root_part, str(MOUNT_ROOT)])
    elif config.filesystem == "ext4":
        format_root_ext4(root_part)
        mount_ext4(root_part, config.mount_options)

    mount_esp(esp_part)
    return esp_part, root_part
