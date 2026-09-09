"""Build contracts with disposable trees and stubbed privilege/package boundaries."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from tests.core.test_install import selected_inputs as selected_inputs

ROOT = Path(__file__).resolve().parents[3]
BLOCKED = (
    "podman",
    "docker",
    "mkarchiso",
    "chown",
    "git",
    "makepkg",
    "pacman",
    "sudo",
    "runuser",
    "repo-add",
    "pacstrap",
    "arch-chroot",
    "chroot",
    "mount",
    "umount",
    "losetup",
    "dd",
    "mkfs",
    "mkfs.btrfs",
    "mkfs.vfat",
    "parted",
    "sgdisk",
    "wipefs",
    "systemctl",
    "usermod",
    "groupmod",
    "useradd",
    "groupadd",
    "curl",
    "wget",
    "ssh",
    "rsync",
    "qemu-img",
    "qemu-system-x86_64",
    "qemu-system-aarch64",
    "cmake",
    "ninja",
    "gcc",
    "g++",
    "cc",
    "c++",
    "ld",
    "cargo",
    "npm",
    "pip",
    "uv",
    "pacman-key",
)


@pytest.fixture
def tree(tmp_path):
    for name in (
        "scripts",
        "platforms",
        "providers",
        "templates",
        "modules",
        "installer",
    ):
        shutil.copytree(
            ROOT / name, tmp_path / name, ignore=shutil.ignore_patterns("__pycache__")
        )
    for name in ("Makefile", "Containerfile"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    (tmp_path / "bin").mkdir()
    # No system PATH fallback: an unknown build/network command cannot escape.
    for command in (
        "bash",
        "python3",
        "make",
        "env",
        "basename",
        "dirname",
        "id",
        "uname",
        "tee",
        "mkdir",
        "mktemp",
        "cp",
        "mv",
        "rm",
        "find",
        "sort",
        "sha256sum",
        "cut",
        "tr",
        "grep",
        "sed",
        "ls",
        "awk",
        "wc",
        "cat",
        "date",
        "ln",
        "chmod",
        "du",
        "readlink",
        "touch",
        "stat",
        "head",
        "tail",
        "true",
        "false",
    ):
        (tmp_path / "bin" / command).symlink_to(Path("/usr/bin") / command)
    for command in BLOCKED:
        stub(
            tmp_path, command, 'echo "Blocked unstubbed build boundary" >&2; exit 98\n'
        )
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()
    # Never discover a real user's home or SSH keys through these utilities.
    stub(tmp_path, "logname", "exit 1\n")
    stub(tmp_path, "whoami", "echo test-builder\n")
    return tmp_path


def run(tree, args, **env):
    return subprocess.run(
        args,
        cwd=tree,
        text=True,
        capture_output=True,
        timeout=30,
        start_new_session=True,
        env={
            "PATH": str(tree / "bin"),
            "HOME": str(tree / "home"),
            "TMPDIR": str(tree / "tmp"),
            "USER": "test-builder",
            "LANG": "C.UTF-8",
            "PYTHONPATH": "",
            "ARCHES_PROVIDER": "",
            "ARCHES_TEMPLATE": "",
            "ARCHES_GPU": "",
            "PLATFORM": "x86-64",
            "CAPTURE": str(tree / "capture"),
            **env,
        },
    )


def stub(tree, name, body):
    path = tree / "bin" / name
    if path.is_symlink():
        path.unlink()
    path.write_text("#!/bin/bash\nset -eu\n" + body)
    path.chmod(0o755)


@pytest.mark.parametrize("mode", ["fb", "graphical"])
def test_live_build_modules_respect_iso_mode(tree, mode):
    result = run(
        tree, ["python3", "scripts/iso-config.py", "live-build-modules", "modules"],
        ARCHES_PROVIDER="omarchy", ARCHES_TEMPLATE="omarchy-workstation", ISO_MODE=mode,
    )
    assert result.returncode == 0, result.stderr
    assert ("kde" in result.stdout.splitlines()) == (mode == "graphical")


def simulate_uid(tree):
    """Instrument disposable copies only; production uses Bash's readonly UID."""
    for name in ("build-iso.sh", "builder.sh", "build-aur-repo.sh"):
        path = tree / "scripts" / name
        path.write_text(path.read_text().replace("$EUID", "${MOCK_UID:-1000}"))


def test_harness_blocks_commands_and_discards_inherited_environment(tree, monkeypatch):
    # This guard only runs harmless utilities and fail-closed stubs.
    for name in (
        "BASH_ENV",
        "ENV",
        "MAKEFLAGS",
        "MFLAGS",
        "MAKEOVERRIDES",
        "ARCHES_BUILD_PURPOSE",
        "ARCHES_BUILD_IMAGE",
        "PROVIDER",
        "LD_PRELOAD",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, "must-not-leak")
    result = run(tree, ["env"])
    assert result.returncode == 0
    assert "must-not-leak" not in result.stdout
    for command in BLOCKED:
        result = run(tree, [command])
        assert result.returncode == 98, (command, result.stdout, result.stderr)
    assert (
        run(tree, ["bash", "-c", "command -v definitely-not-allowed"]).returncode != 0
    )


