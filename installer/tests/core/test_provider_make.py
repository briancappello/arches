"""Run the existing build recipes against disposable output, without a build."""

from pathlib import Path
import shutil

import pytest
from tests.core.test_provider_build import run, tree as tree

ROOT = Path(__file__).resolve().parents[3]


def run_make(target, output, platform="x86-64", platforms=None):
    return run(
        output.parent,
        [
            "make",
            "--no-print-directory",
            "-s",
            target,
            f"PLATFORM={platform}",
            f"ISO_PROFILE={output}",
            f"PLATFORMS={platforms or ROOT / 'platforms'}",
        ],
    )


@pytest.fixture
def iso_output(tree):
    output = tree / "iso"
    for name in [
        "archiso-mkinitcpio-preset.hook.in",
        "grub/grub.cfg.in",
        "grub/loopback.cfg.in",
        "syslinux/archiso_sys-linux.cfg.in",
        "syslinux/archiso_pxe-linux.cfg.in",
    ]:
        dest = output / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "iso" / name, dest)
    return output


@pytest.mark.parametrize(
    "platform,kernels,arch",
    [
        ("x86-64", ["linux-cachyos", "linux-cachyos-lts"], "x86_64"),
        ("aarch64-generic", ["linux-aarch64"], "aarch64"),
        ("aarch64-apple", ["linux-asahi"], "aarch64"),
    ],
)
def test_kernel_readers_use_composed_platform(iso_output, platform, kernels, arch):
    boot = run_make("_stage-bootconfig", iso_output, platform)
    assert boot.returncode == 0, boot.stdout + boot.stderr
    assert f"Default kernel: {kernels[0]}" in boot.stdout
    hook = iso_output / "airootfs/etc/pacman.d/hooks/archiso-mkinitcpio-preset.hook"
    assert kernels[0] in hook.read_text()
    packages = run_make("_assemble-packages", iso_output, platform)
    assert packages.returncode == 0, packages.stdout + packages.stderr
    result = (iso_output / f"packages.{arch}").read_text().splitlines()
    assert set(kernels + [f"{k}-headers" for k in kernels] + ["linux-firmware"]) <= set(
        result
    )
    if "memdisk" in (ROOT / "platforms" / platform / "archiso.conf").read_text():
        assert "syslinux" in result


@pytest.mark.parametrize("target", ["_stage-bootconfig", "_assemble-packages"])
def test_kernel_read_failure_stops_recipe(iso_output, tmp_path, target):
    platforms = tmp_path / "custom-platforms"
    shutil.copytree(ROOT / "platforms/x86-64", platforms / "x86-64")
    (platforms / "x86-64/platform.toml").write_text(
        '[platform]\narch = "x86_64"\n[provider]\nid = "unknown"\n'
    )
    result = run_make(target, iso_output, platforms=platforms)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (iso_output / "packages.x86_64").exists()
    assert not (
        iso_output / "airootfs/etc/pacman.d/hooks/archiso-mkinitcpio-preset.hook"
    ).exists()


def test_boot_reader_keeps_explicit_default_variant(iso_output, tmp_path):
    platforms = tmp_path / "custom-platforms"
    shutil.copytree(ROOT / "platforms/x86-64", platforms / "x86-64")
    config = platforms / "x86-64/platform.toml"
    config.write_text(
        config.read_text().replace(
            "[kernel]",
            """[kernel]
variants = [
    { package = "linux-first", headers = "linux-first-headers" },
    { package = "linux-chosen", headers = "linux-chosen-headers", default = true },
]
""",
            1,
        )
    )
    result = run_make("_stage-bootconfig", iso_output, platforms=platforms)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Default kernel: linux-chosen" in result.stdout


def test_shipped_platform_loads_from_staged_data_only(iso_output, monkeypatch):
    from arches_installer.core import provider
    from arches_installer.core.platform import load_platform

    expected = load_platform(ROOT / "platforms/x86-64/platform.toml")
    result = run_make("_stage-platform", iso_output)
    assert result.returncode == 0, result.stdout + result.stderr
    staged = iso_output / "airootfs/opt/arches"
    monkeypatch.setattr(provider, "_PROVIDERS_SEARCH", [staged / "providers"])
    monkeypatch.setattr(provider, "_PLATFORMS_DIR", staged / "no-checkout-fallback")
    actual = load_platform(staged / "platform/platform.toml")
    assert actual == expected
    assert provider.resolve_provider(actual).kernel.package == "linux-cachyos"
    assert sorted(p.name for p in (staged / "providers").iterdir()) == ["cachyos"]
    for name in ["cachyos"]:
        assert (staged / "providers" / name / "provider.toml").read_bytes() == (
            ROOT / "providers" / name / "provider.toml"
        ).read_bytes()
