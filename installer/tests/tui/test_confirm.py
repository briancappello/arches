"""Tests for the ConfirmScreen."""

from __future__ import annotations

from textual.widgets import Static

from arches_installer.core.disk_layout import (
    DiskLayout,
    PartitionSpec,
    SubvolumeSpec,
)
from arches_installer.core.platform import (
    BootloaderPlatformConfig,
    HardwareDetectionConfig,
    KernelConfig,
    KernelVariant,
    PlatformConfig,
)
from arches_installer.core.template import (
    AnsibleConfig,
    InstallPhases,
    InstallTemplate,
    SystemConfig,
)
from arches_installer.tui.app import ArchesApp


FAKE_TEMPLATE = InstallTemplate(
    name="Dev Workstation",
    description="KDE + btrfs",
    system=SystemConfig(),
    install=InstallPhases(pacstrap=["git", "neovim"]),
    services=["NetworkManager", "sddm"],
    ansible=AnsibleConfig(
        firstboot_roles=["base", "zsh", "kde"],
    ),
)

FAKE_LAYOUT = DiskLayout(
    name="Basic",
    description="Test layout",
    bootloaders=["limine"],
    partitions=[
        PartitionSpec(size="2G", filesystem="vfat", mount_point="/boot", label="ESP"),
        PartitionSpec(
            size="*",
            filesystem="btrfs",
            mount_point="/",
            label="archroot",
            subvolumes=[
                SubvolumeSpec(name="@", mount_point="/"),
                SubvolumeSpec(name="@home", mount_point="/home"),
            ],
        ),
    ],
)

TEST_PLATFORM = PlatformConfig(
    name="x86-64",
    description="x86-64 with CachyOS v3",
    arch="x86_64",
    kernel=KernelConfig(
        variants=[
            KernelVariant(package="linux-cachyos", headers="linux-cachyos-headers")
        ]
    ),
    bootloader=BootloaderPlatformConfig(snapshot_boot=True),
    hardware_detection=HardwareDetectionConfig(
        enabled=True, tool="chwd", args=["-a"], optional=True
    ),
)


def _setup_app_for_confirm() -> ArchesApp:
    """Create an app with all state pre-populated for the confirm screen."""
    app = ArchesApp(platform=TEST_PLATFORM)
    app.selected_device = "/dev/vda"
    app.selected_template = FAKE_TEMPLATE
    app.selected_layout = FAKE_LAYOUT
    app.partition_mode = "auto"
    app.hostname = "testbox"
    app.username = "testuser"
    app.password = "pass1234"
    app.push_screen_on_mount = "confirm"
    return app


async def test_confirm_shows_summary() -> None:
    """Confirm screen should show a summary of all selections."""
    app = _setup_app_for_confirm()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()

        assert app.screen.__class__.__name__ == "ConfirmScreen"

        summary = app.screen.query_one("#summary", Static)
        rendered = str(summary.render())

        assert "Dev Workstation" in rendered
        assert "/dev/vda" in rendered
        assert "Basic" in rendered
        assert "testbox" in rendered
        assert "testuser" in rendered
        assert "linux-cachyos" in rendered
        assert "x86-64" in rendered


async def test_confirm_shows_layout_partitions() -> None:
    """Confirm screen should show partition layout details."""
    app = _setup_app_for_confirm()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()

        summary = app.screen.query_one("#summary", Static)
        rendered = str(summary.render())

        assert "vfat" in rendered
        assert "btrfs" in rendered
        assert "@home" in rendered


async def test_confirm_shows_ansible_roles() -> None:
    """Confirm screen should list ansible roles."""
    app = _setup_app_for_confirm()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()

        summary = app.screen.query_one("#summary", Static)
        rendered = str(summary.render())

        assert "base" in rendered
        assert "zsh" in rendered
        assert "kde" in rendered


async def test_confirm_back_returns_to_user_setup() -> None:
    """Back button should return to the previous screen."""
    app = _setup_app_for_confirm()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.wait_for_animation()
        assert app.screen.__class__.__name__ == "ConfirmScreen"
        await pilot.click("#btn-back")
        # After popping confirm, we should be back to whatever was before
        assert app.screen.__class__.__name__ != "ConfirmScreen"
