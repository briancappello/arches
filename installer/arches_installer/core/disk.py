"""Disk partitioning, formatting, and mounting."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
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


def _part_name(device: str, num: int) -> str:
    """Return partition device path (handles nvme/mmcblk 'p' convention)."""
    if "nvme" in device or "mmcblk" in device:
        return f"{device}p{num}"
    return f"{device}{num}"


@dataclass
class PartitionMap:
    """Maps partition roles to device paths."""

    esp: str
    root: str
    boot: str = ""  # empty if ESP doubles as /boot
    home: str = ""  # empty if no separate /home


def partition_disk_x86(device: str, esp_size_mib: int) -> PartitionMap:
    """Partition for x86-64 (Limine): ESP + root. ESP doubles as /boot."""
    run(["sgdisk", "-n", f"1:0:+{esp_size_mib}M", "-t", "1:EF00", device])
    run(["sgdisk", "-n", "2:0:0", "-t", "2:8300", device])
    return PartitionMap(
        esp=_part_name(device, 1),
        root=_part_name(device, 2),
    )


def partition_disk_aarch64(
    device: str,
    esp_size_mib: int,
    boot_size_mib: int,
) -> PartitionMap:
    """Partition for aarch64 (GRUB): ESP + /boot + root + /home."""
    run(["sgdisk", "-n", f"1:0:+{esp_size_mib}M", "-t", "1:EF00", device])
    run(["sgdisk", "-n", f"2:0:+{boot_size_mib}M", "-t", "2:8300", device])
    # Root gets 50% of remaining space, /home gets the rest
    run(["sgdisk", "-n", "3:0:+50%", "-t", "3:8300", device])
    run(["sgdisk", "-n", "4:0:0", "-t", "4:8300", device])
    return PartitionMap(
        esp=_part_name(device, 1),
        boot=_part_name(device, 2),
        root=_part_name(device, 3),
        home=_part_name(device, 4),
    )


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


def mount_boot(partition: str) -> None:
    """Mount a separate /boot partition at MOUNT_ROOT/boot."""
    boot = MOUNT_ROOT / "boot"
    boot.mkdir(parents=True, exist_ok=True)
    run(["mount", partition, str(boot)])


def mount_boot_efi(partition: str) -> None:
    """Mount ESP at MOUNT_ROOT/boot/efi (when /boot is a separate partition)."""
    efi = MOUNT_ROOT / "boot" / "efi"
    efi.mkdir(parents=True, exist_ok=True)
    run(["mount", partition, str(efi)])


def mount_home(partition: str) -> None:
    """Mount /home partition at MOUNT_ROOT/home."""
    home = MOUNT_ROOT / "home"
    home.mkdir(parents=True, exist_ok=True)
    run(["mount", partition, str(home)])


def detect_mounts() -> PartitionMap | None:
    """Detect partitions currently mounted under MOUNT_ROOT.

    Inspects /proc/mounts for MOUNT_ROOT, MOUNT_ROOT/boot, MOUNT_ROOT/boot/efi,
    and MOUNT_ROOT/home to build a PartitionMap. Returns None if root is not
    mounted at MOUNT_ROOT.

    This is used by the manual partitioning flow — the user partitions, formats,
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
        # Separate /boot and /boot/efi — GRUB-style (aarch64)
        return PartitionMap(
            esp=boot_efi_dev,
            root=root_dev,
            boot=boot_dev,
            home=home_dev,
        )
    elif boot_dev:
        # ESP mounted at /boot — Limine-style (x86-64)
        return PartitionMap(
            esp=boot_dev,
            root=root_dev,
            home=home_dev,
        )
    else:
        # No boot mount detected — return just root
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
            "/mnt/boot (Limine) or /mnt/boot/efi (GRUB)."
        )
    return errors


def prepare_disk(device: str, platform) -> PartitionMap:
    """Full disk preparation pipeline for auto-install.

    Uses the platform's disk_layout config to determine the partition scheme.
    Returns a PartitionMap with device paths for each partition role.
    """
    from arches_installer.core.platform import PlatformConfig

    assert isinstance(platform, PlatformConfig)
    layout = platform.disk_layout

    wipe_disk(device)

    if layout.boot_size_mib > 0:
        # aarch64-style: ESP + /boot + root + /home
        parts = partition_disk_aarch64(
            device, layout.esp_size_mib, layout.boot_size_mib
        )
    else:
        # x86-64-style: ESP (doubles as /boot) + root
        parts = partition_disk_x86(device, layout.esp_size_mib)

    # Format and mount
    format_esp(parts.esp)

    if layout.filesystem == "btrfs":
        format_root_btrfs(parts.root)
        if layout.subvolumes:
            create_btrfs_subvolumes(parts.root, layout.subvolumes)
            mount_btrfs(parts.root, layout.subvolumes, layout.mount_options)
        else:
            MOUNT_ROOT.mkdir(parents=True, exist_ok=True)
            run(["mount", "-o", layout.mount_options, parts.root, str(MOUNT_ROOT)])
    elif layout.filesystem == "ext4":
        format_root_ext4(parts.root)
        mount_ext4(parts.root, layout.mount_options)

    # Mount /boot (separate) or ESP as /boot
    if parts.boot:
        run(["mkfs.ext4", "-L", "archboot", parts.boot])
        mount_boot(parts.boot)
        mount_boot_efi(parts.esp)
    else:
        mount_esp(parts.esp)

    # Mount /home if separate
    if parts.home:
        run(["mkfs.ext4", "-L", "archhome", parts.home])
        mount_home(parts.home)

    return parts
