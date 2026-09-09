"""Resolve package policy without executing provider installation lifecycles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arches_installer.core.platform import (
    HardwareDetectionConfig,
    KernelConfig,
    PlatformConfig,
)
from arches_installer.core.template import InstallTemplate

_PROVIDERS_SEARCH = [
    Path("/opt/arches/providers"),
    Path(__file__).resolve().parents[3] / "providers",
]
_PLATFORMS_DIR = Path(__file__).resolve().parents[3] / "platforms"
STAGED_ROOT = Path("/opt/arches")


def _protected_path(path: Path) -> None:
    """Reject replaceable ancestors as well as symlinked metadata leaves."""
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError(f"Untrusted provider metadata path: {path}")
    for item in (path, *path.parents):
        if not item.exists():
            continue
        info = item.lstat()
        shared = item != path and info.st_mode & stat.S_ISVTX
        if info.st_uid not in (0, os.geteuid()) or (
            info.st_mode & 0o022 and not shared
        ):
            raise ValueError(f"Untrusted provider metadata path: {item}")


def resolve_install_provider(platform, explicit=None, configured=None):
    """M1: CLI > config > build identity; cache contents never select their pin."""
    for value in (explicit, configured):
        if value is not None:
            provider_id_for_platform(platform.arch, platform.name, value)
    path = STAGED_ROOT / "target-provider.json"
    metadata = None
    if path.exists() or path.is_symlink():
        _protected_path(path)
        try:
            metadata = json.loads(path.read_text())
            staged = metadata["provider"]
            if (
                metadata["platform"],
                metadata["architecture"],
                metadata["channel"],
            ) != (platform.name, platform.arch, "stable"):
                raise ValueError("Staged provider platform/channel mismatch")
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid staged provider metadata") from exc
    else:
        staged = resolve_provider(platform).id
    selected = (
        explicit
        if explicit is not None
        else configured
        if configured is not None
        else staged
    )
    provider = resolve_provider(platform, selected)
    if selected != staged:
        raise ValueError(
            f"Provider {selected} is not staged on this M1 media ({staged})"
        )
    inputs = None
    if metadata is not None:
        if (
            metadata.get("config_sha256")
            != hashlib.sha256(provider.pacman_conf.encode()).hexdigest()
        ):
            raise ValueError("Staged provider configuration mismatch")
        if "manifest_sha256" in metadata or "inputs" in metadata:
            pin = metadata.get("manifest_sha256", "")
            if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin):
                raise ValueError("Invalid staged manifest digest")
            relative = f"provider-inputs/{provider.id}/{pin}"
            if metadata.get("inputs") != relative:
                raise ValueError("Invalid staged provider input pointer")
            inputs = ProviderInputs(STAGED_ROOT / relative, pin)
    return provider, inputs


def read_provider_identity(root: Path) -> dict | None:
    """Read installed provenance, never a live ISO's runtime override."""
    path = root / "var/lib/arches/provider.json"
    _protected_path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        fields = {
            "provider",
            "platform",
            "template",
            "channel",
            "bootstrap_revision",
            "status",
        }
        if (
            not isinstance(data, dict)
            or set(data) != fields
            or any(not isinstance(v, str) or not v for v in data.values())
        ):
            raise ValueError("Malformed provider identity")
        if data["provider"] not in (
            "cachyos",
            "alarm",
            "asahi",
            "arch",
            "omarchy",
        ) or data["status"] not in ("installing", "failed", "complete"):
            raise ValueError("Invalid provider identity/status")
        return data
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Unreadable provider identity") from exc


def check_provider_identity(root: Path, identity: dict) -> None:
    previous = read_provider_identity(root)
    if previous is not None:
        raise ValueError(
            "Target has an existing provider identity; explicit recovery is required"
        )


def write_provider_identity(root: Path, identity: dict, status: str) -> None:
    """Only absent -> installing -> complete/failed; never resume or switch."""
    previous = read_provider_identity(root)
    data = identity | {"status": status}
    if set(identity) != {
        "provider",
        "platform",
        "template",
        "channel",
        "bootstrap_revision",
    } or any(not isinstance(v, str) or not v for v in identity.values()):
        raise ValueError("Invalid provider identity")
    if previous is None:
        if status != "installing":
            raise ValueError("Provider identity must start installing")
    elif previous != identity | {"status": "installing"} or status not in (
        "complete",
        "failed",
    ):
        raise ValueError("Conflicting provider identity or interrupted install status")
    path = root / "var/lib/arches/provider.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".provider-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def provider_id_for_platform(
    arch: str, name: str, provider_id: str | None = None
) -> str:
    """ARM repository identities are not aliases for stock x86 Arch."""
    if arch == "x86_64":
        default, supported = "cachyos", ("cachyos", "arch", "omarchy")
    elif arch == "aarch64":
        default = "asahi" if name == "aarch64-apple" else "alarm"
        supported = (default,)
    else:
        raise ValueError(f"Provider architecture unsupported: {arch}")
    selected = default if provider_id is None else provider_id
    if selected not in supported:
        raise ValueError(f"Provider {selected!r} unsupported on {name} ({arch})")
    return selected


