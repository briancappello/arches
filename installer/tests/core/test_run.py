"""Tests for command execution utilities (run, chroot_run, log_step)."""

from __future__ import annotations

import io
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import arches_installer.core.run as run_mod
from arches_installer.core.run import (
    _cleanup_log_files,
    chroot_run,
    log_step,
    run,
)


@pytest.fixture(autouse=True)
def _reset_log_state():
    """Reset module-level log state before each test so lazy init re-triggers."""
    run_mod._log_initialized = False
    run_mod._log_files.clear()
    yield
    # Teardown: ensure no leaked handles
    run_mod._log_initialized = False
    run_mod._log_files.clear()


@pytest.fixture()
def fake_log_files():
    """Patch _get_log_files to return in-memory StringIO buffers."""
    buf = io.StringIO()
    with patch.object(run_mod, "_get_log_files", return_value=[buf]):
        yield buf


# ---------------------------------------------------------------------------
# log_step / _log
# ---------------------------------------------------------------------------


class TestLogStep:
    """Tests for log_step (also aliased as _log)."""

    def test_calls_callback_when_provided(self, fake_log_files: io.StringIO) -> None:
        """log_step should invoke the callback with the original message."""
        cb = MagicMock()
        log_step("[green]Installing packages[/green]", callback=cb)
        cb.assert_called_once_with("[green]Installing packages[/green]")

    def test_strips_rich_markup_in_log_files(self, fake_log_files: io.StringIO) -> None:
        """Rich markup tags must be stripped before writing to log files."""
        log_step("[bold red]ERROR: something broke[/bold red]")
        written = fake_log_files.getvalue()
        assert "[bold red]" not in written
        assert "[/bold red]" not in written
        assert "ERROR: something broke\n" in written

    def test_no_callback_still_writes_to_files(
        self, fake_log_files: io.StringIO
    ) -> None:
        """When no callback is given, output still reaches log files."""
        log_step("hello world")
        assert "hello world\n" in fake_log_files.getvalue()

    def test_callback_receives_raw_markup(self, fake_log_files: io.StringIO) -> None:
        """The callback gets the unmodified message (Rich markup intact)."""
        cb = MagicMock()
        log_step("[cyan]step 1[/cyan]", callback=cb)
        cb.assert_called_once_with("[cyan]step 1[/cyan]")
        # But the file should not contain the markup
        assert "[cyan]" not in fake_log_files.getvalue()
        assert "step 1\n" in fake_log_files.getvalue()


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


class TestRun:
    """Tests for the run() helper."""

    def test_simple_command_returns_completed_process(
        self, fake_log_files: io.StringIO
    ) -> None:
        """run() should return a CompletedProcess on success."""
        stdout_pipe = io.StringIO("line1\nline2\n")
        mock_proc = MagicMock()
        mock_proc.stdout = stdout_pipe
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            result = run(["echo", "hi"])

        assert isinstance(result, subprocess.CompletedProcess)
        assert result.returncode == 0
        assert "line1" in result.stdout
        assert "line2" in result.stdout
        mock_popen.assert_called_once()

    def test_capture_output_returns_stdout(self, fake_log_files: io.StringIO) -> None:
        """run(capture_output=True) should use subprocess.run and return stdout."""
        fake_result = subprocess.CompletedProcess(
            ["cat", "/etc/fstab"], 0, stdout="UUID=abc / ext4\n", stderr=""
        )
        with patch("subprocess.run", return_value=fake_result):
            result = run(["cat", "/etc/fstab"], capture_output=True)

        assert result.stdout == "UUID=abc / ext4\n"
        assert result.returncode == 0

    def test_nonzero_exit_raises_called_process_error(
        self, fake_log_files: io.StringIO
    ) -> None:
        """run() should raise CalledProcessError when the command fails."""
        stdout_pipe = io.StringIO("")
        mock_proc = MagicMock()
        mock_proc.stdout = stdout_pipe
        mock_proc.wait.return_value = 1
        mock_proc.returncode = 1

        with patch("subprocess.Popen", return_value=mock_proc):
            with pytest.raises(subprocess.CalledProcessError) as exc_info:
                run(["false"])

        assert exc_info.value.returncode == 1

    def test_capture_output_nonzero_raises(self, fake_log_files: io.StringIO) -> None:
        """run(capture_output=True) raises on non-zero return code."""
        fake_result = subprocess.CompletedProcess(
            ["bad-cmd"], 2, stdout="", stderr="not found"
        )
        with patch("subprocess.run", return_value=fake_result):
            with pytest.raises(subprocess.CalledProcessError):
                run(["bad-cmd"], capture_output=True)

    def test_streams_output_to_log_callback(self, fake_log_files: io.StringIO) -> None:
        """Each line of output should be forwarded to the log callback."""
        cb = MagicMock()
        stdout_pipe = io.StringIO("alpha\nbeta\ngamma\n")
        mock_proc = MagicMock()
        mock_proc.stdout = stdout_pipe
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            run(["some-cmd"], log=cb)

        # The callback should receive the command echo plus each output line
        logged_msgs = [c.args[0] for c in cb.call_args_list]
        assert "$ some-cmd" in logged_msgs[0]
        assert "alpha" in logged_msgs
        assert "beta" in logged_msgs
        assert "gamma" in logged_msgs


# ---------------------------------------------------------------------------
# chroot_run()
# ---------------------------------------------------------------------------


class TestChrootRun:
    """Tests for chroot_run()."""

    def test_prepends_arch_chroot_and_mount_root(
        self, fake_log_files: io.StringIO
    ) -> None:
        """chroot_run should wrap the command with arch-chroot MOUNT_ROOT."""
        stdout_pipe = io.StringIO("")
        mock_proc = MagicMock()
        mock_proc.stdout = stdout_pipe
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            chroot_run(["pacman", "-Syu"])

        actual_cmd = mock_popen.call_args[0][0]
        assert actual_cmd[0] == "arch-chroot"
        assert actual_cmd[1] == "/mnt"
        assert actual_cmd[2:] == ["pacman", "-Syu"]

    def test_passes_kwargs_through(self, fake_log_files: io.StringIO) -> None:
        """Extra kwargs should propagate to the underlying run() call."""
        fake_result = subprocess.CompletedProcess(
            ["arch-chroot", "/mnt", "cat", "/etc/hostname"],
            0,
            stdout="myhost\n",
            stderr="",
        )
        with patch("subprocess.run", return_value=fake_result):
            result = chroot_run(["cat", "/etc/hostname"], capture_output=True)

        assert result.stdout == "myhost\n"


# ---------------------------------------------------------------------------
# _cleanup_log_files()
# ---------------------------------------------------------------------------


class TestCleanupLogFiles:
    """Tests for _cleanup_log_files()."""

    def test_closes_file_handles(self) -> None:
        """All file handles in _log_files should be closed and the list cleared."""
        f1 = MagicMock()
        f2 = MagicMock()
        run_mod._log_files.extend([f1, f2])

        _cleanup_log_files()

        f1.close.assert_called_once()
        f2.close.assert_called_once()
        assert run_mod._log_files == []

    def test_tolerates_oserror_on_close(self) -> None:
        """If close() raises OSError the function should still clear the list."""
        f_bad = MagicMock()
        f_bad.close.side_effect = OSError("disk gone")
        f_good = MagicMock()
        run_mod._log_files.extend([f_bad, f_good])

        _cleanup_log_files()  # must not raise

        f_bad.close.assert_called_once()
        f_good.close.assert_called_once()
        assert run_mod._log_files == []
