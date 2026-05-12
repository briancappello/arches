"""Tests for disk-roles state persistence and validation.

Covers:
  - to_state_dict / write_disk_roles_state / read_disk_roles_state
  - validate_disk_roles: OK, relocated, missing scenarios
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from arches_installer.core.disk import BlockDevice
from arches_installer.core.disk_layout import (
    DiskRole,
    ResolvedDiskRoles,
    read_disk_roles_state,
    validate_disk_roles,
    write_disk_roles_state,
)


def _disk(
    name: str = "nvme0n1",
    serial: str = "AAA",
    wwn: str = "eui.aaa",
    by_id: list[str] | None = None,
    size_bytes: int = 2_000_000_000_000,
    model: str = "Samsung SSD 990 PRO 2TB",
) -> BlockDevice:
    return BlockDevice(
        name=name,
        path=f"/dev/{name}",
        size="2T",
        size_bytes=size_bytes,
        model=model,
        vendor="",
        serial=serial,
        wwn=wwn,
        transport="nvme",
        rotational=False,
        removable=False,
        partitions=[],
        by_id_links=by_id or [f"nvme-Samsung_SSD_990_PRO_2TB_{serial}"],
    )


def _resolved(role: str, disks: list[BlockDevice]) -> ResolvedDiskRoles:
    return ResolvedDiskRoles(
        assignments={role: disks},
        roles={role: DiskRole(name=role, descriptor=f"role {role}")},
    )


class TestStateRoundTrip:
    def test_write_and_read(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        resolved = _resolved("root", [_disk()])
        write_disk_roles_state(resolved, tmp)
        state = read_disk_roles_state(tmp)
        assert "roles" in state
        assert "root" in state["roles"]
        devs = state["roles"]["root"]["devices"]
        assert len(devs) == 1
        assert devs[0]["serial"] == "AAA"
        assert devs[0]["path_at_install"] == "/dev/nvme0n1"

    def test_missing_file_returns_empty(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        assert read_disk_roles_state(tmp) == {}

    def test_state_includes_stable_identifiers(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        d = _disk(by_id=["nvme-Samsung_X", "nvme-eui.aaa"])
        write_disk_roles_state(_resolved("root", [d]), tmp)
        state = read_disk_roles_state(tmp)
        rec = state["roles"]["root"]["devices"][0]
        assert rec["serial"] == "AAA"
        assert rec["wwn"] == "eui.aaa"
        assert "nvme-Samsung_X" in rec["by_id_links"]


class TestValidate:
    def test_all_present_unchanged(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        d = _disk()
        write_disk_roles_state(_resolved("root", [d]), tmp)
        report = validate_disk_roles(tmp, [d])
        assert report.ok == ["root"]
        assert not report.missing
        assert not report.relocated

    def test_disk_relocated_via_serial(self) -> None:
        """Same physical disk shows up under a different /dev path.

        Common scenario: kernel enumerates NVMe controllers in a
        different order across boots. The serial / by-id stay the same.
        """
        tmp = Path(tempfile.mkdtemp())
        original = _disk(name="nvme0n1")
        write_disk_roles_state(_resolved("root", [original]), tmp)
        # Live: same serial but on /dev/nvme1n1 now.
        relocated = _disk(name="nvme1n1")
        relocated.serial = "AAA"  # same stable id
        report = validate_disk_roles(tmp, [relocated])
        assert report.relocated == {
            "root": [("/dev/nvme0n1", "/dev/nvme1n1")]
        }
        assert not report.missing

    def test_disk_missing(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        original = _disk()
        write_disk_roles_state(_resolved("root", [original]), tmp)
        # Live: no disks at all (disk died/removed).
        report = validate_disk_roles(tmp, [])
        assert "root" in report.missing
        assert report.missing["root"] == ["AAA"]

    def test_match_by_wwn_when_serial_unset(self) -> None:
        """Some disks have no serial (rare), only WWN. Validation
        should fall back to WWN matching."""
        tmp = Path(tempfile.mkdtemp())
        d = _disk()
        d.serial = ""  # blank serial
        write_disk_roles_state(_resolved("root", [d]), tmp)
        # Live: same WWN, different path.
        live = _disk(name="nvme1n1")
        live.serial = ""
        report = validate_disk_roles(tmp, [live])
        # WWN matches → OK or relocated, NOT missing.
        assert "root" not in report.missing

    def test_match_by_by_id(self) -> None:
        """Fallback to by-id symlinks when serial AND WWN are blank
        (very rare but possible with USB/virtio disks)."""
        tmp = Path(tempfile.mkdtemp())
        d = _disk(by_id=["scsi-Generic_USB_Stick"])
        d.serial = ""
        d.wwn = ""
        write_disk_roles_state(_resolved("root", [d]), tmp)
        live = _disk(name="sdb", by_id=["scsi-Generic_USB_Stick"])
        live.serial = ""
        live.wwn = ""
        report = validate_disk_roles(tmp, [live])
        assert "root" not in report.missing
        # Path changed → relocated
        assert "root" in report.relocated

    def test_empty_state_means_no_validation(self) -> None:
        """Pre-roles installs have no disk-roles.json — validation
        should be a no-op (no warnings)."""
        tmp = Path(tempfile.mkdtemp())
        report = validate_disk_roles(tmp, [_disk()])
        assert not report.ok
        assert not report.has_issues
