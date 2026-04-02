"""Disk layout loading, application, and RAID management.

A disk layout defines a partition scheme: a list of partition specs
(filesystem, size, mount point, subvolumes) that are applied to a block
device.  Layouts are defined in TOML files under ``disk-layouts/``.

This module also handles btrfs multi-device RAID and mdadm RAID setup,
and ESP mirroring for RAID 1/10 configurations.
"""

from __future__ import annotations

import enum
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arches_installer.core.disk import (
    MOUNT_ROOT,
    PartitionMap,
    _part_name,
)
from arches_installer.core.run import LogCallback, _log
from arches_installer.core.run import run as logged_run

# Search paths for the disk-layouts directory, checked in order.
# On the live ISO, layouts are staged at /opt/arches/disk-layouts/.
# In development, they're at <project>/disk-layouts/ (relative to the
# installer package: installer/arches_installer/core/disk_layout.py -> ../../../../disk-layouts).
_LAYOUTS_SEARCH = [
    Path("/opt/arches/disk-layouts"),
    Path(__file__).resolve().parents[3] / "disk-layouts",
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class SubvolumeSpec:
    """A btrfs subvolume within a partition."""

    name: str  # e.g. "@", "@home", "@var"
    mount_point: str | None = None  # e.g. "/", "/home", "/var"; None = not mounted


@dataclass
class PartitionSpec:
    """A single partition in a disk layout."""

    size: str  # "2G", "100G", "*" (fill rest)
    filesystem: str = ""  # "vfat", "ext4", "btrfs", "" = raw/unformatted
    mount_point: str | None = None  # "/boot", "/", "/home", None = not mounted
    label: str = ""
    mount_options: str = ""
    subvolumes: list[SubvolumeSpec] = field(default_factory=list)


@dataclass
class DiskLayout:
    """A complete disk layout specification loaded from TOML."""

    name: str
    description: str
    bootloaders: list[str]  # ["limine"], ["grub"], ["limine", "grub"]
    partitions: list[PartitionSpec]
    path: Path | None = None  # source TOML file path


class RaidLevel(enum.Enum):
    """Supported RAID levels."""

    RAID0 = 0
    RAID1 = 1
    RAID10 = 10


class RaidBackend(enum.Enum):
    """RAID implementation backend."""

    MDADM = "mdadm"
    BTRFS = "btrfs"


@dataclass
class RaidConfig:
    """RAID configuration for multi-disk setups."""

    level: RaidLevel
    backend: RaidBackend
    devices: list[str]  # all physical device paths


# ---------------------------------------------------------------------------
# Size parsing
# ---------------------------------------------------------------------------

# Matches "2G", "512M", "100G", etc. — number + unit
_SIZE_RE = re.compile(r"^(\d+)([MGT])$", re.IGNORECASE)


def parse_size_spec(spec: str) -> str:
    """Convert a human-readable size spec to an sgdisk size argument.

    "2G"   -> "+2G"
    "512M" -> "+512M"
    "*"    -> "0"  (fill remaining space)

    Raises ValueError for unrecognized formats.
    """
    if spec == "*":
        return "0"
    m = _SIZE_RE.match(spec)
    if not m:
        raise ValueError(
            f"Invalid partition size: {spec!r}. "
            f"Expected a number with M/G/T suffix (e.g. '2G', '512M') or '*'."
        )
    return f"+{m.group(1)}{m.group(2).upper()}"


# ---------------------------------------------------------------------------
# TOML loading and discovery
# ---------------------------------------------------------------------------


def _parse_partition(data: dict[str, Any]) -> PartitionSpec:
    """Parse a single [[partitions]] entry from TOML."""
    subvols_raw = data.get("subvolumes", [])
    subvols = [
        SubvolumeSpec(
            name=sv["name"],
            mount_point=sv.get("mount_point"),
        )
        for sv in subvols_raw
    ]
    return PartitionSpec(
        size=data["size"],
        filesystem=data.get("filesystem", ""),
        mount_point=data.get("mount_point"),
        label=data.get("label", ""),
        mount_options=data.get("mount_options", ""),
        subvolumes=subvols,
    )


def _validate_layout(layout: DiskLayout) -> list[str]:
    """Validate a disk layout, returning a list of error messages.

    Empty list means valid.
    """
    errors: list[str] = []

    if not layout.partitions:
        errors.append("Layout has no partitions defined.")
        return errors

    # Check that '*' is only used on the last partition
    for i, part in enumerate(layout.partitions):
        if part.size == "*" and i != len(layout.partitions) - 1:
            errors.append(
                f"Partition {i + 1} ({part.label or 'unlabeled'}) uses size '*' "
                f"but is not the last partition. Only the final partition can "
                f"fill the remaining space."
            )

    # Check for duplicate mount points
    mount_points = [
        p.mount_point for p in layout.partitions if p.mount_point is not None
    ]
    seen: set[str] = set()
    for mp in mount_points:
        if mp in seen:
            errors.append(f"Duplicate mount point: {mp}")
        seen.add(mp)

    # Check subvolumes only on btrfs partitions
    for i, part in enumerate(layout.partitions):
        if part.subvolumes and part.filesystem != "btrfs":
            errors.append(
                f"Partition {i + 1} ({part.label or 'unlabeled'}) has "
                f"subvolumes but filesystem is {part.filesystem!r}, not 'btrfs'."
            )

    return errors


def load_disk_layout(path: Path) -> DiskLayout:
    """Load a disk layout from a TOML file.

    Raises ``ValueError`` if the layout fails validation.
    """
    with open(path, "rb") as f:
        data = tomllib.load(f)

    meta = data.get("meta", {})
    partitions_raw = data.get("partitions", [])

    layout = DiskLayout(
        name=meta.get("name", "Unknown"),
        description=meta.get("description", ""),
        bootloaders=meta.get("bootloaders", []),
        partitions=[_parse_partition(p) for p in partitions_raw],
        path=path,
    )

    errors = _validate_layout(layout)
    if errors:
        err_str = "; ".join(errors)
        raise ValueError(f"Invalid disk layout {path.name}: {err_str}")

    return layout


def _find_layouts_dir() -> Path:
    """Locate the disk-layouts directory from the search path."""
    for d in _LAYOUTS_SEARCH:
        if d.is_dir():
            return d
    searched = ", ".join(str(d) for d in _LAYOUTS_SEARCH)
    raise FileNotFoundError(f"Disk layouts directory not found (searched: {searched})")


def resolve_disk_layout(filename: str) -> Path:
    """Resolve a disk layout filename to its full path.

    Accepts a bare filename like ``"basic.toml"`` and returns the absolute
    path.  Raises ``FileNotFoundError`` if the file does not exist.
    """
    layouts_dir = _find_layouts_dir()
    path = layouts_dir / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"Disk layout not found: {filename} (looked in {layouts_dir})"
        )
    return path


