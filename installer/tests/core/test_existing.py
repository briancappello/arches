"""Tests for existing-partition layouts and the D1 guard (core/existing.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from arches_installer.core.disk import BlockDevice
from arches_installer.core.disk_layout import (
    DiskLayout,
    PartitionSpec,
    ResolvedDiskRoles,
    SubvolumeSpec,
    _validate_layout,
    load_disk_layout,
    resolve_disk_roles,
)
from tests.core.fakes import LUKS_UUID
from arches_installer.core.existing import (
    ESP_MARKER,
    ExistingLayoutError,
    SystemProbe,
    apply_existing_layout,
    plan_existing_layout,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
P1_LAYOUT = PROJECT_ROOT / "disk-layouts" / "p1-spare.toml"

DISK_A = "/dev/nvme0n1"
DISK_B = "/dev/nvme1n1"


def _nvme(path: str, serial: str) -> BlockDevice:
    return BlockDevice(
        name=path.rsplit("/", 1)[1],
        path=path,
        size="953.9G",
        model="NVMe disk",
        removable=False,
        partitions=[],
        size_bytes=1_024_209_543_168,
        serial=serial,
        transport="nvme",
    )


def _p1_table() -> dict[str, list[tuple[str, str]]]:
    """p1's partition table: ESP, archroot, spare, archhome on both disks."""
    return {
        disk: [
            (f"{disk}p1", "ESP"),
            (f"{disk}p2", "archroot"),
            (f"{disk}p3", "spare"),
            (f"{disk}p4", "archhome"),
        ]
        for disk in (DISK_A, DISK_B)
    }


class FakeProbe:
    """A read-only probe over a fixed description of the disks."""

    def __init__(self) -> None:
        self.table = _p1_table()
        self.sigs: dict[str, list[str]] = {
            f"{DISK_A}p1": ["vfat"],
            f"{DISK_B}p1": ["vfat"],
            f"{DISK_A}p2": ["btrfs"],
            f"{DISK_B}p2": ["btrfs"],
            f"{DISK_A}p4": ["btrfs"],
            f"{DISK_B}p4": ["btrfs"],
        }
        self.files: dict[str, set[str]] = {f"{DISK_B}p1": {ESP_MARKER}}
        self.users: dict[str, list[str]] = {}
        self.tpm2 = True

    def partitions(self, disk: str) -> list[tuple[str, str]]:
        return self.table[disk]

    def signatures(self, part: str) -> list[str]:
        return self.sigs.get(part, [])

    def in_use(self, part: str) -> list[str]:
        return self.users.get(part, [])

    def has_file(self, part: str, name: str) -> bool:
        return name in self.files.get(part, set())

    def has_tpm2(self) -> bool:
        return self.tpm2


@pytest.fixture
def layout() -> DiskLayout:
    return load_disk_layout(P1_LAYOUT)


@pytest.fixture
def resolved() -> ResolvedDiskRoles:
    return ResolvedDiskRoles(
        assignments={"spare-pair": [_nvme(DISK_A, "A"), _nvme(DISK_B, "B")]}
    )


# ---------------------------------------------------------------------------
# Layout validation
# ---------------------------------------------------------------------------


