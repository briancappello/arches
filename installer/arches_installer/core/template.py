"""Load and validate install templates from TOML files.

Templates define a system workload by selecting composable modules and
setting system-level defaults (timezone, locale).  They are platform-
independent -- disk layout, bootloader, kernel, and base packages all
come from the platform config.

Templates live in ``<project>/templates/`` (development) or
``/opt/arches/templates/`` (live ISO).  Install-specific templates
(auto-install.toml, host-install.toml) and system templates
(kde-workstation.toml, vm-server.toml) all live in the same directory.
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Search paths for the templates directory, checked in order.
# On the live ISO, templates are staged at /opt/arches/templates/.
# In development, they're at <project>/templates/ (relative to the
# installer package: installer/arches_installer/core/template.py -> ../../../../templates).
_TEMPLATES_SEARCH = [
    Path("/opt/arches/templates"),
    Path(__file__).resolve().parents[3] / "templates",
]


@dataclass
class SystemConfig:
    timezone: str = "America/Denver"
    locale: str = "en_US.UTF-8"


@dataclass
class InstallPhases:
    """Package lists separated by installation phase."""

    pacstrap: list[str] = field(default_factory=list)
    override: list[str] = field(default_factory=list)
    firstboot: list[str] = field(default_factory=list)

    @property
    def all_packages(self) -> list[str]:
        """All packages across all phases (for caching, display, etc.)."""
        return self.pacstrap + self.override + self.firstboot


@dataclass
class AnsibleConfig:
    firstboot_roles: list[str] = field(default_factory=list)


@dataclass
class InstallTemplate:
    name: str
    description: str
    system: SystemConfig
    install: InstallPhases
    services: list[str] = field(default_factory=list)
    ansible: AnsibleConfig = field(default_factory=AnsibleConfig)
    # When True, the ISO boots into a graphical desktop with a liveuser
    # autologin. When False, the ISO boots to a text console with the
    # TUI installer on tty1. Derived from whether a desktop module is
    # selected.
    graphical: bool = False
    # Module slugs selected by this template.
    module_slugs: list[str] = field(default_factory=list)
    # GPU stack names this template wants enabled by default. Each
    # stack maps (via scripts/gpu-stacks.toml) to additional modules
    # and packages that get merged into the install. The ARCHES_GPU
    # env var overrides this list at build time.
    gpu_stacks: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstallTemplate:
        """Build an InstallTemplate from a parsed TOML dict.

        Templates declare ``[modules].include`` to select composable modules.
        The ``install``, ``services``, ``ansible``, and ``graphical`` fields
        are left empty here and populated later by
        ``resolve_and_merge_modules()``.
        """
        meta = data.get("meta", {})
        sys_raw = data.get("system", {})
        modules_raw = data.get("modules", {})
        gpu_raw = data.get("gpu", {})

        module_slugs = modules_raw.get("include", [])
        gpu_stacks = gpu_raw.get("stacks", [])

        return cls(
            name=meta.get("name", "Unknown"),
            description=meta.get("description", ""),
            system=SystemConfig(
                timezone=sys_raw.get("timezone", "America/Denver"),
                locale=sys_raw.get("locale", "en_US.UTF-8"),
            ),
            install=InstallPhases(),
            services=[],
            ansible=AnsibleConfig(),
            graphical=False,
            module_slugs=module_slugs,
            gpu_stacks=gpu_stacks,
        )


def resolve_and_merge_modules(template: InstallTemplate) -> InstallTemplate:
    """Resolve a template's modules and merge their contents.

    Loads, validates, and merges the modules declared in
    ``template.module_slugs`` (plus any GPU-stack-derived modules from
    ``template.gpu_stacks``) into the template's ``install``,
    ``services``, ``ansible``, and ``graphical`` fields.

    The GPU-stack merge happens FIRST so the resulting module list goes
    through normal validation (category constraints, dependency checks,
    conflict detection) alongside the template's explicit modules.

    These imports are deferred to avoid circular imports at module level.
    """
    from arches_installer.core.gpu_stacks import resolve_gpu_stacks
    from arches_installer.core.module import resolve_modules

    # 1. Resolve GPU stacks (template default + ARCHES_GPU env override).
    #    Adds extra modules and packages to the template's existing lists.
    gpu = resolve_gpu_stacks(template.gpu_stacks)

    # 2. Merge stack-provided modules into the template's include list,
    #    deduplicating and respecting llama.cpp arbitration exclusions.
    effective_slugs: list[str] = []
    for slug in template.module_slugs:
        if slug in gpu.excluded_modules:
            continue  # arbitration removed it (e.g. llama-cpp-hip excluded by Vulkan wins)
        if slug not in effective_slugs:
            effective_slugs.append(slug)
    for slug in gpu.extra_modules:
        if slug not in effective_slugs:
            effective_slugs.append(slug)

    if not effective_slugs and not gpu.extra_packages:
        return template

    resolved = resolve_modules(effective_slugs) if effective_slugs else None

    # 3. Build the merged install phases. Stack-provided extra packages
    #    are appended to pacstrap so they're cached and installed
    #    alongside everything else.
    if resolved is not None:
        merged_install = resolved.merged_install()
    else:
        merged_install = InstallPhases()
    for pkg in gpu.extra_packages:
        if pkg not in merged_install.pacstrap:
            merged_install.pacstrap.append(pkg)

    return dataclasses.replace(
        template,
        install=merged_install,
        services=resolved.merged_services() if resolved else [],
        ansible=AnsibleConfig(
            firstboot_roles=resolved.ansible_roles if resolved else []
        ),
        graphical=resolved.graphical if resolved else False,
    )


def load_template(path: Path) -> InstallTemplate:
    """Load a single template from a TOML file."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return InstallTemplate.from_dict(data)


def _find_templates_dir() -> Path:
    """Locate the templates directory from the search path."""
    for d in _TEMPLATES_SEARCH:
        if d.is_dir():
            return d
    searched = ", ".join(str(d) for d in _TEMPLATES_SEARCH)
    raise FileNotFoundError(f"Templates directory not found (searched: {searched})")


def resolve_template(filename: str) -> Path:
    """Resolve a template filename to its full path in the templates directory.

    Accepts a bare filename like ``"kde-workstation.toml"`` and returns the
    absolute path.  Raises ``FileNotFoundError`` if the file does not exist.
    """
    templates_dir = _find_templates_dir()
    path = templates_dir / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"Template not found: {filename} (looked in {templates_dir})"
        )
    return path


def discover_templates() -> list[InstallTemplate]:
    """Discover all install templates (system templates only).

    Returns templates that define a system workload (have ``[meta]`` with a
    name and ``[modules]`` with an include list).  Config files like
    auto-install.toml and host-install.toml are excluded -- they reference
    templates by filename, not define them.

    Templates are returned with their modules resolved and merged, so
    downstream code sees fully-populated ``InstallTemplate`` objects.
    """
    templates_dir = _find_templates_dir()
    templates: list[InstallTemplate] = []

    for item in templates_dir.iterdir():
        if item.name.endswith(".toml"):
            try:
                tmpl = load_template(item)
                # Only include system templates (have a meaningful name
                # and module selections)
                if tmpl.name != "Unknown" and tmpl.module_slugs:
                    tmpl = resolve_and_merge_modules(tmpl)
                    templates.append(tmpl)
            except (KeyError, tomllib.TOMLDecodeError):
                pass  # Skip non-template TOML files (auto-install, etc.)

    return sorted(templates, key=lambda t: t.name)
