#!/usr/bin/env python3
"""Pick the appropriate auto-install*.toml for a build.

Usage: pick-auto-install.py <templates_dir> <staged_dir>

Reads the ARCHES_TEMPLATE env var to choose the most specific
auto-install candidate whose ``[install].template`` field references
a template that has already been staged into ``<staged_dir>``.

Prints the absolute path of the chosen candidate, or nothing if none
match.

Selection order:
    1. auto-install-<arches_template_stem>.toml   (exact filter match)
    2. any auto-install-*.toml whose target template is staged AND
       whose suffix appears in the filter stem (e.g.
       auto-install-inference.toml matches ARCHES_TEMPLATE=llm-inference)
    3. auto-install.toml itself, if its target template is staged.

Validation:
    Refuses to emit a path if the selected auto-install file contains
    a placeholder password (``changeme``, ``password``, ``arches``, or
    empty). Override with ARCHES_ALLOW_DEFAULT_PASSWORD=1 if you
    really need to build with a placeholder credential (e.g. for a
    test ISO that will never reach a real network).

    Exits 2 with a clear error message when the password check fails.
    A password mismatch is a build error — silently shipping a
    publicly-known credential to a rack-mounted headless server is
    worse than a noisy build failure.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

# Passwords that are obviously placeholders or example values. Builds
# fail if any of these appears in [install].password. Operator must
# either edit the auto-install file or set ARCHES_ALLOW_DEFAULT_PASSWORD=1.
DEFAULT_PASSWORDS = {
    "changeme",
    "password",
    "arches",
    "admin",
    "root",
    "",
}


def _validate_password(toml_path: Path) -> str | None:
    """Return an error message if the password is a placeholder, else None."""
    if os.environ.get("ARCHES_ALLOW_DEFAULT_PASSWORD", "").strip() == "1":
        return None
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return f"failed to parse {toml_path}: {e}"

    pw = data.get("install", {}).get("password", "")
    if pw in DEFAULT_PASSWORDS:
        return (
            f"refusing to build with placeholder password "
            f"{pw!r} in {toml_path.name}.\n"
            f"        Either edit [install].password in that file, or "
            f"set ARCHES_ALLOW_DEFAULT_PASSWORD=1 to override (NOT "
            f"recommended for any host that will reach a real network)."
        )
    return None


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "Usage: pick-auto-install.py <templates_dir> <staged_dir>",
            file=sys.stderr,
        )
        return 2

    tpl_dir = Path(sys.argv[1])
    staged_dir = Path(sys.argv[2])

    staged = {p.name for p in staged_dir.glob("*.toml")}

    flt = os.environ.get("ARCHES_TEMPLATE", "").strip()
    if flt and not flt.endswith(".toml"):
        flt = f"{flt}.toml"
    flt_stem = Path(flt).stem if flt else ""

    candidates = sorted(tpl_dir.glob("auto-install*.toml"))

    def score(p: Path) -> int:
        name = p.stem
        suffix = name.removeprefix("auto-install-") if name != "auto-install" else ""
        # 0 = exact filter match (auto-install-<stem>.toml)
        if flt_stem and name == f"auto-install-{flt_stem}":
            return 0
        # 1 = suffix is a substring of the filter stem
        # (auto-install-inference matches llm-inference)
        if flt_stem and suffix and suffix in flt_stem:
            return 1
        # 2 = bare auto-install.toml (the project default)
        if name == "auto-install":
            return 2
        # 3 = some other auto-install-foo.toml whose suffix doesn't
        # match the filter; only useful when no filter is set.
        return 3

    for cand in sorted(candidates, key=score):
        try:
            with open(cand, "rb") as f:
                target = tomllib.load(f).get("install", {}).get("template", "")
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if target and target in staged:
            # Validate the password before emitting the path. We do
            # this LATE (after selection) so a build that lands on
            # auto-install.toml.bak or a unused-template variant
            # doesn't trip the check unnecessarily.
            err = _validate_password(cand)
            if err:
                print(f"ERROR: {err}", file=sys.stderr)
                return 2
            print(cand)
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