def discover_disk_layouts() -> list[DiskLayout]:
    """Discover all available disk layout files.

    Returns layouts sorted by name.
    """
    layouts_dir = _find_layouts_dir()
    layouts: list[DiskLayout] = []

    for item in sorted(layouts_dir.iterdir()):
        if item.suffix == ".toml":
            try:
                layout = load_disk_layout(item)
                if layout.name != "Unknown":
                    layouts.append(layout)
            except (KeyError, ValueError, tomllib.TOMLDecodeError):
                pass  # Skip malformed layout files

    return sorted(layouts, key=lambda la: la.name)


# ---------------------------------------------------------------------------
# RAID setup
# ---------------------------------------------------------------------------


def setup_raid_mdadm(
    config: RaidConfig,
    log: LogCallback | None = None,
) -> str:
    """Create an mdadm RAID array from the given config.

    Partitions each physical device identically (one data partition filling
    the whole disk), then assembles them into /dev/md0.

    Returns the virtual block device path (e.g. ``/dev/md0``).
    """
    md_device = "/dev/md0"
    level_str = str(config.level.value)
    device_count = len(config.devices)

    _log(
        f"[bold cyan]Setting up mdadm RAID{level_str} across "
        f"{device_count} devices: {', '.join(config.devices)}[/bold cyan]",
        log,
    )

    # Wipe each device and create a single partition for RAID
    for dev in config.devices:
        _log(f"  Wiping {dev}...", log)
        logged_run(["wipefs", "--all", "--force", dev], log=log)
        logged_run(["sgdisk", "--zap-all", dev], log=log)
        logged_run(["sgdisk", "--clear", dev], log=log)
        _log(f"  Creating RAID partition on {dev}...", log)
        logged_run(
            ["sgdisk", "-n", "1:0:0", "-t", "1:FD00", dev],
            log=log,
        )

    # Wait for partition nodes
    _log("  Waiting for partition device nodes...", log)
    for dev in config.devices:
        logged_run(["partprobe", dev], log=log)
    logged_run(["udevadm", "settle", "--timeout=10"], log=log)

    # Assemble the array
    raid_parts = [_part_name(dev, 1) for dev in config.devices]
    _log(
        f"  Creating mdadm array {md_device} with {', '.join(raid_parts)}...",
        log,
    )
    logged_run(
        [
            "mdadm",
            "--create",
            md_device,
            f"--level={level_str}",
            f"--raid-devices={device_count}",
            "--metadata=1.2",
            "--run",
        ]
        + raid_parts,
        log=log,
    )

    _log(
        f"[green]mdadm RAID{level_str} array ready at {md_device}.[/green]",
        log,
    )
    return md_device


