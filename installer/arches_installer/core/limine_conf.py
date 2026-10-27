"""Merge into a limine.conf that another system owns (design D3).

When the ESP is shared, the new system's entries are added next to the
current ones by limine-entry-tool, which keys each OS entry on its
machine-id. The installer never replaces the file. It:

1. backs ``limine.conf`` up before anything can touch it
   (:func:`backup_limine_conf`);
2. after the bootloader phase, checks that the global options and every
   top-level entry that is not ours are byte-identical to the backup
   (:func:`merge_problems`);
3. restores the backup and fails the install if they are not.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from arches_installer.core.run import LogCallback, _log

CONF_NAME = "limine.conf"
_MACHINE_ID_RE = re.compile(r"^\s*comment:\s*machine-id=([0-9a-f]{32})\b", re.MULTILINE)


class LimineMergeError(RuntimeError):
    """The merge changed something that belongs to another system."""


@dataclass
class TopEntry:
    """A top-level menu entry and everything under it, verbatim."""

    title: str
    text: str

    @property
    def machine_id(self) -> str:
        m = _MACHINE_ID_RE.search(self.text)
        return m.group(1) if m else ""


def split_conf(text: str) -> tuple[str, list[TopEntry]]:
    """Split *text* into the global-options header and its top-level entries.

    A top-level entry starts at a line with exactly one leading ``/``
    (``/Title`` or ``/+Title``); sub-entries (``//``...) stay inside it.
    """
    header: list[str] = []
    entries: list[tuple[str, list[str]]] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("/") and not line.startswith("//"):
            entries.append((line[1:].lstrip("+").strip(), [line]))
        elif entries:
            entries[-1][1].append(line)
        else:
            header.append(line)
    return "".join(header), [TopEntry(t, "".join(lines)) for t, lines in entries]


def _norm(block: str) -> str:
    # Blank lines between entries are layout, not content.
    return block.rstrip("\n") + "\n"


def merge_problems(before: str, after: str, machine_id: str) -> list[str]:
    """Return what the merge changed that it must not have.

    Allowed: adding entries, and any change to the entry that carries
    *machine_id* (ours). Not allowed: changing the global options, or
    changing or dropping any other top-level entry.
    """
    problems: list[str] = []
    head_before, entries_before = split_conf(before)
    head_after, entries_after = split_conf(after)
    if head_before.strip() != head_after.strip():
        problems.append("the global options (before the first entry) changed")
    remaining = [_norm(e.text) for e in entries_after]
    for entry in entries_before:
        if machine_id and entry.machine_id == machine_id:
            continue
        block = _norm(entry.text)
        if block in remaining:
            remaining.remove(block)
        else:
            problems.append(f"entry {entry.title!r} was changed or removed")
    return problems


def backup_limine_conf(esp_mount: Path, log: LogCallback | None = None) -> Path | None:
    """Copy limine.conf next to itself, timestamped. Returns the backup path."""
    conf = esp_mount / CONF_NAME
    if not conf.is_file():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = esp_mount / f"{CONF_NAME}.arches-{stamp}.bak"
    shutil.copy2(conf, backup)
    _log(f"Backed up {conf} to {backup.name}", log)
    return backup


def check_merge(
    esp_mount: Path,
    backup: Path,
    machine_id: str,
    log: LogCallback | None = None,
) -> None:
    """Fail, and put the backup back, if the merge touched another system."""
    conf = esp_mount / CONF_NAME
    after = conf.read_text() if conf.is_file() else ""
    problems = merge_problems(backup.read_text(), after, machine_id)
    if not problems:
        _, entries = split_conf(after)
        ours = [e.title for e in entries if e.machine_id == machine_id]
        _log(
            f"limine.conf merged: other entries unchanged; ours: {', '.join(ours) or 'none'}",
            log,
        )
        return
    shutil.copy2(backup, conf)
    raise LimineMergeError(
        "The bootloader step changed limine.conf entries that belong to "
        "another system, so the backup was restored:\n  - " + "\n  - ".join(problems)
    )


def read_machine_id(root: Path) -> str:
    return (root / "etc" / "machine-id").read_text().strip()
