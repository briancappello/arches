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
    """Represents a detected block device.

    Captures everything we need to match a disk against a user-supplied
    descriptor (model, vendor, size, transport, rotational, serial,
    by-id symlinks). The bare ``model``/``size``/``removable`` fields
    that older callers used are preserved for backward compatibility.
    """

    name: str  # e.g. "sda", "nvme0n1"
    path: str  # e.g. "/dev/sda"
    size: str  # human-readable e.g. "500G" (preserved for TUI display)
    model: str
    removable: bool
    partitions: list[str]
    # Extended attributes used by disk_descriptor matching. Populated
    # by detect_block_devices(); all default to safe empty values so
    # constructors in tests can still pass the minimum set.
    size_bytes: int = 0  # exact size from lsblk -b
    vendor: str = ""  # e.g. "Samsung", "WDC", "Seagate"
    serial: str = ""  # short serial from lsblk SERIAL / udev ID_SERIAL_SHORT
    wwn: str = ""  # World-Wide Name (eui.* / naa.*) — stable across reboots
    transport: str = ""  # "nvme" | "sata" | "sas" | "usb" | "virtio" | ""
    rotational: bool = False  # True for HDDs, False for SSDs / NVMe
    by_id_links: list[str] = field(default_factory=list)
    # Each entry is the basename of a /dev/disk/by-id/ symlink that
    # points at this device, e.g. "nvme-Samsung_SSD_990_PRO_2TB_S5GXNX0..."

    @property
    def display(self) -> str:
        return f"{self.path}  {self.size}  {self.model}"

    @property
    def is_ssd(self) -> bool:
        """True if this disk uses solid-state storage.

        ``rotational == False`` is the kernel's own signal; NVMe is
        always SSD even though some buggy controllers misreport
        rotational. Use this as the canonical 'is it an SSD' check.
        """
        return (not self.rotational) or self.transport == "nvme"


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


def _list_by_id_links(device_path: str) -> list[str]:
    """Return basenames of /dev/disk/by-id/ symlinks pointing at device_path.

    Filters out partition-specific symlinks (those ending in -partN) so
    the returned list refers only to the whole-disk identifiers we'd
    want to use for fstab or persistent role pinning.
    """
    by_id = Path("/dev/disk/by-id")
    if not by_id.is_dir():
        return []
    target = Path(device_path).resolve()
    links: list[str] = []
    for entry in sorted(by_id.iterdir()):
        try:
            if entry.is_symlink() and entry.resolve() == target:
                if "-part" in entry.name:
                    continue  # skip partition links
                links.append(entry.name)
        except OSError:
            continue
    return links


def _udev_properties(device_path: str) -> dict[str, str]:
    """Read udev properties for a device. Empty dict on failure."""
    try:
        result = subprocess.run(
            ["udevadm", "info", "--query=property", f"--name={device_path}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return {}
    props: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            props[k] = v
    return props


def _read_sys_attr(name: str, attr: str) -> str:
    """Read a sysfs attribute for a block device, returning '' on error."""
    p = Path(f"/sys/block/{name}/{attr}")
    try:
        return p.read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return ""


def detect_block_devices() -> list[BlockDevice]:
    """Detect available block devices via lsblk + udev + sysfs.

    Captures the rich attribute set needed by the disk-descriptor
    matcher: size in bytes, model, vendor, serial, WWN, transport,
    rotational flag, plus the /dev/disk/by-id/ symlinks that survive
    across reboots even when kernel device names race.
    """
    # -b: bytes (so size_bytes is exact, not human-rounded)
    # -d: only top-level disks (no partitions)
    # Columns: include everything we need for matching.
    result = run(
        [
            "lsblk",
            "-J",
            "-b",
            "-d",
            "-o",
            "NAME,PATH,SIZE,MODEL,VENDOR,SERIAL,WWN,TRAN,ROTA,RM,TYPE",
        ]
    )
    data = json.loads(result.stdout)
    devices: list[BlockDevice] = []

    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        name = dev.get("name", "")
        if name.startswith(("zram", "loop", "sr", "ram")):
            continue

        # Partitions on this device (separate lsblk call so we get the
        # human-friendly NAME without the byte-size noise).
        part_result = run(
            ["lsblk", "-J", "-o", "NAME,TYPE", dev["path"]]
        )
        part_data = json.loads(part_result.stdout)
        partitions = [
            p["name"]
            for p in part_data.get("blockdevices", [{}])[0].get("children", [])
            if p.get("type") == "part"
        ]

        # lsblk in -b mode emits sizes as integers (bytes).
        raw_size = dev.get("size", 0)
        try:
            size_bytes = int(raw_size)
        except (TypeError, ValueError):
            size_bytes = 0

        # For TUI display we still want a human-readable string.
        size_human = _human_size(size_bytes) if size_bytes else str(raw_size)

        model = (dev.get("model") or "").strip()
        if not model:
            model = "Unknown"
        vendor = (dev.get("vendor") or "").strip()
        serial = (dev.get("serial") or "").strip()
        wwn = (dev.get("wwn") or "").strip()
        transport = (dev.get("tran") or "").strip().lower()
        rotational = bool(dev.get("rota", False))

        # Some kernels report vendor on the SCSI device but not on the
        # block device. Fall back to udev properties (ID_VENDOR) when
        # lsblk's VENDOR column is empty.
        if not vendor:
            props = _udev_properties(dev["path"])
            vendor = props.get("ID_VENDOR", "").strip()

        by_id_links = _list_by_id_links(dev["path"])

        devices.append(
            BlockDevice(
                name=name,
                path=dev["path"],
                size=size_human,
                model=model,
                removable=bool(dev.get("rm", False)),
                partitions=partitions,
                size_bytes=size_bytes,
                vendor=vendor,
                serial=serial,
                wwn=wwn,
                transport=transport,
                rotational=rotational,
                by_id_links=by_id_links,
            )
        )

    return devices


def _human_size(n: int) -> str:
    """Format byte count as a short human string. lsblk-style.

    Uses 1000-based units (matching `lsblk` and disk-vendor labelling),
    not 1024-based — a "2TB" disk shows up as ~2.0 T from this function
    and ~1.8 TiB if you switched to base-1024. We pick the
    vendor-friendly form because that's what users will type in
    descriptors.
    """
    if n <= 0:
        return "0"
    for unit in ("B", "K", "M", "G", "T", "P"):
        if n < 1000:
            # One decimal for small values, no decimal for >=10
            if n < 10 and unit not in ("B",):
                return f"{n:.1f}{unit}"
            return f"{int(n)}{unit}"
        n = n / 1000  # type: ignore[assignment]
    return f"{n:.1f}E"


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
    # Swap partitions created by the layout. genfstab doesn't include
    # them (they aren't swapon'd at install time), so the install
    # pipeline appends their UUID entries to fstab separately.
    swap_partitions: list[str] = field(default_factory=list)
    # Auxiliary mount points beyond the canonical esp/root/boot/home set
    # (e.g. /var/lib/models on a separate disk). Each entry is
    # (device_path, mount_point, filesystem). Used by the install
    # pipeline for fstab sanity and by hardware-rescan for stability
    # validation.
    extra_mounts: list[tuple[str, str, str]] = field(default_factory=list)


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
