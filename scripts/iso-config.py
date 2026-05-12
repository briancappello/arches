#!/usr/bin/env python3
"""Read ISO configuration from templates/iso.toml and module definitions.

Called by the Makefile to extract package lists, graphical session config,
and template lists for the ISO build.  Outputs are printed to stdout in
a format suitable for shell consumption.

Environment:
    ARCHES_TEMPLATE   If set, restrict iso.toml [install].templates to
                      this single template (with or without the .toml
                      suffix). All downstream commands — package
                      caching, AUR/module builds, installer staging —
                      operate on the filtered list. Errors out if the
                      template is not in iso.toml's list.

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
        Filtered by ARCHES_TEMPLATE if set.

    build-modules <modules_dir>
        Print module slugs that have a build.sh and are used by the
        installable templates. Filtered by ARCHES_TEMPLATE if set.

    is-graphical <modules_dir>
        Print 'true' if [graphical].modules contains a desktop module,
        'false' otherwise.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ISO_TOML = PROJECT_ROOT / "templates" / "iso.toml"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
GPU_STACKS_TOML = PROJECT_ROOT / "scripts" / "gpu-stacks.toml"


def _load_gpu_stacks() -> dict:
    """Load scripts/gpu-stacks.toml.

    Returns a dict keyed by stack name, value is the raw TOML table.
    Returns an empty dict if the file is missing (older trees that
    predate GPU-stack support).
    """
    if not GPU_STACKS_TOML.is_file():
        return {}
    with open(GPU_STACKS_TOML, "rb") as f:
        data = tomllib.load(f)
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _apply_arches_gpu_override(template_stacks: list[str]) -> list[str]:
    """Apply the ARCHES_GPU env var to a template's [gpu].stacks list.

    ARCHES_GPU is a comma- or whitespace-separated list of stack names.
    When set, it replaces the template default — same semantics as
    arches_installer.core.gpu_stacks._env_override at install time, so
    the build pipeline and the installer agree on which stacks are in
    play.
    """
    raw = os.environ.get("ARCHES_GPU", "").strip()
    if not raw:
        return list(template_stacks)
    parts: list[str] = []
    for chunk in raw.replace(",", " ").split():
        chunk = chunk.strip()
        if chunk and chunk not in parts:
            parts.append(chunk)
    return parts


def _arbitrate_llama_cpp_modules(
    stacks: list[str], stack_defs: dict
) -> list[str]:
    """Return the set of llama-cpp-* module slugs to EXCLUDE due to
    mutually-exclusive arbitration.

    Mirrors the Vulkan-wins rule in
    arches_installer.core.gpu_stacks._arbitrate_llama_cpp. We do this
    at build time so the per-variant build.sh inputs match what the
    installer will actually pacstrap — otherwise we'd build (and ship)
    package variants that nothing installs, or worse, fail to build
    the variant the installer DOES want.
    """
    variants: list[tuple[str, str]] = []  # (stack_name, variant)
    for name in stacks:
        spec = stack_defs.get(name, {})
        v = spec.get("llama_cpp_variant", "")
        if v:
            variants.append((name, v))

    if not variants:
        return []

    only_variants = [v for _, v in variants]
    if "vulkan" in only_variants:
        winner = "vulkan"
    else:
        winner = only_variants[0]

    variant_to_module = {
        "vulkan": "llama-cpp",
        "hip": "llama-cpp-hip",
        "cuda": "llama-cpp-cuda",
    }
    exclude: list[str] = []
    for _name, v in variants:
        if v == winner:
            continue
        m = variant_to_module.get(v, "")
        if m and m not in exclude:
            exclude.append(m)
    return exclude


def _normalize_template_name(name: str) -> str:
    """Accept ``llm-inference``, ``llm-inference.toml``, or a path."""
    name = os.path.basename(name).strip()
    if not name.endswith(".toml"):
        name = f"{name}.toml"
    return name


def _filter_templates(templates: list[str]) -> list[str]:
    """Apply the ARCHES_TEMPLATE filter to an iso.toml templates list.

    When ``ARCHES_TEMPLATE`` is set in the environment, restrict the list
    to that single template so downstream consumers (package caching,
    AUR/module builds, installer staging) only operate on the chosen
    workload. The filter is applied centrally here so every caller of
    iso-config.py picks it up transparently.

    Validation: if the filter doesn't match any template in iso.toml's
    [install].templates list, exit with an error — silently producing an
    empty list would produce a broken ISO.
    """
    selected = os.environ.get("ARCHES_TEMPLATE", "").strip()
    if not selected:
        return templates

    selected_norm = _normalize_template_name(selected)
    matched = [t for t in templates if _normalize_template_name(t) == selected_norm]

    if not matched:
        available = ", ".join(sorted(_normalize_template_name(t) for t in templates))
        print(
            f"ERROR: ARCHES_TEMPLATE='{selected}' (resolved as '{selected_norm}') "
            f"is not listed in {ISO_TOML.relative_to(PROJECT_ROOT)} "
            f"[install].templates. Available: {available}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Sanity-check the file exists
    tmpl_path = TEMPLATES_DIR / matched[0]
    if not tmpl_path.is_file():
        print(
            f"ERROR: Template '{matched[0]}' is listed in iso.toml but the "
            f"file does not exist at {tmpl_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    return matched


def _load_iso_config() -> dict:
    """Load templates/iso.toml, applying ARCHES_TEMPLATE filter to the
    [install].templates list.

    All downstream commands (templates, build-modules, packages, etc.)
    operate on the filtered list, so a single env var transparently
    narrows the entire build to one workload.
    """
    with open(ISO_TOML, "rb") as f:
        config = tomllib.load(f)

    install = config.setdefault("install", {})
    install["templates"] = _filter_templates(install.get("templates", []))
    return config


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


def cmd_build_modules(modules_dir: Path) -> None:
    """Print module slugs that have a build.sh and are used by installable templates.

    Collects the union of all module slugs across every template listed
    in ``iso.toml``'s ``[install].templates``. This includes:

    1. Modules listed directly in each template's ``[modules].include``.
    2. Modules contributed by each template's ``[gpu].stacks`` —
       looked up in ``scripts/gpu-stacks.toml``. The ``ARCHES_GPU`` env
       var overrides the template's default stack list, matching the
       behaviour of arches_installer.core.gpu_stacks at install time.

    Mutually-exclusive llama.cpp variants are then arbitrated
    (Vulkan-wins) and the losing variants are excluded, so we don't
    waste time building a package variant the installer won't pacstrap.

    Finally we filter to slugs whose module directory contains a
    ``build.sh``.
    """
    config = _load_iso_config()
    templates_dir = PROJECT_ROOT / "templates"
    stack_defs = _load_gpu_stacks()

    all_slugs: set[str] = set()
    excluded_slugs: set[str] = set()

    for tmpl_name in config.get("install", {}).get("templates", []):
        tmpl_path = templates_dir / tmpl_name
        if not tmpl_path.is_file():
            continue
        with open(tmpl_path, "rb") as f:
            tmpl_data = tomllib.load(f)

        # 1. Direct module includes
        all_slugs.update(tmpl_data.get("modules", {}).get("include", []))

        # 2. Modules pulled in via [gpu].stacks (with ARCHES_GPU override)
        template_stacks = tmpl_data.get("gpu", {}).get("stacks", [])
        effective_stacks = _apply_arches_gpu_override(template_stacks)
        for stack_name in effective_stacks:
            spec = stack_defs.get(stack_name)
            if spec is None:
                # Unknown stack — surface a warning but don't fail the
                # build here; the installer will reject it later with a
                # clearer error and the list of known stacks.
                print(
                    f"WARNING: iso-config.py build-modules: unknown GPU "
                    f"stack {stack_name!r} referenced by {tmpl_name} "
                    f"(or ARCHES_GPU). Known: "
                    f"{', '.join(sorted(stack_defs)) or '(none)'}",
                    file=sys.stderr,
                )
                continue
            all_slugs.update(spec.get("modules", []))

        # Apply llama.cpp Vulkan-wins arbitration so we don't build a
        # variant the installer is going to ignore.
        excluded_slugs.update(
            _arbitrate_llama_cpp_modules(effective_stacks, stack_defs)
        )

    all_slugs -= excluded_slugs

    # The llama.cpp build.sh lives in modules/llama-cpp/ but produces
    # all three variant packages (vulkan / hip / cuda) under
    # ARCHES_GPU control. The HIP and CUDA install-side modules
    # (llama-cpp-hip, llama-cpp-cuda) don't ship their own build.sh.
    # Ensure the master build script runs whenever ANY variant is
    # wanted, so the corresponding .pkg.tar.* lands in arches-local.
    if all_slugs & {"llama-cpp", "llama-cpp-hip", "llama-cpp-cuda"}:
        all_slugs.add("llama-cpp")

    # Print only those that have a build.sh
    for slug in sorted(all_slugs):
        if (modules_dir / slug / "build.sh").is_file():
            print(slug)


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

    elif command == "build-modules":
        if len(sys.argv) != 3:
            print(
                "Usage: iso-config.py build-modules <modules_dir>", file=sys.stderr
            )
            sys.exit(1)
        cmd_build_modules(Path(sys.argv[2]))

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
