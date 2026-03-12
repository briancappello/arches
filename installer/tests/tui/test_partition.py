"""Tests for the PartitionScreen."""

from __future__ import annotations

from unittest.mock import patch

from textual.widgets import OptionList

from arches_installer.core.disk import BlockDevice
from arches_installer.tui.app import ArchesApp


FAKE_DEVICES = [
    BlockDevice("vda", "/dev/vda", "20G", "QEMU HARDDISK", False, []),
]


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch("arches_installer.tui.partition.subprocess")
async def test_partition_auto_advances(mock_subprocess, mock_devices) -> None:
    """Auto-partition button should advance to template select."""
    # Mock subprocess.run for lsblk in partition screen
    mock_subprocess.run.return_value = type(
        "Result",
        (),
        {"stdout": "NAME  SIZE\nvda   20G\n", "returncode": 0},
    )()

    app = ArchesApp()
    async with app.run_test(size=(100, 40)) as pilot:
        # Navigate to partition screen
        option_list = app.query_one("#disk-list", OptionList)
        option_list.highlighted = 0
        await pilot.click("#btn-continue")

        assert app.screen.__class__.__name__ == "PartitionScreen"

        # Click auto-partition
        await pilot.click("#btn-auto")
        assert app.screen.__class__.__name__ == "TemplateSelectScreen"


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch("arches_installer.tui.partition.subprocess")
async def test_partition_back_returns_to_welcome(
    mock_subprocess,
    mock_devices,
) -> None:
    """Back button should return to welcome screen."""
    mock_subprocess.run.return_value = type(
        "Result",
        (),
        {"stdout": "NAME  SIZE\nvda   20G\n", "returncode": 0},
    )()

    app = ArchesApp()
    async with app.run_test(size=(100, 40)) as pilot:
        option_list = app.query_one("#disk-list", OptionList)
        option_list.highlighted = 0
        await pilot.click("#btn-continue")

        await pilot.click("#btn-back")
        assert app.screen.__class__.__name__ == "WelcomeScreen"
