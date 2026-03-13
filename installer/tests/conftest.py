"""Shared fixtures for Arches installer tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from arches_installer.core.platform import (
    BootloaderPlatformConfig,
    HardwareDetectionConfig,
    KernelConfig,
    PlatformConfig,
)
from arches_installer.core.template import (
    AnsibleConfig,
    BootloaderConfig,
    DiskConfig,
    InstallTemplate,
    SystemConfig,
)


@pytest.fixture
def x86_64_platform() -> PlatformConfig:
    """An x86-64 CachyOS platform config for testing."""
    return PlatformConfig(
        name="x86-64",
        description="x86-64 with CachyOS v3",
        arch="x86_64",
        kernel=KernelConfig(
            package="linux-cachyos",
            headers="linux-cachyos-headers",
        ),
        bootloader=BootloaderPlatformConfig(
            type="limine",
            efi_binary="BOOTX64.EFI",
            efi_fallback_path="EFI/BOOT/BOOTX64.EFI",
            supports_bios=True,
        ),
        hardware_detection=HardwareDetectionConfig(
            enabled=True,
            tool="chwd",
            args=["-a"],
            optional=True,
        ),
        base_packages=[
            "cachyos-keyring",
            "cachyos-mirrorlist",
            "cachyos-v3-mirrorlist",
            "cachyos-settings",
        ],
    )


@pytest.fixture
def btrfs_template() -> InstallTemplate:
    """A dev-workstation-style template with btrfs + snapshots."""
    return InstallTemplate(
        name="Dev Workstation",
        description="KDE + btrfs + snapshots",
        disk=DiskConfig(
            filesystem="btrfs",
            mount_options="compress=zstd:1,noatime,ssd,discard=async",
            subvolumes=["@", "@home", "@var", "@snapshots"],
            esp_size_mib=2048,
            swap="zram",
        ),
        bootloader=BootloaderConfig(type="limine", snapshot_boot=True),
        system=SystemConfig(
            timezone="America/New_York",
            locale="en_US.UTF-8",
            packages=["git", "neovim", "plasma-meta"],
        ),
        services=["NetworkManager", "sddm"],
        ansible=AnsibleConfig(
            chroot_roles=["base", "kde"],
            firstboot_roles=["dotfiles"],
        ),
    )


@pytest.fixture
def ext4_template() -> InstallTemplate:
    """A VM-server-style template with ext4, no snapshots."""
    return InstallTemplate(
        name="VM Server",
        description="Headless server — ext4",
        disk=DiskConfig(
            filesystem="ext4",
            mount_options="noatime",
            subvolumes=[],
            esp_size_mib=512,
            swap="zram",
        ),
        bootloader=BootloaderConfig(type="limine", snapshot_boot=False),
        system=SystemConfig(
            timezone="America/New_York",
            locale="en_US.UTF-8",
            packages=["openssh", "nginx"],
        ),
        services=["NetworkManager", "sshd"],
        ansible=AnsibleConfig(
            chroot_roles=["base", "vm-server"],
            firstboot_roles=[],
        ),
    )


@pytest.fixture
def platform_toml_file(tmp_path: Path) -> Path:
    """Create a temporary platform TOML file for testing."""
    p = tmp_path / "platform.toml"
    p.write_text("""\
[platform]
name = "x86-64"
description = "x86-64 with CachyOS v3"
arch = "x86_64"

[kernel]
package = "linux-cachyos"
headers = "linux-cachyos-headers"

[bootloader]
type = "limine"
efi_binary = "BOOTX64.EFI"
efi_fallback_path = "EFI/BOOT/BOOTX64.EFI"
supports_bios = true

[hardware_detection]
enabled = true
tool = "chwd"
args = ["-a"]
optional = true

[base_packages]
install = [
    "cachyos-keyring",
    "cachyos-mirrorlist",
    "cachyos-v3-mirrorlist",
    "cachyos-settings",
]
""")
    return p


@pytest.fixture
def templates_dir(tmp_path: Path) -> Path:
    """Create a temp directory with sample template TOML files."""
    d = tmp_path / "templates"
    d.mkdir()

    (d / "dev-workstation.toml").write_text("""\
[meta]
name = "Dev Workstation"
description = "KDE + btrfs + snapshots"

[disk]
filesystem = "btrfs"
subvolumes = ["@", "@home", "@var", "@snapshots"]
mount_options = "compress=zstd:1,noatime"
esp_size_mib = 2048
swap = "zram"

[bootloader]
type = "limine"
snapshot_boot = true

[system]
timezone = "America/New_York"
locale = "en_US.UTF-8"
packages = ["git", "neovim"]

[services]
enable = ["NetworkManager", "sddm"]

[ansible]
chroot_roles = ["base", "kde"]
firstboot_roles = ["dotfiles"]
""")

    (d / "vm-server.toml").write_text("""\
[meta]
name = "VM Server"
description = "Headless server"

[disk]
filesystem = "ext4"
mount_options = "noatime"
esp_size_mib = 512
swap = "zram"

[bootloader]
type = "limine"
snapshot_boot = false

[system]
packages = ["openssh", "nginx"]

[services]
enable = ["NetworkManager", "sshd"]

[ansible]
chroot_roles = ["base", "vm-server"]
firstboot_roles = []
""")

    return d


@pytest.fixture
def auto_config_file(tmp_path: Path, templates_dir: Path) -> Path:
    """Create a valid auto-install TOML config file."""
    template_path = templates_dir / "dev-workstation.toml"
    config = tmp_path / "auto.toml"
    config.write_text(f"""\
[install]
device = "/dev/vda"
template = "{template_path}"
hostname = "testbox"
username = "testuser"
password = "testpass"
""")
    return config


@pytest.fixture
def mock_detect_block_devices():
    """Mock disk detection to return fake devices."""
    from arches_installer.core.disk import BlockDevice

    fake_devices = [
        BlockDevice(
            name="vda",
            path="/dev/vda",
            size="20G",
            model="QEMU HARDDISK",
            removable=False,
            partitions=[],
        ),
        BlockDevice(
            name="sda",
            path="/dev/sda",
            size="500G",
            model="Samsung SSD 970",
            removable=False,
            partitions=["sda1", "sda2"],
        ),
        BlockDevice(
            name="sdb",
            path="/dev/sdb",
            size="32G",
            model="USB Flash Drive",
            removable=True,
            partitions=["sdb1"],
        ),
    ]
    with patch(
        "arches_installer.core.disk.detect_block_devices",
        return_value=fake_devices,
    ) as m:
        yield m


@pytest.fixture
def mock_discover_templates(btrfs_template, ext4_template):
    """Mock template discovery to return test templates."""
    with patch(
        "arches_installer.core.template.discover_templates",
        return_value=[btrfs_template, ext4_template],
    ) as m:
        yield m
