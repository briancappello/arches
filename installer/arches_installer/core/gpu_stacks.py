"""GPU compute stack resolution.

A "GPU stack" is a named combination of hardware vendor + compute API
(e.g. ``amd-vulkan``, ``amd-rocm``, ``intel-vulkan``, ``nvidia-cuda``).

Each template declares default stacks in its ``[gpu].stacks`` array.
The build-time env var ``ARCHES_GPU=<comma-separated-list>`` overrides
the template default so the same workload template can produce
ISOs for different GPU configurations.

The stack-to-modules mapping lives in ``scripts/gpu-stacks.toml`` so it
can be extended without code changes (a new vendor or API just needs
a new top-level table in that file).

Arbitration rules:
  - ``llama-cpp`` and ``llama-cpp-hip`` are mutually exclusive
    (both ``provides=llama.cpp``). When multiple stacks request
    different ``llama_cpp_variant`` values, **Vulkan wins** — see
    :func:`_arbitrate_llama_cpp`.
  - Modules and packages from multiple stacks are merged (deduped by
    name). Order is preserved from the stacks list for any other
    ordering-sensitive consumers.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Search paths for gpu-stacks.toml. Live ISO stages it at
# /opt/arches/scripts; in development it's at <repo>/scripts.
_STACKS_SEARCH = [
    Path("/opt/arches/scripts/gpu-stacks.toml"),
    Path(__file__).resolve().parents[3] / "scripts" / "gpu-stacks.toml",
]


@dataclass
class GpuStack:
    """One named compute-stack definition loaded from gpu-stacks.toml."""

    name: str
    description: str = ""
    modules: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    # Which llama.cpp variant this stack pulls. Empty string = none.
    # Currently recognized: "vulkan", "hip" (and conceptually "cuda"
    # once that variant exists upstream).
    llama_cpp_variant: str = ""


@dataclass
class ResolvedGpuStacks:
    """The effective stack set for one build, after env override and
    arbitration."""

    stacks: list[GpuStack] = field(default_factory=list)
    # Aggregated extras to merge into the template's [modules] + pacstrap.
    extra_modules: list[str] = field(default_factory=list)
    extra_packages: list[str] = field(default_factory=list)
    # Module slugs that should be REMOVED from the merge result because
    # they conflict with a higher-priority stack's choice (e.g. removing
    # `llama-cpp-hip` when Vulkan won arbitration).
    excluded_modules: list[str] = field(default_factory=list)


def _find_stacks_toml() -> Path:
    """Locate gpu-stacks.toml from the search path."""
    for p in _STACKS_SEARCH:
        if p.is_file():
            return p
    searched = ", ".join(str(p) for p in _STACKS_SEARCH)
    raise FileNotFoundError(
        f"gpu-stacks.toml not found (searched: {searched}). "
        "This file ships in scripts/ and is staged into the ISO at "
        "/opt/arches/scripts/."
    )


def load_stacks(path: Path | None = None) -> dict[str, GpuStack]:
    """Load all stack definitions from gpu-stacks.toml.

    Returns a dict keyed by stack name. Each top-level table in the
    TOML file maps to one GpuStack.
    """
    if path is None:
        path = _find_stacks_toml()
    with open(path, "rb") as f:
        data = tomllib.load(f)

    stacks: dict[str, GpuStack] = {}
    for name, spec in data.items():
        if not isinstance(spec, dict):
            continue  # ignore non-table top-level entries
        stacks[name] = GpuStack(
            name=name,
            description=spec.get("description", ""),
            modules=list(spec.get("modules", [])),
            packages=list(spec.get("packages", [])),
            llama_cpp_variant=spec.get("llama_cpp_variant", ""),
        )
    return stacks


def _env_override(default: list[str]) -> list[str]:
    """Apply the ARCHES_GPU env var if set.

    ARCHES_GPU is a comma- or whitespace-separated list of stack names.
    Empty string or unset means "use the template default".
    """
    raw = os.environ.get("ARCHES_GPU", "").strip()
    if not raw:
        return default
    # Accept comma OR whitespace separators for ergonomics.
    parts: list[str] = []
    for chunk in raw.replace(",", " ").split():
        chunk = chunk.strip()
        if chunk and chunk not in parts:
            parts.append(chunk)
    return parts


def _arbitrate_llama_cpp(
    stacks: list[GpuStack],
) -> tuple[str, list[str]]:
    """Decide which llama.cpp variant wins and which module to exclude.

    Returns ``(winning_variant, modules_to_exclude)``. The Vulkan-wins
    rule means: if any stack's variant is ``"vulkan"``, the Vulkan
    module is used and any HIP/CUDA-variant module from another stack
    is excluded. Otherwise the first non-empty variant wins.

    The function operates on stack names rather than packages, so the
    excluded modules can be filtered out at module-resolution time
    before pacman ever sees the conflict.
    """
    variants = [s.llama_cpp_variant for s in stacks if s.llama_cpp_variant]
    if not variants:
        return ("", [])

    # Vulkan-wins arbitration
    if "vulkan" in variants:
        winner = "vulkan"
    else:
        winner = variants[0]  # First-declared wins among non-vulkan

    # Build the exclusion list: any llama-cpp-* module from a stack
    # whose variant != winner.
    exclude: list[str] = []
    variant_to_module = {
        "vulkan": "llama-cpp",
        "hip": "llama-cpp-hip",
        "cuda": "llama-cpp-cuda",
    }
    winning_module = variant_to_module.get(winner, "")
    for s in stacks:
        if not s.llama_cpp_variant:
            continue
        if s.llama_cpp_variant == winner:
            continue
        losing_module = variant_to_module.get(s.llama_cpp_variant, "")
        if losing_module and losing_module not in exclude:
            exclude.append(losing_module)

    return (winning_module, exclude)


def resolve_gpu_stacks(
    template_stacks: list[str],
    available: dict[str, GpuStack] | None = None,
) -> ResolvedGpuStacks:
    """Resolve the effective stack set for a build.

    Apply the ARCHES_GPU env override to ``template_stacks``, look up
    each stack in ``available``, run llama.cpp arbitration, and return
    the merged module/package lists plus an exclusion list.

    Parameters
    ----------
    template_stacks:
        Default stack names declared in the template's ``[gpu].stacks``.
    available:
        Loaded stack definitions. If None, loads from gpu-stacks.toml.

    Raises
    ------
    ValueError
        If a stack name (from template or env) doesn't exist in
        ``available``. Catches typos at build time instead of silently
        producing an ISO without the expected GPU support.
    """
    if available is None:
        available = load_stacks()

    requested = _env_override(template_stacks)

    if not requested:
        # No stacks requested AND no env override — return empty result.
        # Templates without a [gpu] section get this; they don't pull
        # any GPU-related modules/packages.
        return ResolvedGpuStacks()

    chosen: list[GpuStack] = []
    for name in requested:
        if name not in available:
            known = ", ".join(sorted(available)) or "(none)"
            raise ValueError(
                f"Unknown GPU stack {name!r}. Known stacks: {known}. "
                "Check scripts/gpu-stacks.toml or your ARCHES_GPU value."
            )
        chosen.append(available[name])

    # Merge modules and packages from each chosen stack (deduped).
    extra_modules: list[str] = []
    extra_packages: list[str] = []
    for s in chosen:
        for m in s.modules:
            if m not in extra_modules:
                extra_modules.append(m)
        for p in s.packages:
            if p not in extra_packages:
                extra_packages.append(p)

    # Arbitrate llama.cpp variants.
    _winner, excluded = _arbitrate_llama_cpp(chosen)
    # Remove excluded modules from the merged list.
    extra_modules = [m for m in extra_modules if m not in excluded]

    return ResolvedGpuStacks(
        stacks=chosen,
        extra_modules=extra_modules,
        extra_packages=extra_packages,
        excluded_modules=excluded,
    )
