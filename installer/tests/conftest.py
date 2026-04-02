"""Shared fixtures for Arches installer tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

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


@pytest.fixture
def x86_64_platform() -> PlatformConfig:
    """An x86-64 CachyOS platform config for testing."""
    return PlatformConfig(
        name="x86-64",
        description="x86-64 with CachyOS x86-64-v3",
        arch="x86_64",
        kernel=KernelConfig(
            variants=[
                KernelVariant(package="linux-cachyos", headers="linux-cachyos-headers"),
                KernelVariant(
                    package="linux-cachyos-lts", headers="linux-cachyos-lts-headers"
                ),
            ],
        ),
        bootloader=BootloaderPlatformConfig(
            type="limine",
            efi_binary="BOOTX64.EFI",
            efi_fallback_path="EFI/BOOT/BOOTX64.EFI",
            supports_bios=True,
            snapshot_boot=True,
        ),
        hardware_detection=HardwareDetectionConfig(
            enabled=True,
            tool="chwd",
            args=["-a"],
            optional=True,
        ),
        base_packages=[
            "base",
            "linux-firmware",
            "mkinitcpio",
            "sudo",
            "ansible",
            "terminus-font",
            "cachyos-keyring",
            "cachyos-mirrorlist",
            "cachyos-v3-mirrorlist",
            "cachyos-settings",
            "limine",
            "efibootmgr",
            "chwd",
        ],
        swap="zram",
        cachyos_optimization_tier="x86-64-v3",
        kernel_flags=[
            "console=ttyS0,115200",
            "console=tty0",
            "loglevel=5",
            "video=1920x1080",
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
            variants=[
                KernelVariant(package="linux-aarch64", headers="linux-aarch64-headers"),
            ],
        ),
        bootloader=BootloaderPlatformConfig(
            type="grub",
            efi_binary="BOOTAA64.EFI",
            efi_fallback_path="EFI/BOOT/BOOTAA64.EFI",
            supports_bios=False,
            snapshot_boot=True,
        ),
        hardware_detection=HardwareDetectionConfig(enabled=False),
        base_packages=[
            "base",
            "linux-firmware",
            "mkinitcpio",
            "sudo",
            "ansible",
            "terminus-font",
            "archlinuxarm-keyring",
            "grub",
            "efibootmgr",
            "grub-btrfs",
            "btrfs-progs",
        ],
        swap="zram",
        kernel_flags=[
            "console=ttyAMA0,115200",
            "console=tty0",
            "loglevel=5",
            "video=1920x1080",
        ],
    )


@pytest.fixture
def aarch64_apple_platform() -> PlatformConfig:
    """An aarch64-apple Apple Silicon platform config for testing."""
    return PlatformConfig(
        name="aarch64-apple",
        description="Apple Silicon (M1/M2/M3/M4) with Asahi Linux kernel",
        arch="aarch64",
        kernel=KernelConfig(
            variants=[
                KernelVariant(package="linux-asahi", headers="linux-asahi-headers"),
            ],
        ),
        bootloader=BootloaderPlatformConfig(
            type="grub",
            efi_binary="BOOTAA64.EFI",
            efi_fallback_path="EFI/BOOT/BOOTAA64.EFI",
            supports_bios=False,
            snapshot_boot=False,
        ),
        hardware_detection=HardwareDetectionConfig(enabled=False),
        base_packages=[
            "base",
            "linux-firmware",
            "mkinitcpio",
            "sudo",
            "ansible",
            "terminus-font",
            "asahi-alarm-keyring",
            "m1n1",
            "uboot-asahi",
            "grub",
            "efibootmgr",
            "btrfs-progs",
            "asahi-fwextract",
            "asahi-scripts",
        ],
        swap="zram",
        kernel_flags=[
            "console=ttyAMA0,115200",
            "console=tty0",
            "loglevel=5",
            "video=1920x1080",
        ],
    )


@pytest.fixture
def basic_disk_layout() -> DiskLayout:
    """A basic disk layout for testing."""
    return DiskLayout(
        name="Basic",
        description="2 GiB ESP, btrfs root filling the rest",
        bootloaders=["limine", "grub"],
        partitions=[
            PartitionSpec(
                size="2G",
                filesystem="vfat",
                mount_point="/boot",
                label="ESP",
            ),
            PartitionSpec(
                size="*",
                filesystem="btrfs",
                mount_point="/",
                label="archroot",
                mount_options="compress=zstd:1,noatime,ssd,discard=async",
                subvolumes=[
                    SubvolumeSpec(name="@", mount_point="/"),
                    SubvolumeSpec(name="@home", mount_point="/home"),
                    SubvolumeSpec(name="@var", mount_point="/var"),
                ],
            ),
        ],
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
        ),
        install=InstallPhases(
            pacstrap=["git", "neovim", "plasma-meta"],
        ),
        services=["NetworkManager", "sddm"],
        ansible=AnsibleConfig(
            firstboot_roles=["base", "zsh", "kde"],
        ),
    )


@pytest.fixture
def vm_server_template() -> InstallTemplate:
    """A VM-server-style template."""
    return InstallTemplate(
        name="VM Server",
        description="Headless server -- ext4",
        system=SystemConfig(
            timezone="America/New_York",
            locale="en_US.UTF-8",
        ),
        install=InstallPhases(
            pacstrap=["openssh", "nginx"],
        ),
        services=["NetworkManager", "sshd"],
        ansible=AnsibleConfig(
            firstboot_roles=["base", "zsh", "vm-server"],
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
description = "x86-64 with CachyOS x86-64-v3"
arch = "x86_64"
cachyos_optimization_tier = "x86-64-v3"
swap = "zram"

[kernel]
variants = [
    { package = "linux-cachyos", headers = "linux-cachyos-headers" },
    { package = "linux-cachyos-lts", headers = "linux-cachyos-lts-headers" },
]
flags = ["console=ttyS0,115200", "console=tty0", "loglevel=5", "video=1920x1080"]

[bootloader]
type = "limine"
efi_binary = "BOOTX64.EFI"
efi_fallback_path = "EFI/BOOT/BOOTX64.EFI"
supports_bios = true
snapshot_boot = true

[hardware_detection]
enabled = true
tool = "chwd"
args = ["-a"]
optional = true

[base_packages]
install = [
    "base",
    "linux-firmware",
    "mkinitcpio",
    "sudo",
    "ansible",
    "terminus-font",
    "cachyos-keyring",
    "cachyos-mirrorlist",
    "cachyos-v3-mirrorlist",
    "cachyos-settings",
]
""")
    return p


