"""Tests for the arches_installer.__main__ entry point."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from arches_installer.__main__ import (
    _auto_install_error,
    _print_dry_run_summary,
    _run_auto,
    _run_host,
    main,
)


# ---------------------------------------------------------------------------
# main() — root check
# ---------------------------------------------------------------------------


class TestMainRootCheck:
    """main() should require root unless --dry-run is passed."""

    def test_returns_1_when_not_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["arches-install", "--auto", "/tmp/c.toml"])
        with patch("os.geteuid", return_value=1000):
            assert main() == 1

    def test_dry_run_bypasses_root_check(
        self, monkeypatch: pytest.MonkeyPatch, auto_config_file: Path, x86_64_platform
    ) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["arches-install", "--auto", str(auto_config_file), "--dry-run"],
        )
        with (
            patch("os.geteuid", return_value=1000),
            patch(
                "arches_installer.__main__._load_platform",
                return_value=x86_64_platform,
            ),
        ):
            rc = main()
        assert rc == 0


# ---------------------------------------------------------------------------
# _run_auto
# ---------------------------------------------------------------------------


class TestRunAuto:
    """Tests for _run_auto."""

    def test_dry_run_prints_summary_and_returns_0(
        self,
        auto_config_file: Path,
        x86_64_platform,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with patch(
            "arches_installer.__main__._load_platform",
            return_value=x86_64_platform,
        ):
            rc = _run_auto(auto_config_file, dry_run=True)

        assert rc == 0
        out = capsys.readouterr().out
        assert "Arches Auto Install (dry run)" in out
        assert "Dry run complete. No changes made." in out

    def test_returns_1_for_nonexistent_config(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.toml"
        rc = _run_auto(missing, dry_run=False)
        assert rc == 1


# ---------------------------------------------------------------------------
# _run_host
# ---------------------------------------------------------------------------


class TestRunHost:
    """Tests for _run_host."""

    @pytest.fixture
    def host_config_file(self, tmp_path: Path, templates_dir: Path) -> Path:
        """Create a valid host-install TOML config file."""
        config = tmp_path / "host.toml"
        config.write_text(
            """\
[install]
template = "dev-workstation.toml"
hostname = "hostbox"
username = "hostuser"
password = "hostpass"
partition = "/dev/nvme0n1p6"
esp_partition = "/dev/nvme0n1p4"
mode = "alongside"
"""
        )
        return config

    def test_dry_run_prints_summary_and_returns_0(
        self,
        host_config_file: Path,
        x86_64_platform,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with patch(
            "arches_installer.__main__._load_platform",
            return_value=x86_64_platform,
        ):
            rc = _run_host(host_config_file, dry_run=True)

        assert rc == 0
        out = capsys.readouterr().out
        assert "Arches Host Install (dry run)" in out
        assert "Dry run complete. No changes made." in out

    def test_returns_1_for_nonexistent_config(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.toml"
        rc = _run_host(missing, dry_run=False)
        assert rc == 1


# ---------------------------------------------------------------------------
# _print_dry_run_summary
# ---------------------------------------------------------------------------


class TestPrintDryRunSummary:
    """_print_dry_run_summary should output all expected fields."""

    def test_output_contains_correct_fields(
        self,
        x86_64_platform,
        dev_workstation_template,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _print_dry_run_summary(
            title="Test Summary",
            platform=x86_64_platform,
            template=dev_workstation_template,
            hostname="myhost",
            username="myuser",
            extra_lines=["  Extra:       some-value"],
        )
        out = capsys.readouterr().out
        assert "== Test Summary ==" in out
        assert "x86-64" in out
        assert "linux-cachyos" in out
        assert "limine" in out
        assert "Dev Workstation" in out
        assert "myhost" in out
        assert "myuser" in out
        assert "Extra:       some-value" in out


# ---------------------------------------------------------------------------
# _auto_install_error
# ---------------------------------------------------------------------------


class TestAutoInstallError:
    """_auto_install_error should print fallback message when requested."""

    def test_prints_fallback_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        _auto_install_error("something broke", fallback_to_tui=True)
        err = capsys.readouterr().err
        assert "ERROR: something broke" in err
        assert "Falling back to manual install" in err

    def test_no_fallback_message_when_false(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _auto_install_error("something broke", fallback_to_tui=False)
        err = capsys.readouterr().err
        assert "ERROR: something broke" in err
        assert "Falling back" not in err
