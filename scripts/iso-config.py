#!/usr/bin/env python3
"""Read ISO configuration from templates/iso.toml and module definitions.

Called by the Makefile to extract package lists, graphical session config,
and template lists for the ISO build.  Outputs are printed to stdout in
a format suitable for shell consumption.

Environment:
    ARCHES_PROVIDER   Select the target provider. Live packages retain the
                      platform default. Omission retains existing defaults.
    ARCHES_TEMPLATE   If set, restrict iso.toml [install].templates to
                      this single template (with or without the .toml
                      suffix). All downstream commands — package
                      caching, AUR/module builds, installer staging —
                      operate on the filtered list. Errors out if the
                      template is not in iso.toml's list.

Usage:
    python3 scripts/iso-config.py <command> [options]

Commands:
    provider-id <platform.toml>
    target-base <platform.toml>
    target-packages <platform.toml>
        Resolve target identity or packages without package transactions.

    stage-provider <platform.toml> <staged-opt-arches>
        Stage one target identity, hardware configuration, and provider policy.

    stage-cache <platform.toml> <staged-opt-arches> <offline-cache>
        Copy the selected default-provider cache scope, not the old flat cache.

    stage-firstboot-assets <platform.toml> <staged-opt-arches> <asset-cache>
        Pin and stage Oh My Zsh only when selected target roles require it.

    stage-inputs <platform.toml> <staged-opt-arches> <input-root> <manifest-sha256>
    omarchy-bootstrap <platform.toml> <iso> <signature> <public-key> <closure> <output>
        Check and copy ProviderInputs. The caller must authenticate the pin.
        This command does not establish provenance or enable an installer.

    latest-iso <platform.toml> <output-directory>
        Print the newest ISO for this provider and platform, or nothing.

    live-build-modules <modules_dir>
        Print custom module builds needed by the default live environment.

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
import hashlib
import json
import shutil
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Builder images need only stdlib Python, not an editable/TUI installation.
sys.path.insert(0, str(PROJECT_ROOT / "installer"))
ISO_TOML = PROJECT_ROOT / "templates" / "iso.toml"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
GPU_STACKS_TOML = PROJECT_ROOT / "scripts" / "gpu-stacks.toml"


def selected_provider(path: Path):
    from arches_installer.core.platform import load_platform
    from arches_installer.core.provider import resolve_provider

    platform = load_platform(path)
    return platform, resolve_provider(
        platform, os.environ.get("ARCHES_PROVIDER") or None
    )


def target_packages(path: Path, *, base_only: bool = False) -> list[str]:
    from arches_installer.core.provider import validate_selection
    from arches_installer.core.template import load_template, resolve_and_merge_modules

    platform, provider = selected_provider(path)
    packages = list(provider.base_packages)
    for variant in provider.kernel.variants:
        packages.extend((variant.package, variant.headers))
    if not base_only:
        for name in _load_iso_config().get("install", {}).get("templates", []):
            template = load_template(TEMPLATES_DIR / name)
            if provider.id in ("arch", "omarchy"):
                validate_selection(
                    platform,
                    provider,
                    template,
                    firmware="uefi",
                    filesystem="btrfs",
                    install_mode="fresh",
                )
            resolved = resolve_and_merge_modules(template, provider_id=provider.id)
            if provider.id in ("arch", "omarchy") and any(
                (PROJECT_ROOT / "modules" / slug / "build.sh").exists()
                for slug in resolved.module_slugs
            ):
                raise ValueError(
                    "Alternative provider custom builds require reviewed provenance"
                )
            packages.extend(resolved.install.all_packages)
    return list(dict.fromkeys(packages))


def stage_provider(path: Path, output: Path) -> None:
    platform, provider = selected_provider(path)
    text = path.read_text()
    if provider.id in ("arch", "omarchy"):
        # Retain hardware tables, not the previous provider's package policy.
        data = tomllib.loads(text)
        sections = {
            "platform": data["platform"] | {"cachyos_optimization_tier": ""},
            "provider": {"id": provider.id},
            "kernel": {"flags": platform.kernel_flags},
            "bootloader": data.get("bootloader", {}),
        }
        text = (
            "\n".join(
                f"[{section}]\n"
                + "\n".join(
                    f"{key} = {json.dumps(value)}" for key, value in values.items()
                )
                for section, values in sections.items()
            )
            + "\n"
        )
    previous = output / "target-provider.json"
    if (
        not previous.exists()
        or json.loads(previous.read_text()).get("provider") != provider.id
    ):
        for name in ("pkg-cache", "provider-inputs"):
            if (output / name).exists():
                shutil.rmtree(output / name)
    (output / "platform").mkdir(parents=True, exist_ok=True)
    (output / "platform/platform.toml").write_text(text)
    (output / "platform/pacman.conf").write_text(provider.pacman_conf)
    # Only remove stage-owned definitions, never host caches or sources.
    definitions = output / "providers"
    definitions.mkdir(exist_ok=True)
    for name in ("cachyos", "arch", "omarchy"):
        dest = definitions / name
        if dest.exists():
            shutil.rmtree(dest)
        if name == provider.id:
            shutil.copytree(PROJECT_ROOT / "providers" / name, dest)
    metadata = {
        "firstboot_assets": 1,
        "provider": provider.id,
        "platform": platform.name,
        "architecture": platform.arch,
        "channel": "stable",
        "config_sha256": hashlib.sha256(provider.pacman_conf.encode()).hexdigest(),
        "base_packages": target_packages(path, base_only=True),
    }
    if previous.exists():
        old = json.loads(previous.read_text())
        if all(old.get(key) == metadata[key] for key in ("provider", "platform", "architecture", "channel", "config_sha256")):
            for key in ("inputs", "manifest_sha256"):
                if key in old:
                    metadata[key] = old[key]
    # Selection metadata is NOT an authenticated ProviderInputs manifest.
    (output / "target-provider.json").write_text(
        json.dumps(metadata, sort_keys=True, indent=2) + "\n"
    )


def stage_cache(path: Path, output: Path, cache_root: Path) -> None:
    platform, provider = selected_provider(path)
    if provider.id in ("arch", "omarchy"):
        raise ValueError(
            "Alternative provider caches require authenticated ProviderInputs"
        )
    root = (cache_root / platform.name / provider.id).resolve()
    source = Path((root / "current").read_text().strip())
    if (
        source.resolve().parent != root
        or len(source.name) != 64
        or not (source / "sync").is_dir()
    ):
        raise ValueError("Invalid selected cache scope")
    dest = output / "pkg-cache"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)


def stage_firstboot_assets(path: Path, output: Path, cache: Path) -> None:
    from arches_installer.core import ohmyzsh
    from arches_installer.core.template import load_template, resolve_and_merge_modules

    _, provider = selected_provider(path)
    needs_zsh = False
    for name in _load_iso_config().get("install", {}).get("templates", []):
        template = resolve_and_merge_modules(
            load_template(TEMPLATES_DIR / name), provider_id=provider.id
        )
        if "zsh" in template.ansible.firstboot_roles:
            if provider.lifecycle_owner != "arches":
                raise ValueError("Provider owns its shell assets; Arches Zsh overlay rejected")
            needs_zsh = True
    # Live boot does not execute the Zsh role. Its zsh package needs no OMZ asset.
    output.mkdir(parents=True, exist_ok=True)
    # Builder wrappers return staged files to the invoking user after a build.
    # Reclaim this protected input directory when the next root build reuses it.
    if os.geteuid() == 0:
        os.chown(output, 0, 0)
    output.chmod(0o755)
    dest = output / "firstboot-assets"
    if dest.exists():
        shutil.rmtree(dest)
    manifest = {"version": 1, "ohmyzsh": None}
    if needs_zsh:
        source = ohmyzsh.build(cache.resolve())
        dest.mkdir(mode=0o755)
        bundle = dest / "ohmyzsh.bundle"
        shutil.copyfile(source, bundle)
        bundle.chmod(0o644)
        manifest["ohmyzsh"] = {
            "origin": ohmyzsh.ORIGIN, "revision": ohmyzsh.REVISION,
            "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        }
    marker = output / "firstboot-assets.json"
    marker.unlink(missing_ok=True)
    marker.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    marker.chmod(0o644)
    if needs_zsh:
        ohmyzsh.preflight(output)


def stage_inputs(path: Path, output: Path, source: Path, pin: str) -> None:
    from arches_installer.core.provider import ProviderInputs, validate_provider_inputs

    platform, provider = selected_provider(path)
    files = validate_provider_inputs(
        provider, platform.arch, ProviderInputs(source, pin)
    )
    dest = output / "provider-inputs" / provider.id / pin
    if source.is_relative_to(output.resolve()):
        raise ValueError("Provider input source must be outside ISO staging")
    stage_provider(path, output)
    if (output / "provider-inputs").exists():
        shutil.rmtree(output / "provider-inputs")
    for name in ["manifest.json", *files]:
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        shutil.copyfile(source / name, target)
        target.chmod(0o644)
    validate_provider_inputs(
        provider, platform.arch, ProviderInputs(dest.resolve(), pin)
    )
    metadata_path = output / "target-provider.json"
    metadata = json.loads(metadata_path.read_text())
    metadata.update(inputs=dest.relative_to(output).as_posix(), manifest_sha256=pin)
    temporary = metadata_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n")
    temporary.chmod(0o644)
    temporary.replace(metadata_path)


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


def cmd_build_modules(
    modules_dir: Path, *, identity: bool = False, live: bool = False
) -> None:
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

    all_slugs: set[str] = (
        set(
            config.get("framebuffer", {}).get("modules", [])
            + (config.get("graphical", {}).get("modules", [])
               if os.environ.get("ISO_MODE", "graphical") != "fb" else [])
        )
        if live
        else set()
    )
    excluded_slugs: set[str] = set()

    templates = config.get("install", {}).get("templates", [])
    for tmpl_name in [] if live else templates:
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

    if identity:
        print(
            json.dumps(
                {
                    "templates": sorted(
                        {_normalize_template_name(name) for name in templates}
                    ),
                    "modules": sorted(all_slugs),
                },
                sort_keys=True,
            )
        )
        return

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
    from arches_installer.core import provider, module, gpu_stacks
    from arches_installer.core.platform import load_platform

    # This process is a checkout build tool, not the installed runtime. Bind
    # model data to the same sources that staging and cache hashing consume.
    provider._PROVIDERS_SEARCH = [PROJECT_ROOT / "providers"]
    provider._PLATFORMS_DIR = PROJECT_ROOT / "platforms"
    module._MODULES_SEARCH = [PROJECT_ROOT / "modules"]
    gpu_stacks._STACKS_SEARCH = [GPU_STACKS_TOML]
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]

    if command in (
        "provider-id",
        "target-base",
        "target-packages",
        "stage-provider",
        "stage-cache",
        "stage-firstboot-assets",
        "stage-inputs",
        "omarchy-bootstrap",
        "latest-iso",
        "live-kernel",
        "live-kernel-packages",
    ):
        path = Path(sys.argv[2])
        if command in ("live-kernel", "live-kernel-packages"):
            kernel = load_platform(path).kernel
            if command == "live-kernel":
                print(kernel.package)
            else:
                print(
                    "\n".join(
                        [p for v in kernel.variants for p in (v.package, v.headers)]
                        + ["linux-firmware"]
                    )
                )
        elif command == "provider-id":
            print(selected_provider(path)[1].id)
        elif command == "latest-iso":
            platform, provider = selected_provider(path)
            matches = Path(sys.argv[3]).glob(
                f"arches-{provider.id}-{platform.name}-*.iso"
            )
            latest = max(matches, key=lambda p: p.stat().st_mtime_ns, default=None)
            if latest:
                print(latest)
        elif command == "stage-provider":
            stage_provider(path, Path(sys.argv[3]))
        elif command == "stage-cache":
            stage_cache(path, Path(sys.argv[3]), Path(sys.argv[4]))
        elif command == "stage-firstboot-assets":
            stage_firstboot_assets(path, Path(sys.argv[3]), Path(sys.argv[4]))
        elif command == "stage-inputs":
            stage_inputs(path, Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5])
        elif command == "omarchy-bootstrap":
            from arches_installer.core.omarchy import stage_bootstrap

            if len(sys.argv) != 8:
                raise ValueError(
                    "omarchy-bootstrap requires platform, ISO, signature, public key, closure, output"
                )
            platform, selected = selected_provider(path)
            inputs = stage_bootstrap(
                platform, selected, *(Path(arg) for arg in sys.argv[3:])
            )
            print(
                json.dumps(
                    {
                        "root": str(inputs.root),
                        "manifest_sha256": inputs.manifest_sha256,
                    }
                )
            )
        else:
            print("\n".join(target_packages(path, base_only=command == "target-base")))

    elif command == "packages":
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

    elif command == "live-build-modules":
        cmd_build_modules(Path(sys.argv[2]), live=True)

    elif command == "build-selection":
        cmd_build_modules(
            Path(sys.argv[2]),
            identity=True,
            live=os.environ.get("ARCHES_BUILD_PURPOSE") == "live",
        )

    elif command == "build-modules":
        if len(sys.argv) != 3:
            print("Usage: iso-config.py build-modules <modules_dir>", file=sys.stderr)
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
