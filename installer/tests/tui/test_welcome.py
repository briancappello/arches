"""Tests for the WelcomeScreen."""

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
from arches_installer.tui.app import ArchesApp


FAKE_DEVICES = [
    BlockDevice("vda", "/dev/vda", "20G", "QEMU HARDDISK", False, []),
    BlockDevice("sda", "/dev/sda", "500G", "Samsung 970", False, ["sda1"]),
    BlockDevice("sdb", "/dev/sdb", "32G", "USB Flash", True, ["sdb1"]),
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


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
async def test_welcome_renders_title(mock_detect) -> None:
    """Welcome screen should display the ARCHES title."""
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        assert app.screen.__class__.__name__ == "WelcomeScreen"
        # Title text should be present
        assert app.query_one(".title")


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
async def test_welcome_shows_non_removable_disks(mock_detect) -> None:
    """Only non-removable disks should appear in the list."""
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        option_list = app.query_one("#disk-list", OptionList)
        # Should show vda and sda, but NOT sdb (removable)
        assert option_list.option_count == 2


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
async def test_welcome_continue_sets_device(mock_detect) -> None:
    """Clicking Continue should store the selected device and push partition screen."""
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        # Select the first disk and click continue
        option_list = app.query_one("#disk-list", OptionList)
        option_list.highlighted = 0
        await pilot.click("#btn-continue")

        assert app.selected_device == "/dev/vda"
        assert app.screen.__class__.__name__ == "PartitionScreen"


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
async def test_welcome_shell_button_exits(mock_detect) -> None:
    """Recovery Shell button should exit with return code 2."""
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await pilot.click("#btn-shell")
        assert app.return_code == 2


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=[],
)
async def test_welcome_no_disks(mock_detect) -> None:
    """When no disks found, should show a 'no disks' message."""
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        option_list = app.query_one("#disk-list", OptionList)
        assert option_list.option_count == 1  # "No disks found"
