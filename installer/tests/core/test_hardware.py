"""Tests for hardware quirk and machine profile detection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from arches_installer.core.hardware import (
    DMIInfo,
    HardwareConfig,
    MachineProfile,
    Quirk,
    detect_pci_ids,
    discover_machines,
    discover_quirks,
    load_machine,
    load_quirk,
    match_quirks,
    resolve_hardware,
    suggest_machine,
)


# ---------------------------------------------------------------------------
# Fixtures — use the real hardware/ directory from the project root
# ---------------------------------------------------------------------------

HARDWARE_DIR = Path(__file__).resolve().parents[3] / "hardware"


@pytest.fixture
def hw_dir() -> Path:
    """Path to the project's hardware/ directory."""
    assert HARDWARE_DIR.is_dir(), f"hardware/ not found at {HARDWARE_DIR}"
    return HARDWARE_DIR


# ---------------------------------------------------------------------------
# TOML loading
# ---------------------------------------------------------------------------


class TestLoadQuirk:
    def test_load_intel_wifi7(self, hw_dir: Path) -> None:
        q = load_quirk(hw_dir / "quirks" / "intel-wifi7-disable-be.toml")
        assert q.slug == "intel-wifi7-disable-be"
        assert q.name == "Intel Wi-Fi 7 stability fix"
        assert "8086:272b" in q.pci_ids
        assert "arches-intel-wifi7.conf" in q.modprobe
        assert "disable_11be" in q.modprobe["arches-intel-wifi7.conf"]

    def test_load_nvidia_rtd3(self, hw_dir: Path) -> None:
        q = load_quirk(hw_dir / "quirks" / "nvidia-rtd3-laptop.toml")
        assert q.slug == "nvidia-rtd3-laptop"
        assert q.pci_vendor == "10de"
        assert 9 in q.chassis_type  # notebook
        assert "arches-nvidia-rtd3.conf" in q.modprobe
        assert "80-nvidia-rtd3.rules" in q.udev
        assert q.files_dir is not None
        assert q.files_dir.is_dir()


class TestLoadMachine:
    def test_load_thinkpad_p1(self, hw_dir: Path) -> None:
        m = load_machine(hw_dir / "machines" / "thinkpad-p1-gen7.toml")
        assert m.slug == "thinkpad-p1-gen7"
        assert m.sys_vendor == "LENOVO"
        assert m.product_name_pattern == "21KW*"
        assert "intel-wifi7-disable-be" in m.quirk_includes
        assert "nvidia-rtd3-laptop" in m.quirk_includes
        assert "tp_smapi" in m.packages
        assert "tuned" in m.services
        assert "power" in m.ansible_firstboot_roles
        assert not m.is_generic

    def test_load_generic_laptop(self, hw_dir: Path) -> None:
        m = load_machine(hw_dir / "machines" / "generic-laptop.toml")
        assert m.is_generic
        assert 9 in m.chassis_type

    def test_load_vm(self, hw_dir: Path) -> None:
        m = load_machine(hw_dir / "machines" / "vm.toml")
        assert m.slug == "vm"
        assert m.sys_vendor_pattern == "QEMU*"
        # VM-specific packages
        assert "qemu-guest-agent" in m.packages
        assert "spice-vdagent" in m.packages
        # Services to enable
        assert "qemu-guest-agent" in m.services
        # Services to disable (suppress from template)
        assert "bluetooth" in m.services_disable
        assert "fstrim.timer" in m.services_disable
        # Modprobe config for virtio
        assert "arches-vm-virtio.conf" in m.modprobe
        assert "virtio_pci" in m.modprobe["arches-vm-virtio.conf"]
        # Sysctl tuning
        assert "90-arches-vm.conf" in m.sysctl
        assert "nmi_watchdog" in m.sysctl["90-arches-vm.conf"]


class TestDiscovery:
    def test_discover_quirks(self, hw_dir: Path) -> None:
        quirks = discover_quirks(hw_dir)
        assert len(quirks) >= 2
        slugs = {q.slug for q in quirks}
        assert "intel-wifi7-disable-be" in slugs
        assert "nvidia-rtd3-laptop" in slugs

    def test_discover_machines(self, hw_dir: Path) -> None:
        machines = discover_machines(hw_dir)
        assert len(machines) >= 3
        slugs = {m.slug for m in machines}
        assert "thinkpad-p1-gen7" in slugs
        assert "generic-laptop" in slugs


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


