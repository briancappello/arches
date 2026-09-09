"""Pinned Oh My Zsh build input and offline checkout (also a standalone helper)."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


ORIGIN = "https://github.com/ohmyzsh/ohmyzsh.git"
# Initial trust: official GitHub API over HTTPS, 2026-09-08. No independent
# signature verification claimed. Git content addressing authenticates reuse.
REVISION = "6aa16696d508f368586c5dc215b0f73a41d9ff16"


def git(*args: str, cwd: Path, network: bool = False) -> str:
    """Never inherit Git config, templates, hooks, helpers, or protocol overrides."""
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "init.templateDir=",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=always",
            "-c",
            f"protocol.https.allow={'always' if network else 'never'}",
            "-c",
            "fetch.fsckObjects=true",
            "-c",
            "transfer.fsckObjects=true",
            # Historical upstream tree 2b722785 has zero-padded modes. Keep the
            # normal fsck warning for this encoding, not a blanket integrity bypass.
            "-c",
            "fetch.fsck.zeroPaddedFilemode=warn",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(cwd),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "LANG": "C.UTF-8",
        },
    ).stdout


def verify_bundle(bundle: Path, directory: Path) -> Path:
    """Import into a fresh repository, without executing or checking out content."""
    if git("bundle", "list-heads", str(bundle), cwd=directory).splitlines() != [
        f"{REVISION} refs/heads/master"
    ]:
        raise ValueError("Oh My Zsh bundle must contain exactly the pinned master ref")
    repo = directory / "verified.git"
    git("clone", "--bare", "--template=", str(bundle), str(repo), cwd=directory)
    git("fsck", "--full", cwd=repo)
    if git("rev-parse", "refs/heads/master^{commit}", cwd=repo).strip() != REVISION:
        raise ValueError("Oh My Zsh bundle commit mismatch")
    return repo


def build(cache: Path) -> Path:
    """Fetch only the pin on a cache miss; a corrupt cache is a build failure."""
    cache.mkdir(parents=True, exist_ok=True)
    bundle = cache / f"ohmyzsh-{REVISION}.bundle"
    with tempfile.TemporaryDirectory(prefix="ohmyzsh-", dir=cache) as temporary:
        work = Path(temporary)
        if not bundle.exists():
            repo = work / "source.git"
            git("init", "--bare", "--template=", str(repo), cwd=work)
            git("fetch", "--no-tags", ORIGIN, REVISION, cwd=repo, network=True)
            git("update-ref", "refs/heads/master", REVISION, cwd=repo)
            candidate = work / "ohmyzsh.bundle"
            git("bundle", "create", str(candidate), "refs/heads/master", cwd=repo)
            verify_bundle(candidate, work)
            candidate.chmod(0o644)
            candidate.replace(bundle)
        else:
            verify_bundle(bundle, work)
    return bundle


def protected(path: Path, *, directory: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"Missing Oh My Zsh asset: {path}") from exc
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not kind(info.st_mode)
        or info.st_mode & 0o022
        or info.st_uid not in (0, os.geteuid())
    ):
        raise ValueError(f"Unsafe Oh My Zsh asset permissions/type: {path}")


def validate(root: Path) -> Path:
    """Check the protected ISO/target contract and bundle digest before use."""
    protected(root, directory=True)
    manifest = root / "firstboot-assets.json"
    protected(manifest)
    try:
        data = json.loads(manifest.read_text())
    except ValueError as exc:
        raise ValueError("Invalid Oh My Zsh asset manifest JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Invalid Oh My Zsh asset manifest")
    expected = data.get("ohmyzsh")
    if (
        data.get("version") != 1
        or not isinstance(expected, dict)
        or expected.get("revision") != REVISION
        or expected.get("origin") != ORIGIN
    ):
        raise ValueError("Missing or incompatible Oh My Zsh asset contract")
    protected(root / "firstboot-assets", directory=True)
    bundle = root / "firstboot-assets/ohmyzsh.bundle"
    protected(bundle)
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != expected.get("sha256"):
        raise ValueError("Oh My Zsh asset SHA-256 mismatch")
    return bundle


def required(root: Path) -> bool:
    """Older shipped media has neither marker; only that path permits network."""
    metadata = root / "target-provider.json"
    return (root / "firstboot-assets.json").exists() or (
        metadata.exists() and "firstboot_assets" in json.loads(metadata.read_text())
    )


def preflight(root: Path) -> None:
    if required(root):
        bundle = validate(root)
        with tempfile.TemporaryDirectory(prefix="ohmyzsh-check-") as directory:
            verify_bundle(bundle, Path(directory))


def install(root: Path, dest: Path) -> bool:
    bundle = validate(root)
    # Do not reset runtime updates/customizations on a manual firstboot retry.
    # Import and verify even on retry: damaged required inputs must fail closed.
    with tempfile.TemporaryDirectory(prefix=".ohmyzsh-", dir=dest.parent) as directory:
        work = Path(directory)
        repo = verify_bundle(bundle, work)
        if dest.exists() or dest.is_symlink():
            if (
                dest.is_symlink()
                or not (dest / ".git").is_dir()
                or not (dest / "oh-my-zsh.sh").is_file()
            ):
                raise ValueError(f"Oh My Zsh destination is not a working tree: {dest}")
            try:
                git(
                    "--git-dir",
                    str(dest / ".git"),
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                    cwd=dest,
                )
            except subprocess.CalledProcessError as exc:
                raise ValueError(
                    f"Oh My Zsh destination is not a working tree: {dest}"
                ) from exc
            return False
        checkout = work / "checkout"
        git(
            "clone",
            "--no-local",
            "--template=",
            "--no-checkout",
            "--branch",
            "master",
            str(repo),
            str(checkout),
            cwd=work,
        )
        git("remote", "set-url", "origin", ORIGIN, cwd=checkout)
        git("checkout", "master", cwd=checkout)
        checkout.chmod(0o755)
        checkout.rename(dest)
    return True


if __name__ == "__main__":
    try:
        print(
            "changed" if install(Path(sys.argv[1]), Path(sys.argv[2])) else "unchanged"
        )
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(f"Oh My Zsh offline asset failure: {exc}")
