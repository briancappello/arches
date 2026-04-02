"""Tests for disk layout loading, validation, and size parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from arches_installer.core.disk_layout import (
    DiskLayout,
    PartitionSpec,
    RaidBackend,
    RaidConfig,
    RaidLevel,
    SubvolumeSpec,
    _validate_layout,
    discover_disk_layouts,
    load_disk_layout,
    parse_size_spec,
)


# ---------------------------------------------------------------------------
# parse_size_spec
# ---------------------------------------------------------------------------


class TestParseSizeSpec:
    def test_gigabytes(self) -> None:
        assert parse_size_spec("2G") == "+2G"

    def test_megabytes(self) -> None:
        assert parse_size_spec("512M") == "+512M"

    def test_terabytes(self) -> None:
        assert parse_size_spec("1T") == "+1T"

    def test_fill_rest(self) -> None:
        assert parse_size_spec("*") == "0"

    def test_lowercase(self) -> None:
        assert parse_size_spec("100g") == "+100G"

    def test_invalid_no_unit(self) -> None:
        with pytest.raises(ValueError, match="Invalid partition size"):
            parse_size_spec("100")

    def test_invalid_empty(self) -> None:
        with pytest.raises(ValueError, match="Invalid partition size"):
            parse_size_spec("")

    def test_invalid_text(self) -> None:
        with pytest.raises(ValueError, match="Invalid partition size"):
            parse_size_spec("foo")


# ---------------------------------------------------------------------------
# Layout validation
# ---------------------------------------------------------------------------


class TestValidateLayout:
    def test_valid_layout(self) -> None:
        layout = DiskLayout(
            name="Test",
            description="test",
            bootloaders=["limine"],
            partitions=[
                PartitionSpec(size="2G", filesystem="vfat", mount_point="/boot"),
                PartitionSpec(size="*", filesystem="btrfs", mount_point="/"),
            ],
        )
        errors = _validate_layout(layout)
        assert errors == []

    def test_star_not_last(self) -> None:
        layout = DiskLayout(
            name="Test",
            description="test",
            bootloaders=["limine"],
            partitions=[
                PartitionSpec(size="*", filesystem="vfat", mount_point="/boot"),
                PartitionSpec(size="100G", filesystem="btrfs", mount_point="/"),
            ],
        )
        errors = _validate_layout(layout)
        assert len(errors) == 1
        assert "fill the remaining space" in errors[0]

    def test_duplicate_mount_points(self) -> None:
        layout = DiskLayout(
            name="Test",
            description="test",
            bootloaders=["limine"],
            partitions=[
                PartitionSpec(size="2G", filesystem="vfat", mount_point="/boot"),
                PartitionSpec(size="50G", filesystem="btrfs", mount_point="/"),
                PartitionSpec(size="*", filesystem="btrfs", mount_point="/"),
            ],
        )
        errors = _validate_layout(layout)
        assert any("Duplicate mount point" in e for e in errors)

    def test_subvolumes_on_non_btrfs(self) -> None:
        layout = DiskLayout(
            name="Test",
            description="test",
            bootloaders=["limine"],
            partitions=[
                PartitionSpec(
                    size="*",
                    filesystem="ext4",
                    mount_point="/",
                    subvolumes=[SubvolumeSpec(name="@", mount_point="/")],
                ),
            ],
        )
        errors = _validate_layout(layout)
        assert any("subvolumes" in e.lower() for e in errors)

    def test_empty_partitions(self) -> None:
        layout = DiskLayout(
            name="Test",
            description="test",
            bootloaders=["limine"],
            partitions=[],
        )
        errors = _validate_layout(layout)
        assert any("no partitions" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# TOML loading
# ---------------------------------------------------------------------------


class TestLoadDiskLayout:
    def test_load_basic(self, tmp_path: Path) -> None:
        """Load a basic two-partition layout."""
        toml = tmp_path / "basic.toml"
        toml.write_text("""\
[meta]
name = "Basic"
description = "Test layout"
bootloaders = ["limine", "grub"]

[[partitions]]
filesystem = "vfat"
size = "2G"
mount_point = "/boot"
label = "ESP"

[[partitions]]
filesystem = "btrfs"
size = "*"
mount_point = "/"
label = "archroot"
mount_options = "compress=zstd:1,noatime"

[[partitions.subvolumes]]
name = "@"
mount_point = "/"

