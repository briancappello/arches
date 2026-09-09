"""Tests covering descriptor matching for QEMU-style virtual disks.

The scripts/qemu-install.sh harness attaches virtio disks with
predictable serials (``ARCHES-TEST-NN``) and explicit sizes. These
tests verify that the descriptor parser handles QEMU's specific
attribute shape correctly:

  - transport reported by lsblk as ``virtio``
  - model usually empty or 'QEMU HARDDISK' (older kernels)
  - vendor usually empty
  - serial settable via QEMU's ``serial=...`` device option

This is the lowest-cost simulation of the qemu-install-llm flow:
exercise the resolver against synthetic BlockDevices that match what
lsblk on a real QEMU VM would report.
"""

from __future__ import annotations

import pytest
import subprocess

from arches_installer.core.disk import BlockDevice
from arches_installer.core.disk_layout import (
    DiskRole,
    DiskRoleResolutionError,
    load_disk_layout,
    resolve_disk_roles,
)
from arches_installer.core.disk_descriptor import (
    matches,
    parse_descriptor,
)
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("case", ["existing", "missing", "no-value", "rebuild"])
def test_explicit_iso_selection(tmp_path: Path, case: str) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("qemu-install.sh", "detect-platform.sh"):
        source = (_REPO_ROOT / "scripts" / name).read_text()
        source = source.replace("/tmp/arches-test-disk", str(tmp_path / "disk"))
        source = source.replace("/tmp/arches-efi-vars.raw", str(tmp_path / "vars"))
        (scripts / name).write_text(source)
    commands = tmp_path / "bin"
    commands.mkdir()
    (commands / "dirname").symlink_to("/usr/bin/dirname")
    calls = tmp_path / "calls"
    for name in ("python3", "sudo", "rm", "cp", "qemu-img"):
        stub = commands / name
        stub.write_text(f'#!/bin/bash\nprintf "{name}\\n" >> "$CALLS"\nexit 71\n')
        stub.chmod(0o755)
    builder = scripts / "build-iso.sh"
    builder.write_text('#!/bin/bash\nprintf "builder\\n" >> "$CALLS"\nexit 72\n')
    builder.chmod(0o755)
    iso = tmp_path / "selected artifact.iso"
    if case != "missing":
        iso.touch()
    args = ["--iso"] if case == "no-value" else ["--iso", str(iso)]
    if case == "existing":
        args.extend(["--platform", "x86-64"])
    if case == "missing":
        args.append("--fresh-disk")
    if case == "rebuild":
        args.append("--rebuild")
    result = subprocess.run(
        ["/bin/bash", str(scripts / "qemu-install.sh"), *args],
        env={"PATH": str(commands), "PLATFORM": "x86-64", "CALLS": str(calls)},
        capture_output=True, text=True, timeout=10,
    )
    if case == "existing":
        assert f"ISO: {iso}" in result.stdout
        assert calls.read_text() == "qemu-img\n"
        assert result.returncode == 71
    else:
        assert result.returncode == 2
        assert "--iso" in result.stderr
        assert {"missing": "not found", "no-value": "requires", "rebuild": "--rebuild"}[case] in result.stderr
        assert not calls.exists()


# ---------------------------------------------------------------------------
# Helpers — mimic exactly what lsblk on a QEMU VM reports
# ---------------------------------------------------------------------------


