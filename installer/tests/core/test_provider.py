"""Provider policy is pure data resolution, not an installation."""

from dataclasses import replace
from pathlib import Path

import pytest

from arches_installer.core import platform as platform_module
from arches_installer.core.platform import load_platform
from arches_installer.core.template import InstallTemplate

ROOT = Path(__file__).resolve().parents[3]


def test_provider_inputs_require_external_manifest_pin(tmp_path):
    from arches_installer.core import provider as mod

    assert hasattr(mod, "ProviderInputs")
    selected = mod.resolve_provider(
        load_platform(ROOT / "platforms/x86-64/platform.toml"), "omarchy"
    )
    (tmp_path / "manifest.json").write_text('{"authenticated": true}')
    with pytest.raises(ValueError, match="manifest"):
        mod.validate_provider_inputs(
            selected, "x86_64", mod.ProviderInputs(tmp_path, "")
        )


def test_provider_manifest_must_be_an_object(tmp_path):
    import hashlib
    from arches_installer.core import provider as mod

    selected = mod.resolve_provider(
        load_platform(ROOT / "platforms/x86-64/platform.toml"), "omarchy"
    )
    data = b"[]"
    (tmp_path / "manifest.json").write_bytes(data)
    with pytest.raises(ValueError, match="manifest"):
        mod.validate_provider_inputs(
            selected,
            "x86_64",
            mod.ProviderInputs(tmp_path, hashlib.sha256(data).hexdigest()),
        )


X86_PACKAGES = [
    "base",
    "linux-firmware",
    "mkinitcpio",
    "sudo",
    "ansible",
    "terminus-font",
    "cachyos-keyring",
    "cachyos-mirrorlist",
    "cachyos-v3-mirrorlist",
    "cachyos-settings",
    "limine",
    "limine-mkinitcpio-hook",
    "efibootmgr",
    "limine-snapper-sync",
    "memtest86+-efi",
    "chwd",
]


def test_provider_interface_exists():
    # Assertion rather than an import error documents the initial missing model.
    assert (Path(platform_module.__file__).parent / "provider.py").is_file()


def test_default_x86_and_manual_fixture(x86_64_platform):
    from arches_installer.core.provider import resolve_provider

    platform = load_platform(ROOT / "platforms/x86-64/platform.toml")
    provider = resolve_provider(platform)
    assert provider.id == "cachyos"
    assert provider.kernel == platform.kernel
    assert [v.package for v in provider.kernel.variants] == [
        "linux-cachyos",
        "linux-cachyos-lts",
    ]
    assert provider.base_packages == platform.base_packages == X86_PACKAGES
    assert provider.cachyos_optimization_tier == "x86-64-v3"
    assert platform.pacman_architectures == ["x86_64", "x86_64_v2", "x86_64_v3"]
    assert platform.cachyos_mirrorlist_name == "cachyos-v3-mirrorlist"
    assert provider.hardware_detection == platform.hardware_detection
    assert provider.hardware_detection.tool == "chwd"
    assert provider.hardware_detection.optional
    assert provider.pacman_conf == (ROOT / "platforms/x86-64/pacman.conf").read_text()
    assert (
        resolve_provider(x86_64_platform).base_packages == x86_64_platform.base_packages
    )


@pytest.mark.parametrize("provider_id", ["arch", "omarchy"])
def test_alternatives_are_siblings_not_overlays(provider_id):
    from arches_installer.core.provider import resolve_provider

    platform = load_platform(ROOT / "platforms/x86-64/platform.toml")
    provider = resolve_provider(platform, provider_id)
    packages = provider.base_packages + [v.package for v in provider.kernel.variants]
    assert not any("cachyos" in p or p == "chwd" for p in packages)
    assert provider.kernel.package == "linux"
    assert "linux-firmware" in packages
    assert not provider.hardware_detection.enabled
    assert provider.cachyos_optimization_tier == ""
    assert "cachyos" not in provider.pacman_conf
    assert "arches-local" not in provider.pacman_conf
    assert "TrustAll" not in provider.pacman_conf
    assert "LocalFileSigLevel = Required" in provider.pacman_conf
    assert platform.base_packages == X86_PACKAGES  # resolution does not mutate callers


