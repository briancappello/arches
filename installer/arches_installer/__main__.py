"""Entry point for the Arches installer."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from arches_installer.core.platform import PlatformConfig
from arches_installer.core.template import InstallTemplate

AUTO_INSTALL_PATH = Path("/root/auto-install.toml")


def _print_dry_run_summary(
    *,
    title: str,
    platform: PlatformConfig,
    template: InstallTemplate,
    hostname: str,
    username: str,
    extra_lines: list[str] | None = None,
) -> None:
    """Print a dry-run configuration summary (shared by --auto and --host)."""
    print(f"== {title} ==")
    print(f"  Platform:    {platform.name} ({platform.arch})")
    print(f"  Kernel:      {platform.kernel.package}")
    print(f"  Bootloader:  {platform.bootloader.type}")
    if extra_lines:
        for line in extra_lines:
            print(line)
    print(f"  Template:    {template.name}")
    print(f"  Hostname:    {hostname}")
    print(f"  User:        {username}")


def main() -> int:
    """Parse args and launch either TUI or auto-install mode."""
    parser = argparse.ArgumentParser(
        prog="arches-install",
        description="Arches — custom installer",
    )
    parser.add_argument(
        "--auto",
        metavar="CONFIG",
        type=Path,
        help="Run unattended install from a TOML config file (no TUI)",
    )
    parser.add_argument(
        "--host",
        metavar="CONFIG",
        type=Path,
        help="Run host-install from a TOML config (install into btrfs subvolumes)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config and print plan without executing (--auto/--host only)",
    )
    parser.add_argument(
        "--platform",
        metavar="PATH",
        type=Path,
        help="Path to platform.toml (default: /opt/arches/platform/platform.toml)",
    )
    args = parser.parse_args()

    # Require root for all modes except --dry-run
    if not args.dry_run and os.geteuid() != 0:
        print(
            "ERROR: arches-install must be run as root.\n"
            "       Use: sudo arches-install",
            file=sys.stderr,
        )
        return 1

    # --host flag: host-install mode (install into subvolumes on existing system)
    if args.host:
        return _run_host(args.host, platform_path=args.platform, dry_run=args.dry_run)

    # Explicit --auto flag takes priority — fail hard on errors
    if args.auto:
        return _run_auto(args.auto, platform_path=args.platform, dry_run=args.dry_run)

    # Auto-detect config baked into the ISO at /root/auto-install.toml.
    # Runs inside the TUI progress screen (same code path as interactive).
    # On failure, falls through to the interactive TUI.
    if AUTO_INSTALL_PATH.exists() and not args.dry_run:
        rc = _run_auto(
            AUTO_INSTALL_PATH,
            platform_path=args.platform,
            dry_run=False,
            fallback_to_tui=True,
        )
        if rc == 0:
            return 0

    return _run_tui(platform_path=args.platform)


def _load_platform(platform_path: Path | None):
    """Load platform config from explicit path or ISO default."""
    from arches_installer.core.platform import load_platform, load_platform_from_iso

    if platform_path:
        return load_platform(platform_path)
    return load_platform_from_iso()


def _run_tui(*, platform_path: Path | None = None) -> int:
    """Launch the interactive Textual TUI."""
    from arches_installer.tui.app import ArchesApp

    platform = _load_platform(platform_path)
    app = ArchesApp(platform=platform)
    app.run()
    return 0


def _run_auto(
    config_path: Path,
    *,
    platform_path: Path | None = None,
    dry_run: bool = False,
    fallback_to_tui: bool = False,
) -> int:
    """Run unattended install from a TOML config file.

    The install runs inside the TUI progress screen — same code path
    as interactive install. When *fallback_to_tui* is True (ISO auto-detect),
    errors return non-zero so the caller can fall through to interactive mode.
    """
    from arches_installer.core.auto import AutoInstallConfig
    from arches_installer.core.disk import detect_single_disk

    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        return 1

    try:
        platform = _load_platform(platform_path)
    except Exception as e:
        print(f"ERROR: Failed to load platform config: {e}", file=sys.stderr)
        return 1

    try:
        config = AutoInstallConfig.from_file(config_path)
    except Exception as e:
        _auto_install_error(f"Invalid config: {e}", fallback_to_tui)
        return 1

    if dry_run:
        dl = config.disk_layout
        extra = [
            f"  Disk layout: {dl.name}",
            f"  Snapshots:   {platform.bootloader.snapshot_boot}",
            "  Device:      (auto-detect at install time)",
        ]
        _print_dry_run_summary(
            title="Arches Auto Install (dry run)",
            platform=platform,
            template=config.template,
            hostname=config.hostname,
            username=config.username,
            extra_lines=extra,
        )
        print(f"  Reboot:      {config.reboot}")
        print(f"  Shutdown:    {config.shutdown}")
        print(f"  Packages:    {len(config.template.install.all_packages)}")
        print(f"  Services:    {len(config.template.services)}")
        # Show partition summary from disk layout
        for i, part in enumerate(dl.partitions):
            fs = part.filesystem or "raw"
            mp = part.mount_point or "(none)"
            print(f"  Partition {i + 1}: {fs}  {part.size}  -> {mp}")
            for sv in part.subvolumes:
                print(f"    Subvol: {sv.name} -> {sv.mount_point or '(none)'}")
        if config.template.ansible.firstboot_roles:
            print(
                f"  Ansible (1st boot):  {', '.join(config.template.ansible.firstboot_roles)}"
            )
        if platform.hardware_detection.enabled:
            print(f"  HW detect:   {platform.hardware_detection.tool}")
        if config.wifi:
            print(f"  WiFi:        {config.wifi.ssid}")
        if config.wired:
            print(f"  Static IP:   {config.wired.interface} → {config.wired.static_ip}")
        print("")
        print("Dry run complete. No changes made.")
        return 0

    # Connect to network if configured (before disk detection / install)
    if config.wifi or config.wired:
        from arches_installer.core.network import (
            StaticIPConfig,
            connect_ethernet_static,
            connect_wifi,
        )

        if config.wifi:
            print(f"Connecting to WiFi: {config.wifi.ssid}...")
            static = None
            if config.wifi.static_ip and config.wifi.gateway:
                static = StaticIPConfig(
                    ip_cidr=config.wifi.static_ip,
                    gateway=config.wifi.gateway,
                    dns=config.wifi.dns,
                )
            ok, err = connect_wifi(config.wifi.ssid, config.wifi.psk, static)
            if not ok:
                _auto_install_error(f"WiFi connection failed: {err}", fallback_to_tui)
                return 1
            print("  WiFi connected.")

        if config.wired:
            print(f"Configuring static IP on {config.wired.interface}...")
            static = StaticIPConfig(
                ip_cidr=config.wired.static_ip,
                gateway=config.wired.gateway,
                dns=config.wired.dns,
            )
            ok, err = connect_ethernet_static(config.wired.interface, static)
            if not ok:
                _auto_install_error(f"Wired connection failed: {err}", fallback_to_tui)
                return 1
            print("  Network configured.")

    # Detect target disk
    try:
        disk = detect_single_disk()
    except Exception as e:
        _auto_install_error(f"Disk detection failed: {e}", fallback_to_tui)
        return 1

    # Launch TUI with auto-install state pre-populated.
    # The app skips straight to the progress screen.
    from arches_installer.tui.app import ArchesApp

    app = ArchesApp(platform=platform)
    app.selected_device = disk.path
    app.selected_template = config.template
    app.selected_layout = config.disk_layout
    app.partition_mode = "auto"
    app.hostname = config.hostname
    app.username = config.username
    app.password = config.password
    app.auto_install = True
    app.auto_shutdown = config.shutdown
    app.auto_reboot = config.reboot

    # When running under the test harness (virtio log port exists),
    # force shutdown on completion so the test script can detect success.
    if Path("/dev/virtio-ports/arches-log").exists():
        app.auto_shutdown = True
        app.auto_reboot = False

    app.run()

    # Check if install succeeded (set by progress screen)
    if getattr(app, "install_success", False):
        return 0

    if fallback_to_tui:
        print("\nFalling back to manual install...\n", file=sys.stderr)
    return 1


def _run_host(
    config_path: Path,
    *,
    platform_path: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Run host-install from a TOML config file."""
    from arches_installer.core.host_install import HostInstallConfig, run_host_install

    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        return 1

    try:
        platform = _load_platform(platform_path)
    except Exception as e:
        print(f"ERROR: Failed to load platform config: {e}", file=sys.stderr)
        return 1

    try:
        config = HostInstallConfig.from_file(config_path)
    except Exception as e:
        print(f"ERROR: Invalid config: {e}", file=sys.stderr)
        return 1

    if dry_run:
        extra = [
            f"  Partition:   {config.partition}",
            f"  ESP:         {config.esp_partition}",
            f"  Mode:        {config.mode}",
        ]
        _print_dry_run_summary(
            title="Arches Host Install (dry run)",
            platform=platform,
            template=config.template,
            hostname=config.hostname,
            username=config.username,
            extra_lines=extra,
        )
        if config.mode == "alongside":
            print(
                f"  Subvolumes:  {config.subvol_prefix}, {config.subvol_prefix}-home, {config.subvol_prefix}-var"
            )
        else:
            print("  Subvolumes:  @, @home, @var (replace existing)")
        print(f"  GRUB entry:  {'yes (host)' if config.add_grub_entry else 'no'}")
        print(
            f"  Bootloader:  {'install in chroot' if config.install_bootloader else 'skip (host GRUB)'}"
        )
        print(f"  Packages:    {len(config.template.install.all_packages)}")
        print(f"  Services:    {len(config.template.services)}")
        if config.template.ansible.firstboot_roles:
            print(
                f"  Ansible:     {', '.join(config.template.ansible.firstboot_roles)}"
            )
        print("")
        print("Dry run complete. No changes made.")
        return 0

    return run_host_install(platform, config)


def _auto_install_error(msg: str, fallback_to_tui: bool) -> None:
    """Print an auto-install error, with fallback context if applicable."""
    print(f"ERROR: {msg}", file=sys.stderr)
    if fallback_to_tui:
        print("Falling back to manual install...\n", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
