"""Tests for the TemplateSelectScreen."""

from __future__ import annotations

from unittest.mock import patch

from textual.widgets import OptionList, Static

from arches_installer.core.disk import BlockDevice
from arches_installer.core.template import (
    AnsibleConfig,
    BootloaderConfig,
    DiskConfig,
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
        disk=DiskConfig(filesystem="btrfs"),
        bootloader=BootloaderConfig(snapshot_boot=True),
        system=SystemConfig(packages=["git", "neovim", "plasma-meta"]),
        services=["NetworkManager"],
        ansible=AnsibleConfig(chroot_roles=["base"]),
    ),
    InstallTemplate(
        name="VM Server",
        description="Headless ext4",
        disk=DiskConfig(filesystem="ext4"),
        bootloader=BootloaderConfig(snapshot_boot=False),
        system=SystemConfig(packages=["openssh"]),
        services=["sshd"],
        ansible=AnsibleConfig(chroot_roles=["base"]),
    ),
]


async def _navigate_to_template_screen(pilot) -> None:
    """Navigate from welcome to template select screen."""
    option_list = pilot.app.query_one("#disk-list", OptionList)
    option_list.highlighted = 0
    await pilot.click("#btn-continue")
    # Now on partition screen — click auto-partition
    await pilot.click("#btn-auto")


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
async def test_template_screen_lists_templates(
    mock_templates,
    mock_devices,
) -> None:
    """Template screen should show all discovered templates."""
    app = ArchesApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await _navigate_to_template_screen(pilot)

        assert app.screen.__class__.__name__ == "TemplateSelectScreen"
        template_list = app.query_one("#template-list", OptionList)
        assert template_list.option_count == 2


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
async def test_template_select_sets_template(
    mock_templates,
    mock_devices,
) -> None:
    """Selecting a template and clicking Continue should store it."""
    app = ArchesApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await _navigate_to_template_screen(pilot)

        template_list = app.query_one("#template-list", OptionList)
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
async def test_template_back_returns_to_partition(
    mock_templates,
    mock_devices,
) -> None:
    """Back button should return to the partition screen."""
    app = ArchesApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await _navigate_to_template_screen(pilot)

        await pilot.click("#btn-back")
        assert app.screen.__class__.__name__ == "PartitionScreen"
