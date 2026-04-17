"""Composable module system for Arches install templates.

A module is a self-contained unit of system functionality: packages, services,
and an optional Ansible role.  Templates select a set of modules, and the
installer merges them into a single ``InstallTemplate`` for the install
pipeline.

Modules live in ``<project>/modules/`` (development) or
``/opt/arches/modules/`` (live ISO).  Each module is a directory containing
a ``module.toml`` and optionally an ``ansible/`` subdirectory with a
standard Ansible role layout.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arches_installer.core.template import InstallPhases

# ---------------------------------------------------------------------------
# Category system
# ---------------------------------------------------------------------------

#: Execution order for module categories.  Ansible roles run grouped by
#: category in this order; within a category, roles run alphabetically.
CATEGORY_ORDER: list[str] = [
    "base",
    "networking",
    "desktop",
    "dev-toolchain",
    "topic",
    "service",
]

#: Category-level dependency constraints.  For each category, at least one
#: module from each listed dependency category must be selected.
CATEGORY_DEPS: dict[str, list[str]] = {
    "networking": ["base"],
    "desktop": ["networking"],
    "topic": ["desktop"],
    "service": ["base", "networking"],
    "dev-toolchain": ["base"],
}

VALID_CATEGORIES: set[str] = set(CATEGORY_ORDER)

# ---------------------------------------------------------------------------
# Search paths
# ---------------------------------------------------------------------------

_MODULES_SEARCH: list[Path] = [
    Path("/opt/arches/modules"),
    Path(__file__).resolve().parents[3] / "modules",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class IsoConfig:
    """ISO live-session metadata for desktop modules.

    Only desktop-category modules provide this.  It tells the ISO build
    system how to configure the graphical live session (display manager
    autologin, session name, terminal emulator command).
    """

    display_manager: str
    session: str
    terminal: str


@dataclass
class Module:
    """A single composable module loaded from ``module.toml``."""

    slug: str
    name: str
    description: str
    category: str
    install: InstallPhases
    services: list[str] = field(default_factory=list)
    has_ansible_role: bool = False
    requires: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    iso: IsoConfig | None = None

    @classmethod
    def from_dict(cls, slug: str, data: dict[str, Any], *, has_ansible: bool) -> Module:
        """Build a Module from a parsed ``module.toml`` dict."""
        meta = data.get("meta", {})
        install_raw = data.get("install", {})
        svc_raw = data.get("services", {})
        deps_raw = data.get("dependencies", {})
        iso_raw = data.get("iso", {})

        category = meta.get("category", "base")
        if category not in VALID_CATEGORIES:
            raise ModuleError(
                f"Module '{slug}' has invalid category '{category}'. "
                f"Valid categories: {', '.join(CATEGORY_ORDER)}"
            )

        iso_config = None
        if iso_raw:
            iso_config = IsoConfig(
                display_manager=iso_raw.get("display_manager", ""),
                session=iso_raw.get("session", ""),
                terminal=iso_raw.get("terminal", ""),
            )

        return cls(
            slug=slug,
            name=meta.get("name", slug),
            description=meta.get("description", ""),
            category=category,
            install=InstallPhases(
                pacstrap=install_raw.get("pacstrap", {}).get("packages", []),
                override=install_raw.get("override", {}).get("packages", []),
                firstboot=install_raw.get("firstboot", {}).get("packages", []),
            ),
            services=svc_raw.get("enable", []),
            has_ansible_role=has_ansible,
            requires=deps_raw.get("requires", []),
            conflicts=deps_raw.get("conflicts", []),
            iso=iso_config,
        )


@dataclass
class ResolvedModules:
    """Validated, ordered collection of modules ready for installation."""

    modules: list[Module]

    @property
    def graphical(self) -> bool:
        """True if any selected module has category 'desktop'."""
        return any(m.category == "desktop" for m in self.modules)

    @property
    def ansible_roles(self) -> list[str]:
        """Module slugs that have an Ansible role, in execution order."""
        return [m.slug for m in self.modules if m.has_ansible_role]

    def merged_install(self) -> InstallPhases:
        """Merge all module packages into a single ``InstallPhases``."""
        pacstrap: list[str] = []
        override: list[str] = []
        firstboot: list[str] = []

        for mod in self.modules:
            pacstrap.extend(mod.install.pacstrap)
            override.extend(mod.install.override)
            firstboot.extend(mod.install.firstboot)

        # Deduplicate while preserving order
        return InstallPhases(
            pacstrap=_dedup(pacstrap),
            override=_dedup(override),
            firstboot=_dedup(firstboot),
        )

    def merged_services(self) -> list[str]:
        """Merge all module services into a single deduplicated list."""
        services: list[str] = []
        for mod in self.modules:
            services.extend(mod.services)
        return _dedup(services)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ModuleError(Exception):
    """Raised when module loading, validation, or resolution fails."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _find_modules_dir() -> Path:
    """Locate the modules directory from the search path."""
    for d in _MODULES_SEARCH:
        if d.is_dir():
            return d
    searched = ", ".join(str(d) for d in _MODULES_SEARCH)
    raise FileNotFoundError(f"Modules directory not found (searched: {searched})")


