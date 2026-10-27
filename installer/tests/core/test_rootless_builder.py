"""The ISO builder must run on rootless Podman, never through sudo.

A sudoers rule such as ``user ALL=(root) NOPASSWD: /usr/bin/podman`` is
passwordless root for every process the user runs: ``podman run
--privileged -v /:/host`` is a root shell. These tests fail if the repo
again runs Podman through sudo, or tells the reader to create such a rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Podman run through sudo, with or without sudo options (-n, -E, -u root ...).
_SUDO_PODMAN = re.compile(r"\bsudo(\s+(-\S+|[A-Z_]+=\S*|root))*\s+(/usr/bin/)?podman\b")
# A sudoers rule that grants podman without a password.
_NOPASSWD_PODMAN = re.compile(r"NOPASSWD\s*:.*\bpodman\b")
# The sudoers drop-in the builder used to require.
_BUILDER_SUDOERS = re.compile(r"zz-arches-builder")

_PATTERNS = (_SUDO_PODMAN, _NOPASSWD_PODMAN, _BUILDER_SUDOERS)


def _scanned_files() -> list[Path]:
    files = [
        _REPO_ROOT / "Makefile",
        _REPO_ROOT / "Containerfile",
        _REPO_ROOT / "Containerfile.install",
        _REPO_ROOT / ".claude" / "instructions.md",
    ]
    files += sorted((_REPO_ROOT / "scripts").rglob("*"))
    files += sorted(_REPO_ROOT.glob("*.md"))
    files += sorted((_REPO_ROOT / "platforms").rglob("*.md"))
    files += sorted((_REPO_ROOT / "docs").rglob("*.md"))
    return [f for f in files if f.is_file() and "__pycache__" not in f.parts]


def _offences(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if any(p.search(line) for p in _PATTERNS)
    ]


@pytest.mark.parametrize(
    "line",
    [
        "sudo podman run --rm x",
        "sudo -n podman inspect arches-builder",
        "    sudo -n -E podman exec arches-builder true",
        "sudo /usr/bin/podman ps",
        'echo "$(whoami) ALL=(root) NOPASSWD: /usr/bin/podman"',
        "  | sudo tee /etc/sudoers.d/zz-arches-builder",
    ],
)
def test_patterns_catch_root_podman(line: str) -> None:
    assert _offences(line) == [line.strip()]


@pytest.mark.parametrize(
    "line",
    [
        "podman run --rm --privileged x",
        'sudo "$SCRIPT_DIR/write-usb.sh" "$ISO"',
        "sudo pacman -S podman",
        "builder ALL=(ALL) NOPASSWD: ALL",
        "Do not give your user passwordless `sudo` for `podman`.",
    ],
)
def test_patterns_allow_rootless_lines(line: str) -> None:
    assert _offences(line) == []


def test_scan_covers_the_build_path() -> None:
    names = {f.relative_to(_REPO_ROOT).as_posix() for f in _scanned_files()}
    for required in (
        "Makefile",
        "README.md",
        ".claude/instructions.md",
        "scripts/builder.sh",
        "scripts/build-iso.sh",
        "scripts/host-install.sh",
    ):
        assert required in names


def test_repo_never_runs_podman_through_sudo() -> None:
    found = []
    for path in _scanned_files():
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        found += [
            f"{path.relative_to(_REPO_ROOT)}: {line}" for line in _offences(text)
        ]
    assert found == [], "Podman must run rootless, never via sudo:\n" + "\n".join(
        found
    )
