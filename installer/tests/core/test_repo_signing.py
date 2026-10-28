"""Tests for signing arches-local (D7) with a throwaway software GPG key.

The real repository key lives on the operator's YubiKey; these tests never
see it. They need gpg, repo-add, bsdtar and zstd, and skip without them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
SIGN = _REPO / "scripts" / "sign-local-repo.sh"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("gpg", "gpgconf", "repo-add", "bsdtar", "zstd")),
    reason="needs gpg, repo-add, bsdtar and zstd",
)


def _gpg(home: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gpg", "--homedir", str(home), "--batch", *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        **kw,
    )


def _new_key(home: Path, uid: str) -> str:
    _gpg(home, "--passphrase", "", "--quick-gen-key", uid, "ed25519", "sign", "never")
    out = _gpg(home, "--with-colons", "--list-keys", uid).stdout
    return next(
        line.split(":")[9] for line in out.splitlines() if line.startswith("fpr")
    )


def _package(repo: Path, name: str) -> Path:
    work = repo.parent / f"build-{name}"
    work.mkdir()
    (work / ".PKGINFO").write_text(
        f"pkgname = {name}\npkgbase = {name}\npkgver = 1-1\npkgdesc = test\n"
        "url = https://example.invalid\nbuilddate = 1\npackager = test\n"
        "size = 0\narch = any\nlicense = MIT\n"
    )
    pkg = repo / f"{name}-1-1-any.pkg.tar.zst"
    tar = subprocess.run(
        ["bsdtar", "-cf", "-", "-C", str(work), ".PKGINFO"],
        capture_output=True,
        check=True,
    ).stdout
    pkg.write_bytes(
        subprocess.run(
            ["zstd", "-q", "-c"], input=tar, capture_output=True, check=True
        ).stdout
    )
    return pkg


@pytest.fixture
def setup():
    # A short GNUPGHOME: gpg-agent's socket path must stay under 108 bytes.
    home = Path(tempfile.mkdtemp(prefix="gpg-"))
    home.chmod(0o700)
    work = Path(tempfile.mkdtemp(prefix="arches-sign-"))
    try:
        fpr = _new_key(home, "Arches Test Repo <repo@arches.invalid>")
        keys = work / "keys"
        keys.mkdir()
        (keys / "arches-local.fpr").write_text(fpr + "\n")
        repo = work / "repo"
        repo.mkdir()
        pkgs = [_package(repo, "foo"), _package(repo, "bar")]
        env = dict(
            os.environ,
            GNUPGHOME=str(home),
            ARCHES_REPO_KEY_DIR=str(keys),
            ARCHES_ALLOW_SOFTWARE_KEY="1",
        )
        yield dict(home=home, fpr=fpr, repo=repo, pkgs=pkgs, env=env, keys=keys)
    finally:
        subprocess.run(
            ["gpgconf", "--homedir", str(home), "--kill", "all"], check=False
        )
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


def _sign(s, **env) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SIGN), str(s["repo"])],
        env=dict(s["env"], **env),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _valid_by(s, sig: Path, data: Path) -> bool:
    r = subprocess.run(
        [
            "gpg",
            "--homedir",
            str(s["home"]),
            "--batch",
            "--status-fd",
            "1",
            "--verify",
            str(sig),
            str(data),
        ],
        capture_output=True,
        text=True,
    )
    return any(
        line.split()[1] == "VALIDSIG" and line.split()[-1] == s["fpr"]
        for line in r.stdout.splitlines()
        if line.startswith("[GNUPG:]")
    )


def test_signs_packages_and_database(setup) -> None:
    r = _sign(setup)
    assert r.returncode == 0, r.stderr
    for pkg in setup["pkgs"]:
        assert _valid_by(setup, Path(f"{pkg}.sig"), pkg)
    db = setup["repo"] / "arches-local.db.tar.gz"
    assert _valid_by(setup, Path(f"{db}.sig"), db)
    # pacman fetches <repo>.db and <repo>.db.sig
    assert (setup["repo"] / "arches-local.db.sig").exists()


def test_second_run_keeps_package_signatures(setup) -> None:
    assert _sign(setup).returncode == 0
    r = _sign(setup)
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("already signed:") == 2


def test_foreign_signature_is_replaced(setup) -> None:
    other = _new_key(setup["home"], "Someone Else <x@arches.invalid>")
    pkg = setup["pkgs"][0]
    _gpg(
        setup["home"],
        "--local-user",
        other,
        "--detach-sign",
        "--no-armor",
        "--output",
        f"{pkg}.sig",
        str(pkg),
    )
    assert not _valid_by(setup, Path(f"{pkg}.sig"), pkg)
    assert _sign(setup).returncode == 0
    assert _valid_by(setup, Path(f"{pkg}.sig"), pkg)


def test_refuses_a_software_key(setup) -> None:
    r = _sign(setup, ARCHES_ALLOW_SOFTWARE_KEY="")
    assert r.returncode == 1
    assert "not on a smartcard" in r.stderr
    assert not list(setup["repo"].glob("*.sig"))


def test_refuses_without_key_file(setup) -> None:
    (setup["keys"] / "arches-local.fpr").unlink()
    r = _sign(setup)
    assert r.returncode == 1
    assert "keys/README.md" in r.stderr


STAGE = _REPO / "scripts" / "stage-repo-key.sh"


def _stage(s, profile: Path, **env) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(STAGE), str(profile)],
        env=dict(
            s["env"],
            ARCHES_REPO_DIR=str(s["repo"]),
            ARCHES_SKIP_BUILD_KEYRING="1",
            **env,
        ),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _export(s) -> None:
    asc = _gpg(s["home"], "--armor", "--export", s["fpr"]).stdout
    (s["keys"] / "arches-local.asc").write_text(asc)


def test_iso_build_refuses_an_unsigned_repo(setup) -> None:
    _export(setup)
    profile = setup["repo"].parent / "profile"
    r = _stage(setup, profile)
    assert r.returncode == 1
    assert "sign-local-repo.sh" in r.stderr
    assert "foo-1-1-any.pkg.tar.zst" in r.stderr


def test_iso_build_stages_the_key_of_a_signed_repo(setup) -> None:
    _export(setup)
    assert _sign(setup).returncode == 0
    profile = setup["repo"].parent / "profile"
    r = _stage(setup, profile)
    assert r.returncode == 0, r.stderr
    root = profile / "airootfs"
    assert (root / "opt/arches/platform/arches-local.fpr").read_text().strip() == setup[
        "fpr"
    ]
    assert (
        root / "usr/share/pacman/keyrings/arches-trusted"
    ).read_text() == f"{setup['fpr']}:4:\n"
    assert (
        (root / "usr/share/pacman/keyrings/arches.gpg")
        .read_text()
        .startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    )


def test_iso_build_refuses_a_repo_signed_by_another_key(setup) -> None:
    assert _sign(setup).returncode == 0
    # The staged public key is someone else's.
    other = _new_key(setup["home"], "Someone Else <x@arches.invalid>")
    (setup["keys"] / "arches-local.asc").write_text(
        _gpg(setup["home"], "--armor", "--export", other).stdout
    )
    (setup["keys"] / "arches-local.fpr").write_text(other + "\n")
    r = _stage(setup, setup["repo"].parent / "profile")
    assert r.returncode == 1


def test_iso_build_refuses_without_a_key(setup) -> None:
    r = _stage(setup, setup["repo"].parent / "profile")
    assert r.returncode == 1
    assert "keys/README.md" in r.stderr


def test_test_only_build_signs_with_a_throwaway_key(setup) -> None:
    (setup["keys"] / "arches-local.fpr").unlink()  # no YubiKey key at all
    profile = setup["repo"].parent / "profile"
    env = {k: v for k, v in setup["env"].items() if k != "GNUPGHOME"}
    r = subprocess.run(
        ["bash", str(STAGE), str(profile)],
        env=dict(
            env,
            ARCHES_REPO_DIR=str(setup["repo"]),
            ARCHES_SKIP_BUILD_KEYRING="1",
            ARCHES_TEST_BUILD="1",
        ),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert r.returncode == 0, r.stderr
    root = profile / "airootfs"
    marker = (root / "opt/arches/TEST-BUILD").read_text()
    fpr = (root / "opt/arches/platform/arches-local.fpr").read_text().strip()
    assert fpr in marker and fpr != setup["fpr"]
    assert "TEST-ONLY" in (root / "etc/issue.d/arches-test-build.issue").read_text()

    # The packages verify against the staged throwaway key only.
    home = Path(tempfile.mkdtemp(prefix="gpg-"))
    try:
        _gpg(home, "--import", str(root / "opt/arches/platform/arches-local.asc"))
        out = _gpg(
            home,
            "--status-fd",
            "1",
            "--verify",
            f"{setup['pkgs'][0]}.sig",
            str(setup["pkgs"][0]),
        ).stdout
        assert any(
            ln.split()[1] == "VALIDSIG" and ln.split()[-1] == fpr
            for ln in out.splitlines()
            if ln.startswith("[GNUPG:]")
        )
    finally:
        subprocess.run(
            ["gpgconf", "--homedir", str(home), "--kill", "all"], check=False
        )
        shutil.rmtree(home, ignore_errors=True)

    # A normal build of the same tree: still refuses without the YubiKey
    # key, and leaves no TEST marker behind.
    r = _stage(setup, profile)
    assert r.returncode == 1
    assert "keys/README.md" in r.stderr
    assert not (root / "opt/arches/TEST-BUILD").exists()
    assert not (root / "etc/issue.d/arches-test-build.issue").exists()


def test_offline_cache_gets_the_repository_signatures(setup) -> None:
    _export(setup)
    assert _sign(setup).returncode == 0
    cache = setup["repo"].parent / "offline-cache"
    cache.mkdir()
    ours, other = setup["pkgs"]
    # Same bytes as arches-local, signature from an earlier key.
    (cache / ours.name).write_bytes(ours.read_bytes())
    (cache / f"{ours.name}.sig").write_bytes(b"signature from an earlier key")
    # Same name, other bytes: another repository's package (or a stale build).
    (cache / other.name).write_bytes(b"cachyos build")
    (cache / f"{other.name}.sig").write_bytes(b"cachyos signature")
    unrelated = cache / "zstd-1.5.7-4-x86_64.pkg.tar.zst"
    unrelated.write_bytes(b"x")
    r = _stage(setup, setup["repo"].parent / "profile", ARCHES_OFFLINE_CACHE=str(cache))
    assert r.returncode == 0, r.stderr
    assert (cache / f"{ours.name}.sig").read_bytes() == Path(f"{ours}.sig").read_bytes()
    assert not (cache / other.name).exists()
    assert not (cache / f"{other.name}.sig").exists()
    assert unrelated.exists()
