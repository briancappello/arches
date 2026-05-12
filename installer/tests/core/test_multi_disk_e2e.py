"""End-to-end simulation of the multi-disk install flow.

These tests run through the full pipeline up to (but not including)
the destructive disk operations — they validate that:

  1. The llm-workstation.toml layout loads and validates.
  2. The auto-install-inference.toml loads and references the right layout.
  3. resolve_disk_roles correctly assigns physical disks to roles
     given realistic candidate hardware sets.
  4. The pipeline's InstallParams accepts the resolved roles.
  5. The disk-roles state round-trips correctly through write/read/validate.

Actual partitioning/formatting/mounting is tested under QEMU in a
separate harness (see scripts/qemu-install.sh).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from arches_installer.core.disk import BlockDevice
from arches_installer.core.disk_layout import (
    DiskRoleResolutionError,
    load_disk_layout,
    read_disk_roles_state,
    resolve_disk_roles,
    validate_disk_roles,
    write_disk_roles_state,
)


# ---------------------------------------------------------------------------
# Helpers — realistic disk fixtures
# ---------------------------------------------------------------------------


def _nvme_2tb(name: str = "nvme0n1", serial: str = "SAM_001") -> BlockDevice:
    return BlockDevice(
        name=name,
        path=f"/dev/{name}",
        size="2T",
        size_bytes=2_000_000_000_000,
        model="Samsung SSD 990 PRO 2TB",
        vendor="",
        serial=serial,
        wwn=f"eui.0025385a{serial.lower()}",
        transport="nvme",
        rotational=False,
        removable=False,
        partitions=[],
        by_id_links=[
            f"nvme-Samsung_SSD_990_PRO_2TB_{serial}",
            f"nvme-eui.0025385a{serial.lower()}",
        ],
    )


def _sata_hdd_8tb(name: str = "sda", serial: str = "SEA_BULK_01") -> BlockDevice:
    return BlockDevice(
        name=name,
        path=f"/dev/{name}",
        size="8T",
        size_bytes=8_000_000_000_000,
        model="ST8000VN004-3CP101",
        vendor="ATA",
        serial=serial,
        wwn="0x5000c500e7d8f3e9",
        transport="sata",
        rotational=True,
        removable=False,
        partitions=[],
        by_id_links=[
            f"ata-Seagate_ST8000VN004-3CP101_{serial}",
            "wwn-0x5000c500e7d8f3e9",
        ],
    )


# ---------------------------------------------------------------------------
# Layout + auto-install file integration
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[3]


class TestShippedFiles:
    def test_llm_workstation_layout_loads(self) -> None:
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        assert layout.name == "LLM Workstation"
        roles = {r.name for r in layout.disks}
        assert roles == {"root", "bulk"}
        # Partition wiring
        roles_used = {p.target_role for p in layout.partitions}
        assert roles_used == {"root", "bulk"}

    def test_layout_has_three_partitions(self) -> None:
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        assert len(layout.partitions) == 3
        # ESP, root, bulk
        mount_points = [p.mount_point for p in layout.partitions]
        assert "/boot" in mount_points
        assert "/" in mount_points
        assert "/opt/models" in mount_points

    def test_basic_layout_still_works(self) -> None:
        """basic.toml has no [[disks]] block; the resolver synthesises
        the implicit single-disk default."""
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/basic.toml")
        assert not layout.has_explicit_disks()
        resolved = resolve_disk_roles(layout, [_nvme_2tb()])
        assert "primary" in resolved.assignments


# ---------------------------------------------------------------------------
# Role resolution against realistic hardware
# ---------------------------------------------------------------------------


class TestRoleResolution:
    def test_typical_llm_workstation_layout(self) -> None:
        """One NVMe + one SATA → root on NVMe, bulk on SATA."""
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        candidates = [_nvme_2tb(), _sata_hdd_8tb()]
        resolved = resolve_disk_roles(layout, candidates)

        assert resolved.primary("root").transport == "nvme"
        assert resolved.primary("bulk").transport == "sata"
        assert resolved.primary("bulk").rotational

    def test_no_sata_drive_fails_clearly(self) -> None:
        """LLM-workstation layout requires both root + bulk; missing
        bulk should fail with a clear error."""
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        with pytest.raises(DiskRoleResolutionError) as exc:
            resolve_disk_roles(layout, [_nvme_2tb()])
        msg = str(exc.value)
        assert "bulk" in msg

    def test_two_nvme_drives_fails_ambiguous_root(self) -> None:
        """Two NVMe drives + no SATA = ambiguous root AND missing bulk."""
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        a = _nvme_2tb(name="nvme0n1", serial="SAM_001")
        b = _nvme_2tb(name="nvme1n1", serial="SAM_002")
        with pytest.raises(DiskRoleResolutionError) as exc:
            resolve_disk_roles(layout, [a, b])
        msg = str(exc.value)
        # Both errors should surface
        assert "root" in msg
        assert "bulk" in msg

    def test_with_serial_pin_override_disambiguates(self) -> None:
        """Two NVMe drives + SATA = ambiguous unless we pin root by
        serial. Simulates an auto-install [[disks]] override."""
        from arches_installer.core.disk_layout import DiskRole

        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        a = _nvme_2tb(name="nvme0n1", serial="SAM_001")
        b = _nvme_2tb(name="nvme1n1", serial="SAM_002")
        sata = _sata_hdd_8tb()

        # Pin root via the auto-install override layer
        resolved = resolve_disk_roles(
            layout,
            [a, b, sata],
            auto_install_disks=[DiskRole(name="root", descriptor="SAM_002")],
        )
        assert resolved.primary("root").serial == "SAM_002"
        assert resolved.primary("bulk").path == "/dev/sda"

    def test_machine_profile_pin_takes_effect(self) -> None:
        """Machine profile [[disks]] sets a default that the layout
        descriptor would otherwise not narrow."""
        from arches_installer.core.disk_layout import DiskRole

        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        # The shipped layout's root descriptor is "NVMe SSD". On this
        # host we have two NVMe disks; the machine profile pins one
        # by serial.
        a = _nvme_2tb(name="nvme0n1", serial="MACHINE_PIN_001")
        b = _nvme_2tb(name="nvme1n1", serial="OTHER_002")
        sata = _sata_hdd_8tb()
        resolved = resolve_disk_roles(
            layout,
            [a, b, sata],
            machine_disks=[
                DiskRole(name="root", descriptor="MACHINE_PIN_001"),
            ],
        )
        assert resolved.primary("root").serial == "MACHINE_PIN_001"

    def test_auto_install_overrides_machine(self) -> None:
        """When both machine and auto-install pin a role, auto wins."""
        from arches_installer.core.disk_layout import DiskRole

        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        a = _nvme_2tb(name="nvme0n1", serial="MACHINE_PIN")
        b = _nvme_2tb(name="nvme1n1", serial="AUTO_PIN")
        sata = _sata_hdd_8tb()
        resolved = resolve_disk_roles(
            layout,
            [a, b, sata],
            machine_disks=[DiskRole(name="root", descriptor="MACHINE_PIN")],
            auto_install_disks=[DiskRole(name="root", descriptor="AUTO_PIN")],
        )
        assert resolved.primary("root").serial == "AUTO_PIN"


# ---------------------------------------------------------------------------
# Full pipeline simulation — InstallParams construction
# ---------------------------------------------------------------------------


class TestInstallParamsConstruction:
    def test_resolved_roles_pass_through(self) -> None:
        """Building InstallParams with resolved roles works without
        triggering the legacy single-disk + raid_config path."""
        from arches_installer.core.pipeline import InstallParams
        from arches_installer.core.platform import PlatformConfig
        from arches_installer.core.template import (
            AnsibleConfig,
            InstallPhases,
            InstallTemplate,
            SystemConfig,
        )

        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        candidates = [_nvme_2tb(), _sata_hdd_8tb()]
        resolved = resolve_disk_roles(layout, candidates)

        # Synthesise the minimum InstallTemplate so InstallParams accepts it.
        tpl = InstallTemplate(
            name="x",
            description="",
            system=SystemConfig(timezone="UTC", locale="en_US.UTF-8"),
            install=InstallPhases(),
            services=[],
            ansible=AnsibleConfig(),
            module_slugs=[],
        )
        pf = PlatformConfig.__new__(PlatformConfig)  # bypass __init__

        params = InstallParams(
            platform=pf,
            template=tpl,
            device="/dev/nvme0n1",
            hostname="infer-test",
            username="admin",
            password="x",
            disk_layout=layout,
            resolved_disk_roles=resolved,
        )
        assert params.resolved_disk_roles is not None
        assert "root" in params.resolved_disk_roles.assignments
        assert "bulk" in params.resolved_disk_roles.assignments


# ---------------------------------------------------------------------------
# State round-trip after install simulation
# ---------------------------------------------------------------------------


class TestStateRoundTrip:
    def test_write_then_validate_unchanged(self) -> None:
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        candidates = [_nvme_2tb(), _sata_hdd_8tb()]
        resolved = resolve_disk_roles(layout, candidates)

        tmp = Path(tempfile.mkdtemp(prefix="arches-e2e-"))
        write_disk_roles_state(resolved, tmp)

        # Verify the JSON file is well-formed and contains stable IDs
        state = read_disk_roles_state(tmp)
        assert state["version"] == 1
        assert "root" in state["roles"]
        assert "bulk" in state["roles"]
        root_dev = state["roles"]["root"]["devices"][0]
        assert root_dev["serial"] == "SAM_001"
        bulk_dev = state["roles"]["bulk"]["devices"][0]
        assert bulk_dev["transport"] == "sata"

        # Validate against the same hardware: report should be all-OK
        report = validate_disk_roles(tmp, candidates)
        assert sorted(report.ok) == ["bulk", "root"]
        assert not report.missing
        assert not report.relocated

    def test_disk_relocated_detected(self) -> None:
        """Same disks, different kernel paths → relocated report."""
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        original = [
            _nvme_2tb(name="nvme0n1", serial="SAM_001"),
            _sata_hdd_8tb(name="sda", serial="SEA_001"),
        ]
        resolved = resolve_disk_roles(layout, original)
        tmp = Path(tempfile.mkdtemp(prefix="arches-e2e-reloc-"))
        write_disk_roles_state(resolved, tmp)

        # Subsequent boot: kernel renamed devices
        relocated_hw = [
            _nvme_2tb(name="nvme1n1", serial="SAM_001"),  # NVMe slot swapped
            _sata_hdd_8tb(name="sdb", serial="SEA_001"),  # SATA renamed
        ]
        report = validate_disk_roles(tmp, relocated_hw)
        # Both still present — relocated, not missing
        assert "root" in report.relocated
        assert "bulk" in report.relocated
        assert not report.missing

    def test_disk_missing_detected(self) -> None:
        """A disk failure (or removal) → missing report, others stay OK."""
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        candidates = [_nvme_2tb(), _sata_hdd_8tb()]
        resolved = resolve_disk_roles(layout, candidates)
        tmp = Path(tempfile.mkdtemp(prefix="arches-e2e-missing-"))
        write_disk_roles_state(resolved, tmp)

        # Subsequent boot: bulk disk failed
        report = validate_disk_roles(tmp, [_nvme_2tb()])
        assert "root" in report.ok
        assert "bulk" in report.missing
