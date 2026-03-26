"""Tests for host-install configuration and GRUB entry generation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from arches_installer.core.host_install import (
    HostInstallConfig,
    _device_from_partition,
    generate_grub_entry,
)


# ---------------------------------------------------------------------------
# HostInstallConfig.from_dict
# ---------------------------------------------------------------------------


def test_host_config_from_dict_alongside(templates_dir):
    """Alongside mode config parses correctly with defaults."""
    data = {
        "install": {
            "template": "dev-workstation.toml",
            "hostname": "testhost",
            "username": "testuser",
            "password": "testpass",
            "partition": "/dev/nvme0n1p6",
            "esp_partition": "/dev/nvme0n1p4",
            "mode": "alongside",
        }
    }
    config = HostInstallConfig.from_dict(data)
    assert config.hostname == "testhost"
    assert config.partition == "/dev/nvme0n1p6"
    assert config.esp_partition == "/dev/nvme0n1p4"
    assert config.mode == "alongside"
    assert config.subvol_prefix == "@arches"
    assert config.add_grub_entry is True
    assert config.install_bootloader is False


def test_host_config_from_dict_replace(templates_dir):
    """Replace mode config parses correctly with defaults."""
    data = {
        "install": {
            "template": "dev-workstation.toml",
            "hostname": "testhost",
            "username": "testuser",
            "password": "testpass",
            "partition": "/dev/nvme0n1p6",
            "esp_partition": "/dev/nvme0n1p4",
            "mode": "replace",
        }
    }
    config = HostInstallConfig.from_dict(data)
    assert config.mode == "replace"
    assert config.add_grub_entry is False
    assert config.install_bootloader is True


def test_host_config_from_dict_custom_prefix(templates_dir):
    """Custom subvolume prefix is respected."""
    data = {
        "install": {
            "template": "dev-workstation.toml",
            "hostname": "testhost",
            "username": "testuser",
            "password": "testpass",
            "partition": "/dev/nvme0n1p6",
            "esp_partition": "/dev/nvme0n1p4",
            "mode": "alongside",
            "subvol_prefix": "@myarch",
        }
    }
    config = HostInstallConfig.from_dict(data)
    assert config.subvol_prefix == "@myarch"


def test_host_config_missing_partition(templates_dir):
    """Missing partition raises ValueError."""
    data = {
        "install": {
            "template": "dev-workstation.toml",
            "hostname": "testhost",
            "username": "testuser",
            "password": "testpass",
            "esp_partition": "/dev/nvme0n1p4",
        }
    }
    with pytest.raises(ValueError, match="install.partition is required"):
        HostInstallConfig.from_dict(data)


def test_host_config_missing_esp(templates_dir):
    """Missing ESP partition raises ValueError."""
    data = {
        "install": {
            "template": "dev-workstation.toml",
            "hostname": "testhost",
            "username": "testuser",
            "password": "testpass",
            "partition": "/dev/nvme0n1p6",
        }
    }
    with pytest.raises(ValueError, match="install.esp_partition is required"):
        HostInstallConfig.from_dict(data)


def test_host_config_invalid_mode(templates_dir):
    """Invalid mode raises ValueError."""
    data = {
        "install": {
            "template": "dev-workstation.toml",
            "hostname": "testhost",
            "username": "testuser",
            "password": "testpass",
            "partition": "/dev/nvme0n1p6",
            "esp_partition": "/dev/nvme0n1p4",
            "mode": "invalid",
        }
    }
    with pytest.raises(ValueError, match="must be 'alongside' or 'replace'"):
        HostInstallConfig.from_dict(data)


def test_host_config_from_file(templates_dir, tmp_path):
    """Config loads from a TOML file."""
    config_file = tmp_path / "host.toml"
    config_file.write_text("""\
[install]
template = "dev-workstation.toml"
hostname = "archbox"
username = "brian"
password = "secret"
partition = "/dev/nvme0n1p6"
esp_partition = "/dev/nvme0n1p4"
mode = "alongside"
""")
    config = HostInstallConfig.from_file(config_file)
    assert config.hostname == "archbox"
    assert config.username == "brian"


# ---------------------------------------------------------------------------
# _device_from_partition
# ---------------------------------------------------------------------------


def test_device_from_nvme_partition():
    assert _device_from_partition("/dev/nvme0n1p6") == "/dev/nvme0n1"
    assert _device_from_partition("/dev/nvme0n1p1") == "/dev/nvme0n1"


def test_device_from_sata_partition():
    assert _device_from_partition("/dev/sda2") == "/dev/sda"
    assert _device_from_partition("/dev/sdb1") == "/dev/sdb"


def test_device_from_mmc_partition():
    assert _device_from_partition("/dev/mmcblk0p1") == "/dev/mmcblk0"


def test_device_from_whole_disk():
    """Whole disk path is returned unchanged."""
    assert _device_from_partition("/dev/sda") == "/dev/sda"


# ---------------------------------------------------------------------------
# generate_grub_entry
# ---------------------------------------------------------------------------


@patch("arches_installer.core.bootloader.subprocess.run")
def test_generate_grub_entry(mock_subprocess, aarch64_apple_platform):
    """GRUB entry contains correct kernel, UUID, and subvolume."""
    mock_subprocess.return_value = MagicMock(stdout="abcd-1234\n")
    entry = generate_grub_entry(aarch64_apple_platform, "/dev/nvme0n1p6", "@arches")

    assert 'menuentry "Arches Linux"' in entry
    assert "search --no-floppy --fs-uuid --set=root abcd-1234" in entry
    assert "vmlinuz-linux-asahi" in entry
    assert "root=UUID=abcd-1234" in entry
    assert "rootflags=subvol=@arches" in entry
    assert "initramfs-linux-asahi.img" in entry
    assert "console=tty0" in entry  # aarch64 specific


@patch("arches_installer.core.bootloader.subprocess.run")
def test_generate_grub_entry_custom_prefix(mock_subprocess, aarch64_apple_platform):
    """Custom subvolume prefix is used in the GRUB entry."""
    mock_subprocess.return_value = MagicMock(stdout="beef-5678\n")
    entry = generate_grub_entry(aarch64_apple_platform, "/dev/nvme0n1p6", "@mylinux")

    assert "rootflags=subvol=@mylinux" in entry
    assert "/@mylinux/boot/vmlinuz-linux-asahi" in entry
    assert "/@mylinux/boot/initramfs-linux-asahi.img" in entry
