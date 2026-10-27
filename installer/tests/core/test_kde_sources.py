"""KDE module sources: mounts resolve from any worktree; kwin is current."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
LIB = _REPO / "scripts/lib/build-mounts.sh"
KDE_BUILD = _REPO / "modules/kde/build.sh"


def _resolve(project: Path, rel: str, **env) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "{LIB}"; resolve_build_mount "$1" "$2"',
            "_",
            str(project),
            rel,
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, **env),
        timeout=30,
    )


@pytest.fixture
def checkouts(tmp_path: Path):
    """~/dev/arches (main), ~/dev/kde-kwin, ~/dev/arches-worktrees/wt."""
    dev = tmp_path / "dev"
    main = dev / "arches"
    main.mkdir(parents=True)
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(git + ["init", "-q", str(main)], check=True)
    subprocess.run(
        git + ["-C", str(main), "commit", "-q", "--allow-empty", "-m", "x"], check=True
    )
    (dev / "kde-kwin").mkdir()
    wt = dev / "arches-worktrees" / "wt"
    subprocess.run(
        git + ["-C", str(main), "worktree", "add", "-q", str(wt)], check=True
    )
    return dev, main, wt


def test_main_checkout_resolves_project_relative(checkouts) -> None:
    dev, main, _ = checkouts
    r = _resolve(main, "../kde-kwin")
    assert r.returncode == 0
    assert r.stdout.strip() == str(dev / "kde-kwin")


def test_worktree_resolves_against_the_main_checkout(checkouts) -> None:
    dev, _, wt = checkouts
    assert not (wt / "../kde-kwin").exists()
    r = _resolve(wt, "../kde-kwin")
    assert r.returncode == 0
    assert r.stdout.strip() == str(dev / "kde-kwin")


def test_override_wins(checkouts, tmp_path: Path) -> None:
    _, _, wt = checkouts
    other = tmp_path / "elsewhere"
    (other / "kde-kwin").mkdir(parents=True)
    root = other / "root"
    root.mkdir()
    r = _resolve(wt, "../kde-kwin", ARCHES_SOURCES_ROOT=str(root))
    assert r.stdout.strip() == str(other / "kde-kwin")


def test_missing_source_fails(checkouts) -> None:
    _, _, wt = checkouts
    r = _resolve(wt, "../nope")
    assert r.returncode == 1


def test_build_mounts_name_kde_kwin() -> None:
    lines = [
        ln
        for ln in (_REPO / "modules/kde/build.mounts").read_text().splitlines()
        if ln and not ln.startswith("#")
    ]
    assert lines == ["../kde-task-manager", "../kde-kwin"]
    assert "kwin-gestures" not in KDE_BUILD.read_text()


@pytest.mark.skipif(not shutil.which("makepkg"), reason="needs makepkg")
def test_kwin_pkgbuild_tracks_the_kde_kwin_recipe(tmp_path: Path) -> None:
    """The wrapper renames the package but keeps the recipe's version and sources."""
    kde_kwin = tmp_path / "kde-kwin"
    (kde_kwin / "pkg").mkdir(parents=True)
    (kde_kwin / "pkg/PKGBUILD").write_text(
        "pkgname=kwin\npkgver=6.8.1\npkgrel=2\npkgdesc='KWin'\narch=(x86_64)\n"
        "license=(LGPL-2.0-or-later)\ngroups=(plasma)\n"
        "source=(https://example.invalid/$pkgname-$pkgver.tar.xz fix.patch)\n"
        "sha256sums=(SKIP SKIP)\n"
        "prepare() { cd $pkgname-$pkgver; patch -Np1 -i ../fix.patch; }\n"
        "build() { echo build $pkgname-$pkgver; }\n"
        "package() { echo package; }\n"
    )
    (kde_kwin / "pkg/fix.patch").write_text("")
    (kde_kwin / "pkg/kwin-6.8.1.tar.xz").write_text("tarball")
    dest = tmp_path / "build"
    out = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{KDE_BUILD}"; write_kwin_pkgbuild "$1" "$2"',
            "_",
            str(kde_kwin),
            str(dest),
        ],
        env=dict(
            os.environ,
            ARCHES_KDE_BUILD_LIB_ONLY="1",
            REPO_DIR=str(tmp_path / "repo"),
            BUILD_DIR=str(tmp_path),
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "6.8.1"
    assert (dest / "fix.patch").exists() and (dest / "kwin-6.8.1.tar.xz").exists()

    info = subprocess.run(
        ["makepkg", "--printsrcinfo"],
        cwd=dest,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert info.returncode == 0, info.stderr
    srcinfo = info.stdout
    assert "pkgname = arches-kwin-patched" in srcinfo
    assert "pkgver = 6.8.1" in srcinfo
    assert "provides = kwin=6.8.1" in srcinfo
    assert "conflicts = kwin" in srcinfo
    assert "kwin-6.8.1.tar.xz" in srcinfo
    assert "groups = plasma" not in srcinfo

    # The recipe's functions still see pkgname=kwin.
    funcs = subprocess.run(
        ["bash", "-c", "source ./PKGBUILD; pkgname=arches-kwin-patched; build"],
        cwd=dest,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert funcs.stdout.strip() == "build kwin-6.8.1"


def test_real_kde_kwin_recipe_is_the_current_kwin() -> None:
    """If the real checkout is here, its recipe matches the distro's kwin."""
    real = Path(
        subprocess.run(
            [
                "bash",
                "-c",
                f'source "{LIB}"; resolve_build_mount "$1" ../kde-kwin',
                "_",
                str(_REPO),
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    recipe = real / "pkg/PKGBUILD"
    if not recipe.is_file() or not shutil.which("pacman"):
        pytest.skip("no kde-kwin checkout or pacman here")
    ver = subprocess.run(
        ["bash", "-c", 'source "$1"; echo "$pkgver"', "_", str(recipe)],
        capture_output=True,
        text=True,
    ).stdout.strip()
    si = subprocess.run(["pacman", "-Si", "extra/kwin"], capture_output=True, text=True)
    if si.returncode != 0:
        pytest.skip("no extra/kwin in the sync db")
    distro = next(
        ln.split(":", 1)[1].strip()
        for ln in si.stdout.splitlines()
        if ln.startswith("Version")
    )
    assert distro.split("-")[0] == ver
