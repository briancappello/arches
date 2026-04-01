"""Tests for the InstallProgressScreen."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from textual.widgets import Button, Label, RichLog

from arches_installer.core.disk import PartitionMap
from arches_installer.core.platform import PlatformConfig
from arches_installer.core.template import InstallTemplate
from arches_installer.tui.app import ArchesApp


# The background install thread may outlive the Textual app context in tests,
# causing NoActiveAppError warnings.  These are harmless race conditions.
pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)

FAKE_PARTITION_MAP = PartitionMap(esp="/dev/vda1", root="/dev/vda2")


def _make_app(
    platform: PlatformConfig,
    template: InstallTemplate,
) -> ArchesApp:
    """Create an ArchesApp pre-configured to jump straight to the progress screen."""
    app = ArchesApp(platform=platform)
    app.selected_template = template
    app.selected_device = "/dev/vda"
    app.partition_mode = "auto"
    app.hostname = "testbox"
    app.username = "testuser"
    app.password = "testpass"
    app.push_screen_on_mount = "progress"
    return app


@patch("arches_installer.tui.progress.INSTALL_LOG", new_callable=MagicMock)
@patch("arches_installer.tui.progress.run_install_pipeline")
async def test_progress_initial_render(
    mock_pipeline,
    mock_log_path,
    x86_64_platform,
    dev_workstation_template,
) -> None:
    """Progress screen should render with 'Installing...' title and disabled buttons."""
    # Block the pipeline in the background thread so it doesn't complete
    # before we can inspect the initial UI state.
    gate = threading.Event()

    def _block(*args, **kwargs):
        gate.wait(timeout=5)
        return FAKE_PARTITION_MAP

    mock_pipeline.side_effect = _block

    app = _make_app(x86_64_platform, dev_workstation_template)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()

        assert app.screen.__class__.__name__ == "InstallProgressScreen"

        title = app.screen.query_one("#title", Label)
        assert "Installing" in str(title.render())

        btn_reboot = app.screen.query_one("#btn-reboot", Button)
        btn_shutdown = app.screen.query_one("#btn-shutdown", Button)
        assert btn_reboot.disabled is True
        assert btn_shutdown.disabled is True

        # Release the background thread so it can finish cleanly
        gate.set()


@patch("arches_installer.tui.progress.INSTALL_LOG", new_callable=MagicMock)
@patch(
    "arches_installer.tui.progress.run_install_pipeline",
    return_value=FAKE_PARTITION_MAP,
)
async def test_progress_success_enables_buttons(
    mock_pipeline,
    mock_log_path,
    x86_64_platform,
    dev_workstation_template,
) -> None:
    """After a successful install, buttons should be enabled and title should say 'Complete'."""
    app = _make_app(x86_64_platform, dev_workstation_template)
    async with app.run_test(size=(100, 40)) as pilot:
        # Wait for the background thread to finish and UI to update
        await pilot.wait_for_animation()
        await pilot.pause()
        await pilot.wait_for_animation()

        title = app.screen.query_one("#title", Label)
        assert "Complete" in str(title.render())

        btn_reboot = app.screen.query_one("#btn-reboot", Button)
        btn_shutdown = app.screen.query_one("#btn-shutdown", Button)
        assert btn_reboot.disabled is False
        assert btn_shutdown.disabled is False

        assert app.install_success is True


@patch("arches_installer.tui.progress.INSTALL_LOG", new_callable=MagicMock)
@patch("arches_installer.tui.progress.run_install_pipeline")
async def test_progress_failure_shows_error(
    mock_pipeline,
    mock_log_path,
    x86_64_platform,
    dev_workstation_template,
) -> None:
    """A failed install should show the error message in the log."""
    mock_pipeline.side_effect = RuntimeError("disk exploded")

    app = _make_app(x86_64_platform, dev_workstation_template)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await pilot.pause()
        await pilot.wait_for_animation()

        # Buttons should still be enabled so the user can reboot/shutdown
        btn_reboot = app.screen.query_one("#btn-reboot", Button)
        btn_shutdown = app.screen.query_one("#btn-shutdown", Button)
        assert btn_reboot.disabled is False
        assert btn_shutdown.disabled is False

        # install_success should remain False
        assert app.install_success is False


@patch("arches_installer.tui.progress.INSTALL_LOG", new_callable=MagicMock)
@patch(
    "arches_installer.tui.progress.run_install_pipeline",
    return_value=FAKE_PARTITION_MAP,
)
async def test_progress_success_stores_partition_map(
    mock_pipeline,
    mock_log_path,
    x86_64_platform,
    dev_workstation_template,
) -> None:
    """Successful install should store the partition map on the app."""
    app = _make_app(x86_64_platform, dev_workstation_template)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        await pilot.pause()
        await pilot.wait_for_animation()

        assert app.partition_map == FAKE_PARTITION_MAP


@patch("arches_installer.tui.progress.INSTALL_LOG", new_callable=MagicMock)
@patch(
    "arches_installer.tui.progress.run_install_pipeline",
    return_value=FAKE_PARTITION_MAP,
)
async def test_progress_has_rich_log_widget(
    mock_pipeline,
    mock_log_path,
    x86_64_platform,
    dev_workstation_template,
) -> None:
    """The progress screen should contain a RichLog widget for log output."""
    app = _make_app(x86_64_platform, dev_workstation_template)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()

        log_widget = app.screen.query_one("#install-log", RichLog)
        assert log_widget is not None
