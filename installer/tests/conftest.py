"""Shared fixtures for Arches installer tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from arches_installer.core.platform import (
    BootloaderPlatformConfig,
    DiskLayoutConfig,
    HardwareDetectionConfig,
    KernelConfig,
    PlatformConfig,
)
from arches_installer.core.template import (
    AnsibleConfig,
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
            snapshot_boot=True,
        ),
        disk_layout=DiskLayoutConfig(
            filesystem="btrfs",
            mount_options="compress=zstd:1,noatime,ssd,discard=async",
            subvolumes=["@", "@home", "@var", "@snapshots"],
            esp_size_mib=2048,
            swap="zram",
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
def aarch64_platform() -> PlatformConfig:
    """An aarch64-generic ARM platform config for testing."""
    return PlatformConfig(
        name="aarch64-generic",
        description="Generic ARM64 with Arch Linux ARM",
        arch="aarch64",
        kernel=KernelConfig(
            package="linux-aarch64",
            headers="linux-aarch64-headers",
        ),
        bootloader=BootloaderPlatformConfig(
            type="grub",
            efi_binary="BOOTAA64.EFI",
            efi_fallback_path="EFI/BOOT/BOOTAA64.EFI",
            supports_bios=False,
        ),
        disk_layout=DiskLayoutConfig(
            filesystem="ext4",
            mount_options="noatime",
            esp_size_mib=512,
            boot_size_mib=1024,
            home_partition=True,
            swap="zram",
        ),
        hardware_detection=HardwareDetectionConfig(enabled=False),
        base_packages=[],
    )


@pytest.fixture
def dev_workstation_template() -> InstallTemplate:
    """A dev-workstation-style template."""
    return InstallTemplate(
        name="Dev Workstation",
        description="KDE Plasma desktop with full development toolchain",
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
def vm_server_template() -> InstallTemplate:
    """A VM-server-style template."""
    return InstallTemplate(
        name="VM Server",
        description="Headless server — ext4",
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


# Keep old fixture names as aliases for backward compat in tests
@pytest.fixture
def btrfs_template(dev_workstation_template) -> InstallTemplate:
    return dev_workstation_template


@pytest.fixture
def ext4_template(vm_server_template) -> InstallTemplate:
    return vm_server_template


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
snapshot_boot = true

[disk_layout]
filesystem = "btrfs"
mount_options = "compress=zstd:1,noatime,ssd,discard=async"
subvolumes = ["@", "@home", "@var", "@snapshots"]
esp_size_mib = 2048
swap = "zram"

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
description = "KDE Plasma desktop with full development toolchain"

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
def mock_discover_templates(dev_workstation_template, vm_server_template):
    """Mock template discovery to return test templates."""
    with patch(
        "arches_installer.core.template.discover_templates",
        return_value=[dev_workstation_template, vm_server_template],
    ) as m:
        yield m
