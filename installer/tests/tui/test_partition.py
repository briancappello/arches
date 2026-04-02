"""Tests for the PartitionScreen."""

from __future__ import annotations

from unittest.mock import patch

from arches_installer.core.disk import PartitionMap
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


@patch("arches_installer.tui.partition.subprocess")
async def test_partition_back_pops_screen(mock_subprocess) -> None:
    """Back button should pop the partition screen."""
    mock_subprocess.run.return_value = type(
        "Result",
        (),
        {"stdout": "NAME  SIZE\nvda   20G\n", "returncode": 0},
    )()

    app = ArchesApp(platform=TEST_PLATFORM)
    app.selected_device = "/dev/vda"
    app.push_screen_on_mount = "partition"
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        assert app.screen.__class__.__name__ == "PartitionScreen"

        await pilot.click("#btn-back")
        # Should have popped back
        assert app.screen.__class__.__name__ != "PartitionScreen"


@patch("arches_installer.tui.partition.subprocess")
@patch("arches_installer.core.disk.detect_mounts", return_value=None)
async def test_partition_validate_with_no_mounts(
    mock_detect,
    mock_subprocess,
) -> None:
    """Clicking validate with nothing mounted should stay on PartitionScreen."""
    mock_subprocess.run.return_value = type(
        "Result",
        (),
        {"stdout": "NAME  SIZE\nvda   20G\n", "returncode": 0},
    )()

    app = ArchesApp(platform=TEST_PLATFORM)
    app.selected_device = "/dev/vda"
    app.push_screen_on_mount = "partition"
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        assert app.screen.__class__.__name__ == "PartitionScreen"

        await pilot.click("#btn-validate")
        assert app.screen.__class__.__name__ == "PartitionScreen"


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
) -> None:
    """Valid mounts should set manual mode and advance to TemplateSelectScreen."""
    mock_subprocess.run.return_value = type(
        "Result",
        (),
        {"stdout": "NAME  SIZE\nvda   20G\n", "returncode": 0},
    )()

    app = ArchesApp(platform=TEST_PLATFORM)
    app.selected_device = "/dev/vda"
    app.push_screen_on_mount = "partition"
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        assert app.screen.__class__.__name__ == "PartitionScreen"

        await pilot.click("#btn-validate")
        assert app.partition_mode == "manual"
        assert app.partition_map is not None
        assert app.partition_map.esp == "/dev/sda1"
        assert app.partition_map.root == "/dev/sda2"
        assert app.screen.__class__.__name__ == "TemplateSelectScreen"


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
) -> None:
    """Missing ESP should show errors and stay on PartitionScreen."""
    mock_subprocess.run.return_value = type(
        "Result",
        (),
        {"stdout": "NAME  SIZE\nvda   20G\n", "returncode": 0},
    )()

    app = ArchesApp(platform=TEST_PLATFORM)
    app.selected_device = "/dev/vda"
    app.push_screen_on_mount = "partition"
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        assert app.screen.__class__.__name__ == "PartitionScreen"

        await pilot.click("#btn-validate")
        assert app.screen.__class__.__name__ == "PartitionScreen"
