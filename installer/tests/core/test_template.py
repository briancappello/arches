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

    def test_load_dev_workstation_template(self, templates_dir: Path) -> None:
        tmpl = load_template(templates_dir / "dev-workstation.toml")
        assert tmpl.name == "Dev Workstation"
        assert "git" in tmpl.system.packages
        assert "NetworkManager" in tmpl.services
        assert "base" in tmpl.ansible.chroot_roles
        assert "dotfiles" in tmpl.ansible.firstboot_roles

    def test_load_vm_server_template(self, templates_dir: Path) -> None:
        tmpl = load_template(templates_dir / "vm-server.toml")
        assert tmpl.name == "VM Server"
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
        assert tmpl.system.packages == []

    def test_template_has_no_disk_or_bootloader(self, templates_dir: Path) -> None:
        """Templates should not have disk or bootloader attributes."""
        tmpl = load_template(templates_dir / "dev-workstation.toml")
        assert not hasattr(tmpl, "disk")
        assert not hasattr(tmpl, "bootloader")


class TestInstallTemplateFromDict:
    """Test the from_dict constructor."""

    def test_minimal_dict(self) -> None:
        tmpl = InstallTemplate.from_dict({})
        assert tmpl.name == "Unknown"
        assert tmpl.description == ""
        assert tmpl.system.packages == []

    def test_full_dict(self) -> None:
        data = {
            "meta": {"name": "Test", "description": "A test template"},
            "system": {
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

    def test_legacy_disk_and_bootloader_ignored(self) -> None:
        """Templates with legacy [disk] and [bootloader] sections should still load."""
        data = {
            "meta": {"name": "Legacy"},
            "disk": {"filesystem": "btrfs", "subvolumes": ["@"]},
            "bootloader": {"type": "limine", "snapshot_boot": True},
            "system": {"packages": ["git"]},
        }
        tmpl = InstallTemplate.from_dict(data)
        assert tmpl.name == "Legacy"
        assert not hasattr(tmpl, "disk")
        assert not hasattr(tmpl, "bootloader")
        assert tmpl.system.packages == ["git"]

    def test_kernel_in_system_ignored(self) -> None:
        """Templates with a legacy kernel field should still load (ignored)."""
        data = {
            "system": {
                "kernel": "linux-cachyos",
                "packages": ["git"],
            },
        }
        tmpl = InstallTemplate.from_dict(data)
        assert not hasattr(tmpl.system, "kernel")
        assert tmpl.system.packages == ["git"]
