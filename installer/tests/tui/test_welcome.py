"""Tests for the WelcomeScreen."""

from __future__ import annotations

from unittest.mock import patch

from arches_installer.core.platform import (
    BootloaderPlatformConfig,
    HardwareDetectionConfig,
    KernelConfig,
    KernelVariant,
    PlatformConfig,
)
from arches_installer.tui.app import ArchesApp


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


@patch("arches_installer.tui.welcome.check_connectivity", return_value=True)
async def test_welcome_renders_title(mock_net) -> None:
    """Welcome screen should display the ARCHES title."""
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        assert app.screen.__class__.__name__ == "WelcomeScreen"
        # Title text should be present
        assert app.query_one(".title")


@patch("arches_installer.tui.welcome.check_connectivity", return_value=True)
async def test_welcome_continue_pushes_disk_select(mock_net) -> None:
    """Clicking Continue should push the DiskSelectScreen."""
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        # Mock disk detection for the next screen so it doesn't fail
        with patch(
            "arches_installer.tui.disk_select.detect_block_devices",
            return_value=[],
        ):
            await pilot.click("#btn-continue")
            assert app.screen.__class__.__name__ == "DiskSelectScreen"


@patch("arches_installer.tui.welcome.check_connectivity", return_value=True)
async def test_welcome_shell_button_exits(mock_net) -> None:
    """Exit Installer button should exit with return code 2."""
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await pilot.click("#btn-shell")
        assert app.return_code == 2


@patch("arches_installer.tui.welcome.check_connectivity", return_value=False)
async def test_welcome_shows_network_offline(mock_net) -> None:
    """Welcome screen should show offline status when not connected."""
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        # The net-status widget should exist
        status = app.query_one("#net-status")
        assert status is not None
