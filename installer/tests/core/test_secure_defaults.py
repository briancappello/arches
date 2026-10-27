"""One test per row of design D6: each fails if the old default returns.

Design: docs/superpowers/specs/2026-10-27-secure-spare-slot-install-design.md
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[3]


# D6 row 1: no NOPASSWD sudo; first boot needs no sudo.
def test_wheel_sudo_needs_a_password(tmp_path: Path) -> None:
    from arches_installer.core.install import create_user
    from arches_installer.core.firstboot import _firstboot_service_unit

    mod = "arches_installer.core.install"
    with (
        patch(f"{mod}.MOUNT_ROOT", tmp_path),
        patch(f"{mod}.chroot_run"),
        patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)),
    ):
        create_user("u", "pw")
    written = [p.read_text() for p in (tmp_path / "etc/sudoers.d").iterdir()]
    assert written == ["%wheel ALL=(ALL:ALL) ALL\n"]
    assert not any("NOPASSWD" in w for w in written)

    # First boot runs as root, so it has nothing to ask sudo for.
    for graphical in (True, False):
        unit = _firstboot_service_unit(graphical=graphical)
        assert "User=" not in unit
        assert "ExecStart=/opt/arches/firstboot.sh" in unit


# D6 row 2: no implicit build-host SSH key; only declared keys, shown on
# the confirm screen.
def test_only_declared_ssh_keys_are_installed(tmp_path: Path) -> None:
    import arches_installer.core.install as install
    from arches_installer.core.install import deploy_ssh_key
    from arches_installer.tui.confirm import ssh_keys_summary

    # The installer no longer knows about the ISO's build-host key at all.
    assert not hasattr(install, "ISO_BUILD_HOST_PUBKEY")
    assert "build-host.pub" not in Path(install.__file__).read_text()

    (tmp_path / "home/u").mkdir(parents=True)
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/passwd").write_text("u:x:1000:1000::/home/u:/bin/zsh\n")
    with patch(f"{install.__name__}.MOUNT_ROOT", tmp_path):
        deploy_ssh_key("u", [])
    assert not (tmp_path / "home/u/.ssh").exists()

    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOPERATORKEYxx op@p1"
    with patch(f"{install.__name__}.MOUNT_ROOT", tmp_path), patch("os.chown"):
        deploy_ssh_key("u", [key])
    assert (tmp_path / "home/u/.ssh/authorized_keys").read_text() == key + "\n"

    assert "none" in ssh_keys_summary([])
    assert "ssh-ed25519 ...PERATORKEYxx op@p1" in ssh_keys_summary([key])


def test_auto_install_ssh_keys_are_explicit(tmp_path: Path) -> None:
    from arches_installer.core.auto import AutoInstallConfig

    base = {
        "install": {
            "template": "kde-workstation.toml",
            "username": "u",
            "password": "pw",
        }
    }
    assert AutoInstallConfig.from_dict(base).ssh_authorized_keys == []
    key = "ssh-ed25519 AAAA op@p1"
    cfg = AutoInstallConfig.from_dict({**base, "ssh": {"authorized_keys": [key]}})
    assert cfg.ssh_authorized_keys == [key]