@pytest.fixture
def templates_dir(tmp_path: Path) -> Path:
    """Create a temp directory with sample template TOML files.

    Also patches ``resolve_template`` so that bare filenames (e.g.
    ``"dev-workstation.toml"``) resolve against this temp directory
    instead of the installed package's templates directory.
    """
    d = tmp_path / "templates"
    d.mkdir()

    (d / "dev-workstation.toml").write_text("""\
[meta]
name = "Dev Workstation"
description = "KDE Plasma desktop with full development toolchain"
graphical = true

[system]
timezone = "America/New_York"
locale = "en_US.UTF-8"

[install.pacstrap]
packages = ["git", "neovim"]

[services]
enable = ["NetworkManager", "sddm"]

[ansible]
firstboot_roles = ["base", "zsh", "kde"]
""")

    (d / "vm-server.toml").write_text("""\
[meta]
name = "VM Server"
description = "Headless server"

[install.pacstrap]
packages = ["openssh", "nginx"]

[services]
enable = ["NetworkManager", "sshd"]

[ansible]
firstboot_roles = ["base", "zsh", "vm-server"]
""")

    _resolve = lambda name: d / name  # noqa: E731
    with (
        patch("arches_installer.core.template.resolve_template", side_effect=_resolve),
        patch("arches_installer.core.auto.resolve_template", side_effect=_resolve),
    ):
        yield d


@pytest.fixture
def disk_layouts_dir(tmp_path: Path) -> Path:
    """Create a temp directory with sample disk layout TOML files.

    Also patches ``resolve_disk_layout`` so that bare filenames resolve
    against this temp directory.
    """
    d = tmp_path / "disk-layouts"
    d.mkdir()

    (d / "basic.toml").write_text("""\
[meta]
name = "Basic"
description = "2 GiB ESP, btrfs root filling the rest"
bootloaders = ["limine", "grub"]

[[partitions]]
filesystem = "vfat"
size = "2G"
mount_point = "/boot"
label = "ESP"

[[partitions]]
filesystem = "btrfs"
size = "*"
mount_point = "/"
label = "archroot"
mount_options = "compress=zstd:1,noatime,ssd,discard=async"

[[partitions.subvolumes]]
name = "@"
mount_point = "/"

[[partitions.subvolumes]]
name = "@home"
mount_point = "/home"

[[partitions.subvolumes]]
name = "@var"
mount_point = "/var"
""")

    _resolve = lambda name: d / name  # noqa: E731
    with patch("arches_installer.core.auto.resolve_disk_layout", side_effect=_resolve):
        yield d


@pytest.fixture
def auto_config_file(
    tmp_path: Path, templates_dir: Path, disk_layouts_dir: Path
) -> Path:
    """Create a valid auto-install TOML config file.

    Depends on ``templates_dir`` and ``disk_layouts_dir`` so that resolve
    functions are patched to find the test files by bare filename.
    """
    config = tmp_path / "auto.toml"
    config.write_text("""\
[install]
template = "dev-workstation.toml"
disk_layout = "basic.toml"
hostname = "testbox"
username = "testuser"
password = "testpass"
reboot = true
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


@pytest.fixture
def mock_discover_disk_layouts(basic_disk_layout):
    """Mock disk layout discovery to return test layouts."""
    with patch(
        "arches_installer.core.disk_layout.discover_disk_layouts",
        return_value=[basic_disk_layout],
    ) as m:
        yield m
