"""Tests for LUKS2 + TPM2 setup and the target's unlock config (D2)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from arches_installer.core.disk import EncryptedVolume, PartitionMap
from arches_installer.core.encryption import (
    _RECOVERY_KEY_RE,
    EncryptionError,
    configure_mkinitcpio_hooks,
    create_luks_volume,
    crypttab_initramfs,
    edit_hooks,
    tpm2_present,
    validate_pin,
    write_crypttab_initramfs,
    write_mdadm_conf,
)
from arches_installer.core.existing import configure_target_storage, layout_packages
from tests.core.fakes import LUKS_UUID, PIN, RECOVERY_KEY
from tests.core.test_existing import P1_LAYOUT

from arches_installer.core.disk_layout import load_disk_layout


class TestCreateLuksVolume:
    def test_enrolls_recovery_key_and_tpm2_with_pin_on_pcr7(self, tools) -> None:
        vol = create_luks_volume("/dev/md/arches", "arches", tools.secrets())

        assert vol == EncryptedVolume("arches", "/dev/md/arches", LUKS_UUID)
        fmt = next(c for c in tools.calls if c[:2] == ["cryptsetup", "luksFormat"])
        assert fmt[fmt.index("--type") + 1] == "luks2"
        assert fmt[fmt.index("--pbkdf") + 1] == "argon2id"

        tpm_cmd, tpm_env = next(
            (c, e) for c, e in tools.quiet_calls if "--tpm2-device=auto" in c
        )
        assert "--tpm2-pcrs=7" in tpm_cmd
        assert "--tpm2-with-pin=yes" in tpm_cmd
        assert tpm_env["NEWPIN"] == PIN
        assert PIN not in " ".join(tpm_cmd)

    def test_recovery_key_is_shown_once(self, tools) -> None:
        create_luks_volume("/dev/md/arches", "arches", tools.secrets())
        assert tools.keys_shown == [RECOVERY_KEY]

    def test_recovery_key_and_pin_are_never_logged(self, tools) -> None:
        create_luks_volume("/dev/md/arches", "arches", tools.secrets())
        logged = "\n".join(tools.logged)
        # Commands passed to the logging runner are logged verbatim.
        logged += "\n".join(" ".join(c) for c in tools.calls)
        assert RECOVERY_KEY not in logged
        assert PIN not in logged

    def test_formatting_key_slot_is_wiped_and_file_removed(self, tools) -> None:
        create_luks_volume("/dev/md/arches", "arches", tools.secrets())
        wipe = [c for c in tools.calls if "--wipe-slot=0" in c]
        assert len(wipe) == 1
        # The wipe comes after both enrollments.
        idx = tools.calls.index(wipe[0])
        enrolled = [
            i
            for i, c in enumerate(tools.calls)
            if "--recovery-key" in c or "--tpm2-device=auto" in c
        ]
        assert max(enrolled) < idx
        assert list(tools.keyfile_dir.iterdir()) == []

    def test_short_pin_refused_before_any_write(self, tools) -> None:
        from arches_installer.core.encryption import EncryptionSecrets

        with pytest.raises(EncryptionError, match="PIN"):
            create_luks_volume(
                "/dev/md/arches", "arches", EncryptionSecrets("123", lambda k: None)
            )
        assert tools.calls == []

    def test_bad_recovery_output_fails(self, tools) -> None:
        def quiet(cmd, env=None):
            tools.calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "not a key\n", "")

        with patch("arches_installer.core.encryption._quiet", quiet):
            with pytest.raises(EncryptionError, match="recovery-key"):
                create_luks_volume("/dev/md/arches", "arches", tools.secrets())
        assert tools.keys_shown == []
        assert list(tools.keyfile_dir.iterdir()) == []


@pytest.mark.skipif(
    not (shutil.which("cryptsetup") and shutil.which("systemd-cryptenroll")),
    reason="needs cryptsetup and systemd-cryptenroll",
)
def test_real_recovery_key_output_matches(tmp_path: Path) -> None:
    """systemd-cryptenroll's real recovery-key output parses (no root needed)."""
    img, key = tmp_path / "img", tmp_path / "key"
    img.write_bytes(b"\0" * 32 * 1024 * 1024)
    key.write_bytes(b"k" * 64)
    subprocess.run(
        [
            "cryptsetup",
            "luksFormat",
            "--type",
            "luks2",
            "--pbkdf",
            "pbkdf2",
            "--pbkdf-force-iterations",
            "1000",
            "--batch-mode",
            "--key-file",
            str(key),
            str(img),
        ],
        check=True,
        capture_output=True,
    )
    out = subprocess.run(
        ["systemd-cryptenroll", f"--unlock-key-file={key}", "--recovery-key", str(img)],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    ).stdout.strip()
    assert _RECOVERY_KEY_RE.match(out)
    subprocess.run(
        ["systemd-cryptenroll", f"--unlock-key-file={key}", "--wipe-slot=0", str(img)],
        check=True,
        capture_output=True,
    )
    slots = subprocess.run(
        ["systemd-cryptenroll", str(img)], check=True, capture_output=True, text=True
    ).stdout
    assert "recovery" in slots
    assert "password" not in slots