# ---------------------------------------------------------------------------
# Disk layout application
# ---------------------------------------------------------------------------


def _wipe_device(device: str, log: LogCallback | None = None) -> None:
    """Wipe partition table and create a fresh GPT label on a device."""
    _log(f"  Wiping {device}...", log)
    logged_run(["wipefs", "--all", "--force", device], log=log)
    logged_run(["sgdisk", "--zap-all", device], log=log)
    logged_run(["sgdisk", "--clear", device], log=log)


def _create_partitions(
    device: str,
    layout: DiskLayout,
    log: LogCallback | None = None,
) -> list[str]:
    """Create GPT partitions on a device according to the layout.

    Returns a list of partition device paths (e.g. ["/dev/sda1", "/dev/sda2"]).
    """
    part_count = len(layout.partitions)
    _log(f"  Creating {part_count} partitions on {device}...", log)

    part_paths: list[str] = []
    for i, part in enumerate(layout.partitions):
        part_num = i + 1
        size_arg = parse_size_spec(part.size)

        # Determine GPT type code
        if part.filesystem == "vfat":
            type_code = "EF00"  # EFI System Partition
        else:
            type_code = "8300"  # Linux filesystem

        # Build sgdisk size spec: "start:end"
        if size_arg == "0":
            # Fill remaining space
            size_spec = "0:0"
            _log(
                f"    Partition {part_num}: {part.filesystem or 'raw'}  *"
                f"   -> {part.mount_point or '(none)'}  [{part.label or 'unlabeled'}]",
                log,
            )
        else:
            size_spec = f"0:{size_arg}"
            _log(
                f"    Partition {part_num}: {part.filesystem or 'raw'}  {part.size}"
                f"   -> {part.mount_point or '(none)'}  [{part.label or 'unlabeled'}]",
                log,
            )

        cmd = [
            "sgdisk",
            "-n",
            f"{part_num}:{size_spec}",
            "-t",
            f"{part_num}:{type_code}",
        ]
        if part.label:
            cmd.extend(["-c", f"{part_num}:{part.label}"])
        cmd.append(device)
        logged_run(cmd, log=log)

        part_paths.append(_part_name(device, part_num))

    return part_paths