class TestExistingLayoutValidation:
    def test_p1_layout_loads(self, layout: DiskLayout) -> None:
        assert layout.is_existing
        root = next(p for p in layout.partitions if p.mount_point == "/")
        assert root.partlabel == "spare"
        assert root.raid_level == "0"
        assert root.raid_backend == "mdadm"
        assert [s.name for s in root.subvolumes] == [
            "@",
            "@home",
            "@var",
            "@snapshots",
        ]
        esp = next(p for p in layout.partitions if p.reuse)
        assert esp.filesystem == "vfat"
        assert esp.mount_point == "/boot"

    def test_p1_layout_creates_nothing(self, layout: DiskLayout) -> None:
        # No sizes: an existing layout never creates or resizes partitions.
        assert all(not p.size for p in layout.partitions)

    def _layout(self, *parts: PartitionSpec) -> DiskLayout:
        return DiskLayout(
            name="t",
            description="",
            bootloaders=["limine"],
            partitions=list(parts),
            kind="existing",
        )

    def _esp(self, **kw) -> PartitionSpec:
        base = dict(
            size="",
            partlabel="ESP",
            reuse=True,
            filesystem="vfat",
            mount_point="/boot",
        )
        base.update(kw)
        return PartitionSpec(**base)

    def _root(self, **kw) -> PartitionSpec:
        base = dict(
            size="",
            partlabel="spare",
            filesystem="btrfs",
            mount_point="/",
            subvolumes=[SubvolumeSpec("@", "/")],
        )
        base.update(kw)
        return PartitionSpec(**base)

    def test_valid_minimal(self) -> None:
        assert _validate_layout(self._layout(self._esp(), self._root())) == []

    def test_requires_partlabel(self) -> None:
        errors = _validate_layout(self._layout(self._esp(), self._root(partlabel="")))
        assert any("no partlabel" in e for e in errors)

    def test_rejects_size(self) -> None:
        errors = _validate_layout(self._layout(self._esp(), self._root(size="100G")))
        assert any("never creates or resizes" in e for e in errors)

    def test_requires_exactly_one_reused_esp(self) -> None:
        assert any(
            "exactly one ESP" in e for e in _validate_layout(self._layout(self._root()))
        )
        assert any(
            "exactly one ESP" in e
            for e in _validate_layout(
                self._layout(
                    self._esp(), self._esp(mount_point="/boot/efi"), self._root()
                )
            )
        )

    def test_reused_partition_must_be_vfat_esp(self) -> None:
        errors = _validate_layout(
            self._layout(self._esp(filesystem="btrfs"), self._root())
        )
        assert any("vfat ESP" in e for e in errors)

    def test_raid_needs_mdadm(self) -> None:
        errors = _validate_layout(
            self._layout(
                self._esp(),
                self._root(disks="pair", raid_level="0", raid_backend="btrfs"),
            )
        )
        assert any('raid_backend = "mdadm"' in e for e in errors)

    def test_create_layout_rejects_existing_fields(self) -> None:
        layout = DiskLayout(
            name="t",
            description="",
            bootloaders=[],
            partitions=[
                PartitionSpec(size="2G", filesystem="vfat", mount_point="/boot"),
                PartitionSpec(
                    size="*", filesystem="btrfs", mount_point="/", partlabel="x"
                ),
            ],
        )
        assert any('kind = "existing"' in e for e in _validate_layout(layout))

    def test_unknown_kind(self) -> None:
        layout = self._layout(self._esp(), self._root())
        layout.kind = "resize"
        assert any("Unknown layout kind" in e for e in _validate_layout(layout))


# ---------------------------------------------------------------------------
# Disk roles: the p1 machine profile
# ---------------------------------------------------------------------------


class TestP1MachineProfile:
    def test_spare_pair_resolves_to_both_nvme_disks(self, layout: DiskLayout) -> None:
        from arches_installer.core.hardware import load_machine

        machine = load_machine(PROJECT_ROOT / "hardware/machines/thinkpad-p1-gen7.toml")
        from arches_installer.core.hardware import HardwareConfig

        hw = HardwareConfig(machine=machine, quirks=[])
        usb = BlockDevice(
            name="sda",
            path="/dev/sda",
            size="58G",
            model="USB stick",
            removable=True,
            partitions=[],
            size_bytes=62_000_000_000,
            transport="usb",
        )
        resolved = resolve_disk_roles(
            layout,
            [_nvme(DISK_A, "A"), _nvme(DISK_B, "B"), usb],
            machine_disks=hw.disk_role_overrides(),
        )
        assert [d.path for d in resolved.devices("spare-pair")] == [DISK_A, DISK_B]


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


