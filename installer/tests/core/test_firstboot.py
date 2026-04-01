"""Tests for first-boot service injection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from arches_installer.core.firstboot import (
    FIRSTBOOT_SERVICE,
    generate_firstboot_script,
    inject_firstboot_service,
)
from arches_installer.core.template import (
    AnsibleConfig,
    InstallPhases,
    InstallTemplate,
    SystemConfig,
)


@pytest.fixture
def no_roles_template() -> InstallTemplate:
    """A template with no firstboot_roles."""
    return InstallTemplate(
        name="Minimal",
        description="No firstboot roles",
        system=SystemConfig(),
        install=InstallPhases(),
        ansible=AnsibleConfig(firstboot_roles=[]),
    )


@pytest.fixture
def mock_mount_root(tmp_path: Path):
    """Patch MOUNT_ROOT to point at tmp_path."""
    with patch("arches_installer.core.firstboot.MOUNT_ROOT", tmp_path):
        yield tmp_path


class TestGenerateFirstbootScript:
    """Tests for the generate_firstboot_script pure function."""

    def test_contains_ansible_tags_for_roles(
        self, dev_workstation_template: InstallTemplate
    ) -> None:
        script = generate_firstboot_script(dev_workstation_template, "alice")
        assert "--tags base,zsh,kde" in script
        assert "Running Ansible (roles: base,zsh,kde)" in script

    def test_no_ansible_block_without_roles(
        self, no_roles_template: InstallTemplate
    ) -> None:
        script = generate_firstboot_script(no_roles_template, "alice")
        assert "ansible-playbook" not in script
        assert "--tags" not in script
        assert "First-boot setup complete" in script

    def test_shebang_and_username(
        self, dev_workstation_template: InstallTemplate
    ) -> None:
        script = generate_firstboot_script(dev_workstation_template, "bob")
        assert script.startswith("#!/usr/bin/env bash\n")
        assert "install_user=bob" in script
        assert "ansible_user=bob" in script

    def test_vm_server_tags(self, vm_server_template: InstallTemplate) -> None:
        script = generate_firstboot_script(vm_server_template, "deploy")
        assert "--tags base,zsh,vm-server" in script
        assert "install_user=deploy" in script


class TestInjectFirstbootService:
    """Tests for inject_firstboot_service side-effects."""

    def test_writes_service_unit_script_and_sentinel(
        self,
        mock_mount_root: Path,
        dev_workstation_template: InstallTemplate,
    ) -> None:
        inject_firstboot_service(dev_workstation_template, "alice")

        service = mock_mount_root / "etc/systemd/system/arches-firstboot.service"
        assert service.exists()
        assert service.read_text() == FIRSTBOOT_SERVICE

        script = mock_mount_root / "opt/arches/firstboot.sh"
        assert script.exists()
        assert script.read_text().startswith("#!/usr/bin/env bash\n")

        sentinel = mock_mount_root / "opt/arches/firstboot-pending"
        assert sentinel.exists()

    def test_creates_graphical_target_symlink(
        self,
        mock_mount_root: Path,
        dev_workstation_template: InstallTemplate,
    ) -> None:
        inject_firstboot_service(dev_workstation_template, "alice")

        symlink = (
            mock_mount_root
            / "etc/systemd/system/graphical.target.wants/arches-firstboot.service"
        )
        assert symlink.is_symlink()
        assert str(symlink.readlink()) == "/etc/systemd/system/arches-firstboot.service"

    def test_noop_when_no_firstboot_roles(
        self,
        mock_mount_root: Path,
        no_roles_template: InstallTemplate,
    ) -> None:
        inject_firstboot_service(no_roles_template, "alice")

        service = mock_mount_root / "etc/systemd/system/arches-firstboot.service"
        assert not service.exists()

        script = mock_mount_root / "opt/arches/firstboot.sh"
        assert not script.exists()

        sentinel = mock_mount_root / "opt/arches/firstboot-pending"
        assert not sentinel.exists()

    def test_script_chmod_755(
        self,
        mock_mount_root: Path,
        dev_workstation_template: InstallTemplate,
    ) -> None:
        inject_firstboot_service(dev_workstation_template, "alice")

        script = mock_mount_root / "opt/arches/firstboot.sh"
        mode = script.stat().st_mode
        assert mode & 0o777 == 0o755