class TestPinAndTpm:
    def test_validate_pin(self) -> None:
        validate_pin("1234")
        with pytest.raises(EncryptionError):
            validate_pin("")

    def test_tpm2_present(self, tmp_path: Path) -> None:
        assert not tpm2_present(tmp_path)
        (tmp_path / "tpm0").mkdir()
        (tmp_path / "tpm0" / "tpm_version_major").write_text("1\n")
        assert not tpm2_present(tmp_path)
        (tmp_path / "tpm0" / "tpm_version_major").write_text("2\n")
        assert tpm2_present(tmp_path)


class TestCrypttab:
    def test_one_device_unlocked_by_tpm2(self) -> None:
        text = crypttab_initramfs(
            [EncryptedVolume("arches", "/dev/md/arches", LUKS_UUID)]
        )
        lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
        assert lines == [f"arches UUID={LUKS_UUID} none tpm2-device=auto"]

    def test_written_root_only(self, tmp_path: Path) -> None:
        write_crypttab_initramfs(
            [EncryptedVolume("arches", "/dev/md/arches", LUKS_UUID)], root=tmp_path
        )
        path = tmp_path / "etc" / "crypttab.initramfs"
        assert path.stat().st_mode & 0o777 == 0o600


class TestHooks:
    ARCH_DEFAULT = "base systemd autodetect microcode modconf kms keyboard sd-vconsole block filesystems fsck"
    LEGACY = "base udev autodetect microcode modconf kms keyboard keymap consolefont block filesystems fsck"

    def test_arch_default(self) -> None:
        assert (
            edit_hooks(self.ARCH_DEFAULT.split(), mdadm=True)
            == (
                "base systemd autodetect microcode modconf kms keyboard sd-vconsole "
                "block mdadm_udev sd-encrypt filesystems fsck"
            ).split()
        )

    def test_legacy_busybox(self) -> None:
        assert (
            edit_hooks(self.LEGACY.split(), mdadm=True)
            == (
                "base systemd autodetect microcode modconf kms keyboard sd-vconsole "
                "block mdadm_udev sd-encrypt filesystems fsck"
            ).split()
        )

    def test_without_mdadm(self) -> None:
        hooks = edit_hooks(self.ARCH_DEFAULT.split(), mdadm=False)
        assert "mdadm_udev" not in hooks
        assert (
            hooks.index("block")
            < hooks.index("sd-encrypt")
            < hooks.index("filesystems")
        )

    def test_idempotent(self) -> None:
        once = edit_hooks(self.LEGACY.split(), mdadm=True)
        assert edit_hooks(once, mdadm=True) == once

    def test_keeps_other_hooks_and_missing_kms(self) -> None:
        # chwd removes kms for NVIDIA; plymouth stays.
        hooks = edit_hooks(
            "base udev plymouth autodetect modconf block keyboard keymap filesystems".split(),
            mdadm=True,
        )
        assert "kms" not in hooks
        assert "plymouth" in hooks
        assert (
            hooks.index("block") < hooks.index("mdadm_udev") < hooks.index("sd-encrypt")
        )

    def test_edits_the_last_hooks_assignment(self, tmp_path: Path) -> None:
        etc = tmp_path / "etc"
        (etc / "mkinitcpio.conf.d").mkdir(parents=True)
        (etc / "mkinitcpio.conf").write_text(f"MODULES=()\nHOOKS=({self.LEGACY})\n")
        (etc / "mkinitcpio.conf.d" / "10-vendor.conf").write_text(
            f"HOOKS=({self.ARCH_DEFAULT})\n"
        )
        owner = configure_mkinitcpio_hooks(tmp_path, mdadm=True)
        assert owner.name == "10-vendor.conf"
        assert "sd-encrypt" in owner.read_text()
        assert (
            (etc / "mkinitcpio.conf").read_text().endswith(f"HOOKS=({self.LEGACY})\n")
        )

    def test_no_hooks_line_fails(self, tmp_path: Path) -> None:
        (tmp_path / "etc").mkdir()
        (tmp_path / "etc" / "mkinitcpio.conf").write_text("# HOOKS=(base)\n")
        with pytest.raises(EncryptionError):
            configure_mkinitcpio_hooks(tmp_path, mdadm=True)