class TestGuard:
    def test_holds_on_p1_table(self, layout, resolved) -> None:
        plan = plan_existing_layout(layout, resolved, probe=FakeProbe())
        assert plan.esp == f"{DISK_B}p1"
        assert plan.targets == [f"{DISK_A}p3", f"{DISK_B}p3"]

    def test_targets_never_include_other_partitions(self, layout, resolved) -> None:
        plan = plan_existing_layout(layout, resolved, probe=FakeProbe())
        others = {p for parts in _p1_table().values() for p, label in parts} - {
            f"{DISK_A}p3",
            f"{DISK_B}p3",
        }
        assert not others & set(plan.targets)

    def test_refuses_missing_partlabel(self, layout, resolved) -> None:
        probe = FakeProbe()
        probe.table[DISK_B] = [
            (p, lbl) for p, lbl in probe.table[DISK_B] if lbl != "spare"
        ]
        with pytest.raises(ExistingLayoutError, match=r"nvme1n1: expected exactly one"):
            plan_existing_layout(layout, resolved, probe=probe)

    def test_refuses_duplicate_partlabel(self, layout, resolved) -> None:
        probe = FakeProbe()
        probe.table[DISK_A].append((f"{DISK_A}p5", "spare"))
        with pytest.raises(ExistingLayoutError, match="found 2"):
            plan_existing_layout(layout, resolved, probe=probe)

    @pytest.mark.parametrize(
        "sig", ["ext4", "btrfs", "crypto_LUKS", "linux_raid_member", "unknown"]
    )
    def test_refuses_any_signature(self, layout, resolved, sig) -> None:
        probe = FakeProbe()
        probe.sigs[f"{DISK_A}p3"] = [sig]
        with pytest.raises(ExistingLayoutError, match=f"blkid found {sig}"):
            plan_existing_layout(layout, resolved, probe=probe)

    def test_refuses_target_in_use(self, layout, resolved) -> None:
        probe = FakeProbe()
        probe.users[f"{DISK_B}p3"] = ["held by md127"]
        with pytest.raises(ExistingLayoutError, match="in use: held by md127"):
            plan_existing_layout(layout, resolved, probe=probe)

    def test_refuses_esp_without_limine_conf(self, layout, resolved) -> None:
        probe = FakeProbe()
        probe.files = {}
        with pytest.raises(ExistingLayoutError, match="found 0"):
            plan_existing_layout(layout, resolved, probe=probe)

    def test_refuses_two_esps_with_limine_conf(self, layout, resolved) -> None:
        probe = FakeProbe()
        probe.files[f"{DISK_A}p1"] = {ESP_MARKER}
        with pytest.raises(ExistingLayoutError, match="found 2"):
            plan_existing_layout(layout, resolved, probe=probe)

    def test_refuses_esp_that_is_not_vfat(self, layout, resolved) -> None:
        probe = FakeProbe()
        probe.sigs[f"{DISK_B}p1"] = ["ext4"]
        with pytest.raises(
            ExistingLayoutError, match="vfat: nvme0n1p1|vfat: /dev/nvme0n1p1"
        ):
            plan_existing_layout(layout, resolved, probe=probe)

    def test_reports_every_failure_at_once(self, layout, resolved) -> None:
        probe = FakeProbe()
        probe.sigs[f"{DISK_A}p3"] = ["ext4"]
        probe.files = {}
        with pytest.raises(ExistingLayoutError) as exc:
            plan_existing_layout(layout, resolved, probe=probe)
        assert "blkid found ext4" in str(exc.value)
        assert ESP_MARKER in str(exc.value)
        assert "Nothing was written" in str(exc.value)


