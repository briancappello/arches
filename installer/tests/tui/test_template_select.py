"""Tests for the TemplateSelectScreen."""

from __future__ import annotations

from unittest.mock import patch

from textual.widgets import OptionList

from arches_installer.core.platform import (
    BootloaderPlatformConfig,
    HardwareDetectionConfig,
    KernelConfig,
    KernelVariant,
    PlatformConfig,
)
from arches_installer.core.template import (
    AnsibleConfig,
    InstallPhases,
    InstallTemplate,
    SystemConfig,
)
from arches_installer.tui.app import ArchesApp


FAKE_TEMPLATES = [
    InstallTemplate(
        name="Dev Workstation",
        description="KDE Plasma desktop",
        system=SystemConfig(),
        install=InstallPhases(pacstrap=["git", "neovim"]),
        services=["NetworkManager", "sddm"],
        ansible=AnsibleConfig(firstboot_roles=["base", "zsh", "kde"]),
    ),
    InstallTemplate(
        name="VM Server",
        description="Headless ext4",
        system=SystemConfig(),
        install=InstallPhases(pacstrap=["openssh"]),
        services=["sshd"],
        ansible=AnsibleConfig(firstboot_roles=["base", "zsh"]),
    ),
]

TEST_PLATFORM = PlatformConfig(
    name="x86-64",
    description="test",
    arch="x86_64",
    kernel=KernelConfig(
        variants=[
            KernelVariant(package="linux-cachyos", headers="linux-cachyos-headers")
        ]
    ),
    bootloader=BootloaderPlatformConfig(),
    hardware_detection=HardwareDetectionConfig(),
)


@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
async def test_template_screen_lists_templates(mock_templates) -> None:
    """Template screen should show all discovered templates."""
    app = ArchesApp(platform=TEST_PLATFORM)
    app.push_screen_on_mount = "template_select"
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()

        assert app.screen.__class__.__name__ == "TemplateSelectScreen"
        template_list = app.screen.query_one("#template-list", OptionList)
        assert template_list.option_count == 2


@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
async def test_template_select_sets_template(mock_templates) -> None:
    """Selecting a template and clicking Continue should store it."""
    app = ArchesApp(platform=TEST_PLATFORM)
    app.push_screen_on_mount = "template_select"
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()

        template_list = app.screen.query_one("#template-list", OptionList)
        template_list.highlighted = 1  # VM Server
        await pilot.click("#btn-continue")

        assert app.selected_template is not None
        assert app.selected_template.name == "VM Server"
        assert app.screen.__class__.__name__ == "UserSetupScreen"


@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
async def test_template_back_pops_screen(mock_templates) -> None:
    """Back button should pop the template select screen."""
    app = ArchesApp(platform=TEST_PLATFORM)
    app.push_screen_on_mount = "template_select"
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        assert app.screen.__class__.__name__ == "TemplateSelectScreen"

        await pilot.click("#btn-back")
        assert app.screen.__class__.__name__ != "TemplateSelectScreen"
