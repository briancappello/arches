"""Tests for arches_installer.core.hardware_rescan."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from arches_installer.core import hardware_rescan


def _which(*present: str):
    return lambda name: f"/usr/bin/{name}" if name in present else None


class TestRunMkinitcpio:
    def test_limine_system_uses_limine_mkinitcpio(self) -> None:
        """Limine systems have no presets; ``mkinitcpio -P`` would fail there."""
        done = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch.object(
                hardware_rescan.shutil,
                "which",
                _which("limine-mkinitcpio", "mkinitcpio"),
            ),
            patch.object(hardware_rescan.subprocess, "run", return_value=done) as run,
        ):
            assert hardware_rescan._run_mkinitcpio(dry_run=False)
        assert run.call_args.args[0] == ["limine-mkinitcpio"]

    def test_preset_system_uses_mkinitcpio_all_presets(self) -> None:
        done = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch.object(hardware_rescan.shutil, "which", _which("mkinitcpio")),
            patch.object(hardware_rescan.subprocess, "run", return_value=done) as run,
        ):
            assert hardware_rescan._run_mkinitcpio(dry_run=False)
        assert run.call_args.args[0] == ["mkinitcpio", "-P"]

    def test_no_mkinitcpio_skips(self) -> None:
        with (
            patch.object(hardware_rescan.shutil, "which", _which()),
            patch.object(hardware_rescan.subprocess, "run") as run,
        ):
            assert not hardware_rescan._run_mkinitcpio(dry_run=False)
        run.assert_not_called()
