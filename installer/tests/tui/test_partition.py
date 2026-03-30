"""Tests for the PartitionScreen."""

from __future__ import annotations

from unittest.mock import patch

from textual.widgets import OptionList

from arches_installer.core.disk import BlockDevice, PartitionMap
from arches_installer.core.platform import (
    BootloaderPlatformConfig,
    DiskLayoutConfig,
    HardwareDetectionConfig,
    KernelConfig,
    KernelVariant,
    PlatformConfig,
)
from arches_installer.tui.app import ArchesApp


FAKE_DEVICES = [
    BlockDevice("vda", "/dev/vda", "20G", "QEMU HARDDISK", False, []),
]

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
    disk_layout=DiskLayoutConfig(),
    hardware_detection=HardwareDetectionConfig(),
)


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

    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
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

    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        option_list = app.query_one("#disk-list", OptionList)
        option_list.highlighted = 0
        await pilot.click("#btn-continue")

        await pilot.click("#btn-back")
        assert app.screen.__class__.__name__ == "WelcomeScreen"


# --- New shell-first flow tests ---


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch("arches_installer.tui.partition.subprocess")
@patch("arches_installer.core.disk.detect_mounts", return_value=None)
async def test_partition_validate_with_no_mounts(
    mock_detect,
    mock_subprocess,
    mock_devices,
) -> None:
    """Clicking validate with nothing mounted should stay on PartitionScreen."""
    mock_subprocess.run.return_value = type(
        "Result",
        (),
        {"stdout": "NAME  SIZE\nvda   20G\n", "returncode": 0},
    )()

    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        option_list = app.query_one("#disk-list", OptionList)
        option_list.highlighted = 0
        await pilot.click("#btn-continue")
        assert app.screen.__class__.__name__ == "PartitionScreen"

        await pilot.click("#btn-validate")
        assert app.screen.__class__.__name__ == "PartitionScreen"


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch("arches_installer.tui.partition.subprocess")
@patch(
    "arches_installer.core.disk.validate_mounts",
    return_value=[],
)
@patch(
    "arches_installer.core.disk.detect_mounts",
    return_value=PartitionMap(esp="/dev/sda1", root="/dev/sda2"),
)
async def test_partition_validate_with_valid_mounts(
    mock_detect,
    mock_validate,
    mock_subprocess,
    mock_devices,
) -> None:
    """Valid mounts should set manual mode and advance to TemplateSelectScreen."""
    mock_subprocess.run.return_value = type(
        "Result",
        (),
        {"stdout": "NAME  SIZE\nvda   20G\n", "returncode": 0},
    )()

    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        option_list = app.query_one("#disk-list", OptionList)
        option_list.highlighted = 0
        await pilot.click("#btn-continue")
        assert app.screen.__class__.__name__ == "PartitionScreen"

        await pilot.click("#btn-validate")
        assert app.partition_mode == "manual"
        assert app.partition_map is not None
        assert app.partition_map.esp == "/dev/sda1"
        assert app.partition_map.root == "/dev/sda2"
        assert app.screen.__class__.__name__ == "TemplateSelectScreen"


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch("arches_installer.tui.partition.subprocess")
@patch(
    "arches_installer.core.disk.validate_mounts",
    return_value=[
        "No ESP detected. Mount your EFI System Partition at /mnt/boot (Limine) or /mnt/boot/efi (GRUB)."
    ],
)
@patch(
    "arches_installer.core.disk.detect_mounts",
    return_value=PartitionMap(esp="", root="/dev/sda2"),
)
async def test_partition_validate_missing_esp(
    mock_detect,
    mock_validate,
    mock_subprocess,
    mock_devices,
) -> None:
    """Missing ESP should show errors and stay on PartitionScreen."""
    mock_subprocess.run.return_value = type(
        "Result",
        (),
        {"stdout": "NAME  SIZE\nvda   20G\n", "returncode": 0},
    )()

    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        option_list = app.query_one("#disk-list", OptionList)
        option_list.highlighted = 0
        await pilot.click("#btn-continue")
        assert app.screen.__class__.__name__ == "PartitionScreen"

        await pilot.click("#btn-validate")
        assert app.screen.__class__.__name__ == "PartitionScreen"


@patch(
    "arches_installer.tui.welcome.detect_block_devices",
    return_value=FAKE_DEVICES,
)
@patch("arches_installer.tui.partition.subprocess")
@patch("arches_installer.core.disk.detect_mounts", return_value=None)
async def test_partition_auto_sets_auto_mode(
    mock_detect,
    mock_subprocess,
    mock_devices,
) -> None:
    """Auto-partition should set partition_mode='auto' and partition_map=None."""
    mock_subprocess.run.return_value = type(
        "Result",
        (),
        {"stdout": "NAME  SIZE\nvda   20G\n", "returncode": 0},
    )()

    app = ArchesApp(platform=TEST_PLATFORM)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        option_list = app.query_one("#disk-list", OptionList)
        option_list.highlighted = 0
        await pilot.click("#btn-continue")
        assert app.screen.__class__.__name__ == "PartitionScreen"

        await pilot.click("#btn-auto")
        assert app.partition_mode == "auto"
        assert app.partition_map is None
