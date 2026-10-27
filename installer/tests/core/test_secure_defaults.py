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
