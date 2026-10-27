"""Tests for the Secure Boot tools installed on a shared-ESP system (D4).

The shell scripts run for real, against stub commands on PATH.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import call, patch

import pytest

from arches_installer.core.secureboot import (
    FILES_DIR,
    HOOK,
    SCRIPTS,
    install_secure_boot_tools,
    prepare_secure_boot,
)

MACHINE_ID = "a" * 32
OTHER_ID = "0688c1909f7144e5b1573566881d1324"


class TestInstall:
    def test_installs_scripts_and_hook(self, tmp_path: Path) -> None:
        install_secure_boot_tools(root=tmp_path)
        for name in SCRIPTS:
            path = tmp_path / "usr/local/bin" / name
            assert path.read_text() == (FILES_DIR / name).read_text()
            assert path.stat().st_mode & 0o777 == 0o755
        assert (tmp_path / "etc/pacman.d/hooks" / HOOK).is_file()

    def test_keys_created_in_the_target(self, tmp_path: Path) -> None:
        mod = "arches_installer.core.secureboot"
        with (
            patch(f"{mod}.chroot_run") as chroot,
            patch(f"{mod}.MOUNT_ROOT", tmp_path),
            patch(f"{mod}.install_secure_boot_tools") as tools,
        ):
            prepare_secure_boot()
        assert chroot.call_args_list == [call(["sbctl", "create-keys"], log=None)]
        tools.assert_called_once()


class TestHook:
    def test_runs_after_limine_mkinitcpio_and_sbctl_hooks(self) -> None:
        others = [
            "80-limine-efi-deploy.hook",
            "90-mkinitcpio-install.hook",
            "95-memtest-limine.hook",
            "zz-sbctl.hook",
        ]
        assert sorted(others + [HOOK])[-1] == HOOK

    def test_triggers(self) -> None:
        text = (FILES_DIR / HOOK).read_text()
        for target in (
            "limine",
            "limine-mkinitcpio-hook",
            "sbctl",
            "systemd",
            "linux*",
        ):
            assert f"Target = {target}\n" in text
        assert "Exec = /usr/local/bin/arches-secureboot-resign\n" in text
        assert "When = PostTransaction" in text


# ---------------------------------------------------------------------------
# Shell scripts against stubs
# ---------------------------------------------------------------------------


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def env(tmp_path: Path):
    """A fake system: ESP, stub tools, and a log of every stub call."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    esp = tmp_path / "esp"
    (esp / "EFI/Linux").mkdir(parents=True)
    (esp / "EFI/limine").mkdir(parents=True)
    (esp / "EFI/limine/limine_x64.efi").write_text("limine")
    (esp / "EFI/Linux/arches_linux-cachyos.efi").write_text("uki")
    (esp / "limine.conf").write_text(
        f"/+Arch Linux\ncomment: machine-id={OTHER_ID}\n"
        f"/+Arches Linux\ncomment: machine-id={MACHINE_ID}\n"
    )
    unsigned = tmp_path / "unsigned"
    unsigned.write_text("")
    (tmp_path / "machine-id").write_text(MACHINE_ID + "\n")
    defaults = tmp_path / "limine"
    defaults.write_text("ENABLE_UKI=yes\nENABLE_ENROLL_LIMINE_CONFIG=no\n")
    rec = f'echo "$(basename "$0") $*" >> {log}\n'
    _stub(
        bin_dir,
        "sbctl",
        rec + 'case "$1" in\n'
        'setup) echo "{\\"installed\\": ${SB_KEYS:-true}}" ;;\n'
        'status) echo "{\\"setup_mode\\": ${SB_SETUP:-true}}" ;;\n'
        "esac\n",
    )
    _stub(bin_dir, "sbverify", rec + f'! grep -qxF "$3" {unsigned}\n')
    _stub(bin_dir, "limine-update", rec + f": > {unsigned}\n")
    _stub(bin_dir, "limine-enroll-config", rec)
    _stub(bin_dir, "findmnt", rec + 'echo "${ROOT_SRC:-/dev/mapper/arches[/@]}"\n')
    _stub(bin_dir, "systemd-cryptenroll", rec)
    _stub(bin_dir, "arches-freeze-other-systems", rec)
    resign = bin_dir / "arches-secureboot-resign"
    resign.write_text((FILES_DIR / "arches-secureboot-resign").read_text())
    resign.chmod(0o755)
    e = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        ARCHES_ESP=str(esp),
        ARCHES_LIMINE_DEFAULTS=str(defaults),
        ARCHES_MACHINE_ID=str(tmp_path / "machine-id"),
        ARCHES_SKIP_ROOT_CHECK="1",
    )

    class Env:
        pass

    o = Env()
    o.env, o.log, o.esp, o.unsigned, o.defaults, o.tmp = (
        e,
        log,
        esp,
        unsigned,
        defaults,
        tmp_path,
    )

    def calls() -> list[str]:
        return log.read_text().splitlines() if log.exists() else []

    o.calls = calls
    return o