def _qemu_disk(
    name: str,
    size_bytes: int,
    serial: str,
) -> BlockDevice:
    """Synthesise the lsblk output a real QEMU virtio disk produces.

    Real-world reference (lsblk -b -d -o NAME,PATH,SIZE,MODEL,VENDOR,
    SERIAL,WWN,TRAN,ROTA on a QEMU virtio-blk-pci disk):

      NAME    PATH         SIZE          MODEL          VENDOR  SERIAL          WWN  TRAN     ROTA
      vda     /dev/vda     21474836480                          ARCHES-TEST-01       virtio   0

    Note model and vendor are typically empty on virtio-blk; WWN is
    blank; rotational is 0; transport is "virtio".
    """
    return BlockDevice(
        name=name,
        path=f"/dev/{name}",
        size=f"{size_bytes // 10**9}G",
        size_bytes=size_bytes,
        model="",  # virtio-blk-pci has no model
        vendor="",
        serial=serial,
        wwn="",
        transport="virtio",
        rotational=False,
        removable=False,
        partitions=[],
        by_id_links=[f"virtio-{serial}"],  # /dev/disk/by-id/virtio-<serial>
    )


def _qemu_two_disk_setup() -> list[BlockDevice]:
    """The disks created by `make qemu-install-llm` (--disk 20G --disk 60G)."""
    return [
        _qemu_disk("vda", 20 * 10**9, "ARCHES-TEST-01"),
        _qemu_disk("vdb", 60 * 10**9, "ARCHES-TEST-02"),
    ]


# ---------------------------------------------------------------------------
# Direct descriptor matching against QEMU disks
# ---------------------------------------------------------------------------


class TestQemuDirectMatching:
    def test_size_descriptor_picks_correct_disk(self) -> None:
        disks = _qemu_two_disk_setup()

        c = parse_descriptor("20G virtio")
        assert matches(c, disks[0])
        assert not matches(c, disks[1])

        c = parse_descriptor("60G virtio")
        assert matches(c, disks[1])
        assert not matches(c, disks[0])

    def test_serial_descriptor_picks_correct_disk(self) -> None:
        disks = _qemu_two_disk_setup()

        c = parse_descriptor("ARCHES-TEST-01")
        assert matches(c, disks[0])
        assert not matches(c, disks[1])

        c = parse_descriptor("ARCHES-TEST-02")
        assert matches(c, disks[1])
        assert not matches(c, disks[0])

    def test_size_with_ssd_token(self) -> None:
        """QEMU virtio is non-rotational, so SSD descriptors match."""
        disks = _qemu_two_disk_setup()
        c = parse_descriptor("20G SSD")
        assert matches(c, disks[0])
        assert not matches(c, disks[1])

    def test_bare_virtio_matches_both(self) -> None:
        """`device = "virtio"` matches every virtio disk — useful for
        machine profiles that don't care which disk is which."""
        disks = _qemu_two_disk_setup()
        c = parse_descriptor("virtio")
        for d in disks:
            assert matches(c, d)

    def test_size_tolerance_handles_qemu_sizing(self) -> None:
        """QEMU's qcow2 reports `qemu-img create -f qcow2 ... 20G` as
        21474836480 bytes (= 20 × 2^30 = 20 GiB), which is 21.5 GB in
        decimal units. The descriptor "20G" (treated as 20 × 10^9 =
        20 GB) needs the ±10% tolerance to match this."""
        d = _qemu_disk("vda", 21_474_836_480, "ARCHES-TEST-01")  # 20 GiB
        c = parse_descriptor("20G")
        # 20G descriptor -> window [18G, 22G] decimal
        # disk is 21.47G decimal -> matches
        assert matches(c, d)


# ---------------------------------------------------------------------------
# Full llm-workstation layout against QEMU disks
# ---------------------------------------------------------------------------


