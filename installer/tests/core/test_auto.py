"""Tests for auto-install config loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from arches_installer.core.auto import AutoInstallConfig


class TestAutoInstallConfig:
    """Test AutoInstallConfig loading and validation."""

    def test_load_valid_config(self, auto_config_file: Path) -> None:
        config = AutoInstallConfig.from_file(auto_config_file)
        assert config.hostname == "testbox"
        assert config.username == "testuser"
        assert config.password == "testpass"
        assert config.reboot is True
        assert config.template.name == "Dev Workstation"
        assert "git" in config.template.install.pacstrap

    def test_missing_template(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bad.toml"
        config_file.write_text("""\
[install]
username = "user"
password = "pass"
""")
        with pytest.raises(ValueError, match="template"):
            AutoInstallConfig.from_file(config_file)

    def test_missing_username(self, tmp_path: Path, templates_dir: Path) -> None:
        config_file = tmp_path / "bad.toml"
        config_file.write_text("""\
[install]
template = "vm-server.toml"
password = "pass"
""")
        with pytest.raises(ValueError, match="username"):
            AutoInstallConfig.from_file(config_file)

    def test_missing_password(self, tmp_path: Path, templates_dir: Path) -> None:
        config_file = tmp_path / "bad.toml"
        config_file.write_text("""\
[install]
template = "vm-server.toml"
username = "user"
""")
        with pytest.raises(ValueError, match="password"):
            AutoInstallConfig.from_file(config_file)

    def test_nonexistent_template(self, tmp_path: Path, templates_dir: Path) -> None:
        config_file = tmp_path / "bad.toml"
        config_file.write_text("""\
[install]
template = "nonexistent.toml"
username = "user"
password = "pass"
""")
        with pytest.raises(FileNotFoundError):
            AutoInstallConfig.from_file(config_file)

    def test_default_hostname(self, tmp_path: Path, templates_dir: Path) -> None:
        config_file = tmp_path / "nohostname.toml"
        config_file.write_text("""\
[install]
template = "vm-server.toml"
username = "user"
password = "pass"
""")
        config = AutoInstallConfig.from_file(config_file)
        assert config.hostname == "arches"

    def test_default_reboot_false(self, tmp_path: Path, templates_dir: Path) -> None:
        config_file = tmp_path / "noreboot.toml"
        config_file.write_text("""\
[install]
template = "vm-server.toml"
username = "user"
password = "pass"
""")
        config = AutoInstallConfig.from_file(config_file)
        assert config.reboot is False

    def test_nonexistent_config_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            AutoInstallConfig.from_file(tmp_path / "nope.toml")

    def test_default_shutdown_false(self, tmp_path: Path, templates_dir: Path) -> None:
        config_file = tmp_path / "noshutdown.toml"
        config_file.write_text("""\
[install]
template = "vm-server.toml"
username = "user"
password = "pass"
""")
        config = AutoInstallConfig.from_file(config_file)
        assert config.shutdown is False

    def test_shutdown_true(self, tmp_path: Path, templates_dir: Path) -> None:
        config_file = tmp_path / "shutdown.toml"
        config_file.write_text("""\
[install]
template = "vm-server.toml"
username = "user"
password = "pass"
shutdown = true
""")
        config = AutoInstallConfig.from_file(config_file)
        assert config.shutdown is True

    def test_shutdown_overrides_reboot(
        self, tmp_path: Path, templates_dir: Path
    ) -> None:
        config_file = tmp_path / "both.toml"
        config_file.write_text("""\