@pytest.mark.parametrize(
    "target,script", [("iso", "build-iso.sh"), ("builder-iso", "builder.sh")]
)
def test_make_propagates_provider(tree, target, script):
    (tree / "scripts" / script).write_text(
        '#!/bin/bash\nprintf "%s" "$ARCHES_PROVIDER" > "$CAPTURE"\n'
    )
    result = run(tree, ["make", target, "PROVIDER=omarchy", "OFFLINE=0"])
    assert result.returncode == 0, result.stderr
    assert (tree / "capture").read_text() == "omarchy"


@pytest.mark.parametrize(
    "script,args", [("build-iso.sh", []), ("builder.sh", ["build"])]
)
def test_container_and_sudo_propagate_provider(tree, script, args):
    simulate_uid(tree)
    stub(tree, "id", 'case "$*" in *-g*) echo 1000;; *) echo 0;; esac\n')
    stub(tree, "sudo", '[[ "${1:-}" != -n ]] || shift\nexec "$@"\n')
    stub(
        tree,
        "podman",
        """
if [[ "$1" == inspect ]]; then echo true; exit; fi
if [[ "$1" == image && "$2" == inspect ]]; then echo image-digest; exit; fi
if [[ "$1" == exec && "$*" == *"{{.Image}}"* ]]; then echo image-digest; exit; fi
if [[ "$*" == *"printenv ARCHES_BUILD_IMAGE"* ]]; then echo image-digest; exit; fi
printf '%s\n' "$@" >> "$CAPTURE"
""",
    )
    result = run(
        tree,
        ["bash", f"scripts/{script}", *args],
        ARCHES_PROVIDER="omarchy",
        OFFLINE="0",
        SUDO_USER="builder",
        MOCK_UID="0",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ARCHES_PROVIDER=omarchy" in (tree / "capture").read_text()


@pytest.mark.parametrize(
    "platform,provider,kernel",
    [
        ("x86-64", "", "linux-cachyos"),
        ("x86-64", "arch", "linux"),
        ("x86-64", "omarchy", "linux"),
        ("aarch64-generic", "", "linux-aarch64"),
        ("aarch64-apple", "", "linux-asahi"),
    ],
)
def test_target_base_packages(tree, platform, provider, kernel):
    result = run(
        tree,
        [
            "python3",
            "scripts/iso-config.py",
            "target-base",
            f"platforms/{platform}/platform.toml",
        ],
        ARCHES_PROVIDER=provider,
    )
    assert result.returncode == 0, result.stderr
    packages = result.stdout.splitlines()
    assert kernel in packages and f"{kernel}-headers" in packages
    if provider in ("arch", "omarchy"):
        assert not any("cachyos" in p or p == "chwd" for p in packages)
        assert "limine-mkinitcpio-hook" not in packages
    if platform == "aarch64-apple":
        assert "asahi-fwextract" in packages


def test_consecutive_staging_matches_clean_selection(tree, monkeypatch):
    from arches_installer.core import provider
    from arches_installer.core.platform import load_platform

    def stage(output, selected):
        result = run(
            tree,
            [
                "make",
                "-s",
                "_stage-platform",
                "PLATFORM=x86-64",
                f"ISO_PROFILE={output}",
                f"PROVIDER={selected}",
            ],
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return output / "airootfs/opt/arches"

    cache = tree / ".offline-cache/sentinel"
    cache.parent.mkdir()
    cache.write_text("unrelated")
    staged = stage(tree / "iso", "cachyos")
    stage(tree / "iso", "omarchy")
    clean = stage(tree / "clean", "omarchy")
    for name in (
        "platform/platform.toml",
        "platform/pacman.conf",
        "target-provider.json",
    ):
        assert (staged / name).read_bytes() == (clean / name).read_bytes()
    assert sorted(p.name for p in (staged / "providers").iterdir()) == ["omarchy"]
    monkeypatch.setattr(provider, "_PROVIDERS_SEARCH", [staged / "providers"])
    selected = load_platform(staged / "platform/platform.toml")
    assert selected.provider_id == "omarchy"
    assert selected.kernel.package == "linux"
    assert selected.cachyos_optimization_tier == ""
    assert "cachyos" not in selected.pacman_conf
    assert cache.read_text() == "unrelated"


@pytest.mark.parametrize("provider", ["arch", "omarchy"])
def test_unreviewed_target_modules_fail_closed(tree, provider):
    result = run(
        tree,
        [
            "python3",
            "scripts/iso-config.py",
            "target-packages",
            "platforms/x86-64/platform.toml",
        ],
        ARCHES_PROVIDER=provider,
    )
    assert result.returncode != 0
    assert "provider" in result.stderr.lower() or "Omarchy" in result.stderr


def test_omarchy_workload_fixture_needs_no_desktop_module(tree):
    (tree / "templates/iso.toml").write_text(
        '[install]\ntemplates = ["fixture.toml"]\n'
    )
    (tree / "templates/fixture.toml").write_text(
        '[meta]\nname="Fixture"\nworkload="workstation"\nproviders=["omarchy"]\n'
    )
    result = run(
        tree,
        [
            "python3",
            "scripts/iso-config.py",
            "target-packages",
            "platforms/x86-64/platform.toml",
        ],
        ARCHES_PROVIDER="omarchy",
    )
    assert result.returncode == 0, result.stderr
    assert "omarchy-settings" in result.stdout


@pytest.mark.parametrize("script", ["build-aur-repo.sh", "cache-template-packages.sh"])
def test_alternative_cache_builds_reject_without_transactions(tree, script):
    stub(tree, "pacman", 'echo UNSAFE >> "$CAPTURE"; exit 99\n')
    result = run(
        tree, ["bash", f"scripts/{script}", "x86-64"], ARCHES_PROVIDER="omarchy"
    )
    assert result.returncode != 0
    assert not (tree / "capture").exists()
    assert (
        "authenticated" in (result.stdout + result.stderr).lower()
        or "reviewed" in (result.stdout + result.stderr).lower()
    )


def test_aur_caller_scopes_cache_and_privilege_drop(tree):
    simulate_uid(tree)
    # Root branch calls a sanitizing sudo double; child stops before any build.
    stub(tree, "id", "echo 0\n")
    stub(tree, "chown", ":\n")
    stub(tree, "pacman", 'echo "gcc 1.0"\n')
    stub(tree, "sudo", 'printf "%s\\n" "$@" > "$CAPTURE"\n')
    result = run(
        tree,
        ["bash", "scripts/build-aur-repo.sh", "x86-64"],
        ARCHES_PROVIDER="cachyos",
        ARCHES_BUILD_IMAGE="image-a",
        SUDO_USER="builder",
        MOCK_UID="0",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    captured = (tree / "capture").read_text()
    assert "ARCHES_PROVIDER" in captured and "ARCHES_BUILD_IMAGE" in captured
    assert "/.aur-repo/" in result.stdout


@pytest.mark.parametrize(
    "field",
    [
        "provider",
        "channel",
        "architecture",
        "platform",
        "snapshot",
        "dependencies",
        "image",
    ],
)
def test_cache_identity_invalidates_each_dimension(tree, field):
    def digest(value):
        result = run(
            tree,
            [
                "bash",
                "-c",
                'source scripts/lib/build-cache.sh; compute_cache_hash "str:source=fixed"',
            ],
            ARCHES_BUILD_IDENTITY=json.dumps({field: value}),
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    assert digest("a") != digest("b")


def test_profile_output_distinguishes_provider(tree):
    results = []
    for provider in ("cachyos", "omarchy"):
        result = run(
            tree,
            [
                "bash",
                "-c",
                f'declare -A file_permissions; source "{ROOT}/iso/profiledef.sh"; printf "%s" "$iso_name"',
            ],
            ARCHES_PLATFORM="x86-64",
            ARCHES_PROVIDER=provider,
        )
        assert result.returncode == 0
        results.append(result.stdout)
        assert provider in result.stdout
    assert results[0] != results[1]


@pytest.mark.parametrize("platform", ["x86-64", "aarch64-generic", "aarch64-apple"])
def test_default_target_packages_remain_available(tree, platform):
    result = run(
        tree,
        [
            "python3",
            "scripts/iso-config.py",
            "target-packages",
            f"platforms/{platform}/platform.toml",
        ],
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("selected_inputs", ["arch", "omarchy"], indirect=True)
def test_stage_inputs_reuses_authenticated_contract(tree, selected_inputs):
    from arches_installer.core.provider import ProviderInputs, validate_provider_inputs

    provider, inputs, manifest, _ = selected_inputs
    output = tree / "staged"
    command = [
        "python3",
        "scripts/iso-config.py",
        "stage-inputs",
        "platforms/x86-64/platform.toml",
        str(output),
        str(inputs.root),
        inputs.manifest_sha256,
    ]
    bad = run(tree, command[:-1] + ["0" * 64], ARCHES_PROVIDER=provider.id)
    assert bad.returncode != 0 and not output.exists()
    result = run(tree, command, ARCHES_PROVIDER=provider.id)
    assert result.returncode == 0, result.stderr
    staged = output / "provider-inputs" / provider.id / inputs.manifest_sha256
    assert (
        validate_provider_inputs(
            provider, "x86_64", ProviderInputs(staged, inputs.manifest_sha256)
        )
        == manifest["files"]
    )
    assert (inputs.root / "manifest.json").exists()
    metadata = json.loads((output / "target-provider.json").read_text())
    assert metadata["manifest_sha256"] == inputs.manifest_sha256
    assert metadata["inputs"] == staged.relative_to(output).as_posix()
    result = run(tree, ["python3", "scripts/iso-config.py", "stage-provider", "platforms/x86-64/platform.toml", str(output)], ARCHES_PROVIDER=provider.id)
    assert result.returncode == 0, result.stderr
    assert json.loads((output / "target-provider.json").read_text()) == metadata


@pytest.mark.parametrize("provider", ["omarchy", "unknown", "asahi"])
def test_host_shell_dry_run_never_reaches_privilege_or_mounts(tree, provider):
    template = tree / "templates/host-fixture.toml"
    template.write_text('[meta]\nname="Host fixture"\n')
    config = tree / "host.toml"
    config.write_text(f'[install]\ntemplate="{template}"\nusername="tester"\npassword="fixture-only"\npartition="/dev/vda2"\nesp_partition="/dev/vda1"\n')
    result = run(tree, ["bash", "scripts/host-install.sh", "--dry-run", "--provider", provider, str(config)])
    assert result.returncode == (0 if provider == "asahi" else 1), result.stderr
    assert not (tree / ".pkg-cache").exists()


def test_actual_aur_caller_changes_scope_with_dependencies_and_image(tree):
    (tree / "platforms/x86-64/platform.toml").write_text(
        '[platform]\nname="x86-64"\narch="x86_64"\n[provider]\nid="cachyos"\n'
    )
    (tree / "templates/iso.toml").write_text('[install]\ntemplates=["fixture.toml"]\n')
    (tree / "templates/fixture.toml").write_text('[modules]\ninclude=["base"]\n')
    (tree / "modules/base/build.sh").write_text(
        'printf "%s\\n" "$ARCHES_PROVIDER" "$ARCHES_BUILD_IDENTITY" "$REPO_DIR" "$BUILD_DIR" > "$CAPTURE"\n'
    )
    stub(tree, "id", "echo 1000\n")
    stub(tree, "pacman", '[[ "$1" == -Q ]]; printf "%s\\n" "$DEPS"\n')
    values = []
    for image, deps in [
        ("image-a", "gcc 1"),
        ("image-a", "gcc 2"),
        ("image-b", "gcc 2"),
    ]:
        result = run(
            tree,
            ["bash", "scripts/build-aur-repo.sh", "x86-64"],
            ARCHES_BUILD_IMAGE=image,
            DEPS=deps,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        captured = (tree / "capture").read_text().splitlines()
        assert captured[0] == "cachyos"
        assert captured[1] in captured[2] and captured[1] in captured[3]
        values.append(captured[1])
    assert len(set(values)) == 3


def test_cached_default_restage_removes_previous_packages(tree):
    source = tree / ".offline-cache/x86-64/cachyos" / ("a" * 64)
    (source / "sync").mkdir(parents=True)
    (source / "sync/core.db").write_text("snapshot")
    (source / "base.pkg.tar.zst").write_text("selected")
    (source.parent / "current").write_text(str(source))
    output = tree / "stage"
    (output / "pkg-cache").mkdir(parents=True)
    (output / "pkg-cache/old.pkg.tar.zst").write_text("old")
    result = run(
        tree,
        [
            "python3",
            "scripts/iso-config.py",
            "stage-cache",
            "platforms/x86-64/platform.toml",
            str(output),
            str(tree / ".offline-cache"),
        ],
    )
    assert result.returncode == 0, result.stderr
    assert not (output / "pkg-cache/old.pkg.tar.zst").exists()
    assert (source / "base.pkg.tar.zst").exists()


def test_iso_lookup_never_uses_other_provider_or_old_unlabelled_output(tree):
    output = tree / "out"
    output.mkdir()
    for i, name in enumerate(
        (
            "arches-omarchy-x86-64-2026.iso",
            "arches-cachyos-x86-64-2026.iso",
            "arches-x86-64-old.iso",
        )
    ):
        path = output / name
        path.touch()
        os.utime(path, (100 + i, 100 + i))
    result = run(
        tree,
        [
            "python3",
            "scripts/iso-config.py",
            "latest-iso",
            "platforms/x86-64/platform.toml",
            str(output),
        ],
        ARCHES_PROVIDER="omarchy",
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).name == "arches-omarchy-x86-64-2026.iso"


@pytest.mark.parametrize("provider,purpose", [("cachyos", ""), ("omarchy", "live")])
def test_privilege_drop_preserves_provider_in_sanitized_child(tree, provider, purpose):
    simulate_uid(tree)
    (tree / "platforms/x86-64/platform.toml").write_text(
        '[platform]\nname="x86-64"\narch="x86_64"\n[provider]\nid="cachyos"\n'
    )
    (tree / "templates/iso.toml").write_text(
        '[install]\ntemplates=["fixture.toml"]\n[framebuffer]\nmodules=["base"]\n'
    )
    (tree / "templates/fixture.toml").write_text('[modules]\ninclude=["base"]\n')
    (tree / "modules/base/build.sh").write_text(
        'printf "%s\\n" "$ARCHES_PROVIDER" "$ARCHES_BUILD_IMAGE" "$ARCHES_BUILD_PURPOSE" "$REPO_DIR" > "$CAPTURE"\n'
    )
    stub(tree, "id", 'echo "${MOCK_UID:-0}"\n')
    stub(tree, "chown", ":\n")
    stub(tree, "pacman", '[[ "$1" == -Q ]]; echo "gcc 1"\n')
    stub(
        tree,
        "sudo",
        """
shift 2
IFS=, read -ra names <<< "${1#--preserve-env=}"
shift
values=()
for name in "${names[@]}"; do values+=("$name=${!name:-}"); done
exec env -i "${values[@]}" MOCK_UID=1000 CAPTURE="$CAPTURE" "$@"
""",
    )
    result = run(
        tree,
        ["bash", "scripts/build-aur-repo.sh", "x86-64"],
        ARCHES_PROVIDER=provider,
        ARCHES_BUILD_PURPOSE=purpose,
        ARCHES_BUILD_IMAGE="image-a",
        SUDO_USER="builder",
        MOCK_UID="0",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tree / "capture").read_text().splitlines()[:3] == [
        provider,
        "image-a",
        purpose or "target",
    ]


def test_persistent_build_needs_only_exec_permission(tree):
    stub(
        tree,
        "sudo",
        """
[[ "${1:-}" != -n ]] || shift
[[ "$1" == podman && "$2" == exec ]] || exit 77
exec "$@"
""",
    )
    stub(
        tree,
        "podman",
        """
if [[ "$*" == *"printenv ARCHES_BUILD_IMAGE"* ]]; then echo image-a; exit; fi
printf '%s\n' "$@" >> "$CAPTURE"
""",
    )
    result = run(
        tree,
        ["bash", "scripts/builder.sh", "build"],
        ARCHES_PROVIDER="omarchy",
        OFFLINE="0",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ARCHES_BUILD_IMAGE=image-a" in (tree / "capture").read_text()


@pytest.mark.parametrize(
    "platform,kernel",
    [
        ("x86-64", "linux-cachyos"),
        ("aarch64-generic", "linux-aarch64"),
        ("aarch64-apple", "linux-asahi"),
    ],
)
def test_offline_caller_scopes_snapshots_and_preserves_old_caches(
    tree, platform, kernel
):
    stub(
        tree,
        "pacman",
        """
operation="$1"; shift
db= cache=
while [[ $# -gt 0 ]]; do
    case "$1" in --dbpath) db="$2"; shift;; --cachedir) cache="$2"; shift;; esac
    shift
done
case "$operation" in
    -Q) echo "gcc 1";;
    -Sy) mkdir -p "$db/sync"; printf '%s' "$SNAPSHOT" > "$db/sync/core.db";;
    -Sw) printf '%s' "$SNAPSHOT" > "$cache/base-1-1-any.pkg.tar.zst";;
    *) exit 98;;
esac
""",
    )
    paths = []
    for snapshot in ("one", "one", "two"):
        result = run(
            tree,
            ["bash", "scripts/cache-template-packages.sh", platform],
            ARCHES_BUILD_IMAGE="image-a",
            SNAPSHOT=snapshot,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert kernel in result.stdout
        pointers = list((tree / ".offline-cache").glob(f"{platform}/*/current"))
        paths.append(Path(pointers[0].read_text().strip()))
    assert paths[0] == paths[1] and paths[1] != paths[2]
    assert (paths[0] / "sync/core.db").read_text() == "one"
    assert (paths[2] / "sync/core.db").read_text() == "two"


def test_cache_metadata_failure_prevents_sync(tree):
    machine = tree / "hardware/machines/broken.toml"
    machine.parent.mkdir(parents=True)
    machine.write_text("[invalid")
    stub(tree, "pacman", 'printf "%s\\n" "$*" >> "$CAPTURE"; exit 98\n')
    result = run(
        tree,
        ["bash", "scripts/cache-template-packages.sh", "x86-64"],
        ARCHES_BUILD_IMAGE="image-a",
    )
    assert result.returncode != 0
    assert not (tree / "capture").exists()


def test_live_graphical_metadata_failure_stops_recipe(tree):
    (tree / "templates/iso.toml").write_text("[invalid")
    result = run(
        tree, ["make", "-s", "_stage-graphical", f"ISO_PROFILE={tree / 'iso'}"]
    )
    assert result.returncode != 0
    assert not (tree / "iso/airootfs/etc/sddm.conf.d/autologin.conf").exists()


@pytest.mark.parametrize(
    "mode", ["stable", "once", "always", "fail-first", "fail-second"]
)
def test_dependency_drift_rebuilds_all_outputs_with_bounded_publication(
    package_build_tree, mode
):
    tree = package_build_tree
    platform_file = tree / "platforms/x86-64/platform.toml"
    platform_file.write_text(
        platform_file.read_text() + '[aur_packages]\npackages=["aur-fixture"]\n'
    )
    stub(
        tree,
        "git",
        '[[ "$1" == clone ]]; dest="${@: -1}"; mkdir -p "$dest"; printf "# fixture" > "$dest/PKGBUILD"\n',
    )
    stub(
        tree,
        "makepkg",
        'deps=$(pacman -Q); printf "aur %s %s\\n" "$deps" "$FORCE" >> "$CAPTURE"; printf "%s" "$deps" > aur-fixture-1-1-any.pkg.tar.zst\n',
    )
    stub(
        tree,
        "pacman",
        '[[ "$1" == -Q ]]; if [[ -f "$CAPTURE.deps" ]]; then cat "$CAPTURE.deps"; else echo "gcc 1"; fi\n',
    )
    for name in ("alpha", "beta"):
        (tree / "modules" / name / "build.sh").write_text(f"""
set -eu
deps=$(pacman -Q)
printf '{name} %s %s\\n' "$deps" "$FORCE" >> "$CAPTURE"
printf '%s' "$deps" > "$REPO_DIR/{name}-1-1-any.pkg.tar.zst"
if [[ "$deps" == "gcc 1" && "$MODE" != stable ]]; then
    printf unverified > "$REPO_DIR/first-pass-only.pkg.tar.zst"
fi
if [[ {name} == beta && "$MODE" != stable ]]; then
    if [[ "$MODE" == fail-first ]]; then exit 41; fi
    if [[ "$MODE" == always ]]; then
        printf 'gcc %s' "$((${{deps##* }} + 1))" > "$CAPTURE.deps"
    else
        printf 'gcc 2' > "$CAPTURE.deps"
    fi
    if [[ "$MODE" == fail-second && "$deps" == "gcc 2" ]]; then exit 41; fi
fi
""")
    stage = tree / "iso/airootfs/opt/arches-repo"
    stage.mkdir(parents=True)
    (stage / "previous.pkg.tar.zst").write_text("previous completed stage")
    result = run(
        tree,
        ["bash", "scripts/build-aur-repo.sh", "x86-64"],
        MODE=mode,
        ARCHES_BUILD_IMAGE="image-a",
    )
    events = (tree / "capture").read_text().splitlines()
    if mode == "stable":
        assert result.returncode == 0, result.stdout + result.stderr
        assert events == ["aur gcc 1 false", "alpha gcc 1 false", "beta gcc 1 false"]
    elif mode == "fail-first":
        assert result.returncode != 0
        assert events == ["aur gcc 1 false", "alpha gcc 1 false", "beta gcc 1 false"]
        assert staged_packages(tree) == {
            "previous.pkg.tar.zst": b"previous completed stage"
        }
        assert not list((tree / ".aur-repo/x86-64/cachyos").glob("*/repo"))
        result = run(
            tree,
            ["bash", "scripts/build-aur-repo.sh", "x86-64"],
            MODE="stable",
            ARCHES_BUILD_IMAGE="image-a",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert len((tree / "capture").read_text().splitlines()) == 6
        assert "first-pass-only.pkg.tar.zst" not in staged_packages(tree)
    else:
        assert events == [
            "aur gcc 1 false",
            "alpha gcc 1 false",
            "beta gcc 1 false",
            "aur gcc 2 true",
            "alpha gcc 2 true",
            "beta gcc 2 true",
        ]
        assert list((tree / ".aur-repo").glob("**/first-pass-only.pkg.tar.zst"))
        if mode == "once":
            assert result.returncode == 0, result.stdout + result.stderr
            assert staged_packages(tree)["aur-fixture-1-1-any.pkg.tar.zst"] == b"gcc 2"
            assert staged_packages(tree)["alpha-1-1-any.pkg.tar.zst"] == b"gcc 2"
            assert staged_packages(tree)["beta-1-1-any.pkg.tar.zst"] == b"gcc 2"
            assert "first-pass-only.pkg.tar.zst" not in staged_packages(tree)
        else:
            assert result.returncode != 0
            assert staged_packages(tree) == {
                "previous.pkg.tar.zst": b"previous completed stage"
            }
            assert not list((tree / ".aur-repo/x86-64/cachyos").glob("*/repo"))


def test_iso_scratch_is_provider_scoped_without_deleting_previous_work(tree):
    work = tree / "work"
    work.mkdir()
    (work / "sentinel").write_text("previous build")
    stub(tree, "mkarchiso", 'printf "%s\\n" "$@" > "$CAPTURE"\n')
    prerequisites = [
        "_check-root",
        "_check-deps",
        "_aur-repo",
        "_stage-installer",
        "_stage-modules",
        "_stage-ansible",
        "_stage-hardware",
        "_stage-platform",
        "_stage-bootconfig",
        "_assemble-packages",
        "_stage-graphical",
        "_stage-firstboot-assets",
    ]
    result = run(
        tree,
        [
            "make",
            "-s",
            "_iso",
            "PROVIDER=omarchy",
            "PLATFORM=x86-64",
            "OFFLINE=0",
            f"WORK_DIR={work}",
            *[arg for name in prerequisites for arg in ("-o", name)],
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (work / "sentinel").read_text() == "previous build"
    arguments = (tree / "capture").read_text().splitlines()
    assert "/x86-64/omarchy/" in arguments[arguments.index("-w") + 1]


def test_dependency_query_failure_prevents_cache_download(tree):
    stub(
        tree,
        "pacman",
        """
if [[ "$1" == -Q ]]; then exit 97; fi
if [[ "$1" == -Sw ]]; then touch "$CAPTURE"; exit 0; fi
while [[ "$1" != --dbpath ]]; do shift; done
mkdir -p "$2/sync"; printf snapshot > "$2/sync/core.db"
""",
    )
    result = run(
        tree,
        ["bash", "scripts/cache-template-packages.sh", "x86-64"],
        ARCHES_BUILD_IMAGE="image-a",
    )
    assert result.returncode != 0
    assert not (tree / "capture").exists()


@pytest.mark.parametrize(
    "script,args", [("build-iso.sh", []), ("build-aur-repo.sh", ["x86-64"])]
)
def test_failed_id_cannot_choose_root_branch(tree, script, args):
    simulate_uid(tree)
    stub(tree, "id", "exit 97\n")
    stub(tree, "pacman", '[[ "$1" == -Q ]]; echo "gcc 1"\n')
    stub(tree, "chown", 'touch "$CAPTURE"; exit 98\n')
    # The AUR case has no work. A non-root invocation must not call chown/sudo.
    (tree / "platforms/x86-64/platform.toml").write_text(
        '[platform]\nname="x86-64"\narch="x86_64"\n[provider]\nid="cachyos"\n'
    )
    (tree / "templates/iso.toml").write_text("[install]\ntemplates=[]\n")
    result = run(
        tree,
        ["bash", f"scripts/{script}", *args],
        ARCHES_BUILD_IMAGE="image-a",
        SUDO_USER="builder",
        MOCK_UID="1000",
    )
    assert not (tree / "capture").exists()
    if script == "build-iso.sh":
        assert result.returncode != 0 and "requires root" in result.stdout
        assert not (tree / "container-build.log").exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr


def test_make_root_guard_cannot_fail_open_on_id_error(tree):
    path = tree / "Makefile"
    path.write_text(path.read_text().replace("$$EUID", "$${MOCK_UID:-1000}"))
    stub(tree, "id", "exit 97\n")
    result = run(tree, ["make", "-s", "_check-root"], MOCK_UID="1000")
    assert result.returncode != 0
    assert "requires root" in result.stdout


@pytest.mark.parametrize(
    "command",
    ["target-packages", "live-kernel", "live-kernel-packages", "stage-provider"],
)
def test_build_model_ignores_conflicting_installed_definitions(tree, command):
    installed = tree / "installed"
    shutil.copytree(tree / "providers", installed / "providers")
    provider_file = installed / "providers/cachyos/provider.toml"
    provider_file.write_text(
        provider_file.read_text().replace("linux-cachyos", "linux-installed")
    )
    for directory, label in ((tree, "checkout"), (installed, "installed")):
        module = directory / "modules/base/module.toml"
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(
            f'[meta]\nname="Base"\ncategory="base"\n[install.pacstrap]\npackages=["{label}-base"]\n'
        )
        stacks = directory / "scripts/gpu-stacks.toml"
        stacks.parent.mkdir(parents=True, exist_ok=True)
        stacks.write_text(f'[test]\npackages=["{label}-gpu"]\n')
    (tree / "templates/iso.toml").write_text('[install]\ntemplates=["fixture.toml"]\n')
    (tree / "templates/fixture.toml").write_text(
        '[modules]\ninclude=["base"]\n[gpu]\nstacks=["test"]\n'
    )
    driver = """
import sys, runpy
from pathlib import Path
sys.path.insert(0, str(Path("installer").resolve()))
from arches_installer.core import provider, module, gpu_stacks
assert str(provider._PROVIDERS_SEARCH[0]) == "/opt/arches/providers"
assert str(module._MODULES_SEARCH[0]) == "/opt/arches/modules"
assert str(gpu_stacks._STACKS_SEARCH[0]) == "/opt/arches/scripts/gpu-stacks.toml"
provider._PROVIDERS_SEARCH.insert(0, Path("installed/providers"))
module._MODULES_SEARCH.insert(0, Path("installed/modules"))
gpu_stacks._STACKS_SEARCH.insert(0, Path("installed/scripts/gpu-stacks.toml"))
sys.argv = ["scripts/iso-config.py", "target-packages", "platforms/x86-64/platform.toml"]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
    driver = driver.replace(
        '"target-packages", "platforms/x86-64/platform.toml"',
        f'{json.dumps(command)}, "platforms/x86-64/platform.toml", "stage"',
    )
    result = run(tree, ["python3", "-S", "-c", driver])
    assert result.returncode == 0, result.stderr
    if command == "stage-provider":
        result_text = (tree / "stage/target-provider.json").read_text()
        assert '"linux-cachyos"' in result_text and "linux-installed" not in result_text
        assert (tree / "stage/providers/cachyos/provider.toml").read_bytes() == (
            tree / "providers/cachyos/provider.toml"
        ).read_bytes()
    else:
        assert "linux-cachyos\n" in result.stdout
        if command == "target-packages":
            assert (
                "checkout-base\n" in result.stdout and "checkout-gpu\n" in result.stdout
            )
        assert "installed" not in result.stdout


@pytest.fixture
def package_build_tree(tree):
    (tree / "platforms/x86-64/platform.toml").write_text(
        '[platform]\nname="x86-64"\narch="x86_64"\n[provider]\nid="cachyos"\n'
    )
    (tree / "templates/iso.toml").write_text(
        '[install]\ntemplates=["alpha.toml", "beta.toml"]\n[framebuffer]\nmodules=["alpha"]\n'
    )
    for name in ("alpha", "beta"):
        (tree / "templates" / f"{name}.toml").write_text(
            f'[modules]\ninclude=["{name}"]\n'
        )
        directory = tree / "modules" / name
        directory.mkdir()
        (directory / "module.toml").write_text(
            f'[meta]\nname="{name}"\ncategory="base"\n'
        )
        (directory / "build.sh").write_text(
            f'printf "{name}" > "$REPO_DIR/{name}-1-1-any.pkg.tar.zst"\nprintf "%s\\n" "$ARCHES_BUILD_IDENTITY" >> "$CAPTURE"\n'
        )
    stub(tree, "pacman", '[[ "$1" == -Q ]]; echo "gcc 1"\n')
    stub(tree, "repo-add", 'db="$1"; shift; printf "%s\\n" "$@" > "$db"\n')
    return tree


def staged_packages(tree):
    return {
        p.name: p.read_bytes()
        for p in (tree / "iso/airootfs/opt/arches-repo").iterdir()
        if ".pkg.tar." in p.name or ".db." in p.name
    }


def test_broad_then_narrow_stages_same_packages_as_clean_narrow(package_build_tree):
    tree = package_build_tree
    args = ["bash", "scripts/build-aur-repo.sh", "x86-64"]
    clean = tree / "clean-checkout"
    for name in (
        "scripts",
        "platforms",
        "providers",
        "templates",
        "modules",
        "installer",
        "bin",
        "home",
        "tmp",
    ):
        shutil.copytree(tree / name, clean / name, symlinks=True)
    shutil.copyfile(tree / "Containerfile", clean / "Containerfile")
    for root, selected in ((tree, ""), (tree, "alpha"), (clean, "alpha.toml")):
        result = run(root, args, ARCHES_TEMPLATE=selected, ARCHES_BUILD_IMAGE="image-a")
        assert result.returncode == 0, result.stdout + result.stderr
    assert staged_packages(tree) == staged_packages(clean)
    assert set(staged_packages(tree)) == {
        "alpha-1-1-any.pkg.tar.zst",
        "arches-local.db.tar.gz",
    }
    assert list((tree / ".aur-repo").glob("**/beta-1-1-any.pkg.tar.zst"))
    narrow_repo = (tree / "capture").read_text().splitlines()[-1]
    result = run(tree, args, ARCHES_TEMPLATE="alpha.toml", ARCHES_BUILD_IMAGE="image-a")
    assert result.returncode == 0, result.stderr
    assert (tree / "capture").read_text().splitlines()[-1] == narrow_repo


def test_gpu_definition_changes_actual_build_scope(package_build_tree):
    tree = package_build_tree
    scopes = []
    for version in (1, 2):
        (tree / "scripts/gpu-stacks.toml").write_text(
            f'[test]\nmodules=["alpha"]\npackages=["driver-{version}"]\n'
        )
        result = run(
            tree,
            ["bash", "scripts/build-aur-repo.sh", "x86-64"],
            ARCHES_TEMPLATE="alpha",
            ARCHES_GPU="test",
            ARCHES_BUILD_IMAGE="image-a",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        scopes.append((tree / "capture").read_text().splitlines()[-1])
    assert scopes[0] != scopes[1]


@pytest.mark.parametrize("purpose", ["target", "live", "invalid"])
def test_custom_build_purpose_matches_effective_selection(package_build_tree, purpose):
    tree = package_build_tree
    result = run(
        tree,
        ["bash", "scripts/build-aur-repo.sh", "x86-64"],
        ARCHES_BUILD_PURPOSE=purpose,
        ARCHES_BUILD_IMAGE="image-a",
    )
    if purpose == "invalid":
        assert result.returncode != 0
        assert not (tree / "capture").exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        expected = {"alpha-1-1-any.pkg.tar.zst", "arches-local.db.tar.gz"}
        if purpose == "target":
            expected.add("beta-1-1-any.pkg.tar.zst")
        assert set(staged_packages(tree)) == expected


@pytest.mark.parametrize("target", ["iso", "builder-iso"])
def test_nested_container_shell_and_real_make_receive_provider(tree, target):
    simulate_uid(tree)
    for command in ("chown", "usermod", "groupmod"):
        stub(tree, command, ":\n")
    stub(tree, "id", "echo 1000\n")
    stub(tree, "sudo", '[[ "${1:-}" != -n ]] || shift\nexec "$@"\n')
    (tree / "probe.mk").write_text(
        '_provider_probe:\n\t@printf "%s|%s|%s|%s|%s" "$(PROVIDER)" "$$ARCHES_PROVIDER" "$(TEMPLATE)" "$$ARCHES_TEMPLATE" "$$ARCHES_BUILD_IMAGE" > "$$CAPTURE"\n'
    )
    stub(
        tree,
        "make",
        """
args=()
for arg in "$@"; do
    [[ "$arg" != _iso ]] || arg=_provider_probe
    args+=("$arg")
done
exec /usr/bin/make -f "$FIXTURE_ROOT/Makefile" -f "$FIXTURE_ROOT/probe.mk" "${args[@]}"
""",
    )
    stub(
        tree,
        "podman",
        """
case "$1" in
    image) [[ "$2" != inspect ]] || echo image-a; exit 0;;
    inspect) echo true; exit 0;;
esac
if [[ "$*" == *"printenv ARCHES_BUILD_IMAGE"* ]]; then echo image-a; exit 0; fi
if [[ "$*" == *"arches-builder true" || "$*" == *"test -d /var/cache/pacman/pkg" ]]; then exit 0; fi
values=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -e) values+=("$2"); shift;;
        /bin/bash|bash) break;;
    esac
    shift
done
[[ $# == 3 && "$2" == -c ]] || exit 98
body="${3//\\/build/$FIXTURE_ROOT}"
exec env -i PATH="$PATH" HOME="$HOME" TMPDIR="$TMPDIR" CAPTURE="$CAPTURE" FIXTURE_ROOT="$FIXTURE_ROOT" MOCK_UID=0 "${values[@]}" /bin/bash -c "$body"
""",
    )
    result = run(
        tree,
        ["make", target, "PROVIDER=omarchy", "TEMPLATE=alpha", "OFFLINE=0"],
        FIXTURE_ROOT=str(tree),
        MOCK_UID="0" if target == "iso" else "1000",
        SUDO_USER="builder",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tree / "capture").read_text() == "omarchy|omarchy|alpha|alpha|image-a"