class TestMdadmConf:
    SCAN = (
        "ARRAY /dev/md/other metadata=1.2 UUID=aaaa name=x:other\n"
        "ARRAY /dev/md/arches metadata=1.2 UUID=bbbb name=archiso:arches\n"
    )

    def test_records_only_our_array(self, tmp_path: Path) -> None:
        with patch("arches_installer.core.encryption.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, self.SCAN, "")
            write_mdadm_conf(["/dev/md/arches"], root=tmp_path)
            write_mdadm_conf(["/dev/md/arches"], root=tmp_path)
        conf = (tmp_path / "etc" / "mdadm.conf").read_text()
        assert (
            conf == "ARRAY /dev/md/arches metadata=1.2 UUID=bbbb name=archiso:arches\n"
        )

    def test_missing_array_fails(self, tmp_path: Path) -> None:
        with patch("arches_installer.core.encryption.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            with pytest.raises(EncryptionError):
                write_mdadm_conf(["/dev/md/arches"], root=tmp_path)


class TestTargetStorage:
    def test_p1_layout_packages(self) -> None:
        assert layout_packages(load_disk_layout(P1_LAYOUT)) == [
            "mdadm",
            "cryptsetup",
            "tpm2-tss",
        ]

    def test_configures_unlock(self, tmp_path: Path) -> None:
        parts = PartitionMap(
            esp="/dev/nvme1n1p1",
            root="/dev/mapper/arches",
            encrypted_volumes=[EncryptedVolume("arches", "/dev/md/arches", LUKS_UUID)],
            md_devices=["/dev/md/arches"],
        )
        mod = "arches_installer.core.existing"
        with (
            patch(f"{mod}.write_mdadm_conf") as mdadm,
            patch(f"{mod}.write_crypttab_initramfs") as crypttab,
            patch(f"{mod}.configure_mkinitcpio_hooks") as hooks,
        ):
            configure_target_storage(parts)
        mdadm.assert_called_once()
        crypttab.assert_called_once()
        assert hooks.call_args.kwargs["mdadm"] is True

    def test_plain_install_untouched(self) -> None:
        mod = "arches_installer.core.existing"
        with patch(f"{mod}.configure_mkinitcpio_hooks") as hooks:
            configure_target_storage(PartitionMap(esp="/dev/sda1", root="/dev/sda2"))
        hooks.assert_not_called()
