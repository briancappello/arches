"""Entry point for the Arches installer."""

import argparse
import sys
from pathlib import Path

AUTO_INSTALL_PATH = Path("/root/auto-install.toml")


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

    # Explicit --auto flag takes priority — fail hard on errors
    if args.auto:
        return _run_auto(args.auto, platform_path=args.platform, dry_run=args.dry_run)

    # Auto-detect config baked into the ISO at /root/auto-install.toml.
    # On failure, print the error and fall through to the TUI so the user
    # can still perform a manual install.
    if AUTO_INSTALL_PATH.exists():
        rc = _run_auto(
            AUTO_INSTALL_PATH,
            platform_path=args.platform,
            dry_run=args.dry_run,
            fallback_to_tui=True,
        )
        if rc == 0:
            return 0
        # Non-zero means auto-install failed; fall through to TUI

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

    When *fallback_to_tui* is True (ISO auto-detect path), errors are
    printed but return non-zero so the caller can fall through to the
    TUI.  When False (explicit ``--auto``), behaviour is unchanged.
    """
    from arches_installer.core.auto import AutoInstallConfig, run_auto_install

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
        layout = platform.disk_layout
        print("== Arches Auto Install (dry run) ==")
        print(f"  Platform:   {platform.name} ({platform.arch})")
        print(f"  Kernel:     {platform.kernel.package}")
        print(f"  Bootloader: {platform.bootloader.type}")
        print(f"  Filesystem: {layout.filesystem}")
        print(f"  Snapshots:  {platform.bootloader.snapshot_boot}")
        print(f"  Device:     (auto-detect at install time)")
        print(f"  Template:   {config.template.name}")
        print(f"  Hostname:   {config.hostname}")
        print(f"  User:       {config.username}")
        print(f"  Reboot:     {config.reboot}")
        print(f"  Packages:   {len(config.template.install.all_packages)}")
        print(f"  Services:   {len(config.template.services)}")
        if layout.subvolumes:
            print(f"  Subvolumes: {', '.join(layout.subvolumes)}")
        if layout.boot_size_mib > 0:
            print(f"  /boot:      {layout.boot_size_mib}M (ext4)")
        if layout.home_partition:
            print("  /home:      separate partition")
        if config.template.ansible.firstboot_roles:
            print(
                f"  Ansible (1st boot):  {', '.join(config.template.ansible.firstboot_roles)}"
            )
        if platform.hardware_detection.enabled:
            print(f"  HW detect:  {platform.hardware_detection.tool}")
        print("")
        print("Dry run complete. No changes made.")
        return 0

    rc = run_auto_install(platform, config)
    if rc != 0 and fallback_to_tui:
        print("\nFalling back to manual install...\n", file=sys.stderr)
    return rc


def _auto_install_error(msg: str, fallback_to_tui: bool) -> None:
    """Print an auto-install error, with fallback context if applicable."""
    print(f"ERROR: {msg}", file=sys.stderr)
    if fallback_to_tui:
        print("Falling back to manual install...\n", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