def _run(name: str, env, stdin: str = "", **extra) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(FILES_DIR / name)],
        env=dict(env.env, **extra),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestResign:
    def test_no_keys_does_nothing(self, env) -> None:
        r = _run("arches-secureboot-resign", env, SB_KEYS="false")
        assert r.returncode == 0
        assert [c for c in env.calls() if not c.startswith("sbctl setup")] == []

    def test_all_signed(self, env) -> None:
        r = _run("arches-secureboot-resign", env)
        assert r.returncode == 0, r.stderr
        assert "limine-enroll-config " in env.calls()
        assert not any(c.startswith("limine-update") for c in env.calls())

    def test_unsigned_uki_is_rebuilt_not_resigned(self, env) -> None:
        uki = env.esp / "EFI/Linux/arches_linux-cachyos.efi"
        env.unsigned.write_text(f"{uki}\n")
        r = _run("arches-secureboot-resign", env)
        assert r.returncode == 0, r.stderr
        calls = env.calls()
        assert "limine-update " in calls
        assert not any(c.startswith("sbctl sign") for c in calls)
        assert calls.index("limine-update ") < calls.index("limine-enroll-config ")

    def test_fails_if_still_unsigned(self, env) -> None:
        limine = env.esp / "EFI/limine/limine_x64.efi"
        env.unsigned.write_text(f"{limine}\n")
        r = _run("arches-secureboot-resign", env)
        assert r.returncode == 1
        assert f"NOT SIGNED: {limine}" in r.stderr

    def test_fails_without_ukis(self, env) -> None:
        (env.esp / "EFI/Linux/arches_linux-cachyos.efi").unlink()
        r = _run("arches-secureboot-resign", env)
        assert r.returncode == 1


class TestEnroll:
    def test_happy_path(self, env) -> None:
        r = _run("arches-secureboot-enroll", env, stdin="ENROLL\n")
        assert r.returncode == 0, r.stderr
        calls = env.calls()
        assert calls.index("limine-update ") < calls.index(
            "sbctl enroll-keys --microsoft"
        )
        assert "ENABLE_ENROLL_LIMINE_CONFIG=yes" in env.defaults.read_text()
        assert f"machine-id={OTHER_ID}" in r.stdout
        assert "arches-tpm-reenroll" in r.stdout

    def test_refuses_on_the_old_system(self, env) -> None:
        r = _run(
            "arches-secureboot-enroll",
            env,
            stdin="ENROLL\n",
            ROOT_SRC="/dev/nvme1n1p2[/@]",
        )
        assert r.returncode == 1
        assert "not /dev/mapper/arches" in r.stderr
        assert not any("enroll-keys" in c for c in env.calls())

    def test_refuses_outside_setup_mode(self, env) -> None:
        r = _run("arches-secureboot-enroll", env, stdin="ENROLL\n", SB_SETUP="false")
        assert r.returncode == 1
        assert "Setup Mode" in r.stderr
        assert not any("enroll-keys" in c for c in env.calls())

    def test_needs_typed_confirmation(self, env) -> None:
        r = _run("arches-secureboot-enroll", env, stdin="yes\n")
        assert r.returncode == 1
        assert not any("enroll-keys" in c for c in env.calls())
        assert not any("freeze" in c for c in env.calls())
        assert "ENABLE_ENROLL_LIMINE_CONFIG=no" in env.defaults.read_text()

    def test_unsigned_chain_blocks_enrolling(self, env) -> None:
        resign = Path(env.env["PATH"].split(":")[0]) / "arches-secureboot-resign"
        resign.write_text("#!/usr/bin/env bash\nexit 1\n")
        r = _run("arches-secureboot-enroll", env, stdin="ENROLL\n")
        assert r.returncode == 1
        assert "NOT enrolled" in r.stderr
        assert not any("enroll-keys" in c for c in env.calls())


class TestTpmReenroll:
    def test_rebinds_pcr7_with_pin(self, env) -> None:
        crypttab = env.tmp / "crypttab.initramfs"
        crypttab.write_text("# comment\narches UUID=1234-abcd none tpm2-device=auto\n")
        r = _run("arches-tpm-reenroll", env, ARCHES_CRYPTTAB=str(crypttab))
        assert r.returncode == 0, r.stderr
        assert env.calls()[0] == (
            "systemd-cryptenroll --wipe-slot=tpm2 --tpm2-device=auto "
            "--tpm2-pcrs=7 --tpm2-with-pin=yes /dev/disk/by-uuid/1234-abcd"
        )


