"""Tests for the UserSetupScreen."""

from __future__ import annotations

from unittest.mock import patch

from textual.widgets import Input, Label, OptionList

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
        system=SystemConfig(packages=["git"]),
    ),
]


async def _navigate_to_user_setup(pilot) -> None:
    """Navigate from welcome through to user setup screen."""
    # Welcome — select disk
    option_list = pilot.app.query_one("#disk-list", OptionList)
    option_list.highlighted = 0
    await pilot.click("#btn-continue")

    # Partition — auto
    await pilot.click("#btn-auto")

    # Template — select first and continue
    template_list = pilot.app.query_one("#template-list", OptionList)
    template_list.highlighted = 0
    await pilot.click("#btn-continue")


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
async def test_user_setup_renders(mock_templates, mock_devices) -> None:
    """User setup screen should render with all input fields."""
    app = ArchesApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await _navigate_to_user_setup(pilot)

        assert app.screen.__class__.__name__ == "UserSetupScreen"
        assert app.query_one("#input-hostname", Input)
        assert app.query_one("#input-username", Input)
        assert app.query_one("#input-password", Input)
        assert app.query_one("#input-password-confirm", Input)


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch(
    "arches_installer.tui.template_select.discover_templates",
    return_value=FAKE_TEMPLATES,
)
async def test_user_setup_valid_input(mock_templates, mock_devices) -> None:
    """Valid input should advance to confirm screen."""
    app = ArchesApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await _navigate_to_user_setup(pilot)

        # Fill in the form
        hostname_input = app.query_one("#input-hostname", Input)
        hostname_input.value = "myhost"

        username_input = app.query_one("#input-username", Input)
        username_input.value = "brian"

        password_input = app.query_one("#input-password", Input)
        password_input.value = "secret1234"

        confirm_input = app.query_one("#input-password-confirm", Input)
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
async def test_user_setup_password_mismatch(
    mock_templates,
    mock_devices,
) -> None:
    """Mismatched passwords should show error and stay on screen."""
    app = ArchesApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await _navigate_to_user_setup(pilot)

        app.query_one("#input-hostname", Input).value = "myhost"
        app.query_one("#input-username", Input).value = "brian"
        app.query_one("#input-password", Input).value = "pass1"
        app.query_one("#input-password-confirm", Input).value = "pass2"

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
async def test_user_setup_empty_username(
    mock_templates,
    mock_devices,
) -> None:
    """Empty username should show error."""
    app = ArchesApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await _navigate_to_user_setup(pilot)

        app.query_one("#input-hostname", Input).value = "myhost"
        app.query_one("#input-username", Input).value = ""
        app.query_one("#input-password", Input).value = "pass1234"
        app.query_one("#input-password-confirm", Input).value = "pass1234"

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
async def test_user_setup_short_password(
    mock_templates,
    mock_devices,
) -> None:
    """Password under 4 chars should show error."""
    app = ArchesApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await _navigate_to_user_setup(pilot)

        app.query_one("#input-hostname", Input).value = "myhost"
        app.query_one("#input-username", Input).value = "brian"
        app.query_one("#input-password", Input).value = "abc"
        app.query_one("#input-password-confirm", Input).value = "abc"

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
async def test_user_setup_invalid_username(
    mock_templates,
    mock_devices,
) -> None:
    """Username starting with digit should show error."""
    app = ArchesApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await _navigate_to_user_setup(pilot)

        app.query_one("#input-hostname", Input).value = "myhost"
        app.query_one("#input-username", Input).value = "1brian"
        app.query_one("#input-password", Input).value = "pass1234"
        app.query_one("#input-password-confirm", Input).value = "pass1234"

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
async def test_user_setup_back_button(
    mock_templates,
    mock_devices,
) -> None:
    """Back button should return to template select."""
    app = ArchesApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await _navigate_to_user_setup(pilot)
        await pilot.click("#btn-back")

        assert app.screen.__class__.__name__ == "TemplateSelectScreen"
