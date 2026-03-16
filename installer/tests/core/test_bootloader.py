"""Tests for bootloader installation and configuration."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from arches_installer.core.bootloader import (
    _install_grub,
    _install_limine,
    detect_firmware,
    get_root_partuuid,
    get_root_uuid,
    install_bootloader,
)
from arches_installer.core.platform import (
    BootloaderPlatformConfig,
)


# ─── detect_firmware ──────────────────────────────────────


def test_detect_firmware_uefi():
    with patch("arches_installer.core.bootloader.Path") as mock_path_cls:
        mock_path_cls.return_value.exists.return_value = True
        assert detect_firmware() == "uefi"
        mock_path_cls.assert_called_with("/sys/firmware/efi")


def test_detect_firmware_bios():
    with patch("arches_installer.core.bootloader.Path") as mock_path_cls:
        mock_path_cls.return_value.exists.return_value = False
        assert detect_firmware() == "bios"


# ─── get_root_uuid / get_root_partuuid ───────────────────


def test_get_root_uuid():
    fake_result = MagicMock()
    fake_result.stdout = "abcd-1234-efgh-5678\n"
    with patch(
        "arches_installer.core.bootloader.subprocess.run", return_value=fake_result
    ) as mock_run:
        uuid = get_root_uuid("/dev/sda2")
        assert uuid == "abcd-1234-efgh-5678"
        mock_run.assert_called_once_with(
            ["blkid", "-s", "UUID", "-o", "value", "/dev/sda2"],
            capture_output=True,
            text=True,
            check=True,
        )


def test_get_root_partuuid():
    fake_result = MagicMock()
    fake_result.stdout = "part-uuid-1234\n"
    with patch(
        "arches_installer.core.bootloader.subprocess.run", return_value=fake_result
    ) as mock_run:
        partuuid = get_root_partuuid("/dev/sda2")
        assert partuuid == "part-uuid-1234"
        mock_run.assert_called_once_with(
            ["blkid", "-s", "PARTUUID", "-o", "value", "/dev/sda2"],
            capture_output=True,
            text=True,
            check=True,
        )


# ─── install_bootloader dispatch ──────────────────────────


@patch("arches_installer.core.bootloader._install_limine")
def test_install_bootloader_dispatches_limine(mock_limine, x86_64_platform):
    install_bootloader(x86_64_platform, "/dev/sda", "/dev/sda1", "/dev/sda2")
    mock_limine.assert_called_once_with(
        x86_64_platform,
        "/dev/sda",
        "/dev/sda1",
        "/dev/sda2",
        None,
    )


@patch("arches_installer.core.bootloader._install_grub")
def test_install_bootloader_dispatches_grub(mock_grub, aarch64_platform):
    install_bootloader(aarch64_platform, "/dev/sda", "/dev/sda1", "/dev/sda2")
    mock_grub.assert_called_once_with(
        aarch64_platform,
        "/dev/sda",
        "/dev/sda1",
        "/dev/sda2",
        None,
    )


def test_install_bootloader_unknown_type(x86_64_platform):
    x86_64_platform.bootloader = BootloaderPlatformConfig(type="systemd-boot")
    with pytest.raises(ValueError, match="Unknown bootloader type: systemd-boot"):
        install_bootloader(x86_64_platform, "/dev/sda", "/dev/sda1", "/dev/sda2")


# ─── _install_grub ────────────────────────────────────────


@patch("arches_installer.core.bootloader.chroot_run")
@patch("arches_installer.core.bootloader.write_grub_defaults")
@patch("arches_installer.core.bootloader.detect_firmware", return_value="uefi")
def test_install_grub_aarch64_uefi(
    mock_fw, mock_defaults, mock_chroot, aarch64_platform, tmp_path
):
    """GRUB on aarch64 should use arm64-efi target and write defaults."""
    # Set up filesystem stubs for aarch64-specific steps
    (tmp_path / "boot").mkdir()
    (tmp_path / "boot" / "Image").touch()
    grub_header = tmp_path / "etc" / "grub.d"
    grub_header.mkdir(parents=True)
    (grub_header / "00_header").write_text("    insmod efi_uga\n    insmod efi_gop\n")

    with patch("arches_installer.core.bootloader.MOUNT_ROOT", tmp_path):
        _install_grub(aarch64_platform, "/dev/sda", "/dev/sda1", "/dev/sda2")

    # vmlinuz symlink created
    assert (tmp_path / "boot" / "vmlinuz-linux-aarch64").is_symlink()

    # pacman hook for vmlinuz persistence
    assert (
        tmp_path / "etc" / "pacman.d" / "hooks" / "90-vmlinuz-symlink.hook"
    ).exists()

    # efi_uga removed from 00_header
    header_content = (grub_header / "00_header").read_text()
    assert "efi_uga" not in header_content
    assert "efi_gop" in header_content

    mock_defaults.assert_called_once_with(aarch64_platform, None)

    grub_install_call = mock_chroot.call_args_list[0]
    cmd = grub_install_call[0][0]
    assert "grub-install" in cmd
    assert "--target" in cmd
    target_idx = cmd.index("--target") + 1
    assert cmd[target_idx] == "arm64-efi"

    # Verify --efi-directory is /boot/efi
    efi_idx = cmd.index("--efi-directory") + 1
    assert cmd[efi_idx] == "/boot/efi"

    grub_mkconfig_call = mock_chroot.call_args_list[1]
    cmd2 = grub_mkconfig_call[0][0]
    assert "grub-mkconfig" in cmd2
    assert "-o" in cmd2


@patch("arches_installer.core.bootloader.chroot_run")
@patch("arches_installer.core.bootloader.write_grub_defaults")
@patch("arches_installer.core.bootloader.detect_firmware", return_value="uefi")
def test_install_grub_x86_64_uefi(mock_fw, mock_defaults, mock_chroot, x86_64_platform):
    """GRUB on x86_64 should use x86_64-efi target."""
    _install_grub(x86_64_platform, "/dev/sda", "/dev/sda1", "/dev/sda2")

    grub_install_call = mock_chroot.call_args_list[0]
    cmd = grub_install_call[0][0]
    target_idx = cmd.index("--target") + 1
    assert cmd[target_idx] == "x86_64-efi"


@patch("arches_installer.core.bootloader.detect_firmware", return_value="bios")
def test_install_grub_bios_raises(mock_fw, aarch64_platform):
    """GRUB should raise RuntimeError when firmware is BIOS."""
    with pytest.raises(RuntimeError, match="requires UEFI firmware"):
        _install_grub(aarch64_platform, "/dev/sda", "/dev/sda1", "/dev/sda2")


# ─── _install_limine ─────────────────────────────────────


@patch("arches_installer.core.bootloader.chroot_run")
@patch("arches_installer.core.bootloader.write_limine_defaults")
@patch("arches_installer.core.bootloader.detect_firmware", return_value="uefi")
def test_install_limine_uefi_path(
    mock_fw, mock_defaults, mock_chroot, x86_64_platform, tmp_path
):
    """UEFI firmware should run limine-install and limine-mkinitcpio."""
    with patch("arches_installer.core.bootloader.MOUNT_ROOT", tmp_path):
        (tmp_path / "boot").mkdir(parents=True, exist_ok=True)
        _install_limine(x86_64_platform, "/dev/sda", "/dev/sda1", "/dev/sda2")

    mock_defaults.assert_called_once_with(x86_64_platform, "/dev/sda2", None)
    assert mock_chroot.call_args_list == [
        call(
            ["pacman", "-Sy", "--noconfirm", "--needed", "limine-mkinitcpio-hook"],
            log=None,
        ),
        call(["limine-install"], log=None),
        call(["limine-mkinitcpio"], log=None),
    ]


@patch("arches_installer.core.bootloader.chroot_run")
@patch("arches_installer.core.bootloader.write_limine_defaults")
@patch("arches_installer.core.bootloader.install_limine_bios")
@patch("arches_installer.core.bootloader.detect_firmware", return_value="bios")
def test_install_limine_bios_path(
    mock_fw, mock_bios, mock_defaults, mock_chroot, x86_64_platform, tmp_path
):
    """BIOS firmware with supports_bios=True should call install_limine_bios."""
    with patch("arches_installer.core.bootloader.MOUNT_ROOT", tmp_path):
        (tmp_path / "boot").mkdir(parents=True, exist_ok=True)
        _install_limine(x86_64_platform, "/dev/sda", "/dev/sda1", "/dev/sda2")

    mock_bios.assert_called_once_with("/dev/sda", None)
    assert mock_chroot.call_args_list == [
        call(
            ["pacman", "-Sy", "--noconfirm", "--needed", "limine-mkinitcpio-hook"],
            log=None,
        ),
        call(["limine-mkinitcpio"], log=None),
    ]


@patch("arches_installer.core.bootloader.chroot_run")
@patch("arches_installer.core.bootloader.write_limine_defaults")
@patch("arches_installer.core.bootloader.detect_firmware", return_value="bios")
def test_install_limine_bios_unsupported_raises(
    mock_fw, mock_defaults, mock_chroot, aarch64_platform, tmp_path
):
    """BIOS firmware with supports_bios=False should raise RuntimeError."""
    with patch("arches_installer.core.bootloader.MOUNT_ROOT", tmp_path):
        (tmp_path / "boot").mkdir(parents=True, exist_ok=True)
        with pytest.raises(RuntimeError, match="does not support BIOS boot"):
            _install_limine(aarch64_platform, "/dev/sda", "/dev/sda1", "/dev/sda2")
