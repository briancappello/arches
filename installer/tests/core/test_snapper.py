"""Tests for snapper configuration and snapshot boot setup."""

from __future__ import annotations

import subprocess
from unittest.mock import patch


from arches_installer.core.disk import PartitionMap
from arches_installer.core.platform import (
    BootloaderPlatformConfig,
)
from arches_installer.core.snapper import (
    ROOT_SNAPPER_CONFIG,
    _configure_snapshot_boot_limine,
    configure_snapper,
    configure_snapshot_boot,
    setup_snapshots,
)


# ─── configure_snapper ────────────────────────────────────


@patch("arches_installer.core.snapper.chroot_run")
def test_configure_snapper_writes_config_files(mock_chroot, x86_64_platform, tmp_path):
    """configure_snapper should write snapper root config and conf.d file."""
    with patch("arches_installer.core.snapper.MOUNT_ROOT", tmp_path):
        configure_snapper(x86_64_platform)

    mock_chroot.assert_called_once_with(
        ["snapper", "--no-dbus", "create-config", "/"], log=None
    )

    snapper_conf = tmp_path / "etc" / "snapper" / "configs" / "root"
    assert snapper_conf.exists()
    assert snapper_conf.read_text() == ROOT_SNAPPER_CONFIG

    snapper_confd = tmp_path / "etc" / "conf.d" / "snapper"
    assert snapper_confd.exists()
    assert snapper_confd.read_text() == 'SNAPPER_CONFIGS="root"\n'


@patch("arches_installer.core.snapper.chroot_run")
def test_configure_snapper_skips_non_btrfs(mock_chroot, x86_64_platform, tmp_path):
    """configure_snapper should skip setup when filesystem is not btrfs."""
    ext4_parts = PartitionMap(esp="/dev/vda1", root="/dev/vda2", root_filesystem="ext4")

    with patch("arches_installer.core.snapper.MOUNT_ROOT", tmp_path):
        configure_snapper(x86_64_platform, parts=ext4_parts)

    mock_chroot.assert_not_called()
    assert not (tmp_path / "etc" / "snapper" / "configs" / "root").exists()


@patch("arches_installer.core.snapper.chroot_run")
def test_configure_snapper_handles_existing_config(
    mock_chroot, x86_64_platform, tmp_path
):
    """configure_snapper should continue when create-config fails (config exists)."""
    mock_chroot.side_effect = subprocess.CalledProcessError(1, "snapper")

    with patch("arches_installer.core.snapper.MOUNT_ROOT", tmp_path):
        configure_snapper(x86_64_platform)

    # Config files should still be written despite create-config failure
    snapper_conf = tmp_path / "etc" / "snapper" / "configs" / "root"
    assert snapper_conf.exists()
    assert snapper_conf.read_text() == ROOT_SNAPPER_CONFIG

    snapper_confd = tmp_path / "etc" / "conf.d" / "snapper"
    assert snapper_confd.exists()
    assert snapper_confd.read_text() == 'SNAPPER_CONFIGS="root"\n'


# ─── configure_snapshot_boot ──────────────────────────────


@patch("arches_installer.core.snapper._configure_snapshot_boot_grub")
def test_configure_snapshot_boot_dispatches_grub(mock_grub, aarch64_platform):
    """configure_snapshot_boot should dispatch to grub handler for GRUB bootloader."""
    configure_snapshot_boot(aarch64_platform)
    mock_grub.assert_called_once_with(aarch64_platform, None)


@patch("arches_installer.core.snapper._configure_snapshot_boot_limine")
def test_configure_snapshot_boot_dispatches_limine(mock_limine, x86_64_platform):
    """configure_snapshot_boot should dispatch to limine handler for Limine bootloader."""
    configure_snapshot_boot(x86_64_platform)
    mock_limine.assert_called_once_with(x86_64_platform, None)


@patch("arches_installer.core.snapper._configure_snapshot_boot_grub")
@patch("arches_installer.core.snapper._configure_snapshot_boot_limine")
def test_configure_snapshot_boot_skips_when_disabled(
    mock_limine, mock_grub, x86_64_platform
):
    """configure_snapshot_boot should skip when snapshot_boot is False."""
    x86_64_platform.bootloader = BootloaderPlatformConfig(
        type="limine",
        efi_binary="BOOTX64.EFI",
        efi_fallback_path="EFI/BOOT/BOOTX64.EFI",
        supports_bios=True,
        snapshot_boot=False,
    )

    configure_snapshot_boot(x86_64_platform)

    mock_grub.assert_not_called()
    mock_limine.assert_not_called()


# ─── _configure_snapshot_boot_limine ─────────────────────


@patch("arches_installer.core.snapper.chroot_run")
def test_configure_snapshot_boot_limine_updates_config(
    mock_chroot, x86_64_platform, tmp_path
):
    """_configure_snapshot_boot_limine should update TARGET_OS_NAME in config."""
    conf = tmp_path / "etc" / "limine-snapper-sync.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(
        "# limine-snapper-sync config\n"
        '#TARGET_OS_NAME="Arch Linux"\n'
        'SNAPSHOT_PATH="/@/.snapshots"\n'
    )

    with patch("arches_installer.core.snapper.MOUNT_ROOT", tmp_path):
        _configure_snapshot_boot_limine(x86_64_platform)

    content = conf.read_text()
    assert 'TARGET_OS_NAME="Arches Linux"' in content
    assert 'SNAPSHOT_PATH="/@/.snapshots"' in content
    # The original commented line should be replaced
    assert "#TARGET_OS_NAME" not in content

    mock_chroot.assert_called_once_with(
        ["systemctl", "enable", "limine-snapper-sync.service"],
        log=None,
    )


# ─── setup_snapshots ─────────────────────────────────────


@patch("arches_installer.core.snapper.configure_snapshot_boot")
@patch("arches_installer.core.snapper.configure_snapper")
def test_setup_snapshots_calls_both(mock_snapper, mock_boot, x86_64_platform):
    """setup_snapshots should call configure_snapper and configure_snapshot_boot."""
    setup_snapshots(x86_64_platform)

    mock_snapper.assert_called_once_with(x86_64_platform, parts=None, log=None)
    mock_boot.assert_called_once_with(x86_64_platform, None)
