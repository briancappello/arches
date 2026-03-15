"""Tests for the UserSetupScreen."""

from __future__ import annotations

from unittest.mock import patch

from textual.widgets import Input, Label, OptionList

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
        system=SystemConfig(packages=["git"]),
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


async def _navigate_to_user_setup(pilot) -> None:
    """Navigate from welcome through to user setup screen."""
    # Welcome — select disk
    option_list = pilot.app.query_one("#disk-list", OptionList)
    option_list.highlighted = 0
    await pilot.click("#btn-continue")
    await pilot.wait_for_animation()

    # Partition — auto
    await pilot.click("#btn-auto")
    await pilot.wait_for_animation()

    # Template — select first and continue
    template_list = pilot.app.screen.query_one("#template-list", OptionList)
    template_list.highlighted = 0
    await pilot.click("#btn-continue")
    await pilot.wait_for_animation()


def _setup_mocks():
    """Return the patch decorators for disk and template mocking."""
    return [
        patch(
            "arches_installer.tui.welcome.detect_block_devices",
            return_value=FAKE_DEVICES,
        ),
        patch(
            "arches_installer.tui.template_select.discover_templates",
            return_value=FAKE_TEMPLATES,
        ),
    ]


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
@patch("arches_installer.tui.partition.subprocess")
async def test_user_setup_renders(
    mock_subprocess, mock_templates, mock_devices
) -> None:
    """User setup screen should render with all input fields."""
    mock_subprocess.run.return_value.stdout = "NAME SIZE\nvda 20G\n"
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await _navigate_to_user_setup(pilot)

        assert app.screen.__class__.__name__ == "UserSetupScreen"
        assert app.screen.query_one("#input-hostname", Input)
        assert app.screen.query_one("#input-username", Input)
        assert app.screen.query_one("#input-password", Input)
        assert app.screen.query_one("#input-password-confirm", Input)


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
@patch("arches_installer.tui.partition.subprocess")
async def test_user_setup_valid_input(
    mock_subprocess, mock_templates, mock_devices
) -> None:
    """Valid input should advance to confirm screen."""
    mock_subprocess.run.return_value.stdout = "NAME SIZE\nvda 20G\n"
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await _navigate_to_user_setup(pilot)

        # Fill in the form
        hostname_input = app.screen.query_one("#input-hostname", Input)
        hostname_input.value = "myhost"

        username_input = app.screen.query_one("#input-username", Input)
        username_input.value = "brian"

        password_input = app.screen.query_one("#input-password", Input)
        password_input.value = "secret1234"

        confirm_input = app.screen.query_one("#input-password-confirm", Input)
        confirm_input.value = "secret1234"

        await pilot.click("#btn-continue")

        assert app.hostname == "myhost"
        assert app.username == "brian"
        assert app.password == "secret1234"
        assert app.screen.__class__.__name__ == "ConfirmScreen"


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
@patch("arches_installer.tui.partition.subprocess")
async def test_user_setup_password_mismatch(
    mock_subprocess,
    mock_templates,
    mock_devices,
) -> None:
    """Mismatched passwords should show error and stay on screen."""
    mock_subprocess.run.return_value.stdout = "NAME SIZE\nvda 20G\n"
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await _navigate_to_user_setup(pilot)

        app.screen.query_one("#input-hostname", Input).value = "myhost"
        app.screen.query_one("#input-username", Input).value = "brian"
        app.screen.query_one("#input-password", Input).value = "pass1"
        app.screen.query_one("#input-password-confirm", Input).value = "pass2"

        await pilot.click("#btn-continue")

        # Should still be on user setup screen
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
async def test_user_setup_empty_username(
    mock_subprocess,
    mock_templates,
    mock_devices,
) -> None:
    """Empty username should show error."""
    mock_subprocess.run.return_value.stdout = "NAME SIZE\nvda 20G\n"
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await _navigate_to_user_setup(pilot)

        app.screen.query_one("#input-hostname", Input).value = "myhost"
        app.screen.query_one("#input-username", Input).value = ""
        app.screen.query_one("#input-password", Input).value = "pass1234"
        app.screen.query_one("#input-password-confirm", Input).value = "pass1234"

        await pilot.click("#btn-continue")

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
async def test_user_setup_short_password(
    mock_subprocess,
    mock_templates,
    mock_devices,
) -> None:
    """Password under 4 chars should show error."""
    mock_subprocess.run.return_value.stdout = "NAME SIZE\nvda 20G\n"
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await _navigate_to_user_setup(pilot)

        app.screen.query_one("#input-hostname", Input).value = "myhost"
        app.screen.query_one("#input-username", Input).value = "brian"
        app.screen.query_one("#input-password", Input).value = "abc"
        app.screen.query_one("#input-password-confirm", Input).value = "abc"

        await pilot.click("#btn-continue")

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
async def test_user_setup_invalid_username(
    mock_subprocess,
    mock_templates,
    mock_devices,
) -> None:
    """Username starting with digit should show error."""
    mock_subprocess.run.return_value.stdout = "NAME SIZE\nvda 20G\n"
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await _navigate_to_user_setup(pilot)

        app.screen.query_one("#input-hostname", Input).value = "myhost"
        app.screen.query_one("#input-username", Input).value = "1brian"
        app.screen.query_one("#input-password", Input).value = "pass1234"
        app.screen.query_one("#input-password-confirm", Input).value = "pass1234"

        await pilot.click("#btn-continue")

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
async def test_user_setup_back_button(
    mock_subprocess,
    mock_templates,
    mock_devices,
) -> None:
    """Back button should return to template select."""
    mock_subprocess.run.return_value.stdout = "NAME SIZE\nvda 20G\n"
    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await _navigate_to_user_setup(pilot)
        await pilot.click("#btn-back")

        assert app.screen.__class__.__name__ == "TemplateSelectScreen"
