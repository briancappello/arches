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
    """Run a command, raising on failure with stderr in the exception."""
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
        # ESP at /boot/efi — GRUB-style. /boot may be a separate partition
        # (ext4 layout) or a directory on the root filesystem (btrfs layout).
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
            "/mnt/boot (Limine) or /mnt/boot/efi (GRUB/btrfs)."
        )
    return errors


def cleanup_mounts() -> None:
    """Unmount everything under MOUNT_ROOT in reverse order.

    Safe to call even if nothing is mounted — ignores errors on individual
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


def prepare_subvolume(
    partition: str,
    esp_partition: str,
    platform,
    mode: str = "alongside",
    subvol_prefix: str = "@arches",
) -> PartitionMap:
    """Prepare btrfs subvolumes for host-install on an existing partition.

    This is the non-destructive alternative to prepare_disk() — it creates
    subvolumes on an existing btrfs filesystem without touching the partition
    table. Used for installing Arches alongside (or replacing) an existing
    Linux system on Apple Silicon.

    Args:
        partition: The btrfs partition device (e.g. /dev/nvme0n1p6).
        esp_partition: The existing ESP device (e.g. /dev/nvme0n1p4).
        platform: PlatformConfig with disk_layout settings.
        mode: "alongside" creates new named subvolumes; "replace" creates
              standard @/@home/@var subvolumes (may conflict with existing).
        subvol_prefix: Prefix for subvolume names in alongside mode.
                       Default "@arches" creates @arches, @arches-home, @arches-var.

    Returns:
        PartitionMap with the partition devices (root and esp).
    """
    from arches_installer.core.platform import PlatformConfig

    assert isinstance(platform, PlatformConfig)
    layout = platform.disk_layout

    if layout.filesystem != "btrfs":
        raise RuntimeError(
            f"prepare_subvolume requires btrfs, but platform uses {layout.filesystem}"
        )

    # Determine subvolume names based on mode
    if mode == "alongside":
        root_subvol = subvol_prefix
        subvol_map = {
            "@home": f"{subvol_prefix}-home",
            "@var": f"{subvol_prefix}-var",
        }
    elif mode == "replace":
        root_subvol = "@"
        subvol_map = {
            "@home": "@home",
            "@var": "@var",
        }
    else:
        raise ValueError(f"Unknown subvolume mode: {mode}")

    # Mount the btrfs top-level (subvolid=5) to create subvolumes
    import tempfile

    top_mount = Path(tempfile.mkdtemp(prefix="arches-btrfs-"))
    top_mount.mkdir(parents=True, exist_ok=True)

    run(["mount", "-o", "subvolid=5", partition, str(top_mount)])
    try:
        if mode == "replace":
            # Delete existing subvolumes if they exist (destructive!)
            for subvol in [root_subvol] + list(subvol_map.values()):
                subvol_path = top_mount / subvol
                if subvol_path.exists():
                    run(["btrfs", "subvolume", "delete", str(subvol_path)])

        # Create the subvolumes
        for subvol in [root_subvol] + list(subvol_map.values()):
            subvol_path = top_mount / subvol
            if not subvol_path.exists():
                run(["btrfs", "subvolume", "create", str(subvol_path)])
    finally:
        run(["umount", str(top_mount)])

    # Mount the subvolumes at MOUNT_ROOT
    opts = f"subvol={root_subvol},{layout.mount_options}"
    MOUNT_ROOT.mkdir(parents=True, exist_ok=True)
    run(["mount", "-o", opts, partition, str(MOUNT_ROOT)])

    # Mount child subvolumes
    child_mounts = {
        "@home": MOUNT_ROOT / "home",
        "@var": MOUNT_ROOT / "var",
    }
    for canonical_name, mount_point in child_mounts.items():
        actual_name = subvol_map.get(canonical_name, canonical_name)
        if actual_name in subvol_map.values() or mode == "replace":
            mount_point.mkdir(parents=True, exist_ok=True)
            opts = f"subvol={actual_name},{layout.mount_options}"
            run(["mount", "-o", opts, partition, str(mount_point)])

    # Mount ESP at /mnt/boot/efi (GRUB-style)
    efi_dir = MOUNT_ROOT / "boot" / "efi"
    efi_dir.mkdir(parents=True, exist_ok=True)
    run(["mount", esp_partition, str(efi_dir)])

    return PartitionMap(
        esp=esp_partition,
        root=partition,
    )


def prepare_disk(device: str, platform) -> PartitionMap:
    """Full disk preparation pipeline for auto-install.

    Uses the platform's disk_layout config to determine the partition scheme.
    Returns a PartitionMap with device paths for each partition role.

    Raises RuntimeError if the platform disallows auto-install (e.g. Apple
    Silicon, where the partition table is managed by the Asahi installer).
    """
    from arches_installer.core.platform import PlatformConfig

    assert isinstance(platform, PlatformConfig)

    if not platform.allow_auto_install:
        raise RuntimeError(
            f"Auto-install is disabled for platform '{platform.name}'. "
            "This platform's disk layout is managed externally and must "
            "not be wiped. Use host-install or manual partitioning."
        )

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

    # Wait for the kernel to create partition device nodes.
    # partprobe forces a re-read of the partition table; udevadm settle
    # waits for udev to finish creating all /dev nodes.
    run(["partprobe", device])
    run(["udevadm", "settle", "--timeout=10"])

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
        # Separate /boot partition (ext4) + ESP at /boot/efi — legacy GRUB+ext4
        run(["mkfs.ext4", "-L", "archboot", parts.boot])
        mount_boot(parts.boot)
        mount_boot_efi(parts.esp)
    elif platform.bootloader.type == "grub":
        # GRUB+btrfs: /boot lives on btrfs (@ subvolume), ESP at /boot/efi only.
        # GRUB reads kernels from btrfs natively.
        mount_boot_efi(parts.esp)
    else:
        # Limine: ESP doubles as /boot
        mount_esp(parts.esp)

    # Mount /home if separate
    if parts.home:
        run(["mkfs.ext4", "-L", "archhome", parts.home])
        mount_home(parts.home)

    return parts
