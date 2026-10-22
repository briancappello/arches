"""Tests for hardware quirk and machine profile detection."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from arches_installer.core.hardware import (
    DMIInfo,
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

    def test_thinkpad_p1_fan_control(self, hw_dir: Path) -> None:
        """thinkfan is installed from arches-local, configured by the power role.

        thinkfan is AUR-only, so pacstrap can only install it if the platform
        pre-builds it.  The profile must NOT enable the service: its config
        (/etc/thinkfan.yaml) is deployed by the power role at first boot, and
        that role enables thinkfan once the config exists.
        """
        import tomllib

        m = load_machine(hw_dir / "machines" / "thinkpad-p1-gen7.toml")
        assert "thinkfan" in m.packages
        assert "thinkfan" not in m.services

        platform_toml = hw_dir.parent / "platforms" / m.platform / "platform.toml"
        aur = tomllib.loads(platform_toml.read_text())["aur_packages"]["build"]
        assert "thinkfan" in aur

        role = hw_dir.parent / "ansible" / "roles" / "power"
        assert (role / "files" / "thinkfan.yaml").is_file()
        tasks = (role / "tasks" / "main.yml").read_text()
        assert "/etc/thinkfan.yaml" in tasks
        assert "thinkfan.service" in tasks
        # thinkpad_acpi loads from the initramfs; without a rebuild the
        # fan_control option is lost on reboot and thinkfan crash-loops.
        assert "limine-mkinitcpio" in tasks

    def test_thinkpad_p1_fan_curve_covers_gpu(self, hw_dir: Path) -> None:
        """The curve watches the dGPU through the EC, never through NVML.

        With thinkfan in control the EC no longer reacts to GPU heat, so
        the GPU needs a column of its own.  An nvml sensor would wake the
        dGPU from runtime suspend on every poll and keep it at ~16 W.
        thinkfan needs one limit per sensor on every level.
        """
        conf = hw_dir.parent / "ansible" / "roles" / "power" / "files" / "thinkfan.yaml"
        lines = [ln.split("#", 1)[0] for ln in conf.read_text().splitlines()]
        body = "\n".join(lines)
        assert "nvml" not in body
        assert re.search(r"tpacpi: /proc/acpi/ibm/thermal\s+indices: \[1\]", body)

        block = body.split("\nsensors:", 1)[1].split("\nfans:", 1)[0]
        sensors = len(re.findall(r"^\s+- (?:hwmon|tpacpi|nvml):", block, re.M))
        assert sensors == 3
        limits = re.findall(r"(?:lower|upper)_limit: \[([^\]]*)\]", body)
        assert limits
        for lim in limits:
            assert len(lim.split(",")) == sensors, lim

    def test_thinkpad_p1_tuned_profiles(self, hw_dir: Path) -> None:
        """The power role's tuned profiles stay verifiable and complete.

        intel_pstate exposes no cpufreq boost knob: tuned applies ``boost``
        via no_turbo, but ``tuned-adm verify`` then fails on every CPU.
        Profiles must use ``no_turbo`` instead.  A profile that declares
        a ``[script]`` section must ship that script.
        """
        import configparser

        tuned = hw_dir.parent / "ansible" / "roles" / "power" / "files" / "tuned"
        profiles = sorted(tuned.iterdir())
        assert [p.name for p in profiles] == [
            "p1-balanced",
            "p1-performance",
            "p1-powersave",
        ]
        for profile in profiles:
            cfg = configparser.ConfigParser(interpolation=None)
            cfg.read(profile / "tuned.conf")
            if cfg.has_section("cpu"):
                assert "boost" not in cfg["cpu"], f"{profile.name}: use no_turbo"
            has_script = cfg.has_section("script")
            assert (profile / "script.sh").is_file() == has_script, profile.name

    def test_thinkpad_p1_edp_panel_workaround(self, hw_dir: Path) -> None:
        """The internal panel fix: i915 power features off, plus a watchdog.

        The modprobe drop must disable PSR, Panel Replay and DC5/DC6
        together; any one left on brings back the black-panel failure.
        The edp-recovery role must be in the playbook, or first boot
        skips it silently.  Its scripts must never hardcode a Wayland
        socket name: it changes between sessions, and kscreen-doctor
        dumps core on a wrong one.
        """
        m = load_machine(hw_dir / "machines" / "thinkpad-p1-gen7.toml")
        options = m.modprobe["arches-i915-psr.conf"]
        for opt in ("enable_psr=0", "enable_panel_replay=0", "enable_dc=0"):
            assert opt in options

        assert "edp-recovery" in m.ansible_firstboot_roles
        ansible = hw_dir.parent / "ansible"
        assert "role: edp-recovery" in (ansible / "playbook.yml").read_text()

        files = ansible / "roles" / "edp-recovery" / "files"
        for script in ("edp-watchdog", "edp-recover"):
            text = (files / script).read_text()
            assert "source /usr/local/lib/edp-recovery/kscreen.sh" in text
            assert "WAYLAND_DISPLAY" not in text, script
            # Direct calls bypass the socket lookup in kdoc.
            direct = re.search(r"^\s*kscreen-doctor\b|\$\(kscreen-doctor\b", text, re.M)
            assert direct is None, script
        unit = (files / "edp-watchdog.service").read_text()
        assert "WantedBy=graphical-session.target" in unit

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
