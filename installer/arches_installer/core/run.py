"""Shared command execution utilities.

Provides run() and chroot_run() for all installer modules. All command
output is streamed line-by-line to the log callback for real-time feedback.

All output is also written to ``/var/log/arches-install.log`` for
post-mortem debugging, regardless of whether a callback is provided.
"""

from __future__ import annotations

import atexit
import re
import subprocess
from pathlib import Path
from typing import IO, Callable

from arches_installer.core.disk import MOUNT_ROOT

LogCallback = Callable[[str], None]

# Installer log — always written, regardless of TUI/auto/dry-run mode.
# Writes to two destinations:
#   1. /dev/virtio-ports/arches-log — virtio-serial port piped to host
#      by QEMU (for real-time test visibility). Only exists in QEMU VMs
#      with the arches-log chardev configured.
#   2. /var/log/arches-install.log — local fallback, always available.
_VIRTIO_LOG = Path("/dev/virtio-ports/arches-log")
_FILE_LOG = Path("/var/log/arches-install.log")
_log_files: list[IO] = []
_log_initialized = False

# Rich markup tag pattern — compiled once, reused on every log call.
_MARKUP_RE = re.compile(r"\[/?[a-z_ ]+\]")


def _cleanup_log_files() -> None:
    """Close all open log file handles."""
    for f in _log_files:
        try:
            f.close()
        except OSError:
            pass
    _log_files.clear()


atexit.register(_cleanup_log_files)


def _get_log_files() -> list[IO]:
    """Lazily open log destinations on first write."""
    global _log_files, _log_initialized
    if _log_initialized:
        return _log_files
    _log_initialized = True

    # Virtio serial port (QEMU test harness)
    if _VIRTIO_LOG.exists():
        try:
            _log_files.append(open(_VIRTIO_LOG, "w"))
        except OSError:
            pass

    # Local file log (always)
    try:
        _FILE_LOG.parent.mkdir(parents=True, exist_ok=True)
        _log_files.append(open(_FILE_LOG, "a"))
    except OSError:
        pass

    return _log_files


def log_step(msg: str, callback: LogCallback | None = None) -> None:
    """Log a message to the callback (if any) and to persistent log files."""
    if callback:
        callback(msg)
    # Always write to log destinations (virtio port + local file)
    clean = _MARKUP_RE.sub("", msg)
    for f in _get_log_files():
        try:
            f.write(clean + "\n")
            f.flush()
        except OSError:
            pass


# Backward-compatible alias
_log = log_step


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
