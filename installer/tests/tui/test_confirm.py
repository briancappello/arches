"""Tests for the ConfirmScreen."""

from __future__ import annotations

from unittest.mock import patch

from textual.widgets import Input, OptionList, Static

from arches_installer.core.disk import BlockDevice
from arches_installer.core.platform import (
    BootloaderPlatformConfig,
    HardwareDetectionConfig,
    KernelConfig,
    PlatformConfig,
)
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
        disk=DiskConfig(
            filesystem="btrfs",
            subvolumes=["@", "@home", "@var", "@snapshots"],
        ),
        bootloader=BootloaderConfig(snapshot_boot=True),
        system=SystemConfig(packages=["git", "neovim"]),
        services=["NetworkManager", "sddm"],
        ansible=AnsibleConfig(
            chroot_roles=["base", "kde"],
            firstboot_roles=["dotfiles"],
        ),
    ),
]

TEST_PLATFORM = PlatformConfig(
    name="x86-64",
    description="x86-64 with CachyOS v3",
    arch="x86_64",
    kernel=KernelConfig(package="linux-cachyos", headers="linux-cachyos-headers"),
    bootloader=BootloaderPlatformConfig(),
    hardware_detection=HardwareDetectionConfig(
        enabled=True, tool="chwd", args=["-a"], optional=True
    ),
)


async def _navigate_to_confirm(pilot) -> None:
    """Navigate all the way from welcome to confirm screen."""
    # Welcome — select disk
    option_list = pilot.app.query_one("#disk-list", OptionList)
    option_list.highlighted = 0
    await pilot.click("#btn-continue")
    await pilot.wait_for_animation()

    # Partition — auto
    await pilot.click("#btn-auto")
    await pilot.wait_for_animation()

    # Template — select first
    template_list = pilot.app.screen.query_one("#template-list", OptionList)
    template_list.highlighted = 0
    await pilot.click("#btn-continue")
    await pilot.wait_for_animation()

    # User setup — fill in and continue
    pilot.app.screen.query_one("#input-hostname", Input).value = "testbox"
    pilot.app.screen.query_one("#input-username", Input).value = "testuser"
    pilot.app.screen.query_one("#input-password", Input).value = "pass1234"
    pilot.app.screen.query_one("#input-password-confirm", Input).value = "pass1234"
    await pilot.click("#btn-continue")
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
async def test_confirm_shows_summary(
    mock_subprocess, mock_templates, mock_devices
) -> None:
    """Confirm screen should show a summary of all selections."""
    mock_subprocess.run.return_value.stdout = "NAME SIZE\nvda 20G\n"
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await _navigate_to_confirm(pilot)

        assert app.screen.__class__.__name__ == "ConfirmScreen"

        summary = app.screen.query_one("#summary", Static)
        rendered = str(summary.render())

        assert "Dev Workstation" in rendered
        assert "/dev/vda" in rendered
        assert "btrfs" in rendered
        assert "testbox" in rendered
        assert "testuser" in rendered
        assert "linux-cachyos" in rendered
        assert "x86-64" in rendered


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
@patch("arches_installer.tui.partition.subprocess")
async def test_confirm_shows_subvolumes(
    mock_subprocess, mock_templates, mock_devices
) -> None:
    """Confirm screen should list btrfs subvolumes."""
    mock_subprocess.run.return_value.stdout = "NAME SIZE\nvda 20G\n"
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await _navigate_to_confirm(pilot)

        summary = app.screen.query_one("#summary", Static)
        rendered = str(summary.render())

        assert "@home" in rendered
        assert "@snapshots" in rendered


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
@patch("arches_installer.tui.partition.subprocess")
async def test_confirm_shows_ansible_roles(
    mock_subprocess,
    mock_templates,
    mock_devices,
) -> None:
    """Confirm screen should list chroot and first-boot ansible roles."""
    mock_subprocess.run.return_value.stdout = "NAME SIZE\nvda 20G\n"
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await _navigate_to_confirm(pilot)

        summary = app.screen.query_one("#summary", Static)
        rendered = str(summary.render())

        assert "base" in rendered
        assert "kde" in rendered
        assert "dotfiles" in rendered


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
@patch("arches_installer.tui.partition.subprocess")
async def test_confirm_back_returns_to_user_setup(
    mock_subprocess,
    mock_templates,
    mock_devices,
) -> None:
    """Back button should return to user setup screen."""
    mock_subprocess.run.return_value.stdout = "NAME SIZE\nvda 20G\n"
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await _navigate_to_confirm(pilot)
        await pilot.click("#btn-back")

        assert app.screen.__class__.__name__ == "UserSetupScreen"