class TestSystemProbe:
    def test_no_signature_is_blkid_exit_2(self) -> None:
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 2, "", "")
            assert SystemProbe().signatures("/dev/x") == []
        cmd = run.call_args[0][0]
        assert cmd[:2] == ["blkid", "-p"]

    def test_signature_types(self) -> None:
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, "DEVNAME=/dev/x\nUUID=1\nTYPE=linux_raid_member\n", ""
            )
            assert SystemProbe().signatures("/dev/x") == ["linux_raid_member"]

    def test_empty_gpt_partition_has_no_signature(self) -> None:
        # Real `blkid -p -o export` output for p1-style empty "spare"
        # partition (QEMU proof, 2026-10-27): exit 0, only the GPT entry.
        out = (
            "DEVNAME=/dev/nvme0n1p3\nPART_ENTRY_SCHEME=gpt\nPART_ENTRY_NAME=spare\n"
            "PART_ENTRY_UUID=feb986e9-ff92-40fb-8bd6-bc6d06ada1f7\n"
            "PART_ENTRY_TYPE=0fc63daf-8483-4772-8e79-3d69d8477de4\n"
            "PART_ENTRY_NUMBER=3\nPART_ENTRY_OFFSET=20973568\n"
            "PART_ENTRY_SIZE=50331648\nPART_ENTRY_DISK=259:1\n"
        )
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, out, "")
            assert SystemProbe().signatures("/dev/nvme0n1p3") == []

    def test_filesystem_next_to_gpt_entry_is_a_signature(self) -> None:
        out = (
            "DEVNAME=/dev/nvme0n1p2\nLABEL=archroot\nUUID=bdb1\nTYPE=btrfs\n"
            "USAGE=filesystem\nPART_ENTRY_SCHEME=gpt\nPART_ENTRY_NAME=archroot\n"
        )
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, out, "")
            assert SystemProbe().signatures("/dev/nvme0n1p2") == ["btrfs"]

    def test_unnamed_signature_still_counts(self) -> None:
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, "PART_ENTRY_NAME=x\nUSAGE=other\n", ""
            )
            assert SystemProbe().signatures("/dev/x") == ["unknown"]

    def test_blkid_error_refuses(self) -> None:
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 4, "", "boom")
            with pytest.raises(ExistingLayoutError):
                SystemProbe().signatures("/dev/x")


# ---------------------------------------------------------------------------
# Applying the plan touches only the targets
# ---------------------------------------------------------------------------

DESTRUCTIVE = (
    "mdadm",
    "mkfs.btrfs",
    "mkfs.fat",
    "mkfs.ext4",
    "mkfs.xfs",
    "mkswap",
    "cryptsetup",
    "systemd-cryptenroll",
    "wipefs",
    "sgdisk",
    "parted",
    "sfdisk",
)


class TestApply:
    def test_writes_only_to_targets(self, layout, resolved, tools) -> None:
        plan = plan_existing_layout(layout, resolved, probe=FakeProbe())
        parts = apply_existing_layout(layout, plan, encryption=tools.secrets())

        allowed = set(plan.targets) | {"/dev/md/arches", "/dev/mapper/arches"}
        for cmd in tools.calls:
            assert cmd[0] not in ("sgdisk", "wipefs", "parted", "sfdisk", "mkfs.fat")
            if cmd[0] in DESTRUCTIVE:
                devs = {a for a in cmd[1:] if a.startswith("/dev/")}
                assert devs <= allowed, cmd

        mdadm = next(c for c in tools.calls if c[0] == "mdadm")
        assert "--level=0" in mdadm
        assert mdadm[-2:] == [f"{DISK_A}p3", f"{DISK_B}p3"]
        luks = next(c for c in tools.calls if c[:2] == ["cryptsetup", "luksFormat"])
        assert luks[-1] == "/dev/md/arches"
        assert any(
            c[0] == "mkfs.btrfs" and c[-1] == "/dev/mapper/arches" for c in tools.calls
        )
        # ESP mounted as-is at /boot, never formatted
        assert ["mount", f"{DISK_B}p1", str(tools.mnt / "boot")] in tools.calls
        assert parts.esp == f"{DISK_B}p1"
        assert parts.esp_reused
        assert parts.root == "/dev/mapper/arches"
        assert parts.root_subvolumes == ["@", "@home", "@var", "@snapshots"]
        assert parts.md_devices == ["/dev/md/arches"]
        assert [v.luks_uuid for v in parts.encrypted_volumes] == [LUKS_UUID]

    def test_encrypted_layout_needs_a_pin(self, layout, resolved, tools) -> None:
        plan = plan_existing_layout(layout, resolved, probe=FakeProbe())
        with pytest.raises(ExistingLayoutError, match="PIN"):
            apply_existing_layout(layout, plan)
        assert tools.calls == []

    def test_guard_refuses_encrypted_layout_without_tpm(self, layout, resolved) -> None:
        probe = FakeProbe()
        probe.tpm2 = False
        with pytest.raises(ExistingLayoutError, match="no TPM 2.0"):
            plan_existing_layout(layout, resolved, probe=probe)


