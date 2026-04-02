"""Tests for the UserSetupScreen."""

from __future__ import annotations

from textual.widgets import Input

from arches_installer.core.platform import (
    BootloaderPlatformConfig,
    HardwareDetectionConfig,
    KernelConfig,
    KernelVariant,
    PlatformConfig,
)
from arches_installer.core.template import (
    InstallPhases,
    InstallTemplate,
    SystemConfig,
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

FAKE_TEMPLATE = InstallTemplate(
    name="Dev Workstation",
    description="KDE + btrfs",
    system=SystemConfig(),
    install=InstallPhases(pacstrap=["git"]),
)


def _setup_app() -> ArchesApp:
    """Create an app with state pre-populated and user_setup pushed."""
    app = ArchesApp(platform=TEST_PLATFORM)
    app.selected_device = "/dev/vda"
    app.selected_template = FAKE_TEMPLATE
    app.push_screen_on_mount = "user_setup"
    return app


async def test_user_setup_renders() -> None:
    """User setup screen should render with all input fields."""
    app = _setup_app()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()

        assert app.screen.__class__.__name__ == "UserSetupScreen"
        assert app.screen.query_one("#input-hostname", Input)
        assert app.screen.query_one("#input-username", Input)
        assert app.screen.query_one("#input-password", Input)
        assert app.screen.query_one("#input-password-confirm", Input)


async def test_user_setup_valid_input() -> None:
    """Valid input should advance to confirm screen."""
    app = _setup_app()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()

        app.screen.query_one("#input-hostname", Input).value = "myhost"
        app.screen.query_one("#input-username", Input).value = "brian"
        app.screen.query_one("#input-password", Input).value = "secret1234"
        app.screen.query_one("#input-password-confirm", Input).value = "secret1234"

        await pilot.click("#btn-continue")

        assert app.hostname == "myhost"
        assert app.username == "brian"
        assert app.password == "secret1234"
        assert app.screen.__class__.__name__ == "ConfirmScreen"


async def test_user_setup_password_mismatch() -> None:
    """Mismatched passwords should show error and stay on screen."""
    app = _setup_app()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()

        app.screen.query_one("#input-hostname", Input).value = "myhost"
        app.screen.query_one("#input-username", Input).value = "brian"
        app.screen.query_one("#input-password", Input).value = "pass1"
        app.screen.query_one("#input-password-confirm", Input).value = "pass2"

        await pilot.click("#btn-continue")

        assert app.screen.__class__.__name__ == "UserSetupScreen"


async def test_user_setup_empty_username() -> None:
    """Empty username should show error."""
    app = _setup_app()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()

        app.screen.query_one("#input-hostname", Input).value = "myhost"
        app.screen.query_one("#input-username", Input).value = ""
        app.screen.query_one("#input-password", Input).value = "pass1234"
        app.screen.query_one("#input-password-confirm", Input).value = "pass1234"

        await pilot.click("#btn-continue")

        assert app.screen.__class__.__name__ == "UserSetupScreen"


async def test_user_setup_short_password() -> None:
    """Password under 4 chars should show error."""
    app = _setup_app()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()

        app.screen.query_one("#input-hostname", Input).value = "myhost"
        app.screen.query_one("#input-username", Input).value = "brian"
        app.screen.query_one("#input-password", Input).value = "abc"
        app.screen.query_one("#input-password-confirm", Input).value = "abc"

        await pilot.click("#btn-continue")

        assert app.screen.__class__.__name__ == "UserSetupScreen"


async def test_user_setup_invalid_username() -> None:
    """Username starting with digit should show error."""
    app = _setup_app()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()

        app.screen.query_one("#input-hostname", Input).value = "myhost"
        app.screen.query_one("#input-username", Input).value = "1brian"
        app.screen.query_one("#input-password", Input).value = "pass1234"
        app.screen.query_one("#input-password-confirm", Input).value = "pass1234"

        await pilot.click("#btn-continue")

        assert app.screen.__class__.__name__ == "UserSetupScreen"


async def test_user_setup_back_button() -> None:
    """Back button should pop the user setup screen."""
    app = _setup_app()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        assert app.screen.__class__.__name__ == "UserSetupScreen"
        await pilot.click("#btn-back")
        assert app.screen.__class__.__name__ != "UserSetupScreen"