@pytest.mark.parametrize(
    "name,provider_id,kernel",
    [
        ("aarch64-generic", "alarm", "linux-aarch64"),
        ("aarch64-apple", "asahi", "linux-asahi"),
    ],
)
def test_arm_defaults_and_repositories(name, provider_id, kernel):
    from arches_installer.core.provider import resolve_provider

    platform = load_platform(ROOT / "platforms" / name / "platform.toml")
    provider = resolve_provider(platform)
    assert provider.id == provider_id
    assert provider.kernel.package == kernel
    assert provider.base_packages == platform.base_packages
    assert (
        provider.pacman_conf == (ROOT / "platforms" / name / "pacman.conf").read_text()
    )
    assert platform.bootloader.type == "grub"
    assert not platform.bootloader.supports_bios
    assert not provider.hardware_detection.enabled
    assert provider.cachyos_optimization_tier == ""
    if provider_id == "asahi":
        assert not platform.allow_auto_install
        assert not platform.bootloader.snapshot_boot
        assert {"asahi-fwextract", "asahi-scripts", "m1n1", "uboot-asahi"} <= set(
            provider.base_packages
        )
    for alternative in ["cachyos", "arch", "omarchy"]:
        with pytest.raises(ValueError, match="unsupported"):
            resolve_provider(platform, alternative)


@pytest.mark.parametrize("provider_id", ["unknown", "", "../arch", "alarm", "asahi"])
def test_reject_invalid_x86_provider(x86_64_platform, provider_id):
    from arches_installer.core.provider import resolve_provider

    with pytest.raises(ValueError):
        resolve_provider(x86_64_platform, provider_id)


def test_omarchy_ownership_and_proven_selection(x86_64_platform):
    from arches_installer.core.provider import resolve_provider, validate_selection

    provider = resolve_provider(x86_64_platform, "omarchy")
    assert provider.pacman_owner == provider.lifecycle_owner == "omarchy"
    assert "https://stable-mirror.omarchy.org/$repo/os/$arch" in provider.pacman_conf
    assert "https://pkgs.omarchy.org/stable/$arch" in provider.pacman_conf
    template = InstallTemplate.from_dict(
        {"meta": {"workload": "workstation", "providers": ["omarchy"]}}
    )
    validate_selection(
        x86_64_platform,
        provider,
        template,
        firmware="uefi",
        filesystem="btrfs",
        install_mode="fresh",
    )
    for change in [
        {"firmware": "bios"},
        {"filesystem": "ext4"},
        {"install_mode": "alongside"},
        {"install_mode": "replace"},
        {"install_mode": "unknown"},
    ]:
        args = dict(firmware="uefi", filesystem="btrfs", install_mode="fresh") | change
        with pytest.raises(ValueError):
            validate_selection(x86_64_platform, provider, template, **args)
    for incompatible in [
        replace(template, workload="server"),
        replace(template, module_slugs=["base"]),
        replace(template, gpu_stacks=["amd-vulkan"]),
    ]:
        with pytest.raises(ValueError):
            validate_selection(
                x86_64_platform,
                provider,
                incompatible,
                firmware="uefi",
                filesystem="btrfs",
                install_mode="fresh",
            )


def test_selection_validates_unknown_and_unapproved_modules(x86_64_platform):
    from arches_installer.core.module import ModuleError
    from arches_installer.core.provider import resolve_provider, validate_selection

    for slug in ["not-a-module", "hardware-rescan", "base"]:
        template = InstallTemplate.from_dict({"modules": {"include": [slug]}})
        with pytest.raises((ValueError, ModuleError)):
            validate_selection(
                x86_64_platform,
                resolve_provider(x86_64_platform, "arch"),
                template,
                firmware="uefi",
                filesystem="btrfs",
                install_mode="fresh",
            )


def test_legacy_staged_default_needs_no_provider_definitions(tmp_path, monkeypatch):
    from arches_installer.core import provider as providers

    config = tmp_path / "platform.toml"
    config.write_text(
        '[platform]\nname = "x86-64"\n[kernel]\npackage = "linux-custom"\n'
    )
    (tmp_path / "pacman.conf").write_text("legacy-repo-config")
    monkeypatch.setattr(providers, "_PROVIDERS_SEARCH", [])
    provider = providers.resolve_provider(load_platform(config))
    assert provider.kernel.package == "linux-custom"
    assert provider.pacman_conf == "legacy-repo-config"


@pytest.mark.parametrize("provider_id", ["arch", "omarchy"])
def test_alternative_rejects_contaminated_platform_overrides(provider_id):
    from arches_installer.core.provider import resolve_provider

    platform = platform_module.PlatformConfig.from_dict(
        {
            "provider": {"id": provider_id},
            "base_packages": {"install": ["chwd"]},
        }
    )
    with pytest.raises(ValueError, match="CachyOS"):
        resolve_provider(platform)


def test_arch_rejects_direct_cachyos_template_packages(x86_64_platform):
    from arches_installer.core.provider import resolve_provider, validate_selection

    template = InstallTemplate.from_dict({})
    template.install.pacstrap = ["cachyos-settings"]
    with pytest.raises(ValueError, match="CachyOS"):
        validate_selection(
            x86_64_platform,
            resolve_provider(x86_64_platform, "arch"),
            template,
            firmware="uefi",
            filesystem="btrfs",
            install_mode="fresh",
        )


