"""Fakes shared by the existing-layout and encryption tests."""

from __future__ import annotations

import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

RECOVERY_KEY = "-".join(["cbdefghi"] * 8)
PIN = "4711-pin"
LUKS_UUID = "11111111-2222-3333-4444-555555555555"


class FakeTools:
    """Record every command the install would run; return canned output."""

    def __init__(self, tmp_path: Path) -> None:
        self.calls: list[list[str]] = []
        self.quiet_calls: list[tuple[list[str], dict | None]] = []
        self.logged: list[str] = []
        self.keys_shown: list[str] = []
        self.keyfile_dir = tmp_path / "run"
        self.keyfile_dir.mkdir()
        self.mnt = tmp_path / "mnt"

    def run(self, cmd, log=None, **kw):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def quiet(self, cmd, env=None):
        self.quiet_calls.append((list(cmd), env))
        self.calls.append(list(cmd))
        out = RECOVERY_KEY + "\n" if "--recovery-key" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def subprocess_run(self, cmd, **kw):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, LUKS_UUID + "\n", "")

    def log(self, msg: str) -> None:
        self.logged.append(msg)

    def secrets(self):
        from arches_installer.core.encryption import EncryptionSecrets

        return EncryptionSecrets(pin=PIN, on_recovery_key=self.keys_shown.append)

    def patches(self):
        enc = "arches_installer.core.encryption"
        return [
            patch("arches_installer.core.existing.logged_run", self.run),
            patch("arches_installer.core.disk_layout.logged_run", self.run),
            patch(f"{enc}.logged_run", self.run),
            patch(f"{enc}._quiet", self.quiet),
            patch(f"{enc}.subprocess.run", self.subprocess_run),
            patch(f"{enc}.KEYFILE_DIR", self.keyfile_dir),
            patch(f"{enc}._log", lambda m, log=None: self.logged.append(m)),
            patch(
                "arches_installer.core.existing._log",
                lambda m, log=None: self.logged.append(m),
            ),
            patch("arches_installer.core.disk_layout.MOUNT_ROOT", self.mnt),
        ]


def patched_tools(tmp_path: Path):
    """Yield a FakeTools with every command runner patched."""
    t = FakeTools(tmp_path)
    with ExitStack() as stack:
        for p in t.patches():
            stack.enter_context(p)
        yield t
