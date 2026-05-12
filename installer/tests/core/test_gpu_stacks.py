"""Tests for GPU stack resolution.

Covers:
  - Loading scripts/gpu-stacks.toml
  - Default stack list from a template's [gpu].stacks
  - ARCHES_GPU env var override (with comma/whitespace separators)
  - Vulkan-wins arbitration for mutually-exclusive llama.cpp variants
  - Module-list merging into the template's [modules].include
  - Error on unknown stack names
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from arches_installer.core.gpu_stacks import (
    GpuStack,
    _arbitrate_llama_cpp,
    load_stacks,
    resolve_gpu_stacks,
)


_REPO_ROOT = Path(__file__).resolve().parents[3]
_STACKS_TOML = _REPO_ROOT / "scripts" / "gpu-stacks.toml"


# ---------------------------------------------------------------------------
# Loading the shipped gpu-stacks.toml
# ---------------------------------------------------------------------------


class TestLoadStacks:
    def test_load_shipped_stacks(self) -> None:
        """The shipped scripts/gpu-stacks.toml loads and parses cleanly."""
        stacks = load_stacks(_STACKS_TOML)
        # Sanity-check the canonical stacks ship.
        assert "amd-vulkan" in stacks
        assert "amd-rocm" in stacks
        assert "intel-vulkan" in stacks
        assert "nvidia-cuda" in stacks

    def test_stack_has_required_fields(self) -> None:
        stacks = load_stacks(_STACKS_TOML)
        amd = stacks["amd-vulkan"]
        assert amd.name == "amd-vulkan"
        assert amd.description
        assert "llama-cpp" in amd.modules
        assert "vulkan-radeon" in amd.packages
        assert amd.llama_cpp_variant == "vulkan"

    def test_amd_rocm_pulls_hip_variant(self) -> None:
        stacks = load_stacks(_STACKS_TOML)
        rocm = stacks["amd-rocm"]
        assert rocm.llama_cpp_variant == "hip"
        assert "rocm" in rocm.modules
        assert "ollama-rocm" in rocm.modules
        assert "llama-cpp-hip" in rocm.modules

    def test_nvidia_cuda_modules(self) -> None:
        stacks = load_stacks(_STACKS_TOML)
        nv = stacks["nvidia-cuda"]
        assert "cuda" in nv.modules
        assert "ollama-cuda" in nv.modules


# ---------------------------------------------------------------------------
# llama.cpp arbitration
# ---------------------------------------------------------------------------


class TestArbitration:
    def test_vulkan_wins_over_hip(self) -> None:
        stacks = [
            GpuStack(name="amd-vulkan", llama_cpp_variant="vulkan"),
            GpuStack(name="amd-rocm", llama_cpp_variant="hip"),
        ]
        winner, excluded = _arbitrate_llama_cpp(stacks)
        assert winner == "llama-cpp"
        assert "llama-cpp-hip" in excluded

    def test_vulkan_wins_even_when_hip_listed_first(self) -> None:
        """Order shouldn't matter — Vulkan always wins."""
        stacks = [
            GpuStack(name="amd-rocm", llama_cpp_variant="hip"),
            GpuStack(name="amd-vulkan", llama_cpp_variant="vulkan"),
        ]
        winner, excluded = _arbitrate_llama_cpp(stacks)
        assert winner == "llama-cpp"
        assert "llama-cpp-hip" in excluded

    def test_single_stack_no_arbitration(self) -> None:
        stacks = [GpuStack(name="amd-vulkan", llama_cpp_variant="vulkan")]
        winner, excluded = _arbitrate_llama_cpp(stacks)
        assert winner == "llama-cpp"
        assert excluded == []

    def test_no_llama_cpp_variants(self) -> None:
        stacks = [
            GpuStack(name="nvidia-cuda", llama_cpp_variant=""),
        ]
        winner, excluded = _arbitrate_llama_cpp(stacks)
        assert winner == ""
        assert excluded == []

    def test_hip_only_no_exclusion(self) -> None:
        """If only HIP is requested, HIP wins (no Vulkan to override)."""
        stacks = [GpuStack(name="amd-rocm", llama_cpp_variant="hip")]
        winner, excluded = _arbitrate_llama_cpp(stacks)
        assert winner == "llama-cpp-hip"
        assert excluded == []

    def test_cuda_only_picks_cuda(self) -> None:
        """If only CUDA is requested, the CUDA variant wins."""
        stacks = [GpuStack(name="nvidia-cuda", llama_cpp_variant="cuda")]
        winner, excluded = _arbitrate_llama_cpp(stacks)
        assert winner == "llama-cpp-cuda"
        assert excluded == []

    def test_vulkan_wins_over_cuda(self) -> None:
        """Vulkan beats CUDA too, not just HIP."""
        stacks = [
            GpuStack(name="amd-vulkan", llama_cpp_variant="vulkan"),
            GpuStack(name="nvidia-cuda", llama_cpp_variant="cuda"),
        ]
        winner, excluded = _arbitrate_llama_cpp(stacks)
        assert winner == "llama-cpp"
        assert "llama-cpp-cuda" in excluded

    def test_vulkan_wins_over_both_hip_and_cuda(self) -> None:
        """Multi-vendor build: Vulkan trumps every other variant."""
        stacks = [
            GpuStack(name="amd-rocm", llama_cpp_variant="hip"),
            GpuStack(name="amd-vulkan", llama_cpp_variant="vulkan"),
            GpuStack(name="nvidia-cuda", llama_cpp_variant="cuda"),
        ]
        winner, excluded = _arbitrate_llama_cpp(stacks)
        assert winner == "llama-cpp"
        assert "llama-cpp-hip" in excluded
        assert "llama-cpp-cuda" in excluded

    def test_hip_and_cuda_no_vulkan(self) -> None:
        """No Vulkan → first non-Vulkan variant wins. Stack order matters."""
        stacks_hip_first = [
            GpuStack(name="amd-rocm", llama_cpp_variant="hip"),
            GpuStack(name="nvidia-cuda", llama_cpp_variant="cuda"),
        ]
        winner, excluded = _arbitrate_llama_cpp(stacks_hip_first)
        assert winner == "llama-cpp-hip"
        assert "llama-cpp-cuda" in excluded

        stacks_cuda_first = [
            GpuStack(name="nvidia-cuda", llama_cpp_variant="cuda"),
            GpuStack(name="amd-rocm", llama_cpp_variant="hip"),
        ]
        winner, excluded = _arbitrate_llama_cpp(stacks_cuda_first)
        assert winner == "llama-cpp-cuda"
        assert "llama-cpp-hip" in excluded