def _format_partition(
    part_path: str,
    spec: PartitionSpec,
    part_num: int,
    btrfs_extra_devices: list[str] | None = None,
    raid_level: RaidLevel | None = None,
    log: LogCallback | None = None,
) -> None:
    """Format a single partition according to its spec.

    For btrfs partitions that are part of a multi-device RAID,
    ``btrfs_extra_devices`` contains the matching partition paths on the
    secondary disks, and ``raid_level`` determines the data/metadata profile.
    """
    if not spec.filesystem:
        _log(
            f"  Partition {part_num} ({spec.label or 'unlabeled'}): "
            f"raw, unformatted -- skipping mkfs.",
            log,
        )
        return

    if spec.filesystem == "vfat":
        _log(f"  Formatting {part_path} as FAT32...", log)
        cmd = ["mkfs.fat", "-F", "32"]
        if spec.label:
            cmd.extend(["-n", spec.label.upper()])
        cmd.append(part_path)
        logged_run(cmd, log=log)

    elif spec.filesystem == "ext4":
        _log(f"  Formatting {part_path} as ext4...", log)
        cmd = ["mkfs.ext4", "-F"]
        if spec.label:
            cmd.extend(["-L", spec.label])
        cmd.append(part_path)
        logged_run(cmd, log=log)

    elif spec.filesystem == "btrfs":
        if btrfs_extra_devices:
            # Multi-device btrfs RAID
            assert raid_level is not None
            raid_str = f"raid{raid_level.value}"
            all_devices = [part_path] + btrfs_extra_devices
            _log(
                f"  Formatting btrfs {raid_str} across {', '.join(all_devices)}...",
                log,
            )
            cmd = [
                "mkfs.btrfs",
                "-f",
                "-d",
                raid_str,
                "-m",
                raid_str,
            ]
            if spec.label:
                cmd.extend(["-L", spec.label])
            cmd.extend(all_devices)
            logged_run(cmd, log=log)
        else:
            _log(f"  Formatting {part_path} as btrfs...", log)
            cmd = ["mkfs.btrfs", "-f"]
            if spec.label:
                cmd.extend(["-L", spec.label])
            cmd.append(part_path)
            logged_run(cmd, log=log)

    else:
        _log(
            f"[yellow]  Warning: unknown filesystem {spec.filesystem!r} "
            f"for partition {part_num} -- skipping mkfs.[/yellow]",
            log,
        )


def _create_and_mount_subvolumes(
    part_path: str,
    spec: PartitionSpec,
    log: LogCallback | None = None,
) -> list[str]:
    """Create btrfs subvolumes and mount them under MOUNT_ROOT.

    Returns the list of subvolume names created (e.g. ["@", "@home", "@var"]).
    """
    if not spec.subvolumes:
        return []

    _log(f"  Creating btrfs subvolumes on {part_path}...", log)

    # Temporarily mount the top-level subvolume to create children
    tmp_mount = Path(tempfile.mkdtemp(prefix="arches-btrfs-layout-"))
    tmp_mount.mkdir(parents=True, exist_ok=True)
    logged_run(["mount", part_path, str(tmp_mount)], log=log)

    subvol_names: list[str] = []
    try:
        for sv in spec.subvolumes:
            _log(
                f"    {sv.name} -> {sv.mount_point or '(not mounted)'}",
                log,
            )
            logged_run(
                ["btrfs", "subvolume", "create", str(tmp_mount / sv.name)],
                log=log,
            )
            subvol_names.append(sv.name)
    finally:
        logged_run(["umount", str(tmp_mount)], log=log)

    # Mount subvolumes in order: root subvol (@) first, then children
    root_sv = None
    child_svs = []
    for sv in spec.subvolumes:
        if sv.mount_point == "/":
            root_sv = sv
        elif sv.mount_point is not None:
            child_svs.append(sv)

    if root_sv:
        root_mount = MOUNT_ROOT
        root_mount.mkdir(parents=True, exist_ok=True)
        opts = f"subvol={root_sv.name}"
        if spec.mount_options:
            opts = f"{opts},{spec.mount_options}"
        _log(
            f"  Mounting subvolume {root_sv.name} at {root_mount}...",
            log,
        )
        logged_run(
            ["mount", "-o", opts, part_path, str(root_mount)],
            log=log,
        )

    for sv in child_svs:
        assert sv.mount_point is not None
        mount_target = MOUNT_ROOT / sv.mount_point.lstrip("/")
        mount_target.mkdir(parents=True, exist_ok=True)
        opts = f"subvol={sv.name}"
        if spec.mount_options:
            opts = f"{opts},{spec.mount_options}"
        _log(
            f"  Mounting subvolume {sv.name} at {mount_target}...",
            log,
        )
        logged_run(
            ["mount", "-o", opts, part_path, str(mount_target)],
            log=log,
        )

    return subvol_names


def _mount_partition(
    part_path: str,
    spec: PartitionSpec,
    log: LogCallback | None = None,
) -> None:
    """Mount a non-subvolume partition under MOUNT_ROOT."""
    if spec.mount_point is None:
        _log(
            f"  Partition {part_path} ({spec.label or 'unlabeled'}): "
            f"no mount point -- skipping mount.",
            log,
        )
        return

    mount_target = MOUNT_ROOT / spec.mount_point.lstrip("/")
    mount_target.mkdir(parents=True, exist_ok=True)

    _log(f"  Mounting {part_path} at {mount_target}...", log)

    if spec.mount_options:
        logged_run(
            ["mount", "-o", spec.mount_options, part_path, str(mount_target)],
            log=log,
        )
    else:
        logged_run(["mount", part_path, str(mount_target)], log=log)


