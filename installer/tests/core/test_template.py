"""Tests for template loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from arches_installer.core.template import (
    InstallTemplate,
    load_template,
)


class TestLoadTemplate:
    """Test loading templates from TOML files."""

    def test_load_btrfs_template(self, templates_dir: Path) -> None:
        tmpl = load_template(templates_dir / "dev-workstation.toml")
        assert tmpl.name == "Dev Workstation"
        assert tmpl.disk.filesystem == "btrfs"
        assert tmpl.disk.subvolumes == ["@", "@home", "@var", "@snapshots"]
        assert tmpl.disk.esp_size_mib == 2048
        assert tmpl.bootloader.snapshot_boot is True
        assert tmpl.system.kernel == "linux-cachyos"
        assert "git" in tmpl.system.packages
        assert "NetworkManager" in tmpl.services
        assert "base" in tmpl.ansible.chroot_roles
        assert "dotfiles" in tmpl.ansible.firstboot_roles

    def test_load_ext4_template(self, templates_dir: Path) -> None:
        tmpl = load_template(templates_dir / "vm-server.toml")
        assert tmpl.name == "VM Server"
        assert tmpl.disk.filesystem == "ext4"
        assert tmpl.disk.subvolumes == []
        assert tmpl.disk.esp_size_mib == 512
        assert tmpl.bootloader.snapshot_boot is False
        assert "openssh" in tmpl.system.packages
        assert "sshd" in tmpl.services
        assert tmpl.ansible.firstboot_roles == []

    def test_load_nonexistent_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_template(tmp_path / "nope.toml")

    def test_load_invalid_toml(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text("this is not [valid toml")
        with pytest.raises(Exception):
            load_template(bad)

    def test_load_empty_toml(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.toml"
        empty.write_text("")
        tmpl = load_template(empty)
        # Should use all defaults
        assert tmpl.name == "Unknown"
        assert tmpl.disk.filesystem == "ext4"
        assert tmpl.bootloader.type == "limine"
        assert tmpl.system.kernel == "linux-cachyos"


class TestInstallTemplateFromDict:
    """Test the from_dict constructor."""

    def test_minimal_dict(self) -> None:
        tmpl = InstallTemplate.from_dict({})
        assert tmpl.name == "Unknown"
        assert tmpl.description == ""
        assert tmpl.disk.filesystem == "ext4"

    def test_full_dict(self) -> None:
        data = {
            "meta": {"name": "Test", "description": "A test template"},
            "disk": {
                "filesystem": "btrfs",
                "subvolumes": ["@", "@home"],
                "mount_options": "compress=zstd:1",
                "esp_size_mib": 1024,
                "swap": "zram",
            },
            "bootloader": {"type": "limine", "snapshot_boot": True},
            "system": {
                "kernel": "linux-cachyos-bore",
                "timezone": "Europe/London",
                "locale": "en_GB.UTF-8",
                "packages": ["vim", "curl"],
            },
            "services": {"enable": ["sshd"]},
            "ansible": {
                "chroot_roles": ["base"],
                "firstboot_roles": ["dotfiles"],
            },
        }
        tmpl = InstallTemplate.from_dict(data)
        assert tmpl.name == "Test"
        assert tmpl.disk.filesystem == "btrfs"
        assert tmpl.disk.subvolumes == ["@", "@home"]
        assert tmpl.bootloader.snapshot_boot is True
        assert tmpl.system.kernel == "linux-cachyos-bore"
        assert tmpl.system.timezone == "Europe/London"
        assert tmpl.services == ["sshd"]
        assert tmpl.ansible.chroot_roles == ["base"]

    def test_unknown_keys_ignored(self) -> None:
        data = {
            "meta": {"name": "Test", "unknown_key": "ignored"},
            "extra_section": {"foo": "bar"},
        }
        tmpl = InstallTemplate.from_dict(data)
        assert tmpl.name == "Test"


class TestDiskConfig:
    """Test DiskConfig dataclass defaults."""

    def test_defaults(self) -> None:
        from arches_installer.core.template import DiskConfig

        dc = DiskConfig(filesystem="ext4")
        assert dc.mount_options == "noatime"
        assert dc.subvolumes == []
        assert dc.esp_size_mib == 512
        assert dc.swap == "zram"
