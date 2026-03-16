"""Tests for the TemplateSelectScreen."""

from __future__ import annotations

from unittest.mock import patch

from textual.widgets import OptionList

from arches_installer.core.disk import BlockDevice
from arches_installer.core.platform import (
    BootloaderPlatformConfig,
    DiskLayoutConfig,
    HardwareDetectionConfig,
    KernelConfig,
    PlatformConfig,
)
from arches_installer.core.template import (
    AnsibleConfig,
    InstallPhases,
    InstallTemplate,
    SystemConfig,
)
from arches_installer.tui.app import ArchesApp


FAKE_DEVICES = [
    BlockDevice("vda", "/dev/vda", "20G", "QEMU HARDDISK", False, []),
]

FAKE_TEMPLATES = [
    InstallTemplate(
        name="Dev Workstation",
        description="KDE + btrfs",
        system=SystemConfig(),
        install=InstallPhases(pacstrap=["git", "neovim", "plasma-meta"]),
        services=["NetworkManager"],
        ansible=AnsibleConfig(firstboot_roles=["base", "zsh"]),
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
    kernel=KernelConfig(package="linux-cachyos", headers="linux-cachyos-headers"),
    bootloader=BootloaderPlatformConfig(),
    disk_layout=DiskLayoutConfig(),
    hardware_detection=HardwareDetectionConfig(),
)


async def _navigate_to_template_screen(pilot) -> None:
    """Navigate from welcome to template select screen."""
    option_list = pilot.app.query_one("#disk-list", OptionList)
    option_list.highlighted = 0
    await pilot.click("#btn-continue")
    await pilot.wait_for_animation()
    # Now on partition screen — click auto-partition
    await pilot.click("#btn-auto")
    await pilot.wait_for_animation()


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
@patch("arches_installer.tui.partition.subprocess")
async def test_template_screen_lists_templates(
    mock_subprocess,
    mock_templates,
    mock_devices,
) -> None:
    """Template screen should show all discovered templates."""
    mock_subprocess.run.return_value.stdout = "NAME SIZE\nvda 20G\n"
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await _navigate_to_template_screen(pilot)

        assert app.screen.__class__.__name__ == "TemplateSelectScreen"
        template_list = app.screen.query_one("#template-list", OptionList)
        assert template_list.option_count == 2


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
@patch("arches_installer.tui.partition.subprocess")
async def test_template_select_sets_template(
    mock_subprocess,
    mock_templates,
    mock_devices,
) -> None:
    """Selecting a template and clicking Continue should store it."""
    mock_subprocess.run.return_value.stdout = "NAME SIZE\nvda 20G\n"
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await _navigate_to_template_screen(pilot)

        template_list = app.screen.query_one("#template-list", OptionList)
        template_list.highlighted = 1  # VM Server
        await pilot.click("#btn-continue")

        assert app.selected_template is not None
        assert app.selected_template.name == "VM Server"
        assert app.screen.__class__.__name__ == "UserSetupScreen"


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
@patch("arches_installer.tui.partition.subprocess")
async def test_template_back_returns_to_partition(
    mock_subprocess,
    mock_templates,
    mock_devices,
) -> None:
    """Back button should return to the partition screen."""
    mock_subprocess.run.return_value.stdout = "NAME SIZE\nvda 20G\n"
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await _navigate_to_template_screen(pilot)

        await pilot.click("#btn-back")
        assert app.screen.__class__.__name__ == "PartitionScreen"