def apply_disk_layout(
    device: str,
    layout: DiskLayout,
    extra_devices: list[str] | None = None,
    raid_config: RaidConfig | None = None,
    log: LogCallback | None = None,
) -> PartitionMap:
    """Apply a disk layout to a device (or devices for btrfs RAID).

    This is the main entry point for the layout-based partitioning flow.

    1. Wipes the device(s)
    2. Creates GPT partitions via sgdisk
    3. Formats each partition (handling btrfs multi-device RAID)
    4. Creates btrfs subvolumes if specified
    5. Mounts everything under MOUNT_ROOT

    For btrfs RAID, ``extra_devices`` contains the secondary disk paths.
    Each device is partitioned identically; btrfs data partitions from all
    devices are passed to ``mkfs.btrfs -d raidN``.  The ESP from the primary
    device is the boot ESP; ESPs on secondary devices are formatted but not
    mounted (they are mirrored in a separate step after bootloader install).

    Returns a ``PartitionMap`` describing the resulting mount layout.
    """
    _log(
        f'[bold cyan]Applying layout "{layout.name}" to {device}[/bold cyan]',
        log,
    )

    # Step 1: Wipe primary device
    _wipe_device(device, log=log)

    # Wipe extra devices for btrfs RAID
    if extra_devices:
        for dev in extra_devices:
            _wipe_device(dev, log=log)

    # Step 2: Create partitions on primary device
    primary_parts = _create_partitions(device, layout, log=log)

    # Create identical partitions on extra devices (btrfs RAID)
    extra_part_lists: list[list[str]] = []
    if extra_devices:
        for dev in extra_devices:
            _log(f"  Creating matching partitions on {dev}...", log)
            extra_parts = _create_partitions(dev, layout, log=log)
            extra_part_lists.append(extra_parts)

    # Step 3: Probe partition tables and wait for device nodes
    _log("  Probing partition table...", log)
    logged_run(["partprobe", device], log=log)
    if extra_devices:
        for dev in extra_devices:
            logged_run(["partprobe", dev], log=log)
    logged_run(["udevadm", "settle", "--timeout=10"], log=log)

    # Step 4: Format partitions
    # Track which partition is the root for building the PartitionMap
    esp_path = ""
    root_path = ""
    boot_path = ""
    home_path = ""
    root_filesystem = ""
    root_subvolumes: list[str] = []

    # Determine btrfs RAID level for multi-device formatting
    btrfs_raid_level = None
    if raid_config and raid_config.backend == RaidBackend.BTRFS:
        btrfs_raid_level = raid_config.level

    for i, spec in enumerate(layout.partitions):
        part_path = primary_parts[i]

        # Collect matching partitions from extra devices for btrfs RAID
        btrfs_extra: list[str] | None = None
        if (
            extra_part_lists
            and spec.filesystem == "btrfs"
            and btrfs_raid_level is not None
        ):
            btrfs_extra = [ep[i] for ep in extra_part_lists]

        # Format ESPs on extra devices separately (mirrored, not RAID)
        if spec.filesystem == "vfat" and extra_part_lists:
            _format_partition(part_path, spec, i + 1, log=log)
            for ep in extra_part_lists:
                _log(f"  Formatting secondary ESP {ep[i]} as FAT32...", log)
                _format_partition(ep[i], spec, i + 1, log=log)
        else:
            _format_partition(
                part_path,
                spec,
                i + 1,
                btrfs_extra_devices=btrfs_extra,
                raid_level=btrfs_raid_level,
                log=log,
            )

        # Identify partition roles
        if spec.filesystem == "vfat" and spec.mount_point in ("/boot", "/boot/efi"):
            esp_path = part_path
        if spec.mount_point == "/":
            root_path = part_path
            root_filesystem = spec.filesystem
        if spec.mount_point == "/boot/efi":
            boot_path = ""  # ESP at /boot/efi: no separate /boot partition
        if spec.mount_point == "/home":
            home_path = part_path

    # Step 5: Mount partitions (root first, then others in order)
    # First pass: mount root (with subvolumes if btrfs)
    for i, spec in enumerate(layout.partitions):
        if spec.mount_point == "/":
            if spec.filesystem == "btrfs" and spec.subvolumes:
                root_subvolumes = _create_and_mount_subvolumes(
                    primary_parts[i], spec, log=log
                )
            else:
                _mount_partition(primary_parts[i], spec, log=log)
            break

    # Second pass: mount everything else (non-root, non-subvolume-managed)
    for i, spec in enumerate(layout.partitions):
        if spec.mount_point == "/":
            continue  # Already mounted
        if spec.filesystem == "btrfs" and spec.subvolumes:
            # Subvolumes on non-root btrfs partitions
            _create_and_mount_subvolumes(primary_parts[i], spec, log=log)
        elif spec.mount_point is not None:
            _mount_partition(primary_parts[i], spec, log=log)
        elif not spec.filesystem:
            # Raw partition — already logged during format
            pass
        else:
            _log(
                f"  Partition {primary_parts[i]} ({spec.label or 'unlabeled'}): "
                f"no mount point -- skipping mount.",
                log,
            )

    _log(
        f'[green]Disk layout "{layout.name}" applied successfully.[/green]',
        log,
    )

    return PartitionMap(
        esp=esp_path,
        root=root_path,
        boot=boot_path,
        home=home_path,
        root_filesystem=root_filesystem,
        root_subvolumes=root_subvolumes,
    )