class TestQuirkMatching:
    def test_intel_wifi7_matches_be200(self, hw_dir: Path) -> None:
        quirks = discover_quirks(hw_dir)
        pci_ids = {"8086:272b", "8086:7d55"}  # BE200 + Intel Arc
        matched = match_quirks(quirks, pci_ids, chassis_type=9)
        slugs = {q.slug for q in matched}
        assert "intel-wifi7-disable-be" in slugs

    def test_intel_wifi7_no_match_without_device(self, hw_dir: Path) -> None:
        quirks = discover_quirks(hw_dir)
        pci_ids = {"8086:7d55"}  # Intel Arc only, no WiFi 7
        matched = match_quirks(quirks, pci_ids, chassis_type=9)
        slugs = {q.slug for q in matched}
        assert "intel-wifi7-disable-be" not in slugs

    def test_nvidia_rtd3_matches_laptop_with_nvidia(self, hw_dir: Path) -> None:
        quirks = discover_quirks(hw_dir)
        pci_ids = {"10de:2838", "8086:7d55"}  # RTX 3000 Ada + Intel Arc
        matched = match_quirks(quirks, pci_ids, chassis_type=10)  # laptop
        slugs = {q.slug for q in matched}
        assert "nvidia-rtd3-laptop" in slugs

    def test_nvidia_rtd3_no_match_on_desktop(self, hw_dir: Path) -> None:
        quirks = discover_quirks(hw_dir)
        pci_ids = {"10de:2838"}  # RTX 3000 Ada
        matched = match_quirks(quirks, pci_ids, chassis_type=3)  # desktop
        slugs = {q.slug for q in matched}
        assert "nvidia-rtd3-laptop" not in slugs

    def test_no_quirks_on_clean_vm(self, hw_dir: Path) -> None:
        quirks = discover_quirks(hw_dir)
        pci_ids = {"1234:1111", "8086:1237"}  # QEMU VGA + host bridge
        matched = match_quirks(quirks, pci_ids, chassis_type=1)  # "other"
        assert len(matched) == 0