[[partitions.subvolumes]]
name = "@home"
mount_point = "/home"
""")
        layout = load_disk_layout(toml)
        assert layout.name == "Basic"
        assert layout.bootloaders == ["limine", "grub"]
        assert len(layout.partitions) == 2

        esp = layout.partitions[0]
        assert esp.filesystem == "vfat"
        assert esp.size == "2G"
        assert esp.mount_point == "/boot"
        assert esp.label == "ESP"

        root = layout.partitions[1]
        assert root.filesystem == "btrfs"
        assert root.size == "*"
        assert root.mount_point == "/"
        assert len(root.subvolumes) == 2
        assert root.subvolumes[0].name == "@"
        assert root.subvolumes[0].mount_point == "/"
        assert root.subvolumes[1].name == "@home"
        assert root.subvolumes[1].mount_point == "/home"

    def test_load_raw_partition(self, tmp_path: Path) -> None:
        """Partition with no filesystem is loaded as raw."""
        toml = tmp_path / "spare.toml"
        toml.write_text("""\
[meta]
name = "Spare"
description = "Has a raw partition"
bootloaders = ["limine"]

[[partitions]]
filesystem = "vfat"
size = "2G"
mount_point = "/boot"

[[partitions]]
size = "100G"
label = "spare"

[[partitions]]
filesystem = "btrfs"
size = "*"
mount_point = "/"
""")
        layout = load_disk_layout(toml)
        assert len(layout.partitions) == 3
        spare = layout.partitions[1]
        assert spare.filesystem == ""
        assert spare.mount_point is None
        assert spare.label == "spare"

    def test_load_invalid_star_not_last(self, tmp_path: Path) -> None:
        """Should raise ValueError if * is not the last partition."""
        toml = tmp_path / "bad.toml"
        toml.write_text("""\
[meta]
name = "Bad"
description = "Invalid"
bootloaders = ["limine"]

[[partitions]]
filesystem = "vfat"
size = "*"
mount_point = "/boot"

[[partitions]]
filesystem = "btrfs"
size = "100G"
mount_point = "/"
""")
        with pytest.raises(ValueError, match="fill the remaining space"):
            load_disk_layout(toml)


# ---------------------------------------------------------------------------
# discover_disk_layouts (real files)
# ---------------------------------------------------------------------------


class TestDiscoverRealLayouts:
    """Test that the real disk layout TOML files in the repo load correctly."""

    PROJECT_ROOT = Path(__file__).resolve().parents[3]

    def test_discover_finds_layouts(self) -> None:
        """discover_disk_layouts should find at least 2 layouts."""
        layouts = discover_disk_layouts()
        assert len(layouts) >= 2
        names = [la.name for la in layouts]
        assert "Basic" in names
        assert "Flexible" in names

    def test_basic_layout_structure(self) -> None:
        """Basic layout has expected partition structure."""
        layouts = discover_disk_layouts()
        basic = next(la for la in layouts if la.name == "Basic")
        assert len(basic.partitions) == 2
        assert basic.partitions[0].filesystem == "vfat"
        assert basic.partitions[0].size == "2G"
        assert basic.partitions[1].filesystem == "btrfs"
        assert basic.partitions[1].size == "*"
        assert len(basic.partitions[1].subvolumes) == 3  # @, @home, @var

    def test_flexible_layout_structure(self) -> None:
        """Flexible layout has expected partition structure."""
        layouts = discover_disk_layouts()
        flexible = next(la for la in layouts if la.name == "Flexible")
        assert len(flexible.partitions) == 4

        # ESP
        assert flexible.partitions[0].filesystem == "vfat"
        assert flexible.partitions[0].size == "8G"

        # Root with subvolumes
        assert flexible.partitions[1].filesystem == "btrfs"
        assert flexible.partitions[1].size == "100G"
        assert len(flexible.partitions[1].subvolumes) == 2  # @, @var

        # Raw spare
        assert flexible.partitions[2].filesystem == ""
        assert flexible.partitions[2].mount_point is None
        assert flexible.partitions[2].label == "spare"

        # /home filling rest
        assert flexible.partitions[3].filesystem == "btrfs"
        assert flexible.partitions[3].size == "*"
        assert flexible.partitions[3].mount_point == "/home"


# ---------------------------------------------------------------------------
# RaidConfig / RaidLevel / RaidBackend
# ---------------------------------------------------------------------------


class TestRaidConfig:
    def test_raid_config_creation(self) -> None:
        config = RaidConfig(
            level=RaidLevel.RAID1,
            backend=RaidBackend.BTRFS,
            devices=["/dev/sda", "/dev/sdb"],
        )
        assert config.level == RaidLevel.RAID1
        assert config.backend == RaidBackend.BTRFS
        assert len(config.devices) == 2

    def test_raid_level_values(self) -> None:
        assert RaidLevel.RAID0.value == 0
        assert RaidLevel.RAID1.value == 1
        assert RaidLevel.RAID10.value == 10

    def test_raid_backend_values(self) -> None:
        assert RaidBackend.MDADM.value == "mdadm"
        assert RaidBackend.BTRFS.value == "btrfs"
