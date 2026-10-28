"""TUI for the encrypted existing-partition layout: PIN, guard, recovery key."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from textual.widgets import Button, Input, Static

from arches_installer.core.disk_layout import load_disk_layout
from arches_installer.core.existing import (
    ExistingLayoutError,
    ExistingPlan,
    ExistingVolume,
)
from arches_installer.tui.app import ArchesApp
from arches_installer.tui.progress import RecoveryKeyScreen
from tests.tui.test_confirm import FAKE_TEMPLATE, TEST_PLATFORM

P1_LAYOUT = Path(__file__).resolve().parents[3] / "disk-layouts" / "p1-spare.toml"
KEY = "-".join(["cbdefghi"] * 8)


def _app(screen: str) -> ArchesApp:
    app = ArchesApp(platform=TEST_PLATFORM)
    app.selected_device = "/dev/nvme0n1"
    app.selected_template = FAKE_TEMPLATE
    app.selected_layout = load_disk_layout(P1_LAYOUT)
    app.partition_mode = "auto"
    app.hostname = "p1"
    app.username = "brian"
    app.password = "pass1234"
    app.push_screen_on_mount = screen
    return app


def _plan() -> ExistingPlan:
    layout = load_disk_layout(P1_LAYOUT)
    esp = next(p for p in layout.partitions if p.reuse)
    root = next(p for p in layout.partitions if not p.reuse)
    return ExistingPlan(
        esp="/dev/nvme1n1p1",
        esp_spec=esp,
        volumes=[ExistingVolume(root, ["/dev/nvme0n1p3", "/dev/nvme1n1p3"])],
    )


async def _fill_user(app, pilot, pin: str, confirm: str) -> None:
    screen = app.screen
    screen.query_one("#input-hostname", Input).value = "p1"
    screen.query_one("#input-username", Input).value = "brian"
    screen.query_one("#input-password", Input).value = "secret1234"
    screen.query_one("#input-password-confirm", Input).value = "secret1234"
    screen.query_one("#input-pin", Input).value = pin
    screen.query_one("#input-pin-confirm", Input).value = confirm
    screen._try_continue()
    await pilot.pause()


async def test_pin_required_for_encrypted_layout() -> None:
    app = _app("user_setup")
    with patch(
        "arches_installer.tui.confirm.resolve_and_plan", return_value=(None, _plan())
    ):
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.wait_for_animation()
            assert app.screen.query_one("#input-pin", Input).display
            await _fill_user(app, pilot, "12", "12")
            assert app.screen.__class__.__name__ == "UserSetupScreen"
            await _fill_user(app, pilot, "123456", "654321")
            assert app.screen.__class__.__name__ == "UserSetupScreen"
            await _fill_user(app, pilot, "123456", "123456")
            await pilot.wait_for_animation()
            assert app.tpm_pin == "123456"
            assert app.screen.__class__.__name__ == "ConfirmScreen"


async def test_no_pin_fields_for_plain_layout() -> None:
    from tests.tui.test_confirm import FAKE_LAYOUT

    app = _app("user_setup")
    app.selected_layout = FAKE_LAYOUT
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.wait_for_animation()
        assert not app.screen.query_one("#input-pin", Input).display


async def test_confirm_shows_only_the_targets() -> None:
    app = _app("confirm")
    with patch(
        "arches_installer.tui.confirm.resolve_and_plan", return_value=("roles", _plan())
    ):
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.wait_for_animation()
            summary = str(app.screen.query_one("#summary", Static).render())
            warning = str(app.screen.query_one("#disk-warning", Static).render())
    assert "/dev/nvme0n1p3, /dev/nvme1n1p3  (erased)" in summary
    assert "mdadm RAID0 -> LUKS2 (TPM2 + PIN, PCR 7) -> btrfs -> /" in summary
    assert "/dev/nvme1n1p1  (reused, not formatted)" in summary
    assert "ERASE only the target partitions" in warning
    assert "recovery key is shown ONCE" in warning
    assert app.resolved_disk_roles == "roles"


async def test_confirm_blocks_install_when_the_guard_fails() -> None:
    app = _app("confirm")
    err = ExistingLayoutError("Refusing to install: /dev/nvme0n1p3 is not empty ['x']")
    with patch("arches_installer.tui.confirm.resolve_and_plan", side_effect=err):
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.wait_for_animation()
            assert app.screen.query_one("#btn-install", Button).disabled
            warning = str(app.screen.query_one("#disk-warning", Static).render())
    assert "is not empty" in warning
    assert app.resolved_disk_roles is None


async def test_recovery_key_screen_shows_key_once() -> None:
    from textual.app import App

    class Host(App):
        dismissed = False

        def on_mount(self) -> None:
            self.push_screen(
                RecoveryKeyScreen(KEY), lambda _: setattr(self, "dismissed", True)
            )

    app = Host()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.wait_for_animation()
        assert KEY in str(app.screen.query_one("#recovery-key", Static).render())
        await pilot.click("#btn-key-stored")
        await pilot.wait_for_animation()
    assert app.dismissed


def test_auto_install_prints_key_to_consoles_only(tmp_path) -> None:
    from arches_installer.tui.progress import InstallProgressScreen

    screen = InstallProgressScreen()
    with (
        patch.object(
            InstallProgressScreen, "app", new=type("A", (), {"auto_install": True})()
        ),
        patch("arches_installer.tui.progress.write_to_consoles") as consoles,
        patch("arches_installer.tui.progress.INSTALL_LOG", tmp_path / "install.log"),
    ):
        screen._show_recovery_key(KEY)
    assert KEY in consoles.call_args[0][0]
    assert not (tmp_path / "install.log").exists()


def test_auto_config_needs_a_pin_for_encrypted_layouts() -> None:
    from arches_installer.core.auto import AutoInstallConfig
    from arches_installer.core.encryption import EncryptionError

    base = {
        "install": {
            "template": "kde-workstation.toml",
            "disk_layout": "p1-spare.toml",
            "username": "u",
            "password": "pw",
        }
    }
    with pytest.raises(EncryptionError):
        AutoInstallConfig.from_dict(base)
    cfg = AutoInstallConfig.from_dict({**base, "encryption": {"tpm_pin": "123456"}})
    assert cfg.tpm_pin == "123456"
    assert cfg.disk_layout.is_encrypted
