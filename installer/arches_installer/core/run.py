"""Shared command execution utilities.

Provides run() and chroot_run() for all installer modules. All command
output is streamed line-by-line to the log callback for real-time feedback.
"""

from __future__ import annotations

import subprocess
from typing import Callable

from arches_installer.core.disk import MOUNT_ROOT

LogCallback = Callable[[str], None]


def _log(msg: str, callback: LogCallback | None = None) -> None:
    if callback:
        callback(msg)


def run(
    cmd: list[str],
    log: LogCallback | None = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a command, streaming output line-by-line to the log callback.

    By default, stdout and stderr are merged and streamed in real time.
    Pass capture_output=True to buffer output instead (e.g. when you
    need to capture stdout for further processing).
    """
    _log(f"$ {' '.join(cmd)}", log)

    # If caller needs to capture stdout (e.g. genfstab), fall back to buffered
    if kwargs.pop("capture_output", False) or kwargs.get("stdout"):
        result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
        if result.stdout.strip():
            _log(result.stdout.strip(), log)
        if result.returncode != 0:
            _log(f"[red]ERROR: {result.stderr.strip()}[/red]", log)
            result.check_returncode()
        return result

    # Stream stdout and stderr line-by-line for real-time feedback
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **kwargs,
    )
    output_lines = []
    assert process.stdout is not None
    for line in process.stdout:
        stripped = line.rstrip("\n")
        output_lines.append(stripped)
        _log(stripped, log)
    process.wait()

    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd)

    return subprocess.CompletedProcess(
        cmd, process.returncode, "\n".join(output_lines), ""
    )


def chroot_run(
    cmd: list[str],
    log: LogCallback | None = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a command inside the target chroot via arch-chroot."""
    return run(["arch-chroot", str(MOUNT_ROOT)] + cmd, log=log, **kwargs)