def load_provider_data(provider_id: str) -> dict[str, Any]:
    """Read a known provider definition from staged media or the checkout."""
    if provider_id not in ("cachyos", "arch", "omarchy"):
        raise ValueError(f"Unknown provider: {provider_id!r}")
    for directory in _PROVIDERS_SEARCH:
        path = directory / provider_id / "provider.toml"
        if path.is_file():
            return tomllib.loads(path.read_text())
    raise FileNotFoundError(f"Provider definition not found: {provider_id}")


@dataclass
class ProviderConfig:
    id: str
    kernel: KernelConfig
    base_packages: list[str]
    hardware_detection: HardwareDetectionConfig
    cachyos_optimization_tier: str
    pacman_conf: str
    pacman_owner: str = "arches"
    lifecycle_owner: str = "arches"
    workloads: tuple[str, ...] = ()
    firmware: tuple[str, ...] = ("uefi", "bios")
    filesystems: tuple[str, ...] = ("btrfs", "ext4", "xfs")
    install_modes: tuple[str, ...] = ("fresh", "alongside", "replace")


@dataclass(frozen=True)
class ProviderInputs:
    """Selected artifacts, pinned by a trusted caller, never by the cache itself.

    Task 5 must authenticate provenance and bind this digest to the selected
    release before constructing this value. Hashes here check that binding;
    they do not establish a signing identity or repository freshness.
    """

    root: Path
    manifest_sha256: str


def validate_provider_inputs(
    provider: ProviderConfig, arch: str, inputs: ProviderInputs
) -> dict[str, str]:
    """Check identity and file integrity without changing a target or importing keys."""
    try:
        if (
            provider.id not in ("arch", "omarchy")
            or provider.pacman_conf
            != load_provider_data(provider.id)["repository"]["config"]
        ):
            raise ValueError(
                "Isolated inputs require the selected provider repository policy"
            )
        root = inputs.root
        if not root.is_absolute() or root.resolve() != root:
            raise ValueError("Provider inputs must use a real absolute directory")
        if not re.fullmatch(r"[0-9a-f]{64}", inputs.manifest_sha256):
            raise ValueError("An externally authenticated manifest digest is required")

        def digest(path: Path) -> str:
            for item in (path, *path.parents):
                info = item.lstat()
                # Sticky shared ancestors (e.g. /tmp) cannot replace owned children.
                shared_parent = (
                    item not in (root, path)
                    and root.is_relative_to(item)
                    and info.st_mode & stat.S_ISVTX
                )
                if (
                    stat.S_ISLNK(info.st_mode)
                    or info.st_uid not in (0, os.geteuid())
                    or (info.st_mode & 0o022 and not shared_parent)
                ):
                    raise ValueError(f"Untrusted provider input path: {item}")
            if not path.is_file():
                raise ValueError(f"Not a regular provider input: {path}")
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()

        manifest_path = root / "manifest.json"
        if digest(manifest_path) != inputs.manifest_sha256:
            raise ValueError("Provider manifest digest mismatch")
        data = json.loads(manifest_path.read_text())
        if not isinstance(data, dict):
            raise ValueError("Provider manifest must be an object")
        expected = (
            provider.id,
            "stable",
            arch,
            hashlib.sha256(provider.pacman_conf.encode()).hexdigest(),
        )
        actual = tuple(
            data.get(k)
            for k in ("provider", "channel", "architecture", "config_sha256")
        )
        if actual != expected:
            raise ValueError("Provider cache identity/configuration mismatch")
        files = data["files"]
        if not isinstance(files, dict):
            raise ValueError("Invalid provider file manifest")
        repos = re.findall(r"^\[([\w-]+)\]$", provider.pacman_conf, re.MULTILINE)
        required = {f"sync/{repo}.db" for repo in repos if repo != "options"}
        keyrings = (
            ("archlinux", "omarchy") if provider.id == "omarchy" else ("archlinux",)
        )
        required.update(
            f"keyrings/{name}{suffix}"
            for name in keyrings
            for suffix in (".gpg", "-trusted", "-revoked")
        )
        if "Include = /etc/pacman.d/mirrorlist" in provider.pacman_conf:
            required.add("mirrorlist")
        if not required <= files.keys():
            raise ValueError("Missing selected databases or bootstrap trust material")
        for name, checksum in files.items():
            if not re.fullmatch(
                r"(?:sync/[\w-]+\.db(?:\.sig)?|keyrings/[\w.-]+|packages/[\w.+:@-]+\.pkg\.tar\.(?:zst|xz|gz)(?:\.sig)?|mirrorlist|omarchy/(?:base\.packages|targets\.txt|node-v[0-9.]+-linux-x64\.tar\.gz))",
                name,
            ):
                raise ValueError(f"Invalid provider artifact path: {name}")
            path = root / name
            if digest(path) != checksum:
                raise ValueError(f"Provider artifact digest mismatch: {name}")
            if (
                name in required
                and not name.endswith("-revoked")
                and not path.stat().st_size
            ):
                raise ValueError(f"Empty required provider artifact: {name}")
        # Unlisted archives must not become implicit pacman cache candidates.
        if (root / "packages").exists():
            for path in (root / "packages").iterdir():
                if path.relative_to(root).as_posix() not in files:
                    raise ValueError(f"Unlisted provider cache artifact: {path.name}")
        return files
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid isolated provider inputs: {exc}") from exc