class TestMachineMatching:
    def test_thinkpad_p1_detected(self, hw_dir: Path) -> None:
        machines = discover_machines(hw_dir)
        dmi = DMIInfo(
            sys_vendor="LENOVO",
            product_name="21KWS3UJ00",
            chassis_type=10,
        )
        result = suggest_machine(machines, dmi)
        assert result is not None
        assert result.slug == "thinkpad-p1-gen7"

    def test_unknown_lenovo_gets_generic_laptop(self, hw_dir: Path) -> None:
        machines = discover_machines(hw_dir)
        dmi = DMIInfo(
            sys_vendor="LENOVO",
            product_name="21XXUNKNOWN",
            chassis_type=10,
        )
        result = suggest_machine(machines, dmi)
        assert result is not None
        assert result.slug == "generic-laptop"

    def test_qemu_gets_vm(self, hw_dir: Path) -> None:
        machines = discover_machines(hw_dir)
        dmi = DMIInfo(
            sys_vendor="QEMU",
            product_name="Standard PC (Q35 + ICH9, 2009)",
            chassis_type=1,
        )
        result = suggest_machine(machines, dmi)
        assert result is not None
        assert result.slug == "vm"

    def test_specific_beats_generic(self, hw_dir: Path) -> None:
        """ThinkPad P1 should match specific profile, not generic-laptop."""
        machines = discover_machines(hw_dir)
        dmi = DMIInfo(
            sys_vendor="LENOVO",
            product_name="21KWS3UJ00",
            chassis_type=10,
        )
        result = suggest_machine(machines, dmi)
        assert result is not None
        assert result.slug == "thinkpad-p1-gen7"
        assert not result.is_generic


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestResolveHardware:
    def test_merge_machine_quirks_with_auto_detected(self, hw_dir: Path) -> None:
        all_quirks = discover_quirks(hw_dir)
        machine = load_machine(hw_dir / "machines" / "thinkpad-p1-gen7.toml")

        # Simulate: only wifi quirk was auto-detected
        wifi_quirk = [q for q in all_quirks if q.slug == "intel-wifi7-disable-be"]
        hw = resolve_hardware(machine, wifi_quirk, all_quirks)

        # Both quirks should be present (wifi auto-detected + nvidia from machine includes)
        slugs = {q.slug for q in hw.quirks}
        assert "intel-wifi7-disable-be" in slugs
        assert "nvidia-rtd3-laptop" in slugs

    def test_deduplicate_quirks(self, hw_dir: Path) -> None:
        all_quirks = discover_quirks(hw_dir)
        machine = load_machine(hw_dir / "machines" / "thinkpad-p1-gen7.toml")

        # Both quirks auto-detected AND included by machine -> no duplicates
        hw = resolve_hardware(machine, all_quirks, all_quirks)
        slugs = [q.slug for q in hw.quirks]
        assert len(slugs) == len(set(slugs))

    def test_packages_from_machine(self, hw_dir: Path) -> None:
        machine = load_machine(hw_dir / "machines" / "thinkpad-p1-gen7.toml")
        hw = resolve_hardware(machine, [], [])
        assert "tp_smapi" in hw.all_packages
        assert "tuned" in hw.all_services

    def test_no_machine_quirks_only(self, hw_dir: Path) -> None:
        all_quirks = discover_quirks(hw_dir)
        wifi_quirk = [q for q in all_quirks if q.slug == "intel-wifi7-disable-be"]
        hw = resolve_hardware(None, wifi_quirk)
        assert hw.machine is None
        assert len(hw.quirks) == 1
        assert hw.all_packages == []
        assert "arches-intel-wifi7.conf" in hw.all_modprobe

    def test_merged_modprobe(self, hw_dir: Path) -> None:
        all_quirks = discover_quirks(hw_dir)
        machine = load_machine(hw_dir / "machines" / "thinkpad-p1-gen7.toml")
        hw = resolve_hardware(machine, all_quirks, all_quirks)

        modprobe = hw.all_modprobe
        assert "arches-intel-wifi7.conf" in modprobe
        assert "arches-nvidia-rtd3.conf" in modprobe

    def test_merged_udev(self, hw_dir: Path) -> None:
        all_quirks = discover_quirks(hw_dir)
        hw = resolve_hardware(None, all_quirks)
        udev = hw.all_udev
        assert "80-nvidia-rtd3.rules" in udev

    def test_vm_services_disable(self, hw_dir: Path) -> None:
        machine = load_machine(hw_dir / "machines" / "vm.toml")
        hw = resolve_hardware(machine, [])
        assert "qemu-guest-agent" in hw.all_services
        assert "bluetooth" in hw.all_services_disable
        assert "fstrim.timer" in hw.all_services_disable

    def test_vm_modprobe_and_sysctl(self, hw_dir: Path) -> None:
        machine = load_machine(hw_dir / "machines" / "vm.toml")
        hw = resolve_hardware(machine, [])
        assert "arches-vm-virtio.conf" in hw.all_modprobe
        assert "90-arches-vm.conf" in hw.all_sysctl

    def test_services_disable_empty_without_machine(self) -> None:
        hw = resolve_hardware(None, [])
        assert hw.all_services_disable == []


# ---------------------------------------------------------------------------
# PCI ID detection (mocked)
# ---------------------------------------------------------------------------


class TestDetectPciIds:
    def test_parses_lspci_output(self) -> None:
        fake_output = (
            "00:02.0 VGA [0300]: Intel Corp [8086:7d55] (rev 08)\n"
            "01:00.0 3D ctrl [0302]: NVIDIA Corp [10de:2838] (rev a1)\n"
            "09:00.0 Net ctrl [0280]: Intel Corp [8086:272b] (rev 1a)\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = fake_output
            mock_run.return_value.returncode = 0
            ids = detect_pci_ids()

        assert "8086:7d55" in ids
        assert "10de:2838" in ids
        assert "8086:272b" in ids

    def test_empty_on_no_lspci(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            ids = detect_pci_ids()
        assert ids == set()