class TestLlmWorkstationOnQemu:
    """Simulate `make qemu-install-llm` end-to-end resolution."""

    def test_default_layout_descriptors_dont_match_virtio(self) -> None:
        """The shipped llm-workstation.toml uses 'NVMe SSD' and 'SATA'
        descriptors, neither of which matches virtio. This is the
        intended behaviour — the operator must supply per-install
        overrides for QEMU testing (or pin via machine profile)."""
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        disks = _qemu_two_disk_setup()

        with pytest.raises(DiskRoleResolutionError) as exc:
            resolve_disk_roles(layout, disks)
        msg = str(exc.value)
        # Both roles fail their descriptor since neither virtio disk
        # is NVMe or SATA.
        assert "root" in msg
        assert "bulk" in msg

    def test_override_with_size_descriptors(self) -> None:
        """The expected QEMU-test workflow: pin roles by size in an
        auto-install [[disks]] override."""
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        disks = _qemu_two_disk_setup()

        resolved = resolve_disk_roles(
            layout,
            disks,
            auto_install_disks=[
                DiskRole(name="root", descriptor="20G virtio"),
                DiskRole(name="bulk", descriptor="60G virtio"),
            ],
        )
        assert resolved.primary("root").path == "/dev/vda"
        assert resolved.primary("bulk").path == "/dev/vdb"

    def test_override_with_serial_descriptors(self) -> None:
        """Same flow but pinning by the QEMU-assigned serial number."""
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        disks = _qemu_two_disk_setup()

        resolved = resolve_disk_roles(
            layout,
            disks,
            auto_install_disks=[
                DiskRole(name="root", descriptor="ARCHES-TEST-01"),
                DiskRole(name="bulk", descriptor="ARCHES-TEST-02"),
            ],
        )
        assert resolved.primary("root").serial == "ARCHES-TEST-01"
        assert resolved.primary("bulk").serial == "ARCHES-TEST-02"

    def test_role_swap_via_descriptors(self) -> None:
        """Same disks, swap which is 'root' vs 'bulk'. The resolver
        should honour the descriptors regardless of disk enumeration
        order."""
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        disks = _qemu_two_disk_setup()

        resolved = resolve_disk_roles(
            layout,
            disks,
            auto_install_disks=[
                # Put root on the BIGGER disk; bulk on the smaller one.
                DiskRole(name="root", descriptor="60G virtio"),
                DiskRole(name="bulk", descriptor="20G virtio"),
            ],
        )
        assert resolved.primary("root").size_bytes == 60 * 10**9
        assert resolved.primary("bulk").size_bytes == 20 * 10**9

    def test_overlapping_descriptors_fail_cleanly(self) -> None:
        """If both role descriptors match the same disk and there's no
        other candidate, resolution must fail rather than silently
        partitioning the same disk twice."""
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/llm-workstation.toml")
        disks = _qemu_two_disk_setup()

        with pytest.raises(DiskRoleResolutionError) as exc:
            resolve_disk_roles(
                layout,
                disks,
                auto_install_disks=[
                    DiskRole(name="root", descriptor="virtio"),
                    DiskRole(name="bulk", descriptor="virtio"),
                ],
            )
        msg = str(exc.value)
        # Both descriptors match both disks (ambiguous) AND the second
        # role would claim a disk the first already took. Either error
        # is acceptable; both should mention the conflict.
        assert "bulk" in msg


# ---------------------------------------------------------------------------
# Single-disk QEMU mode (legacy `make qemu-install` flow)
# ---------------------------------------------------------------------------


class TestSingleDiskQemu:
    def test_basic_layout_implicit_default_matches_single_virtio(self) -> None:
        """The implicit single-disk default ('removable=False') should
        match a QEMU virtio disk without any overrides."""
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/basic.toml")
        disks = [_qemu_disk("vda", 60 * 10**9, "ARCHES-TEST-01")]
        resolved = resolve_disk_roles(layout, disks)
        # The legacy basic.toml uses the implicit "primary" role with
        # criteria {removable: False}. virtio disks are non-removable.
        assert resolved.primary("primary").path == "/dev/vda"

    def test_basic_layout_two_disks_is_ambiguous(self) -> None:
        """basic.toml has no [[disks]] block, so the implicit single-disk
        default is used. Two disks → ambiguous → fail."""
        layout = load_disk_layout(_REPO_ROOT / "disk-layouts/basic.toml")
        with pytest.raises(DiskRoleResolutionError, match="ambiguous"):
            resolve_disk_roles(layout, _qemu_two_disk_setup())