def _detect_ansible_role(module_dir: Path) -> bool:
    """Check whether a module has an ansible role.

    In development, the role lives at ``modules/<slug>/ansible/``.
    On the live ISO, module directories only contain ``module.toml`` and
    ansible roles are staged separately at ``/opt/arches/ansible/roles/<slug>/``.
    We check both locations.
    """
    slug = module_dir.name

    # Development layout: ansible/ colocated with module.toml
    if (module_dir / "ansible" / "tasks" / "main.yml").is_file():
        return True

    # ISO layout: roles staged at /opt/arches/ansible/roles/<slug>/
    iso_role = Path("/opt/arches/ansible/roles") / slug / "tasks" / "main.yml"
    if iso_role.is_file():
        return True

    return False


def load_module(module_dir: Path) -> Module:
    """Load a single module from its directory.

    Parameters
    ----------
    module_dir:
        Path to the module directory (must contain ``module.toml``).
    """
    toml_path = module_dir / "module.toml"
    if not toml_path.is_file():
        raise ModuleError(f"No module.toml found in {module_dir}")

    with open(toml_path, "rb") as f:
        data = tomllib.load(f)

    has_ansible = _detect_ansible_role(module_dir)

    return Module.from_dict(module_dir.name, data, has_ansible=has_ansible)


def discover_modules() -> list[Module]:
    """Discover all available modules.

    Returns a list of modules sorted alphabetically by slug.
    """
    modules_dir = _find_modules_dir()
    modules: list[Module] = []

    for item in sorted(modules_dir.iterdir()):
        if item.is_dir() and (item / "module.toml").is_file():
            try:
                modules.append(load_module(item))
            except (ModuleError, tomllib.TOMLDecodeError) as e:
                # Skip malformed modules but warn
                import sys

                print(f"WARNING: Skipping module {item.name}: {e}", file=sys.stderr)

    return modules


# ---------------------------------------------------------------------------
# Resolution and validation
# ---------------------------------------------------------------------------


def resolve_modules(
    slugs: list[str],
    available: list[Module] | None = None,
) -> ResolvedModules:
    """Validate and order a selection of modules.

    Parameters
    ----------
    slugs:
        List of module slugs to include.
    available:
        Available modules.  If ``None``, calls ``discover_modules()``.

    Returns
    -------
    ResolvedModules:
        Validated, ordered collection ready for merging.

    Raises
    ------
    ModuleError:
        If validation fails (unknown slug, missing dependency, conflict,
        unsatisfied category constraint, or multiple desktop modules).
    """
    if available is None:
        available = discover_modules()

    by_slug: dict[str, Module] = {m.slug: m for m in available}

    # 1. Check all slugs are known
    selected: list[Module] = []
    for slug in slugs:
        if slug not in by_slug:
            known = ", ".join(sorted(by_slug.keys()))
            raise ModuleError(f"Unknown module '{slug}'. Available modules: {known}")
        selected.append(by_slug[slug])

    selected_slugs = set(slugs)

    # 2. Check per-module requires
    for mod in selected:
        for req in mod.requires:
            if req not in selected_slugs:
                raise ModuleError(
                    f"Module '{mod.slug}' requires '{req}', which is not selected."
                )

    # 3. Check per-module conflicts
    for mod in selected:
        for conflict in mod.conflicts:
            if conflict in selected_slugs:
                raise ModuleError(
                    f"Module '{mod.slug}' conflicts with '{conflict}'. "
                    f"Both cannot be selected."
                )

    # 4. Check category-level constraints
    selected_categories = {m.category for m in selected}
    for mod in selected:
        cat = mod.category
        if cat in CATEGORY_DEPS:
            for required_cat in CATEGORY_DEPS[cat]:
                if required_cat not in selected_categories:
                    raise ModuleError(
                        f"Module '{mod.slug}' (category '{cat}') requires at "
                        f"least one module from category '{required_cat}'."
                    )

    # 5. At most one desktop module
    desktop_modules = [m for m in selected if m.category == "desktop"]
    if len(desktop_modules) > 1:
        names = ", ".join(m.slug for m in desktop_modules)
        raise ModuleError(f"Only one desktop module may be selected. Found: {names}")

    # 6. Sort by category order, then alphabetically within category
    def sort_key(mod: Module) -> tuple[int, str]:
        try:
            cat_idx = CATEGORY_ORDER.index(mod.category)
        except ValueError:
            cat_idx = len(CATEGORY_ORDER)
        return (cat_idx, mod.slug)

    ordered = sorted(selected, key=sort_key)

    return ResolvedModules(modules=ordered)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dedup(items: list[str]) -> list[str]:
    """Deduplicate a list while preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