[install]
template = "vm-server.toml"
username = "user"
password = "pass"
reboot = true
shutdown = true
""")
        config = AutoInstallConfig.from_file(config_file)
        assert config.shutdown is True
        assert (
            config.reboot is True
        )  # both parsed, shutdown takes precedence at runtime

    def test_from_dict_directly(self, templates_dir: Path) -> None:
        data = {
            "install": {
                "template": "dev-workstation.toml",
                "hostname": "mybox",
                "username": "admin",
                "password": "secret",
                "reboot": True,
            },
        }
        config = AutoInstallConfig.from_dict(data)
        assert config.hostname == "mybox"
        assert config.reboot is True
        assert config.shutdown is False
        assert config.template.name == "Dev Workstation"

    def test_no_network_config_by_default(self, templates_dir: Path) -> None:
        """Config without [wifi] or [network] tables has None for both."""
        data = {
            "install": {
                "template": "dev-workstation.toml",
                "username": "user",
                "password": "pass",
            },
        }
        config = AutoInstallConfig.from_dict(data)
        assert config.wifi is None
        assert config.wired is None

    def test_wifi_config(self, templates_dir: Path) -> None:
        """[wifi] table is parsed into WifiConfig."""
        data = {
            "install": {
                "template": "dev-workstation.toml",
                "username": "user",
                "password": "pass",
            },
            "wifi": {
                "ssid": "MyNetwork",
                "psk": "hunter2",
                "static_ip": "192.168.1.50/24",
                "gateway": "192.168.1.1",
                "dns": ["1.1.1.1", "8.8.8.8"],
            },
        }
        config = AutoInstallConfig.from_dict(data)
        assert config.wifi is not None
        assert config.wifi.ssid == "MyNetwork"
        assert config.wifi.psk == "hunter2"
        assert config.wifi.static_ip == "192.168.1.50/24"
        assert config.wifi.gateway == "192.168.1.1"
        assert config.wifi.dns == ["1.1.1.1", "8.8.8.8"]

    def test_wifi_config_open_network(self, templates_dir: Path) -> None:
        """[wifi] without psk is valid (open network)."""
        data = {
            "install": {
                "template": "dev-workstation.toml",
                "username": "user",
                "password": "pass",
            },
            "wifi": {
                "ssid": "OpenNet",
            },
        }
        config = AutoInstallConfig.from_dict(data)
        assert config.wifi is not None
        assert config.wifi.ssid == "OpenNet"
        assert config.wifi.psk is None

    def test_wifi_config_missing_ssid(self, templates_dir: Path) -> None:
        """[wifi] without ssid raises ValueError."""
        data = {
            "install": {
                "template": "dev-workstation.toml",
                "username": "user",
                "password": "pass",
            },
            "wifi": {
                "psk": "hunter2",
            },
        }
        with pytest.raises(ValueError, match="ssid"):
            AutoInstallConfig.from_dict(data)

    def test_wired_config(self, templates_dir: Path) -> None:
        """[network] table is parsed into WiredConfig."""
        data = {
            "install": {
                "template": "dev-workstation.toml",
                "username": "user",
                "password": "pass",
            },
            "network": {
                "interface": "eth0",
                "static_ip": "192.168.1.50/24",
                "gateway": "192.168.1.1",
                "dns": ["1.1.1.1"],
            },
        }
        config = AutoInstallConfig.from_dict(data)
        assert config.wired is not None
        assert config.wired.interface == "eth0"
        assert config.wired.static_ip == "192.168.1.50/24"
        assert config.wired.gateway == "192.168.1.1"

    def test_wired_config_missing_interface(self, templates_dir: Path) -> None:
        """[network] without interface raises ValueError."""
        data = {
            "install": {
                "template": "dev-workstation.toml",
                "username": "user",
                "password": "pass",
            },
            "network": {
                "static_ip": "192.168.1.50/24",
                "gateway": "192.168.1.1",
            },
        }
        with pytest.raises(ValueError, match="interface"):
            AutoInstallConfig.from_dict(data)

    def test_wired_config_missing_gateway(self, templates_dir: Path) -> None:
        """[network] without gateway raises ValueError."""
        data = {
            "install": {
                "template": "dev-workstation.toml",
                "username": "user",
                "password": "pass",
            },
            "network": {
                "interface": "eth0",
                "static_ip": "192.168.1.50/24",
            },
        }
        with pytest.raises(ValueError, match="gateway"):
            AutoInstallConfig.from_dict(data)
