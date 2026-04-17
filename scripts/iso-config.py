#!/usr/bin/env python3
"""Read ISO configuration from templates/iso.toml and module definitions.

Called by the Makefile to extract package lists, graphical session config,
and template lists for the ISO build.  Outputs are printed to stdout in
a format suitable for shell consumption.

Usage:
    python3 scripts/iso-config.py <command> [options]

Commands:
    packages <modules_dir> <mode>
        Print all ISO packages (one per line) for the given mode.
        mode is 'graphical' or 'fb'.

    graphical-config <modules_dir>
        Print display_manager, session, and terminal from the desktop
        module's [iso] section (KEY=VALUE lines).

    templates
        Print the template filenames listed in iso.toml (one per line).

    is-graphical <modules_dir>
        Print 'true' if [graphical].modules contains a desktop module,
        'false' otherwise.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ISO_TOML = PROJECT_ROOT / "templates" / "iso.toml"


def _load_iso_config() -> dict:
    with open(ISO_TOML, "rb") as f:
        return tomllib.load(f)


def _load_module(modules_dir: Path, slug: str) -> dict:
    toml_path = modules_dir / slug / "module.toml"
    if not toml_path.is_file():
        print(f"WARNING: Module {slug} not found at {toml_path}", file=sys.stderr)
        return {}
    with open(toml_path, "rb") as f:
        return tomllib.load(f)


def _packages_from_modules(
    modules_dir: Path, slugs: list[str], *, include_override: bool = False
) -> list[str]:
    """Collect packages from a list of module slugs."""
    packages: list[str] = []
    for slug in slugs:
        data = _load_module(modules_dir, slug)
        install = data.get("install", {})
        packages.extend(install.get("pacstrap", {}).get("packages", []))
        if include_override:
            packages.extend(install.get("override", {}).get("packages", []))
        packages.extend(install.get("firstboot", {}).get("packages", []))
    return packages


def _override_packages_from_modules(modules_dir: Path, slugs: list[str]) -> list[str]:
    """Collect only override packages from a list of module slugs."""
    packages: list[str] = []
    for slug in slugs:
        data = _load_module(modules_dir, slug)
        install = data.get("install", {})
        packages.extend(install.get("override", {}).get("packages", []))
    return packages


def cmd_packages(modules_dir: Path, mode: str) -> None:
    """Print all ISO packages for the given build mode."""
    config = _load_iso_config()

    # Framebuffer layer (always included)
    fb = config.get("framebuffer", {})
    fb_modules = fb.get("modules", [])
    fb_packages = fb.get("packages", [])

    packages = _packages_from_modules(modules_dir, fb_modules)
    packages.extend(fb_packages)

    # Graphical layer (only in graphical mode)
    if mode == "graphical":
        gfx = config.get("graphical", {})
        gfx_modules = gfx.get("modules", [])
        packages.extend(_packages_from_modules(modules_dir, gfx_modules))

    # Deduplicate while preserving order
    seen: set[str] = set()
    for pkg in packages:
        if pkg not in seen:
            seen.add(pkg)
            print(pkg)


def cmd_override_packages(modules_dir: Path, mode: str) -> None:
    """Print override packages that need --overwrite --ask 4 installation."""
    config = _load_iso_config()

    # Framebuffer layer
    fb_modules = config.get("framebuffer", {}).get("modules", [])
    packages = _override_packages_from_modules(modules_dir, fb_modules)

    # Graphical layer
    if mode == "graphical":
        gfx_modules = config.get("graphical", {}).get("modules", [])
        packages.extend(_override_packages_from_modules(modules_dir, gfx_modules))

    seen: set[str] = set()
    for pkg in packages:
        if pkg not in seen:
            seen.add(pkg)
            print(pkg)


def cmd_graphical_config(modules_dir: Path) -> None:
    """Print the desktop module's [iso] config as KEY=VALUE lines."""
    config = _load_iso_config()
    gfx_modules = config.get("graphical", {}).get("modules", [])

    for slug in gfx_modules:
        data = _load_module(modules_dir, slug)
        if data.get("meta", {}).get("category") == "desktop":
            iso = data.get("iso", {})
            dm = iso.get("display_manager", "")
            session = iso.get("session", "")
            terminal = iso.get("terminal", "")
            print(f"DISPLAY_MANAGER='{dm}'")
            print(f"SESSION='{session}'")
            print(f"TERMINAL='{terminal}'")
            return

    print("ERROR: No desktop module found in [graphical].modules", file=sys.stderr)
    sys.exit(1)


def cmd_templates() -> None:
    """Print template filenames from iso.toml."""
    config = _load_iso_config()
    for t in config.get("install", {}).get("templates", []):
        print(t)


def cmd_is_graphical(modules_dir: Path) -> None:
    """Print 'true' if [graphical] includes a desktop module."""
    config = _load_iso_config()
    gfx_modules = config.get("graphical", {}).get("modules", [])

    for slug in gfx_modules:
        data = _load_module(modules_dir, slug)
        if data.get("meta", {}).get("category") == "desktop":
            print("true")
            return
    print("false")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]

    if command == "packages":
        if len(sys.argv) != 4:
            print("Usage: iso-config.py packages <modules_dir> <mode>", file=sys.stderr)
            sys.exit(1)
        cmd_packages(Path(sys.argv[2]), sys.argv[3])

    elif command == "override-packages":
        if len(sys.argv) != 4:
            print(
                "Usage: iso-config.py override-packages <modules_dir> <mode>",
                file=sys.stderr,
            )
            sys.exit(1)
        cmd_override_packages(Path(sys.argv[2]), sys.argv[3])

    elif command == "graphical-config":
        if len(sys.argv) != 3:
            print(
                "Usage: iso-config.py graphical-config <modules_dir>", file=sys.stderr
            )
            sys.exit(1)
        cmd_graphical_config(Path(sys.argv[2]))

    elif command == "templates":
        cmd_templates()

    elif command == "is-graphical":
        if len(sys.argv) != 3:
            print("Usage: iso-config.py is-graphical <modules_dir>", file=sys.stderr)
            sys.exit(1)
        cmd_is_graphical(Path(sys.argv[2]))

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