def resolve_provider(
    platform: PlatformConfig, provider_id: str | None = None
) -> ProviderConfig:
    """Resolve a sibling provider; preserve legacy overrides only for its default.

    Returned lists are independent of the compatibility PlatformConfig view.
    ARM retains its platform-required kernel, boot packages, and repositories.
    """
    default = provider_id_for_platform(
        platform.arch, platform.name, platform.provider_id
    )
    selected = provider_id_for_platform(
        platform.arch,
        platform.name,
        provider_id if provider_id is not None else default,
    )
    data = (
        load_provider_data(selected)
        if selected in ("arch", "omarchy") or selected != default
        else {}
    )
    policy = data.get("policy", {})
    source = platform if selected == default else PlatformConfig.from_dict(data)
    packages = list(dict.fromkeys(source.base_packages + platform.required_packages))
    _reject_cachyos_packages(
        selected,
        packages
        + [p for v in source.kernel.variants for p in (v.package, v.headers)]
        + [source.hardware_detection.tool],
    )
    conf = data.get("repository", {}).get("config", "")
    if not conf:
        # Existing staged configs and ARM repository ordering remain authoritative.
        profile = {
            "cachyos": "x86-64",
            "alarm": "aarch64-generic",
            "asahi": "aarch64-apple",
        }[selected]
        conf = platform.pacman_conf if selected == default else ""
        if not conf:
            conf = (_PLATFORMS_DIR / profile / "pacman.conf").read_text()
    return ProviderConfig(
        id=selected,
        kernel=deepcopy(source.kernel),
        base_packages=packages,
        hardware_detection=deepcopy(source.hardware_detection),
        cachyos_optimization_tier=source.cachyos_optimization_tier
        if selected == "cachyos"
        else "",
        pacman_conf=conf,
        **{
            key: tuple(value) if isinstance(value, list) else value
            for key, value in policy.items()
        },
    )


def _reject_cachyos_packages(provider_id: str, packages: list[str]) -> None:
    if provider_id != "cachyos" and any(
        "cachyos" in p or p == "chwd" for p in packages
    ):
        raise ValueError(
            f"Provider {provider_id} cannot use CachyOS packages or hardware tooling"
        )


def validate_selection(
    platform: PlatformConfig,
    provider: ProviderConfig,
    template: InstallTemplate,
    *,
    firmware: str,
    filesystem: str,
    install_mode: str,
) -> None:
    """Validate model compatibility, not disk layout or transaction readiness.

    ``fresh`` means a new target, not permission to auto-partition a platform.
    The installation preflight must separately check encryption, RAID, hardware,
    and the actual subvolume layout before invoking an Omarchy adapter.
    """
    from arches_installer.core.module import select_modules
    from arches_installer.core.template import resolve_and_merge_modules

    provider_id_for_platform(platform.arch, platform.name, provider.id)
    for label, value, supported in (
        ("firmware", firmware, provider.firmware),
        ("filesystem", filesystem, provider.filesystems),
        ("install mode", install_mode, provider.install_modes),
    ):
        if value not in supported:
            raise ValueError(f"Provider {provider.id}: unsupported {label} {value!r}")
    if firmware == "bios" and not platform.bootloader.supports_bios:
        raise ValueError(f"Platform {platform.name} does not support BIOS")
    if template.providers and provider.id not in template.providers:
        raise ValueError(
            f"Template {template.name} does not support provider {provider.id}"
        )
    if provider.workloads and template.workload not in provider.workloads:
        raise ValueError(
            f"Provider {provider.id}: unsupported workload {template.workload!r}"
        )
    if provider.id == "omarchy":
        if (
            template.module_slugs
            or template.gpu_stacks
            or template.install.all_packages
            or template.services
            or template.ansible.firstboot_roles
        ):
            raise ValueError(
                "Omarchy owns the workload; additional modules and overrides are unsupported"
            )
    # Validate the requested slugs before GPU arbitration can remove any of them.
    select_modules(template.module_slugs, provider_id=provider.id)
    resolved = resolve_and_merge_modules(template, provider_id=provider.id)
    if provider.id == "omarchy" and resolved.install.all_packages:
        raise ValueError(
            "Omarchy owns the workload; environment package overrides are unsupported"
        )
    _reject_cachyos_packages(
        provider.id, template.install.all_packages + resolved.install.all_packages
    )
