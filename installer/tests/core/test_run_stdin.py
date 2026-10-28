"""No installer command may wait on the terminal (unattended installs)."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

_INSTALLER = Path(__file__).resolve().parents[2]

CHILD = """
from arches_installer.core.run import run
run(["bash", "-c", "read -r -t 5 answer; echo read-rc=$?"], log=print)
"""


def test_commands_get_eof_not_the_console() -> None:
    # The child's stdin is a pipe that stays open, like a console nobody
    # types on. A command that prompts must see EOF at once.
    start = time.monotonic()
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD],
        cwd=_INSTALLER,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        # Read without closing stdin (communicate() would close it).
        out = child.stdout.read()
        child.wait(timeout=30)
    finally:
        child.stdin.close()
    assert "read-rc=1" in out  # EOF, not 142 (timed out waiting)
    assert time.monotonic() - start < 4


def test_limine_mkinitcpio_wrapper_defaults_to_yes(tmp_path: Path) -> None:
    """The wrapper's prompt, fed EOF, runs limine-mkinitcpio (its default)."""
    from arches_installer.core.run import run

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "ran"
    (bin_dir / "limine-mkinitcpio").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (bin_dir / "limine-mkinitcpio").chmod(0o755)
    # The prompt logic of limine-mkinitcpio-hook 1.40.0's /usr/local/bin/mkinitcpio
    wrapper = tmp_path / "mkinitcpio"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "read -rp \"==> Would you like to run 'limine-mkinitcpio' now? [Y/n]: \" answer\n"
        'case "${answer,,}" in "" | "y" | "yes") limine-mkinitcpio ;; esac\n'
    )
    wrapper.chmod(0o755)
    import os

    run(
        [str(wrapper), "-P"],
        env=dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}"),
    )
    assert marker.exists()
