"""Tests for first-boot service injection."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from arches_installer.core.firstboot import (
    FIRSTBOOT_SERVICE,
    _firstboot_service_unit,
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


class TestFirstbootServiceUnit:
    """Tests for the _firstboot_service_unit generator."""

    def test_graphical_targets_graphical(self) -> None:
        unit = _firstboot_service_unit(graphical=True)
        assert "WantedBy=graphical.target" in unit
        assert "Before=display-manager.service" in unit

    def test_headless_targets_multi_user(self) -> None:
        unit = _firstboot_service_unit(graphical=False)
        assert "WantedBy=multi-user.target" in unit
        assert "Before=display-manager.service" not in unit
        assert "graphical.target" not in unit

    def test_constant_matches_graphical(self) -> None:
        """FIRSTBOOT_SERVICE constant equals graphical=True output."""
        assert FIRSTBOOT_SERVICE == _firstboot_service_unit(graphical=True)


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

    @pytest.mark.skipif(
        shutil.which("bash") is None,
        reason="bash not available — skipping syntax check",
    )
    def test_generated_script_is_valid_bash(
        self, dev_workstation_template: InstallTemplate, tmp_path: Path
    ) -> None:
        """`bash -n` must accept the generated firstboot.sh as syntactically
        valid. Regression test for the operator-banner block: a brace
        group cannot take positional args (`} "$@"` is a parse error),
        and the script's tail blocks must close cleanly without
        propagating that mistake again.
        """
        script_text = generate_firstboot_script(dev_workstation_template, "alice")
        script_path = tmp_path / "firstboot.sh"
        script_path.write_text(script_text)

        result = subprocess.run(
            ["bash", "-n", str(script_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"bash -n rejected the generated firstboot.sh.\n"
            f"stderr:\n{result.stderr}\n"
            f"--- script ---\n{script_text}"
        )

    def test_banner_does_not_use_hostname_binary(
        self, dev_workstation_template: InstallTemplate
    ) -> None:
        """The operator banner must read the hostname via `uname -n`
        (coreutils → always in the `base` group), not via `hostname(1)`
        (inetutils → optional, NOT installed by default on Arch).

        Regression test for: firstboot.sh emitting
        `hostname: command not found` because the host was missing the
        inetutils package.
        """
        script = generate_firstboot_script(dev_workstation_template, "alice")
        # `$(hostname)` and `` `hostname` `` are both forbidden in the
        # banner. Substring is acceptable as long as it's not invoking
        # the binary — currently the only such uses are comments,
        # log-file labels, and the `_hn` shell variable.
        assert "$(hostname)" not in script
        assert "`hostname`" not in script
        assert "uname -n" in script


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

    def test_headless_uses_multi_user_target(
        self,
        mock_mount_root: Path,
        vm_server_template: InstallTemplate,
    ) -> None:
        """Non-graphical templates use multi-user.target, not graphical.target."""
        inject_firstboot_service(vm_server_template, "deploy")

        service = mock_mount_root / "etc/systemd/system/arches-firstboot.service"
        assert service.exists()
        content = service.read_text()
        assert "WantedBy=multi-user.target" in content
        assert "graphical.target" not in content
        assert "Before=display-manager.service" not in content

        symlink = (
            mock_mount_root
            / "etc/systemd/system/multi-user.target.wants/arches-firstboot.service"
        )
        assert symlink.is_symlink()
        assert str(symlink.readlink()) == "/etc/systemd/system/arches-firstboot.service"

        # graphical.target.wants should NOT exist
        graphical_wants = mock_mount_root / "etc/systemd/system/graphical.target.wants"
        assert not graphical_wants.exists()

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

    def test_template_gpu_stacks_exposed_as_ansible_var(
        self, mock_mount_root: Path
    ) -> None:
        """Templates that declare ``[gpu].stacks`` must surface them to
        Ansible as ``arches_gpu_stacks=<comma-separated>``.

        Roles like ``aphrodite`` branch on the GPU stack at install
        time (e.g. picking the pytorch CPU wheel index when no NVIDIA
        stack is in play). Without this plumbing they have no way to
        know what hardware they're running on.
        """
        template = InstallTemplate(
            name="LLM Inference",
            description="",
            system=SystemConfig(),
            install=InstallPhases(),
            ansible=AnsibleConfig(firstboot_roles=["base", "aphrodite"]),
            gpu_stacks=["amd-vulkan"],
        )

        inject_firstboot_service(template, "alice")

        script = (mock_mount_root / "opt/arches/firstboot.sh").read_text()
        assert "-e arches_gpu_stacks=amd-vulkan" in script

    def test_caller_extra_vars_override_gpu_stacks(
        self, mock_mount_root: Path
    ) -> None:
        """Caller-supplied extra_vars must beat the auto-derived
        ``arches_gpu_stacks`` value, so machine profiles or
        auto-install configs can pin the variable explicitly."""
        template = InstallTemplate(
            name="LLM Inference",
            description="",
            system=SystemConfig(),
            install=InstallPhases(),
            ansible=AnsibleConfig(firstboot_roles=["base", "aphrodite"]),
            gpu_stacks=["amd-vulkan"],
        )

        inject_firstboot_service(
            template,
            "alice",
            extra_vars={"arches_gpu_stacks": "nvidia-cuda"},
        )

        script = (mock_mount_root / "opt/arches/firstboot.sh").read_text()
        assert "-e arches_gpu_stacks=nvidia-cuda" in script
        assert "-e arches_gpu_stacks=amd-vulkan" not in script

    def test_no_gpu_stacks_no_var(self, mock_mount_root: Path) -> None:
        """Templates without [gpu].stacks must NOT inject an empty
        ``arches_gpu_stacks=`` var (would shadow per-role defaults)."""
        template = InstallTemplate(
            name="Headless",
            description="",
            system=SystemConfig(),
            install=InstallPhases(),
            ansible=AnsibleConfig(firstboot_roles=["base"]),
            gpu_stacks=[],
        )

        inject_firstboot_service(template, "alice")

        script = (mock_mount_root / "opt/arches/firstboot.sh").read_text()
        assert "arches_gpu_stacks" not in script