# ---------------------------------------------------------------------------
# resolve_gpu_stacks — template default + env override
# ---------------------------------------------------------------------------


class TestResolveGpuStacks:
    def test_template_default_amd_vulkan(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARCHES_GPU", None)
            result = resolve_gpu_stacks(["amd-vulkan"])
        assert len(result.stacks) == 1
        assert result.stacks[0].name == "amd-vulkan"
        assert "llama-cpp" in result.extra_modules
        assert "vulkan-radeon" in result.extra_packages

    def test_env_override_replaces_default(self) -> None:
        with patch.dict(os.environ, {"ARCHES_GPU": "nvidia-cuda"}):
            result = resolve_gpu_stacks(["amd-vulkan"])
        # amd-vulkan defaults are ignored; only nvidia-cuda applied.
        assert len(result.stacks) == 1
        assert result.stacks[0].name == "nvidia-cuda"
        assert "cuda" in result.extra_modules
        assert "ollama-cuda" in result.extra_modules
        # amd-vulkan's modules NOT present
        assert "vulkan-radeon" not in result.extra_packages

    def test_env_override_comma_separated(self) -> None:
        with patch.dict(os.environ, {"ARCHES_GPU": "amd-vulkan,nvidia-cuda"}):
            result = resolve_gpu_stacks(["amd-rocm"])
        assert len(result.stacks) == 2
        names = [s.name for s in result.stacks]
        assert names == ["amd-vulkan", "nvidia-cuda"]

    def test_env_override_whitespace_separated(self) -> None:
        with patch.dict(os.environ, {"ARCHES_GPU": "amd-vulkan intel-vulkan"}):
            result = resolve_gpu_stacks([])
        names = [s.name for s in result.stacks]
        assert names == ["amd-vulkan", "intel-vulkan"]

    def test_env_override_mixed_separators(self) -> None:
        with patch.dict(
            os.environ, {"ARCHES_GPU": "amd-vulkan, intel-vulkan ,nvidia-cuda"}
        ):
            result = resolve_gpu_stacks([])
        names = [s.name for s in result.stacks]
        assert names == ["amd-vulkan", "intel-vulkan", "nvidia-cuda"]

    def test_empty_template_no_env_returns_empty(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARCHES_GPU", None)
            result = resolve_gpu_stacks([])
        assert result.stacks == []
        assert result.extra_modules == []
        assert result.extra_packages == []

    def test_unknown_stack_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown GPU stack"):
            resolve_gpu_stacks(["bogus-stack"])

    def test_unknown_stack_via_env_raises(self) -> None:
        with patch.dict(os.environ, {"ARCHES_GPU": "amd-typo"}):
            with pytest.raises(ValueError, match="Unknown GPU stack"):
                resolve_gpu_stacks(["amd-vulkan"])

    def test_vulkan_wins_excludes_hip_module(self) -> None:
        """When user asks for both amd-vulkan AND amd-rocm, the result
        should include rocm + ollama-rocm modules but exclude
        llama-cpp-hip (Vulkan wins) and include llama-cpp instead."""
        with patch.dict(os.environ, {"ARCHES_GPU": "amd-vulkan,amd-rocm"}):
            result = resolve_gpu_stacks([])
        assert "llama-cpp" in result.extra_modules
        assert "llama-cpp-hip" in result.excluded_modules
        assert "llama-cpp-hip" not in result.extra_modules
        # Other amd-rocm bits still get included
        assert "rocm" in result.extra_modules
        assert "ollama-rocm" in result.extra_modules

    def test_dedupe_across_stacks(self) -> None:
        """amd-vulkan and intel-vulkan both pull mesa + vulkan-icd-loader.
        The merged package list should deduplicate."""
        with patch.dict(os.environ, {"ARCHES_GPU": "amd-vulkan,intel-vulkan"}):
            result = resolve_gpu_stacks([])
        assert result.extra_packages.count("mesa") == 1
        assert result.extra_packages.count("vulkan-icd-loader") == 1
        # Both Vulkan stacks share the same llama-cpp module (variant=vulkan)
        assert result.extra_modules.count("llama-cpp") == 1


# ---------------------------------------------------------------------------
# Integration with template loading
# ---------------------------------------------------------------------------


class TestTemplateGpuField:
    """The InstallTemplate dataclass picks up [gpu].stacks from TOML."""

    def test_llm_inference_default_stacks(self) -> None:
        from arches_installer.core.template import load_template

        tmpl = load_template(_REPO_ROOT / "templates" / "llm-inference.toml")
        assert tmpl.gpu_stacks == ["amd-vulkan"]

    def test_template_without_gpu_section(self) -> None:
        """Templates without [gpu].stacks default to empty list — no
        GPU modules pulled."""
        from arches_installer.core.template import load_template

        # vm-server has no [gpu] section
        tmpl = load_template(_REPO_ROOT / "templates" / "vm-server.toml")
        assert tmpl.gpu_stacks == []


# ---------------------------------------------------------------------------
# End-to-end: template resolution with GPU stacks
# ---------------------------------------------------------------------------


class TestResolveAndMergeWithGpu:
    """resolve_and_merge_modules pulls stack modules into the install."""

    def test_amd_vulkan_default_pulls_llama_cpp(self) -> None:
        from arches_installer.core.template import (
            load_template,
            resolve_and_merge_modules,
        )

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARCHES_GPU", None)
            tmpl = load_template(_REPO_ROOT / "templates" / "llm-inference.toml")
            resolved = resolve_and_merge_modules(tmpl)
        # llama-cpp (Vulkan) module pulled in by amd-vulkan stack
        assert "llama.cpp-vulkan-arches" in resolved.install.pacstrap
        # vulkan-radeon pulled in as an extra package
        assert "vulkan-radeon" in resolved.install.pacstrap
        # cuda NOT pulled in (only amd-vulkan stack active)
        assert "cuda" not in resolved.install.pacstrap
        assert "nvidia-utils" not in resolved.install.pacstrap

    def test_env_override_to_nvidia(self) -> None:
        from arches_installer.core.template import (
            load_template,
            resolve_and_merge_modules,
        )

        with patch.dict(os.environ, {"ARCHES_GPU": "nvidia-cuda"}):
            tmpl = load_template(_REPO_ROOT / "templates" / "llm-inference.toml")
            resolved = resolve_and_merge_modules(tmpl)
        # cuda pulled in by nvidia-cuda stack
        assert "cuda" in resolved.install.pacstrap
        assert "nvidia-utils" in resolved.install.pacstrap
        # ollama-cuda module's package pulled in
        assert "ollama-cuda" in resolved.install.pacstrap
        # CUDA llama.cpp variant pulled in (no Vulkan to override)
        assert "llama.cpp-cuda-arches" in resolved.install.pacstrap
        # AMD/Vulkan bits NOT pulled in
        assert "llama.cpp-vulkan-arches" not in resolved.install.pacstrap
        assert "llama.cpp-hip-arches" not in resolved.install.pacstrap
        assert "vulkan-radeon" not in resolved.install.pacstrap
        assert "rocm-hip-runtime" not in resolved.install.pacstrap

    def test_vulkan_wins_over_cuda_in_full_resolve(self) -> None:
        """Multi-vendor build: amd-vulkan,nvidia-cuda → llama.cpp Vulkan,
        not CUDA (Vulkan-wins arbitration). But cuda toolkit + ollama-cuda
        still install for other consumers."""
        from arches_installer.core.template import (
            load_template,
            resolve_and_merge_modules,
        )

        with patch.dict(os.environ, {"ARCHES_GPU": "amd-vulkan,nvidia-cuda"}):
            tmpl = load_template(_REPO_ROOT / "templates" / "llm-inference.toml")
            resolved = resolve_and_merge_modules(tmpl)
        # Vulkan llama.cpp wins
        assert "llama.cpp-vulkan-arches" in resolved.install.pacstrap
        assert "llama.cpp-cuda-arches" not in resolved.install.pacstrap
        # CUDA userspace + ollama-cuda still install
        assert "cuda" in resolved.install.pacstrap
        assert "nvidia-utils" in resolved.install.pacstrap
        assert "ollama-cuda" in resolved.install.pacstrap
        # AMD Vulkan ICD installs
        assert "vulkan-radeon" in resolved.install.pacstrap

    def test_amd_rocm_override(self) -> None:
        from arches_installer.core.template import (
            load_template,
            resolve_and_merge_modules,
        )

        with patch.dict(os.environ, {"ARCHES_GPU": "amd-rocm"}):
            tmpl = load_template(_REPO_ROOT / "templates" / "llm-inference.toml")
            resolved = resolve_and_merge_modules(tmpl)
        # rocm userspace
        assert "rocm-hip-runtime" in resolved.install.pacstrap
        # ollama-rocm
        assert "ollama-rocm" in resolved.install.pacstrap
        # HIP llama.cpp variant (no Vulkan to override it)
        assert "llama.cpp-hip-arches" in resolved.install.pacstrap
        # CUDA NOT pulled in
        assert "cuda" not in resolved.install.pacstrap