# ---------------------------------------------------------------------------
# ESP mirroring (btrfs RAID 1/10 post-bootloader step)
# ---------------------------------------------------------------------------


def mirror_esp(
    primary_esp: str,
    secondary_esps: list[str],
    log: LogCallback | None = None,
) -> None:
    """Mirror ESP contents from the primary ESP to secondary disk ESPs.

    This provides boot redundancy for btrfs RAID 1/10 configurations.
    Each secondary ESP is formatted, mounted at a temporary location,
    and synced from the primary ESP mount at MOUNT_ROOT/boot.

    After syncing, an EFI boot entry is created for each secondary disk.
    """
    if not secondary_esps:
        _log("  No secondary ESPs to mirror.", log)
        return

    _log(
        f"[bold cyan]Mirroring ESP from {primary_esp} to "
        f"{len(secondary_esps)} secondary disk(s)...[/bold cyan]",
        log,
    )

    primary_mount = MOUNT_ROOT / "boot"

    for sec_esp in secondary_esps:
        # Mount secondary ESP to a temp dir
        tmp_mount = Path(tempfile.mkdtemp(prefix="arches-esp-mirror-"))
        tmp_mount.mkdir(parents=True, exist_ok=True)

        _log(f"  Syncing ESP contents to {sec_esp}...", log)
        logged_run(["mount", sec_esp, str(tmp_mount)], log=log)
        try:
            logged_run(
                ["rsync", "-a", "--delete", f"{primary_mount}/", f"{tmp_mount}/"],
                log=log,
            )
        finally:
            logged_run(["umount", str(tmp_mount)], log=log)

        # Add UEFI boot entry for this secondary ESP
        # Extract the disk and partition number for efibootmgr
        # e.g. /dev/sdb1 -> disk=/dev/sdb, part=1
        _log(f"  Adding UEFI boot entry for {sec_esp}...", log)
        try:
            # Strip trailing digits (and 'p' for nvme) to get the disk
            disk = sec_esp.rstrip("0123456789")
            if disk.endswith("p"):
                disk = disk[:-1]
            part_num = sec_esp[len(disk) :]
            if part_num.startswith("p"):
                part_num = part_num[1:]
            logged_run(
                [
                    "efibootmgr",
                    "--create",
                    "--disk",
                    disk,
                    "--part",
                    part_num,
                    "--label",
                    f"Arches Linux (mirror {sec_esp})",
                    "--loader",
                    "/EFI/BOOT/BOOTX64.EFI",
                ],
                log=log,
            )
        except Exception as e:
            _log(
                f"[yellow]  Warning: failed to create UEFI boot entry "
                f"for {sec_esp}: {e}[/yellow]",
                log,
            )

    _log(
        f"[green]ESP mirrored to {len(secondary_esps)} secondary disk(s).[/green]",
        log,
    )
