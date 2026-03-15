"""Tests for auto-install config loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from arches_installer.core.auto import AutoInstallConfig, log_stdout


class TestAutoInstallConfig:
    """Test AutoInstallConfig loading and validation."""

    def test_load_valid_config(self, auto_config_file: Path) -> None:
        config = AutoInstallConfig.from_file(auto_config_file)
        assert config.device == "/dev/vda"
        assert config.hostname == "testbox"
        assert config.username == "testuser"
        assert config.password == "testpass"
        assert config.template.name == "Dev Workstation"
        assert "git" in config.template.system.packages

    def test_missing_device(self, tmp_path: Path, templates_dir: Path) -> None:
        config_file = tmp_path / "bad.toml"
        config_file.write_text(f"""\
[install]
template = "{templates_dir / "vm-server.toml"}"
username = "user"
password = "pass"
""")
        with pytest.raises(ValueError, match="device"):
            AutoInstallConfig.from_file(config_file)

    def test_missing_template(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bad.toml"
        config_file.write_text("""\
[install]
device = "/dev/sda"
username = "user"
password = "pass"
""")
        with pytest.raises(ValueError, match="template"):
            AutoInstallConfig.from_file(config_file)

    def test_missing_username(self, tmp_path: Path, templates_dir: Path) -> None:
        config_file = tmp_path / "bad.toml"
        config_file.write_text(f"""\
[install]
device = "/dev/sda"
template = "{templates_dir / "vm-server.toml"}"
password = "pass"
""")
        with pytest.raises(ValueError, match="username"):
            AutoInstallConfig.from_file(config_file)

    def test_missing_password(self, tmp_path: Path, templates_dir: Path) -> None:
        config_file = tmp_path / "bad.toml"
        config_file.write_text(f"""\
[install]
device = "/dev/sda"
template = "{templates_dir / "vm-server.toml"}"
username = "user"
""")
        with pytest.raises(ValueError, match="password"):
            AutoInstallConfig.from_file(config_file)

    def test_nonexistent_template_path(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bad.toml"
        config_file.write_text("""\
[install]
device = "/dev/sda"
template = "/nonexistent/template.toml"
username = "user"
password = "pass"
""")
        with pytest.raises(FileNotFoundError):
            AutoInstallConfig.from_file(config_file)

    def test_default_hostname(self, tmp_path: Path, templates_dir: Path) -> None:
        config_file = tmp_path / "nohostname.toml"
        config_file.write_text(f"""\
[install]
device = "/dev/sda"
template = "{templates_dir / "vm-server.toml"}"
username = "user"
password = "pass"
""")
        config = AutoInstallConfig.from_file(config_file)
        assert config.hostname == "arches"

    def test_nonexistent_config_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            AutoInstallConfig.from_file(tmp_path / "nope.toml")

    def test_from_dict_directly(self, templates_dir: Path) -> None:
        data = {
            "install": {
                "device": "/dev/nvme0n1",
                "template": str(templates_dir / "dev-workstation.toml"),
                "hostname": "mybox",
                "username": "admin",
                "password": "secret",
            },
        }
        config = AutoInstallConfig.from_dict(data)
        assert config.device == "/dev/nvme0n1"
        assert config.hostname == "mybox"
        assert config.template.name == "Dev Workstation"


class TestLogStdout:
    """Test the plain-text log function."""

    def test_strips_rich_markup(self, capsys) -> None:
        log_stdout("[bold cyan]-- Phase 1 --[/bold cyan]")
        out = capsys.readouterr().out
        assert "bold" not in out
        assert "Phase 1" in out

    def test_plain_text_passthrough(self, capsys) -> None:
        log_stdout("Hello world")
        out = capsys.readouterr().out
        assert out.strip() == "Hello world"
