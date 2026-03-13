"""Entry point for the Arches installer."""

import argparse
import sys
from pathlib import Path


def main() -> int:
    """Parse args and launch either TUI or auto-install mode."""
    parser = argparse.ArgumentParser(
        prog="arches-install",
        description="Arches — custom Arch/CachyOS installer",
    )
    parser.add_argument(
        "--auto",
        metavar="CONFIG",
        type=Path,
        help="Run unattended install from a TOML config file (no TUI)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config and print plan without executing (--auto only)",
    )
    parser.add_argument(
        "--platform",
        metavar="PATH",
        type=Path,
        help="Path to platform.toml (default: /opt/arches/platform/platform.toml)",
    )
    args = parser.parse_args()

    if args.auto:
        return _run_auto(args.auto, platform_path=args.platform, dry_run=args.dry_run)
    else:
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
) -> int:
    """Run unattended install from a TOML config file."""
    from arches_installer.core.auto import AutoInstallConfig, run_auto_install

    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        return 1

    platform = _load_platform(platform_path)

    try:
        config = AutoInstallConfig.from_file(config_path)
    except (ValueError, KeyError) as e:
        print(f"ERROR: Invalid config: {e}", file=sys.stderr)
        return 1

    if dry_run:
        print("== Arches Auto Install (dry run) ==")
        print(f"  Platform:   {platform.name} ({platform.arch})")
        print(f"  Kernel:     {platform.kernel.package}")
        print(f"  Device:     {config.device}")
        print(f"  Template:   {config.template.name}")
        print(f"  Filesystem: {config.template.disk.filesystem}")
        print(f"  Bootloader: {config.template.bootloader.type}")
        print(f"  Snapshots:  {config.template.bootloader.snapshot_boot}")
        print(f"  Hostname:   {config.hostname}")
        print(f"  User:       {config.username}")
        print(f"  Packages:   {len(config.template.system.packages)}")
        print(f"  Services:   {len(config.template.services)}")
        if config.template.disk.subvolumes:
            print(f"  Subvolumes: {', '.join(config.template.disk.subvolumes)}")
        if config.template.ansible.chroot_roles:
            print(
                f"  Ansible (chroot):    {', '.join(config.template.ansible.chroot_roles)}"
            )
        if config.template.ansible.firstboot_roles:
            print(
                f"  Ansible (1st boot):  {', '.join(config.template.ansible.firstboot_roles)}"
            )
        if platform.hardware_detection.enabled:
            print(f"  HW detect:  {platform.hardware_detection.tool}")
        print("")
        print("Dry run complete. No changes made.")
        return 0

    return run_auto_install(platform, config)


if __name__ == "__main__":
    sys.exit(main())
