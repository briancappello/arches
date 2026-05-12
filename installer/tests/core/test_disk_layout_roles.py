"""Tests for the disk-role resolution system.

Covers:
  - Implicit single-disk default for legacy layouts (no [[disks]] block)
  - Single-disk role resolution
  - RAID-role multi-disk count validation
  - Override layering: layout < machine < auto-install
  - Conflict detection (disk claimed twice, ambiguous matches)
  - Error messages for unresolved roles
"""

from __future__ import annotations

import pytest

from arches_installer.core.disk import BlockDevice
from arches_installer.core.disk_layout import (
    DEFAULT_DISK_ROLE,
    DiskLayout,
    DiskRole,
    DiskRoleResolutionError,
    PartitionSpec,
    SubvolumeSpec,
    resolve_disk_roles,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _disk(
    name: str = "nvme0n1",
    path: str | None = None,
    size_bytes: int = 2_000_000_000_000,
    model: str = "Samsung SSD 990 PRO 2TB",
    vendor: str = "",
    serial: str = "S5GXNX0R123456",
    transport: str = "nvme",
    rotational: bool = False,
    removable: bool = False,
    by_id_links: list[str] | None = None,
) -> BlockDevice:
    return BlockDevice(
        name=name,
        path=path or f"/dev/{name}",
        size=f"{size_bytes // 10**12}T",
        size_bytes=size_bytes,
        model=model,
        vendor=vendor,
        serial=serial,
        wwn="",
        transport=transport,
        rotational=rotational,
        removable=removable,
        partitions=[],
        by_id_links=by_id_links or [f"nvme-Samsung_SSD_990_PRO_2TB_{serial}"],
    )


def _samsung_a() -> BlockDevice:
    return _disk(name="nvme0n1", serial="SAM_AAA")


def _samsung_b() -> BlockDevice:
    return _disk(name="nvme1n1", serial="SAM_BBB")


def _wd_2tb() -> BlockDevice:
    return _disk(
        name="nvme2n1",
        model="WDC WDS200T2X0E-00BCA0 (SN850x)",
        serial="WD12345678",
        by_id_links=["nvme-WDC_WDS200T2X0E-00BCA0_WD12345678"],
    )


def _seagate_8tb() -> BlockDevice:
    return _disk(
        name="sda",
        size_bytes=8_000_000_000_000,
        model="ST8000VN004",
        vendor="ATA",
        serial="SEA_8TB_001",
        transport="sata",
        rotational=True,
        by_id_links=["ata-Seagate_ST8000VN004_SEA_8TB_001"],
    )


# ---------------------------------------------------------------------------
# Implicit single-disk default
# ---------------------------------------------------------------------------


class TestImplicitDefault:
    def test_layout_without_disks_uses_primary_role(self) -> None:
        """Legacy layouts (basic.toml, flexible.toml) have no [[disks]]
        block. The resolver should synthesise a 'primary' role that
        matches any non-removable disk."""
        layout = DiskLayout(
            name="Basic",
            description="",
            bootloaders=["limine"],
            partitions=[
                PartitionSpec(size="2G", filesystem="vfat", mount_point="/boot"),
                PartitionSpec(size="*", filesystem="btrfs", mount_point="/"),
            ],
        )
        resolved = resolve_disk_roles(layout, [_samsung_a()])
        assert DEFAULT_DISK_ROLE in resolved.assignments
        assert resolved.primary(DEFAULT_DISK_ROLE).path == "/dev/nvme0n1"

    def test_implicit_default_ambiguous_with_two_disks(self) -> None:
        """The implicit 'primary' role matches any non-removable disk —
        so on a multi-disk host it's ambiguous and should fail."""
        layout = DiskLayout(
            name="Basic",
            description="",
            bootloaders=["limine"],
            partitions=[PartitionSpec(size="*", filesystem="btrfs")],
        )
        with pytest.raises(DiskRoleResolutionError, match="ambiguous"):
            resolve_disk_roles(layout, [_samsung_a(), _wd_2tb()])

    def test_implicit_default_skips_removable(self) -> None:
        """USB sticks should not be picked up by implicit single-disk."""
        layout = DiskLayout(
            name="Basic",
            description="",
            bootloaders=["limine"],
            partitions=[PartitionSpec(size="*", filesystem="btrfs")],
        )
        usb = _disk(name="sdc", removable=True, transport="usb")
        resolved = resolve_disk_roles(layout, [_samsung_a(), usb])
        # The implicit criteria has removable=False, so the USB is
        # excluded and the resolver finds exactly the NVMe.
        assert resolved.primary(DEFAULT_DISK_ROLE).path == "/dev/nvme0n1"


# ---------------------------------------------------------------------------
# Explicit single-disk roles
# ---------------------------------------------------------------------------


class TestSingleDiskRoles:
    def test_two_roots_resolve_distinct_disks(self) -> None:
        layout = DiskLayout(
            name="Workstation",
            description="",
            bootloaders=["limine"],
            disks=[
                DiskRole(name="root", descriptor="2TB Samsung"),
                DiskRole(name="bulk", descriptor="8TB Seagate SATA"),
            ],
            partitions=[
                PartitionSpec(
                    size="*", filesystem="btrfs", mount_point="/", disk="root"
                ),
                PartitionSpec(
                    size="*",
                    filesystem="xfs",
                    mount_point="/var/lib/models",
                    disk="bulk",
                ),
            ],
        )
        resolved = resolve_disk_roles(layout, [_samsung_a(), _seagate_8tb()])
        assert resolved.primary("root").path == "/dev/nvme0n1"
        assert resolved.primary("bulk").path == "/dev/sda"

    def test_missing_descriptor_match_fails(self) -> None:
        layout = DiskLayout(
            name="x",
            description="",
            bootloaders=[],
            disks=[DiskRole(name="root", descriptor="2TB Samsung")],
            partitions=[
                PartitionSpec(size="*", filesystem="btrfs", disk="root"),
            ],
        )
        with pytest.raises(DiskRoleResolutionError, match="root"):
            resolve_disk_roles(layout, [_seagate_8tb()])  # only HDD, no Samsung

    def test_ambiguous_match_fails(self) -> None:
        """Two disks match the same single-disk role descriptor."""
        layout = DiskLayout(
            name="x",
            description="",
            bootloaders=[],
            disks=[DiskRole(name="root", descriptor="2TB Samsung")],
            partitions=[PartitionSpec(size="*", filesystem="btrfs", disk="root")],
        )
        with pytest.raises(DiskRoleResolutionError, match="ambiguous"):
            resolve_disk_roles(layout, [_samsung_a(), _samsung_b()])

    def test_disambiguation_via_serial(self) -> None:
        """Two identical Samsungs can be disambiguated by serial."""
        layout = DiskLayout(
            name="x",
            description="",
            bootloaders=[],
            disks=[DiskRole(name="root", descriptor="Samsung SAM_AAA")],
            partitions=[PartitionSpec(size="*", filesystem="btrfs", disk="root")],
        )
        resolved = resolve_disk_roles(layout, [_samsung_a(), _samsung_b()])
        assert resolved.primary("root").serial == "SAM_AAA"


# ---------------------------------------------------------------------------
# RAID roles
# ---------------------------------------------------------------------------


class TestRaidRoles:
    def test_raid1_needs_two_disks(self) -> None:
        layout = DiskLayout(
            name="Mirror",
            description="",
            bootloaders=[],
            disks=[DiskRole(name="mirror", descriptor="2TB Samsung")],
            partitions=[
                PartitionSpec(
                    size="*",
                    filesystem="btrfs",
                    mount_point="/",
                    disks="mirror",
                    raid_level="1",
                )
            ],
        )
        # Only one Samsung → fail
        with pytest.raises(DiskRoleResolutionError, match="needs 2"):
            resolve_disk_roles(layout, [_samsung_a()])
        # Two matching → OK
        resolved = resolve_disk_roles(layout, [_samsung_a(), _samsung_b()])
        assert len(resolved.devices("mirror")) == 2

    def test_raid1_three_matches_fail(self) -> None:
        layout = DiskLayout(
            name="Mirror",
            description="",
            bootloaders=[],
            disks=[DiskRole(name="mirror", descriptor="2TB Samsung")],
            partitions=[
                PartitionSpec(
                    size="*",
                    filesystem="btrfs",
                    disks="mirror",
                    raid_level="1",
                )
            ],
        )
        # Three matching Samsungs but raid_level=1 wants exactly 2 → fail
        c = _samsung_b()
        c.path, c.name, c.serial = "/dev/nvme2n1", "nvme2n1", "SAM_CCC"
        with pytest.raises(DiskRoleResolutionError, match="only 2 are required"):
            resolve_disk_roles(layout, [_samsung_a(), _samsung_b(), c])

    def test_raid5_minimum_three(self) -> None:
        layout = DiskLayout(
            name="R5",
            description="",
            bootloaders=[],
            disks=[DiskRole(name="data", descriptor="2TB Samsung")],
            partitions=[
                PartitionSpec(
                    size="*",
                    filesystem="btrfs",
                    disks="data",
                    raid_level="5",
                )
            ],
        )
        c = _samsung_b()
        c.path, c.name, c.serial = "/dev/nvme2n1", "nvme2n1", "SAM_CCC"
        with pytest.raises(DiskRoleResolutionError, match="needs 3"):
            resolve_disk_roles(layout, [_samsung_a(), _samsung_b()])
        # Three matching → OK
        resolved = resolve_disk_roles(
            layout, [_samsung_a(), _samsung_b(), c]
        )
        assert len(resolved.devices("data")) == 3

    def test_raid_ordering_is_stable_by_serial(self) -> None:
        """Two-disk RAID picks the same disk-to-index mapping regardless
        of enumeration order (kernel may rename devices across boots)."""
        layout = DiskLayout(
            name="Mirror",
            description="",
            bootloaders=[],
            disks=[DiskRole(name="mirror", descriptor="2TB Samsung")],
            partitions=[
                PartitionSpec(
                    size="*",
                    filesystem="btrfs",
                    disks="mirror",
                    raid_level="1",
                )
            ],
        )
        a = _samsung_a()  # serial SAM_AAA
        b = _samsung_b()  # serial SAM_BBB
        r1 = resolve_disk_roles(layout, [a, b])
        r2 = resolve_disk_roles(layout, [b, a])  # reversed input
        # match_disks sorts by serial; both runs should produce same order.
        assert [d.serial for d in r1.devices("mirror")] == [
            "SAM_AAA",
            "SAM_BBB",
        ]
        assert [d.serial for d in r2.devices("mirror")] == [
            "SAM_AAA",
            "SAM_BBB",
        ]


# ---------------------------------------------------------------------------
# Override stack
# ---------------------------------------------------------------------------


class TestOverrideStack:
    def test_machine_overrides_layout(self) -> None:
        """A machine profile can pin a role to a specific serial."""
        layout = DiskLayout(
            name="x",
            description="",
            bootloaders=[],
            disks=[DiskRole(name="root", descriptor="2TB NVMe")],
            partitions=[PartitionSpec(size="*", filesystem="btrfs", disk="root")],
        )
        # Layout matches both Samsungs (ambiguous), but machine pins
        # the second one by serial.
        machine = [DiskRole(name="root", descriptor="SAM_BBB")]
        resolved = resolve_disk_roles(
            layout,
            [_samsung_a(), _samsung_b()],
            machine_disks=machine,
        )
        assert resolved.primary("root").serial == "SAM_BBB"

    def test_auto_install_overrides_machine(self) -> None:
        """An auto-install config has highest priority."""
        layout = DiskLayout(
            name="x",
            description="",
            bootloaders=[],
            disks=[DiskRole(name="root", descriptor="2TB NVMe")],
            partitions=[PartitionSpec(size="*", filesystem="btrfs", disk="root")],
        )
        machine = [DiskRole(name="root", descriptor="SAM_AAA")]
        auto = [DiskRole(name="root", descriptor="SAM_BBB")]
        resolved = resolve_disk_roles(
            layout,
            [_samsung_a(), _samsung_b()],
            machine_disks=machine,
            auto_install_disks=auto,
        )
        assert resolved.primary("root").serial == "SAM_BBB"

    def test_explicit_device_path_in_override(self) -> None:
        layout = DiskLayout(
            name="x",
            description="",
            bootloaders=[],
            disks=[DiskRole(name="root", descriptor="2TB NVMe")],
            partitions=[PartitionSpec(size="*", filesystem="btrfs", disk="root")],
        )
        auto = [DiskRole(name="root", descriptor="/dev/nvme1n1")]
        resolved = resolve_disk_roles(
            layout,
            [_samsung_a(), _samsung_b()],
            auto_install_disks=auto,
        )
        assert resolved.primary("root").path == "/dev/nvme1n1"


# ---------------------------------------------------------------------------
# Conflict / overlap detection
# ---------------------------------------------------------------------------


class TestConflicts:
    def test_two_roles_cant_claim_same_disk(self) -> None:
        """If role A and role B both match the same physical disk and
        no other candidate exists for B, resolution should fail with
        a clear conflict message."""
        layout = DiskLayout(
            name="x",
            description="",
            bootloaders=[],
            disks=[
                DiskRole(name="root", descriptor="2TB Samsung"),
                DiskRole(name="other", descriptor="Samsung"),
            ],
            partitions=[
                PartitionSpec(size="*", filesystem="btrfs", disk="root"),
                PartitionSpec(size="100G", filesystem="ext4", disk="other"),
            ],
        )
        with pytest.raises(DiskRoleResolutionError, match="claimed by"):
            resolve_disk_roles(layout, [_samsung_a()])

    def test_two_roles_resolve_distinct_when_candidates_allow(self) -> None:
        """With two matching candidates, role A and role B can each
        claim one without conflict."""
        layout = DiskLayout(
            name="x",
            description="",
            bootloaders=[],
            disks=[
                DiskRole(name="a", descriptor="Samsung SAM_AAA"),
                DiskRole(name="b", descriptor="Samsung SAM_BBB"),
            ],
            partitions=[
                PartitionSpec(size="*", filesystem="btrfs", disk="a"),
                PartitionSpec(size="*", filesystem="btrfs", disk="b"),
            ],
        )
        resolved = resolve_disk_roles(layout, [_samsung_a(), _samsung_b()])
        assert resolved.primary("a").serial == "SAM_AAA"
        assert resolved.primary("b").serial == "SAM_BBB"


# ---------------------------------------------------------------------------
# Validation at load time
# ---------------------------------------------------------------------------


class TestValidation:
    def test_partition_references_undeclared_role(self) -> None:
        from arches_installer.core.disk_layout import _validate_layout

        layout = DiskLayout(
            name="x",
            description="",
            bootloaders=[],
            disks=[DiskRole(name="root", descriptor="2TB")],
            partitions=[
                PartitionSpec(size="*", filesystem="btrfs", disk="bulk"),
            ],
        )
        errors = _validate_layout(layout)
        assert any("bulk" in e for e in errors)

    def test_duplicate_role_name(self) -> None:
        from arches_installer.core.disk_layout import _validate_layout

        layout = DiskLayout(
            name="x",
            description="",
            bootloaders=[],
            disks=[
                DiskRole(name="root", descriptor="2TB"),
                DiskRole(name="root", descriptor="4TB"),
            ],
            partitions=[],
        )
        errors = _validate_layout(layout)
        assert any("Duplicate disk role" in e for e in errors)

    def test_raid_level_without_disks_plural(self) -> None:
        from arches_installer.core.disk_layout import _validate_layout

        layout = DiskLayout(
            name="x",
            description="",
            bootloaders=[],
            partitions=[
                PartitionSpec(
                    size="*", filesystem="btrfs", disk="primary", raid_level="1"
                )
            ],
        )
        errors = _validate_layout(layout)
        assert any("RAID requires multiple disks" in e for e in errors)

    def test_unsupported_raid_level(self) -> None:
        from arches_installer.core.disk_layout import _validate_layout

        layout = DiskLayout(
            name="x",
            description="",
            bootloaders=[],
            disks=[DiskRole(name="mirror", descriptor="2TB")],
            partitions=[
                PartitionSpec(
                    size="*", filesystem="btrfs", disks="mirror", raid_level="99"
                )
            ],
        )
        errors = _validate_layout(layout)
        assert any("unsupported raid_level" in e for e in errors)
