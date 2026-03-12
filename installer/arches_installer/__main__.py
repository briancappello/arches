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
    args = parser.parse_args()

    if args.auto:
        return _run_auto(args.auto, dry_run=args.dry_run)
    else:
        return _run_tui()


def _run_tui() -> int:
    """Launch the interactive Textual TUI."""
    from arches_installer.tui.app import ArchesApp

    app = ArchesApp()
    app.run()
    return 0


def _run_auto(config_path: Path, *, dry_run: bool = False) -> int:
    """Run unattended install from a TOML config file."""
    from arches_installer.core.auto import AutoInstallConfig, run_auto_install

    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        return 1

    try:
        config = AutoInstallConfig.from_file(config_path)
    except (ValueError, KeyError) as e:
        print(f"ERROR: Invalid config: {e}", file=sys.stderr)
        return 1

    if dry_run:
        print("== Arches Auto Install (dry run) ==")
        print(f"  Device:     {config.device}")
        print(f"  Template:   {config.template.name}")
        print(f"  Filesystem: {config.template.disk.filesystem}")
        print(f"  Kernel:     {config.template.system.kernel}")
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
        print("")
        print("Dry run complete. No changes made.")
        return 0

    return run_auto_install(config)


if __name__ == "__main__":
    sys.exit(main())