class TestFreeze:
    OLD_UUID = "64a8b4ff-b1aa-4501-b9f6-8ccfc1ac763a"

    @pytest.fixture
    def old(self, env):
        """The old system's root, as the freeze script will mount it."""
        mnt = env.tmp / "freeze-mnt"
        root = mnt / OTHER_ID
        (root / "etc/boot/hooks/pre.d").mkdir(parents=True)
        (root / "etc/boot/hooks/post.d").mkdir(parents=True)
        (root / "etc/machine-id").write_text(OTHER_ID + "\n")
        (root / "etc/fstab").write_text(
            "# /dev/nvme1n1p2\n"
            f"UUID={self.OLD_UUID}\t/\tbtrfs\trw,subvol=/@\t0 0\n"
            "UUID=ABCD-1234\t/boot\tvfat\trw,relatime,fmask=0022\t0 2\n"
        )
        (root / "etc/boot/hooks/pre.d/10-limine-reset-enroll").symlink_to(
            "/usr/bin/limine-reset-enroll"
        )
        (root / "etc/boot/hooks/post.d/90-limine-enroll-config").symlink_to(
            "/usr/bin/limine-enroll-config"
        )
        (env.esp / "limine.conf").write_text(
            "timeout: 3\n\n/+Arch Linux\n"
            f"comment: machine-id={OTHER_ID} order-priority=50\n"
            "  //linux-cachyos\n  protocol: linux\n"
            f"  cmdline: root=UUID={self.OLD_UUID} rw rootflags=subvol=/@ quiet\n"
            f"/+Arches Linux\ncomment: machine-id={MACHINE_ID}\n"
            "  //linux-cachyos\n  protocol: efi\n  cmdline: root=UUID=new rw\n"
        )
        (env.esp / "EFI/limine/limine_x64.bak").write_text("bak")
        bin_dir = Path(env.env["PATH"].split(":")[0])
        rec = f'echo "$(basename "$0") $*" >> {env.log}\n'
        _stub(bin_dir, "mount", rec)
        _stub(bin_dir, "umount", rec)
        _stub(bin_dir, "systemctl", rec)
        env.state = env.tmp / "state"
        extra = dict(
            ARCHES_FREEZE_MNT=str(mnt),
            ARCHES_STATE_DIR=str(env.state),
            ARCHES_FREEZE_STAMP="20261027T000000Z",
        )
        return root, extra

    def test_freezes_the_old_system_reversibly(self, env, old) -> None:
        root, extra = old
        conf_before = (env.esp / "limine.conf").read_text()
        r = _run("arches-freeze-other-systems", env, **extra)
        assert r.returncode == 0, r.stderr

        # mounted the right root, rw, its subvolume
        assert f"mount -o rw,subvol=/@ UUID={self.OLD_UUID} {root}" in env.calls()
        assert (
            f"systemctl --root={root} mask limine-snapper-sync.service" in env.calls()
        )
        assert f"umount {root}" in env.calls()

        fstab = (root / "etc/fstab").read_text()
        boot = next(ln for ln in fstab.splitlines() if "\t/boot\t" in ln)
        assert boot.split("\t")[3] == "rw,relatime,fmask=0022,noauto,nofail"
        root_line = next(ln for ln in fstab.splitlines() if "\t/\t" in ln)
        assert "noauto" not in root_line
        assert (root / "etc/fstab.arches-freeze-20261027T000000Z").read_text().count(
            "noauto"
        ) == 0

        assert (
            root / "etc/boot/hooks/pre.d/10-limine-reset-enroll.disabled"
        ).is_symlink()
        assert (
            root / "etc/boot/hooks/post.d/90-limine-enroll-config.disabled"
        ).is_symlink()
        assert not (root / "etc/boot/hooks/pre.d/10-limine-reset-enroll").exists()

        # ESP backups, in two places; limine.conf itself untouched
        for d in (
            env.esp / "arches-freeze-20261027T000000Z",
            env.state / "freeze-20261027T000000Z",
        ):
            assert (d / "limine.conf").read_text() == conf_before
            assert (d / "EFI_limine_limine_x64.efi").read_text() == "limine"
            assert (d / "EFI_limine_limine_x64.bak").read_text() == "bak"
        assert (env.esp / "limine.conf").read_text() == conf_before

        # The undo command restores everything.
        undo = root / "usr/local/sbin/arches-unfreeze"
        text = undo.read_text().replace(" /etc/", f" {root}/etc/")
        undo.write_text(text)
        u = subprocess.run(
            ["bash", str(undo)], env=env.env, capture_output=True, text=True, timeout=30
        )
        assert u.returncode == 0, u.stderr
        assert (root / "etc/fstab").read_text().count("noauto") == 0
        assert (root / "etc/boot/hooks/pre.d/10-limine-reset-enroll").is_symlink()
        assert (root / "etc/boot/hooks/post.d/90-limine-enroll-config").is_symlink()
        assert "systemctl unmask limine-snapper-sync.service" in env.calls()

    def test_refuses_the_wrong_root(self, env, old) -> None:
        root, extra = old
        (root / "etc/machine-id").write_text("b" * 32 + "\n")
        r = _run("arches-freeze-other-systems", env, **extra)
        assert r.returncode == 1
        assert "is not the system" in r.stderr
        assert "noauto" not in (root / "etc/fstab").read_text()

    def test_nothing_to_freeze(self, env, old) -> None:
        _, extra = old
        (env.esp / "limine.conf").write_text(
            f"/+Arches Linux\ncomment: machine-id={MACHINE_ID}\n"
        )
        r = _run("arches-freeze-other-systems", env, **extra)
        assert r.returncode == 0
        assert "nothing to freeze" in r.stdout