def test_default_selection_preserves_shipped_workloads(monkeypatch):
    from arches_installer.core.provider import resolve_provider, validate_selection
    from arches_installer.core.template import load_template

    monkeypatch.delenv("ARCHES_GPU", raising=False)
    platform = load_platform(ROOT / "platforms/x86-64/platform.toml")
    for name in ["kde-workstation", "cosmic-workstation", "vm-server", "llm-inference"]:
        validate_selection(
            platform,
            resolve_provider(platform),
            load_template(ROOT / "templates" / f"{name}.toml"),
            firmware="uefi",
            filesystem="btrfs",
            install_mode="fresh",
        )


def test_switching_back_to_cachyos_does_not_keep_alternative_repositories():
    from arches_installer.core.provider import resolve_provider

    platform = platform_module.PlatformConfig.from_dict({"provider": {"id": "arch"}})
    platform.pacman_conf = "stock-arch-config"
    assert (
        resolve_provider(platform, "cachyos").pacman_conf
        == (ROOT / "platforms/x86-64/pacman.conf").read_text()
    )


@pytest.mark.parametrize(
    "provider_id,repos",
    [
        ("arch", ["core", "extra", "multilib"]),
        ("omarchy", ["core", "extra", "multilib", "omarchy"]),
    ],
)
def test_generated_repository_config(provider_id, repos, x86_64_platform):
    from configparser import ConfigParser
    from arches_installer.core.provider import resolve_provider

    config = ConfigParser(interpolation=None)
    config.read_string(resolve_provider(x86_64_platform, provider_id).pacman_conf)
    assert config.sections() == ["options", *repos]
    assert config["options"]["SigLevel"] == "Required DatabaseOptional"
    assert config["options"]["RemoteFileSigLevel"] == "Required"
    assert all("SigLevel" not in config[repo] for repo in repos)


def test_default_rejects_unknown_modes_and_arm_bios(aarch64_platform):
    from arches_installer.core.provider import resolve_provider, validate_selection

    provider = resolve_provider(aarch64_platform)
    for args in [
        dict(firmware="uefi", filesystem="btrfs", install_mode="typo"),
        dict(firmware="bios", filesystem="btrfs", install_mode="fresh"),
    ]:
        with pytest.raises(ValueError):
            validate_selection(
                aarch64_platform, provider, InstallTemplate.from_dict({}), **args
            )


@pytest.mark.parametrize("stacks", [["amd-rocm"], ["amd-rocm", "amd-vulkan"]])
def test_selection_defers_dependencies_until_gpu_resolution(
    stacks, x86_64_platform, monkeypatch
):
    from arches_installer.core.provider import resolve_provider, validate_selection
    from arches_installer.core.template import resolve_and_merge_modules

    monkeypatch.delenv("ARCHES_GPU", raising=False)
    template = InstallTemplate.from_dict(
        {
            "modules": {"include": ["base", "networking", "ollama", "llama-cpp-hip"]},
            "gpu": {"stacks": stacks},
        }
    )
    assert resolve_and_merge_modules(template).install.pacstrap
    validate_selection(
        x86_64_platform,
        resolve_provider(x86_64_platform),
        template,
        firmware="uefi",
        filesystem="btrfs",
        install_mode="fresh",
    )


@pytest.mark.parametrize("rejection", ["unknown", "unapproved"])
def test_arbitration_cannot_hide_invalid_explicit_modules(
    rejection, x86_64_platform, monkeypatch
):
    from arches_installer.core import module
    from arches_installer.core.provider import resolve_provider, validate_selection
    from arches_installer.core.template import resolve_and_merge_modules

    monkeypatch.delenv("ARCHES_GPU", raising=False)
    available = module.discover_modules()
    if rejection == "unknown":
        available = [m for m in available if m.slug != "llama-cpp-hip"]
    else:
        next(m for m in available if m.slug == "llama-cpp-hip").providers = ["arch"]
    monkeypatch.setattr(module, "discover_modules", lambda: available)
    template = InstallTemplate.from_dict(
        {
            "modules": {"include": ["base", "networking", "ollama", "llama-cpp-hip"]},
            "gpu": {"stacks": ["amd-rocm", "amd-vulkan"]},
        }
    )
    # HIP disappears from the effective selection, but its explicit request must fail.
    assert resolve_and_merge_modules(template, provider_id="cachyos").install.pacstrap
    with pytest.raises(module.ModuleError, match="llama-cpp-hip"):
        validate_selection(
            x86_64_platform,
            resolve_provider(x86_64_platform),
            template,
            firmware="uefi",
            filesystem="btrfs",
            install_mode="fresh",
        )