class TestPipeline:
    def test_guard_failure_stops_before_any_write(
        self, layout, resolved, x86_64_platform, dev_workstation_template
    ) -> None:
        from arches_installer.core.pipeline import InstallParams, run_install_pipeline

        params = InstallParams(
            platform=x86_64_platform,
            template=dev_workstation_template,
            device="",
            hostname="h",
            username="u",
            password="p",
            disk_layout=layout,
            resolved_disk_roles=resolved,
        )
        with (
            patch(
                "arches_installer.core.pipeline.plan_existing_layout",
                side_effect=ExistingLayoutError("no"),
            ),
            patch("arches_installer.core.pipeline.apply_existing_layout") as apply,
            patch("arches_installer.core.pipeline.install_system") as install,
        ):
            with pytest.raises(ExistingLayoutError):
                run_install_pipeline(params, log=lambda m: None)
        apply.assert_not_called()
        install.assert_not_called()

    def test_existing_layout_needs_resolved_roles(
        self, layout, x86_64_platform, dev_workstation_template
    ) -> None:
        from arches_installer.core.pipeline import InstallParams, run_install_pipeline

        params = InstallParams(
            platform=x86_64_platform,
            template=dev_workstation_template,
            device="/dev/nvme0n1",
            hostname="h",
            username="u",
            password="p",
            disk_layout=layout,
        )
        with patch("arches_installer.core.pipeline.apply_disk_layout") as legacy:
            with pytest.raises(RuntimeError, match="existing partitions"):
                run_install_pipeline(params, log=lambda m: None)
        legacy.assert_not_called()


class TestPipelineEncrypted:
    def test_unlock_config_written_between_install_and_bootloader(
        self, layout, resolved, x86_64_platform, dev_workstation_template
    ) -> None:
        from unittest.mock import MagicMock

        from arches_installer.core.disk import PartitionMap
        from arches_installer.core.encryption import EncryptionSecrets
        from arches_installer.core.pipeline import InstallParams, run_install_pipeline

        secrets = EncryptionSecrets(pin="123456", on_recovery_key=lambda k: None)
        params = InstallParams(
            platform=x86_64_platform,
            template=dev_workstation_template,
            device="",
            hostname="h",
            username="u",
            password="p",
            disk_layout=layout,
            resolved_disk_roles=resolved,
            encryption=secrets,
        )
        from arches_installer.core.disk import EncryptedVolume

        parts = PartitionMap(
            esp=f"{DISK_B}p1",
            root="/dev/mapper/arches",
            root_filesystem="btrfs",
            esp_reused=True,
            encrypted_volumes=[EncryptedVolume("arches", "/dev/md/arches", LUKS_UUID)],
        )
        order = MagicMock()
        mod = "arches_installer.core.pipeline"
        with (
            patch(f"{mod}.plan_existing_layout"),
            patch(f"{mod}.apply_existing_layout", return_value=parts) as apply,
            patch(f"{mod}.backup_limine_conf", return_value=None),
            patch(f"{mod}.write_limine_defaults"),
            patch(f"{mod}.unmount_shared_esp", order.unmount_shared_esp),
            patch(f"{mod}.install_system", order.install_system),
            patch(f"{mod}.configure_target_storage", order.configure_target_storage),
            patch(f"{mod}.enroll_tpm2", order.enroll_tpm2),
            patch(f"{mod}.prepare_secure_boot", order.prepare_secure_boot),
            patch(f"{mod}.install_bootloader", order.install_bootloader),
            patch(f"{mod}.setup_snapshots"),
            patch(f"{mod}.inject_firstboot_service"),
            patch(f"{mod}.write_disk_roles_state"),
            patch(f"{mod}._persist_install_log"),
        ):
            run_install_pipeline(params, log=lambda m: None)

        assert apply.call_args.kwargs["encryption"] is secrets
        assert [c[0] for c in order.mock_calls] == [
            "unmount_shared_esp",
            "install_system",
            "configure_target_storage",
            "enroll_tpm2",
            "prepare_secure_boot",
            "install_bootloader",
        ]
        template = order.install_system.call_args[0][1]
        for pkg in ("mdadm", "cryptsetup", "tpm2-tss", "sbctl", "sbsigntools"):
            assert pkg in template.install.pacstrap


