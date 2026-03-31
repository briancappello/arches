#!/usr/bin/env python3
"""Demo the TUI screens with fake data, no root or disk required."""

import argparse
import sys
import time

sys.path.insert(0, "installer")

from arches_installer.core.platform import (
    BootloaderPlatformConfig,
    DiskLayoutConfig,
    HardwareDetectionConfig,
    KernelConfig,
    KernelVariant,
    PlatformConfig,
)
from arches_installer.core.template import InstallTemplate
from arches_installer.tui.app import ArchesApp
from arches_installer.tui.progress import InstallProgressScreen


def make_platform():
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
        base_packages=["base", "linux-firmware"],
        cachyos_optimization_tier="x86-64-v3",
        kernel_flags=[
            "console=ttyS0,115200",
            "console=tty0",
            "loglevel=5",
            "video=1920x1080",
        ],
    )


def make_template():
    return InstallTemplate.from_dict(
        {
            "meta": {
                "name": "Dev Workstation",
                "description": "KDE Plasma desktop",
                "graphical": True,
            },
            "system": {"timezone": "America/Denver", "locale": "en_US.UTF-8"},
            "install": {
                "pacstrap": {"packages": ["git", "neovim", "firefox", "plasma-meta"]}
            },
            "services": {"enable": ["NetworkManager", "sddm"]},
            "ansible": {"firstboot_roles": ["base", "zsh", "kde"]},
        }
    )


def fake_progress_install(screen):
    """Simulate an install with log output."""
    log = screen.log_msg
    time.sleep(0.5)

    log("[bold cyan]── Phase 1: Disk Setup ──[/bold cyan]")
    log("Partitioning /dev/vda...")
    time.sleep(0.3)
    log("[green]Disk prepared successfully.[/green]")

    log("[bold cyan]── Phase 2: System Install ──[/bold cyan]")
    log("Running pacstrap...")
    log("Total packages: 882")
    time.sleep(0.3)

    for i in range(1, 51):
        log(f"  Installing package {i}/882...")
        time.sleep(0.05)

    log("[green]System installed successfully.[/green]")

    log("[bold cyan]── Phase 3: Bootloader ──[/bold cyan]")
    log("Installing Limine EFI...")
    time.sleep(0.3)
    log("[green]Bootloader installed.[/green]")

    log("[bold cyan]── Phase 4: Snapshots ──[/bold cyan]")
    log("Configuring snapper...")
    time.sleep(0.3)
    log("[green]Snapshot support configured.[/green]")

    log("[bold cyan]── Phase 5: First-Boot ──[/bold cyan]")
    log("Injecting first-boot service...")
    time.sleep(0.3)
    log("First-boot service installed.")

    log("")
    log("[bold green]== Installation complete ==[/bold green]")
    log("Remove the installation media and reboot.")

    screen.app.call_from_thread(screen._enable_reboot)


SCREENS = {
    "welcome": "Welcome screen (default start)",
    "partition": "Disk/partition selection",
    "template": "Template selection",
    "user": "User setup (hostname, username, password)",
    "confirm": "Pre-install confirmation summary",
    "progress": "Install progress with simulated output",
}


def main():
    parser = argparse.ArgumentParser(
        prog="tui-demo",
        description="Demo the Arches TUI screens without root or disk access.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Available screens:\n"
        + "\n".join(f"  {name:12s}  {desc}" for name, desc in SCREENS.items()),
    )
    parser.add_argument(
        "screen",
        nargs="?",
        default="welcome",
        choices=SCREENS.keys(),
        help="Screen to display (default: welcome)",
    )
    args = parser.parse_args()

    screen = args.screen
    platform = make_platform()
    template = make_template()

    if screen == "progress":
        # Monkey-patch to use fake install
        InstallProgressScreen._run_install = fake_progress_install
        app = ArchesApp(platform=platform)
        app.auto_install = True
        app.selected_device = "/dev/vda"
        app.selected_template = template
        app.hostname = "arches"
        app.username = "arches"
        app.password = "password"
        app.partition_mode = "auto"
        app.run()

    elif screen == "confirm":
        app = ArchesApp(platform=platform)
        app.selected_device = "/dev/vda"
        app.selected_template = template
        app.hostname = "arches"
        app.username = "arches"
        app.password = "password"
        app.partition_mode = "auto"
        app.push_screen_on_mount = "confirm"
        app.run()

    elif screen == "user":
        app = ArchesApp(platform=platform)
        app.selected_device = "/dev/vda"
        app.selected_template = template
        app.partition_mode = "auto"
        app.push_screen_on_mount = "user_setup"
        app.run()

    elif screen == "template":
        app = ArchesApp(platform=platform)
        app.selected_device = "/dev/vda"
        app.partition_mode = "auto"
        app.push_screen_on_mount = "template_select"
        app.run()

    elif screen == "partition":
        app = ArchesApp(platform=platform)
        app.push_screen_on_mount = "partition"
        app.run()

    else:  # welcome
        app = ArchesApp(platform=platform)
        app.run()


if __name__ == "__main__":
    main()
