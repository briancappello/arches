"""Tests for template loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from arches_installer.core.template import (
    InstallTemplate,
    load_template,
    resolve_and_merge_modules,
)


class TestLoadTemplate:
    """Test loading templates from TOML files."""

    def test_load_dev_workstation_template(self, templates_dir: Path) -> None:
        tmpl = load_template(templates_dir / "dev-workstation.toml")
        assert tmpl.name == "Dev Workstation"
        assert tmpl.module_slugs == ["base", "zsh", "networking", "kde"]
        # Before resolve, install/services/ansible are empty
        assert tmpl.install.pacstrap == []

    def test_load_vm_server_template(self, templates_dir: Path) -> None:
        tmpl = load_template(templates_dir / "vm-server.toml")
        assert tmpl.name == "VM Server"
        assert tmpl.module_slugs == ["base", "zsh", "networking", "postgresql"]

    def test_resolve_populates_packages(self, templates_dir: Path) -> None:
        tmpl = load_template(templates_dir / "dev-workstation.toml")
        resolved = resolve_and_merge_modules(tmpl)
        assert "git" in resolved.install.pacstrap
        assert "plasma-meta" in resolved.install.pacstrap
        assert "zsh" in resolved.install.pacstrap
        assert "networkmanager" in resolved.install.pacstrap

    def test_resolve_populates_services(self, templates_dir: Path) -> None:
        tmpl = load_template(templates_dir / "dev-workstation.toml")
        resolved = resolve_and_merge_modules(tmpl)
        assert "NetworkManager" in resolved.services
        assert "sddm" in resolved.services

    def test_resolve_populates_ansible_roles(self, templates_dir: Path) -> None:
        tmpl = load_template(templates_dir / "dev-workstation.toml")
        resolved = resolve_and_merge_modules(tmpl)
        assert "base" in resolved.ansible.firstboot_roles
        assert "kde" in resolved.ansible.firstboot_roles
        assert "zsh" in resolved.ansible.firstboot_roles

    def test_resolve_sets_graphical(self, templates_dir: Path) -> None:
        tmpl = load_template(templates_dir / "dev-workstation.toml")
        resolved = resolve_and_merge_modules(tmpl)
        assert resolved.graphical is True

    def test_resolve_not_graphical_for_server(self, templates_dir: Path) -> None:
        tmpl = load_template(templates_dir / "vm-server.toml")
        resolved = resolve_and_merge_modules(tmpl)
        assert resolved.graphical is False

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
        assert tmpl.name == "Unknown"
        assert tmpl.module_slugs == []

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
        assert tmpl.module_slugs == []

    def test_modules_dict(self) -> None:
        data = {
            "meta": {"name": "Test", "description": "A test template"},
            "system": {
                "timezone": "Europe/London",
                "locale": "en_GB.UTF-8",
            },
            "modules": {
                "include": ["base", "networking"],
            },
        }
        tmpl = InstallTemplate.from_dict(data)
        assert tmpl.name == "Test"
        assert tmpl.system.timezone == "Europe/London"
        assert tmpl.module_slugs == ["base", "networking"]
        # Install/services/ansible are empty before resolve
        assert tmpl.install.pacstrap == []
        assert tmpl.services == []
        assert tmpl.ansible.firstboot_roles == []

    def test_unknown_keys_ignored(self) -> None:
        data = {
            "meta": {"name": "Test", "unknown_key": "ignored"},
            "extra_section": {"foo": "bar"},
        }
        tmpl = InstallTemplate.from_dict(data)
        assert tmpl.name == "Test"

    def test_system_defaults(self) -> None:
        tmpl = InstallTemplate.from_dict({"meta": {"name": "Test"}})
        assert tmpl.system.timezone == "America/Denver"
        assert tmpl.system.locale == "en_US.UTF-8"