class TestSharedEspDuringPacstrap:
    """QEMU, 2026-10-27: with the ESP mounted, pacstrap's kernel hooks wrote
    vmlinuz-*/initramfs-*.img over the other system's files on the ESP."""

    def test_remount_cleans_root_boot_and_presets(self, tmp_path) -> None:
        from arches_installer.core.existing import remount_shared_esp

        boot = tmp_path / "boot"
        (boot / "memtest86+").mkdir(parents=True)
        (boot / "vmlinuz-linux-cachyos").write_text("k")
        (boot / "initramfs-linux-cachyos.img").write_text("i")
        presets = tmp_path / "etc/mkinitcpio.d"
        presets.mkdir(parents=True)
        (presets / "linux-cachyos.preset").write_text("p")
        calls: list[list[str]] = []
        with (
            patch("arches_installer.core.existing.MOUNT_ROOT", tmp_path),
            patch(
                "arches_installer.core.existing.logged_run",
                lambda cmd, log=None: calls.append(cmd),
            ),
        ):
            remount_shared_esp("/dev/nvme1n1p1")
        assert list(boot.iterdir()) == []
        assert list(presets.iterdir()) == []
        assert calls == [["mount", "/dev/nvme1n1p1", str(boot)]]

    def test_refuses_to_clean_a_mounted_boot(self, tmp_path) -> None:
        from arches_installer.core.existing import remount_shared_esp

        (tmp_path / "boot").mkdir()
        with (
            patch("arches_installer.core.existing.MOUNT_ROOT", tmp_path),
            patch("os.path.ismount", return_value=True),
        ):
            with pytest.raises(ExistingLayoutError, match="still a mount point"):
                remount_shared_esp("/dev/nvme1n1p1")

    def test_install_system_mounts_esp_between_pacstrap_and_fstab(
        self, tmp_path, x86_64_platform, dev_workstation_template
    ) -> None:
        from unittest.mock import MagicMock

        from arches_installer.core import install

        order = MagicMock()
        names = [
            "require_local_repo_key",
            "_pre_pacstrap_setup",
            "pacstrap",
            "generate_fstab",
            "configure_var_bind_mounts",
            "install_pacman_conf",
            "install_local_repo_key",
            "copy_mirrorlists",
            "sync_chroot_databases",
            "install_override_packages",
            "configure_locale",
            "configure_timezone",
            "configure_hostname",
            "create_user",
            "deploy_ssh_key",
            "copy_apple_firmware",
            "configure_apple_input",
            "copy_network_profiles",
            "preseed_network_deps",
            "run_hardware_detection",
            "_unmount_iso_cache_from_target",
            "enable_services",
            "configure_ssh",
            "stage_ansible",
        ]
        from contextlib import ExitStack

        with ExitStack() as stack:
            for n in names:
                stack.enter_context(patch.object(install, n, getattr(order, n)))
            stack.enter_context(patch.object(install, "MOUNT_ROOT", tmp_path))
            install.install_system(
                x86_64_platform,
                dev_workstation_template,
                "h",
                "u",
                "p",
                after_pacstrap=order.after_pacstrap,
            )
        seq = [c[0] for c in order.mock_calls]
        assert (
            seq.index("pacstrap")
            < seq.index("after_pacstrap")
            < seq.index("generate_fstab")
        )
