"""Tests for disk partitioning, mount detection, and block device discovery."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, mock_open, patch

import pytest

from arches_installer.core.disk import (
    BlockDevice,
    PartitionMap,
    _part_name,
    cleanup_mounts,
    detect_block_devices,
    detect_mounts,
    detect_single_disk,
    partition_disk_aarch64,
    partition_disk_x86,
    prepare_subvolume,
    validate_mounts,
)


# ---------------------------------------------------------------------------
# PartitionMap dataclass
# ---------------------------------------------------------------------------


def test_partition_map_defaults() -> None:
    """boot and home default to empty strings."""
    pm = PartitionMap(esp="/dev/sda1", root="/dev/sda2")
    assert pm.esp == "/dev/sda1"
    assert pm.root == "/dev/sda2"
    assert pm.boot == ""
    assert pm.home == ""


def test_partition_map_all_fields() -> None:
    pm = PartitionMap(
        esp="/dev/sda1",
        root="/dev/sda3",
        boot="/dev/sda2",
        home="/dev/sda4",
    )
    assert pm.esp == "/dev/sda1"
    assert pm.boot == "/dev/sda2"
    assert pm.root == "/dev/sda3"
    assert pm.home == "/dev/sda4"


# ---------------------------------------------------------------------------
# _part_name()
# ---------------------------------------------------------------------------


def test_part_name_regular_disk() -> None:
    assert _part_name("/dev/sda", 1) == "/dev/sda1"
    assert _part_name("/dev/sda", 2) == "/dev/sda2"


def test_part_name_nvme() -> None:
    assert _part_name("/dev/nvme0n1", 1) == "/dev/nvme0n1p1"
    assert _part_name("/dev/nvme0n1", 3) == "/dev/nvme0n1p3"


def test_part_name_mmc() -> None:
    assert _part_name("/dev/mmcblk0", 1) == "/dev/mmcblk0p1"
    assert _part_name("/dev/mmcblk0", 2) == "/dev/mmcblk0p2"


# ---------------------------------------------------------------------------
# partition_disk_x86()
# ---------------------------------------------------------------------------


@patch("arches_installer.core.disk.run")
def test_partition_disk_x86(mock_run: MagicMock) -> None:
    """x86 partitioning: two sgdisk calls, returns ESP + root, no boot/home."""
    pm = partition_disk_x86("/dev/sda", esp_size_mib=2048)

    assert mock_run.call_count == 2
    mock_run.assert_any_call(["sgdisk", "-n", "1:0:+2048M", "-t", "1:EF00", "/dev/sda"])
    mock_run.assert_any_call(["sgdisk", "-n", "2:0:0", "-t", "2:8300", "/dev/sda"])

    assert pm.esp == "/dev/sda1"
    assert pm.root == "/dev/sda2"
    assert pm.boot == ""
    assert pm.home == ""


@patch("arches_installer.core.disk.run")
def test_partition_disk_x86_nvme(mock_run: MagicMock) -> None:
    """x86 partitioning on NVMe uses the 'p' partition separator."""
    pm = partition_disk_x86("/dev/nvme0n1", esp_size_mib=512)

    assert pm.esp == "/dev/nvme0n1p1"
    assert pm.root == "/dev/nvme0n1p2"


# ---------------------------------------------------------------------------
# partition_disk_aarch64()
# ---------------------------------------------------------------------------


@patch("arches_installer.core.disk.run")
def test_partition_disk_aarch64(mock_run: MagicMock) -> None:
    """aarch64 partitioning: four sgdisk calls, returns all four fields."""
    pm = partition_disk_aarch64("/dev/sda", esp_size_mib=512, boot_size_mib=1024)

    assert mock_run.call_count == 4
    mock_run.assert_any_call(["sgdisk", "-n", "1:0:+512M", "-t", "1:EF00", "/dev/sda"])
    mock_run.assert_any_call(["sgdisk", "-n", "2:0:+1024M", "-t", "2:8300", "/dev/sda"])
    mock_run.assert_any_call(["sgdisk", "-n", "3:0:+50%", "-t", "3:8300", "/dev/sda"])
    mock_run.assert_any_call(["sgdisk", "-n", "4:0:0", "-t", "4:8300", "/dev/sda"])

    assert pm.esp == "/dev/sda1"
    assert pm.boot == "/dev/sda2"
    assert pm.root == "/dev/sda3"
    assert pm.home == "/dev/sda4"


@patch("arches_installer.core.disk.run")
def test_partition_disk_aarch64_nvme(mock_run: MagicMock) -> None:
    pm = partition_disk_aarch64("/dev/nvme0n1", esp_size_mib=512, boot_size_mib=1024)

    assert pm.esp == "/dev/nvme0n1p1"
    assert pm.boot == "/dev/nvme0n1p2"
    assert pm.root == "/dev/nvme0n1p3"
    assert pm.home == "/dev/nvme0n1p4"


# ---------------------------------------------------------------------------
# detect_mounts()
# ---------------------------------------------------------------------------


def _proc_mounts(lines: list[str]) -> str:
    """Build a fake /proc/mounts content string."""
    return "\n".join(lines) + "\n"


def test_detect_mounts_no_root() -> None:
    """If /mnt is not mounted, returns None."""
    content = _proc_mounts(
        [
            "/dev/sda1 / ext4 rw,noatime 0 0",
            "tmpfs /tmp tmpfs rw 0 0",
        ]
    )
    with patch("builtins.open", mock_open(read_data=content)):
        result = detect_mounts()
    assert result is None


def test_detect_mounts_limine_style() -> None:
    """Root at /mnt + ESP at /mnt/boot → Limine-style PartitionMap."""
    content = _proc_mounts(
        [
            "/dev/sda2 /mnt ext4 rw,noatime 0 0",
            "/dev/sda1 /mnt/boot vfat rw 0 0",
        ]
    )
    with patch("builtins.open", mock_open(read_data=content)):
        result = detect_mounts()

    assert result is not None
    assert result.esp == "/dev/sda1"
    assert result.root == "/dev/sda2"
    assert result.boot == ""
    assert result.home == ""


def test_detect_mounts_grub_style() -> None:
    """Root at /mnt + /boot at /mnt/boot + ESP at /mnt/boot/efi → GRUB ext4-style."""
    content = _proc_mounts(
        [
            "/dev/sda3 /mnt ext4 rw,noatime 0 0",
            "/dev/sda2 /mnt/boot ext4 rw 0 0",
            "/dev/sda1 /mnt/boot/efi vfat rw 0 0",
        ]
    )
    with patch("builtins.open", mock_open(read_data=content)):
        result = detect_mounts()

    assert result is not None
    assert result.esp == "/dev/sda1"
    assert result.root == "/dev/sda3"
    assert result.boot == "/dev/sda2"
    assert result.home == ""


def test_detect_mounts_grub_btrfs_style() -> None:
    """Root at /mnt (btrfs) + ESP at /mnt/boot/efi, no separate /boot → GRUB+btrfs."""
    content = _proc_mounts(
        [
            "/dev/sda2 /mnt btrfs rw,noatime,compress=zstd:1,subvol=/@ 0 0",
            "/dev/sda1 /mnt/boot/efi vfat rw 0 0",
        ]
    )
    with patch("builtins.open", mock_open(read_data=content)):
        result = detect_mounts()

    assert result is not None
    assert result.esp == "/dev/sda1"
    assert result.root == "/dev/sda2"
    assert result.boot == ""
    assert result.home == ""


def test_detect_mounts_full() -> None:
    """Root + boot + ESP + home → all four fields populated."""
    content = _proc_mounts(
        [
            "/dev/sda3 /mnt ext4 rw,noatime 0 0",
            "/dev/sda2 /mnt/boot ext4 rw 0 0",
            "/dev/sda1 /mnt/boot/efi vfat rw 0 0",
            "/dev/sda4 /mnt/home ext4 rw 0 0",
        ]
    )
    with patch("builtins.open", mock_open(read_data=content)):
        result = detect_mounts()

    assert result is not None
    assert result.esp == "/dev/sda1"
    assert result.boot == "/dev/sda2"
    assert result.root == "/dev/sda3"
    assert result.home == "/dev/sda4"


def test_detect_mounts_oserror_returns_none() -> None:
    """If /proc/mounts cannot be read, returns None."""
    with patch("builtins.open", side_effect=OSError("permission denied")):
        result = detect_mounts()
    assert result is None


# ---------------------------------------------------------------------------
# validate_mounts()
# ---------------------------------------------------------------------------


def test_validate_mounts_valid() -> None:
    """A PartitionMap with root + esp → no errors."""
    pm = PartitionMap(esp="/dev/sda1", root="/dev/sda2")
    errors = validate_mounts(pm)
    assert errors == []


def test_validate_mounts_missing_root() -> None:
    pm = PartitionMap(esp="/dev/sda1", root="")
    errors = validate_mounts(pm)
    assert len(errors) == 1
    assert "root" in errors[0].lower() or "/mnt" in errors[0]


def test_validate_mounts_missing_esp() -> None:
    pm = PartitionMap(esp="", root="/dev/sda2")
    errors = validate_mounts(pm)
    assert len(errors) == 1
    assert "ESP" in errors[0] or "EFI" in errors[0]


def test_validate_mounts_missing_both() -> None:
    pm = PartitionMap(esp="", root="")
    errors = validate_mounts(pm)
    assert len(errors) == 2


# ---------------------------------------------------------------------------
# detect_block_devices()
# ---------------------------------------------------------------------------


def _lsblk_json(devices: list[dict]) -> str:
    return json.dumps({"blockdevices": devices})


def _lsblk_children_json(device_name: str, children: list[dict]) -> str:
    return json.dumps({"blockdevices": [{"name": device_name, "children": children}]})


@patch("arches_installer.core.disk.run")
def test_detect_block_devices_basic(mock_run: MagicMock) -> None:
    """Parses lsblk JSON, filters disk type, and resolves partitions."""
    top_level = _lsblk_json(
        [
            {
                "name": "sda",
                "path": "/dev/sda",
                "size": "500G",
                "model": "Samsung SSD 970",
                "rm": False,
                "type": "disk",
            },
            {
                "name": "loop0",
                "path": "/dev/loop0",
                "size": "100M",
                "model": None,
                "rm": False,
                "type": "loop",
            },
        ]
    )
    children = _lsblk_children_json(
        "sda",
        [
            {"name": "sda1", "type": "part"},
            {"name": "sda2", "type": "part"},
        ],
    )

    mock_run.side_effect = [
        MagicMock(stdout=top_level),
        MagicMock(stdout=children),
    ]

    devices = detect_block_devices()

    assert len(devices) == 1
    assert devices[0].name == "sda"
    assert devices[0].path == "/dev/sda"
    assert devices[0].size == "500G"
    assert devices[0].model == "Samsung SSD 970"
    assert devices[0].removable is False
    assert devices[0].partitions == ["sda1", "sda2"]


@patch("arches_installer.core.disk.run")
def test_detect_block_devices_no_partitions(mock_run: MagicMock) -> None:
    """A disk with no partitions returns an empty partition list."""
    top_level = _lsblk_json(
        [
            {
                "name": "vda",
                "path": "/dev/vda",
                "size": "20G",
                "model": "QEMU HARDDISK",
                "rm": False,
                "type": "disk",
            },
        ]
    )
    # No children key at all
    children = json.dumps({"blockdevices": [{"name": "vda"}]})

    mock_run.side_effect = [
        MagicMock(stdout=top_level),
        MagicMock(stdout=children),
    ]

    devices = detect_block_devices()
    assert len(devices) == 1
    assert devices[0].partitions == []


@patch("arches_installer.core.disk.run")
def test_detect_block_devices_null_model(mock_run: MagicMock) -> None:
    """A device with null model gets 'Unknown'."""
    top_level = _lsblk_json(
        [
            {
                "name": "vda",
                "path": "/dev/vda",
                "size": "20G",
                "model": None,
                "rm": False,
                "type": "disk",
            },
        ]
    )
    children = _lsblk_children_json("vda", [])

    mock_run.side_effect = [
        MagicMock(stdout=top_level),
        MagicMock(stdout=children),
    ]

    devices = detect_block_devices()
    assert devices[0].model == "Unknown"


@patch("arches_installer.core.disk.run")
def test_detect_block_devices_multiple_disks(mock_run: MagicMock) -> None:
    """Multiple disks are all returned; non-disk types are filtered out."""
    top_level = _lsblk_json(
        [
            {
                "name": "sda",
                "path": "/dev/sda",
                "size": "500G",
                "model": "SSD",
                "rm": False,
                "type": "disk",
            },
            {
                "name": "sdb",
                "path": "/dev/sdb",
                "size": "32G",
                "model": "USB Flash",
                "rm": True,
                "type": "disk",
            },
            {
                "name": "sr0",
                "path": "/dev/sr0",
                "size": "1024M",
                "model": "DVD-ROM",
                "rm": True,
                "type": "rom",
            },
        ]
    )
    children_sda = _lsblk_children_json("sda", [{"name": "sda1", "type": "part"}])
    children_sdb = _lsblk_children_json("sdb", [])

    mock_run.side_effect = [
        MagicMock(stdout=top_level),
        MagicMock(stdout=children_sda),
        MagicMock(stdout=children_sdb),
    ]

    devices = detect_block_devices()
    assert len(devices) == 2
    assert devices[0].name == "sda"
    assert devices[0].removable is False
    assert devices[1].name == "sdb"
    assert devices[1].removable is True


# ---------------------------------------------------------------------------
# BlockDevice.display property
# ---------------------------------------------------------------------------


def test_block_device_display() -> None:
    dev = BlockDevice(
        name="sda",
        path="/dev/sda",
        size="500G",
        model="Samsung SSD 970",
        removable=False,
        partitions=["sda1", "sda2"],
    )
    assert dev.display == "/dev/sda  500G  Samsung SSD 970"


# ---------------------------------------------------------------------------
# detect_single_disk
# ---------------------------------------------------------------------------


@patch("arches_installer.core.disk.detect_block_devices")
def test_detect_single_disk_one_non_removable(mock_detect: MagicMock) -> None:
    """Single non-removable disk is returned; removable disks are ignored."""
    mock_detect.return_value = [
        BlockDevice("vda", "/dev/vda", "20G", "QEMU HARDDISK", False, []),
        BlockDevice("sdb", "/dev/sdb", "32G", "USB Flash", True, ["sdb1"]),
    ]
    result = detect_single_disk()
    assert result.path == "/dev/vda"


@patch("arches_installer.core.disk.detect_block_devices")
def test_detect_single_disk_no_disks(mock_detect: MagicMock) -> None:
    """Error when no non-removable disks are found."""
    mock_detect.return_value = [
        BlockDevice("sdb", "/dev/sdb", "32G", "USB Flash", True, ["sdb1"]),
    ]
    with pytest.raises(RuntimeError, match="No non-removable disks"):
        detect_single_disk()


@patch("arches_installer.core.disk.detect_block_devices")
def test_detect_single_disk_multiple_disks(mock_detect: MagicMock) -> None:
    """Error when multiple non-removable disks are found."""
    mock_detect.return_value = [
        BlockDevice("sda", "/dev/sda", "500G", "Samsung SSD", False, []),
        BlockDevice("sdb", "/dev/sdb", "1T", "WD Blue", False, []),
    ]
    with pytest.raises(RuntimeError, match="Multiple non-removable disks"):
        detect_single_disk()


# ---------------------------------------------------------------------------
# cleanup_mounts()
# ---------------------------------------------------------------------------


def test_cleanup_mounts_nothing_mounted() -> None:
    """cleanup_mounts is a no-op when nothing is mounted at /mnt."""
    content = _proc_mounts(
        [
            "/dev/sda1 / ext4 rw 0 0",
            "tmpfs /tmp tmpfs rw 0 0",
        ]
    )
    with patch("builtins.open", mock_open(read_data=content)):
        with patch("arches_installer.core.disk.run") as mock_run:
            cleanup_mounts()
            mock_run.assert_not_called()


def test_cleanup_mounts_unmounts_in_reverse() -> None:
    """cleanup_mounts unmounts deepest paths first."""
    content = _proc_mounts(
        [
            "/dev/sda2 /mnt btrfs rw 0 0",
            "/dev/sda2 /mnt/home btrfs rw 0 0",
            "/dev/sda1 /mnt/boot/efi vfat rw 0 0",
        ]
    )
    with patch("builtins.open", mock_open(read_data=content)):
        with patch("arches_installer.core.disk.run") as mock_run:
            cleanup_mounts()
            # Should unmount deepest first: /mnt/boot/efi, /mnt/home, /mnt
            calls = [c[0][0] for c in mock_run.call_args_list]
            assert calls == [
                ["umount", "/mnt/home"],
                ["umount", "/mnt/boot/efi"],
                ["umount", "/mnt"],
            ]


def test_cleanup_mounts_oserror_is_safe() -> None:
    """cleanup_mounts handles OSError gracefully."""
    with patch("builtins.open", side_effect=OSError("permission denied")):
        # Should not raise
        cleanup_mounts()


# ---------------------------------------------------------------------------
# prepare_subvolume()
# ---------------------------------------------------------------------------


@patch("arches_installer.core.disk.run")
def test_prepare_subvolume_alongside(
    mock_run, aarch64_apple_platform, tmp_path
) -> None:
    """Alongside mode creates @arches, @arches-home, @arches-var subvolumes."""
    mount_root = tmp_path / "mnt"

    with patch("arches_installer.core.disk.MOUNT_ROOT", mount_root):
        pm = prepare_subvolume(
            partition="/dev/nvme0n1p6",
            esp_partition="/dev/nvme0n1p4",
            platform=aarch64_apple_platform,
            mode="alongside",
            subvol_prefix="@arches",
        )

    assert pm.esp == "/dev/nvme0n1p4"
    assert pm.root == "/dev/nvme0n1p6"

    # Verify subvolume creation commands were issued
    run_cmds = [c[0][0] for c in mock_run.call_args_list]
    subvol_creates = [
        c for c in run_cmds if len(c) >= 3 and c[:3] == ["btrfs", "subvolume", "create"]
    ]
    assert len(subvol_creates) == 3  # @arches, @arches-home, @arches-var

    # Verify mount commands include the subvolume options
    mount_cmds = [c for c in run_cmds if c[0] == "mount"]
    assert len(mount_cmds) >= 4  # top-level, root, home, var, ESP


@patch("arches_installer.core.disk.run")
def test_prepare_subvolume_replace(mock_run, aarch64_apple_platform, tmp_path) -> None:
    """Replace mode creates standard @, @home, @var subvolumes."""
    mount_root = tmp_path / "mnt"

    with patch("arches_installer.core.disk.MOUNT_ROOT", mount_root):
        pm = prepare_subvolume(
            partition="/dev/nvme0n1p6",
            esp_partition="/dev/nvme0n1p4",
            platform=aarch64_apple_platform,
            mode="replace",
        )

    assert pm.esp == "/dev/nvme0n1p4"
    assert pm.root == "/dev/nvme0n1p6"

    # Verify subvolume creation commands include @, @home, @var
    run_cmds = [c[0][0] for c in mock_run.call_args_list]
    subvol_creates = [
        c for c in run_cmds if len(c) >= 3 and c[:3] == ["btrfs", "subvolume", "create"]
    ]
    assert len(subvol_creates) == 3


def test_prepare_subvolume_rejects_ext4(aarch64_apple_platform) -> None:
    """prepare_subvolume raises if platform uses ext4."""
    aarch64_apple_platform.disk_layout.filesystem = "ext4"
    with pytest.raises(RuntimeError, match="requires btrfs"):
        prepare_subvolume(
            partition="/dev/sda2",
            esp_partition="/dev/sda1",
            platform=aarch64_apple_platform,
            mode="alongside",
        )


def test_prepare_subvolume_rejects_invalid_mode(aarch64_apple_platform) -> None:
    """prepare_subvolume raises on invalid mode."""
    with pytest.raises(ValueError, match="Unknown subvolume mode"):
        prepare_subvolume(
            partition="/dev/sda2",
            esp_partition="/dev/sda1",
            platform=aarch64_apple_platform,
            mode="invalid",
        )
