"""Runtime hardware reconciliation.

This module powers ``arches-hardware-rescan``, the systemd-driven CLI
that runs on every boot of an installed Arches system and re-evaluates
the hardware profile against what's currently on disk.

What it does on each boot:

1. Read the persisted hardware fingerprint at
   ``/var/lib/arches/hardware-fingerprint``.
2. Compute a fresh fingerprint from current PCI/DMI/chwd state.
3. If they match and ``--force`` was not given: exit 0 silently.
   This is the hot path — ~50 ms on a no-change boot.
4. If they differ (e.g. GPU was swapped, dock connected, motherboard
   replaced):

   a. Re-resolve quirks + machine profile against current hardware.
   b. Reconcile ``/etc/modprobe.d/``, ``/etc/udev/rules.d/``, and
      ``/etc/sysctl.d/`` — remove our orphans, add newcomers,
      rewrite updated content. Never touches files we don't own.
   c. Run ``chwd -a`` so the GPU driver profile follows the hardware.
      chwd is idempotent and handles its own removal/install.
   d. Run ``mkinitcpio -P`` if module-affecting files changed.
   e. Update the fingerprint + manifest.

All output goes through Python's ``logging`` module so the systemd
unit picks it up via journald automatically.

The tool is also useful manually:

    sudo arches-hardware-rescan              # idempotent rescan
    sudo arches-hardware-rescan --force      # always re-reconcile
    sudo arches-hardware-rescan --dry-run    # show what would change
    sudo arches-hardware-rescan --status     # print current state
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

# Import the module itself (not individual names) so tests can patch
# detect_pci_ids / get_dmi_info / detect_chwd_profile on the source
# module and have it take effect here.
from arches_installer.core import hardware as _hw
from arches_installer.core.hardware import (
    HardwareConfig,
    is_managed_file,
)
# Disk-role validation: ensure the disks recorded at install time are
# still present (or at least the same physical devices, even if their
# kernel /dev names have changed).
from arches_installer.core.disk import detect_block_devices
from arches_installer.core.disk_layout import validate_disk_roles

# Files written by the reconcile pass that, if changed, indicate a
# kernel-module-affecting update worth regenerating the initramfs for.
# Anything under modprobe.d influences module load behaviour;
# udev/sysctl don't.
_INITRAMFS_AFFECTING_CATEGORIES = {"modprobe"}

# Process exit codes — distinct values so a CI/automation caller can
# tell what happened without parsing logs.
EXIT_OK = 0
EXIT_NO_CHANGE = 0  # same as OK; alias for readability
EXIT_RECONCILED = 0
EXIT_DRYRUN_DIFF = 10  # dry-run found differences (informational)
EXIT_ERROR = 1
EXIT_NOT_ROOT = 2


log = logging.getLogger("arches-hardware-rescan")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_root() -> None:
    """Exit if not running as root.

    The reconcile pass writes to /etc and invokes chwd, both of which
    require root. The fingerprint-only fast path could in principle
    run unprivileged, but we want a single predictable behaviour.
    """
    import os

    if os.geteuid() != 0:
        log.error(
            "must run as root (writes to /etc and invokes chwd/mkinitcpio)"
        )
        sys.exit(EXIT_NOT_ROOT)


def _run_chwd(dry_run: bool) -> bool:
    """Run ``chwd -a`` to refresh GPU driver selection.

    Returns True if chwd ran (or would have under --dry-run). chwd is
    idempotent: if the right profile is already installed, it's a
    no-op. If a different profile applies (GPU swap), chwd removes
    the old driver packages and installs the new ones.
    """
    if shutil.which("chwd") is None:
        log.warning("chwd not installed — skipping driver re-selection")
        return False

    if dry_run:
        log.info("[dry-run] would run: chwd -a")
        return True

    log.info("running chwd -a to refresh GPU driver selection")
    try:
        result = subprocess.run(
            ["chwd", "-a"],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,  # driver install can be slow over network
        )
    except subprocess.TimeoutExpired:
        log.error("chwd timed out after 10 minutes")
        return False
    for line in result.stdout.splitlines():
        log.info("chwd: %s", line)
    for line in result.stderr.splitlines():
        log.warning("chwd: %s", line)
    if result.returncode != 0:
        log.error("chwd exited %d", result.returncode)
        return False
    return True


def _run_mkinitcpio(dry_run: bool) -> bool:
    """Regenerate all initramfs presets."""
    if shutil.which("mkinitcpio") is None:
        log.warning("mkinitcpio not installed — skipping initramfs rebuild")
        return False

    if dry_run:
        log.info("[dry-run] would run: mkinitcpio -P")
        return True

    log.info("regenerating initramfs (mkinitcpio -P)")
    try:
        result = subprocess.run(
            ["mkinitcpio", "-P"],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        log.error("mkinitcpio timed out after 5 minutes")
        return False
    for line in result.stdout.splitlines():
        log.info("mkinitcpio: %s", line)
    for line in result.stderr.splitlines():
        # mkinitcpio writes progress to stderr — only flag warnings/errors
        lvl = logging.WARNING if "WARNING" in line or "ERROR" in line else logging.INFO
        log.log(lvl, "mkinitcpio: %s", line)
    if result.returncode != 0:
        log.error("mkinitcpio exited %d", result.returncode)
        return False
    return True


# ---------------------------------------------------------------------------
# Main rescan logic
# ---------------------------------------------------------------------------


def cmd_status(target_root: Path) -> int:
    """Print current hardware state (no changes)."""
    pci_ids = _hw.detect_pci_ids()
    dmi = _hw.get_dmi_info()
    chwd_profile = _hw.detect_chwd_profile()
    current_fp = _hw.compute_fingerprint(pci_ids, dmi, chwd_profile)
    saved_fp = _hw.read_fingerprint(target_root)

    print(f"DMI vendor:     {dmi.sys_vendor or '(none)'}")
    print(f"DMI product:    {dmi.product_name or '(none)'}")
    print(f"DMI chassis:    {dmi.chassis_type}")
    print(f"PCI devices:    {len(pci_ids)} matched")
    print(f"chwd profile:   {chwd_profile or '(none)'}")
    print()
    print(f"current fp:     {current_fp}")
    print(f"saved fp:       {saved_fp or '(none — never recorded)'}")
    print()
    if not saved_fp:
        print("status:         FIRST-RUN — rescan will record baseline")
    elif saved_fp == current_fp:
        print("status:         UP-TO-DATE — no changes since last rescan")
    else:
        print("status:         CHANGED — rescan will reconcile")

    manifest = _hw.read_manifest(target_root)
    total = sum(len(v) for v in manifest.values())
    if total:
        print()
        print(f"managed files:  {total}")
        for cat, files in manifest.items():
            for f in files:
                print(f"  /etc/{cat}.d/{f}")

    # Disk-role validation (no-op if no roles were recorded).
    try:
        disks = detect_block_devices()
    except Exception:
        disks = []
    disk_report = validate_disk_roles(target_root, disks)
    if disk_report.ok or disk_report.has_issues:
        print()
        print("disk roles:")
        for role in disk_report.ok:
            print(f"  {role}: OK")
        for role, paths in disk_report.relocated.items():
            for old, new in paths:
                print(f"  {role}: RELOCATED  {old} -> {new}")
        for role, ids in disk_report.missing.items():
            for sid in ids:
                print(f"  {role}: MISSING    {sid}")
    return EXIT_OK


def _validate_disk_roles(target_root: Path) -> bool:
    """Run disk-role validation and log results. Returns True iff OK.

    Called from :func:`run_rescan`. Does NOT mutate state — disk
    layouts are too dangerous to auto-reconcile. We only log and let
    the operator intervene if something has drifted.
    """
    try:
        disks = detect_block_devices()
    except Exception as e:
        log.warning("disk role validation skipped: lsblk failed: %s", e)
        return True

    report = validate_disk_roles(target_root, disks)
    if not report.ok and not report.has_issues:
        # No roles recorded — older single-disk install or pre-roles
        # install. Silently skip.
        return True

    for role in report.ok:
        log.info("disk role %r: OK", role)
    for role, relocs in report.relocated.items():
        for old, new in relocs:
            log.info("disk role %r: relocated %s -> %s", role, old, new)
    for role, missing in report.missing.items():
        for sid in missing:
            log.warning(
                "disk role %r: device %r is MISSING. Filesystem mounts "
                "depending on this role may fail to come up. Run "
                "'arches-hardware-rescan --status' for details.",
                role,
                sid,
            )

    return not report.has_issues


def run_rescan(
    *,
    target_root: Path,
    force: bool = False,
    dry_run: bool = False,
    skip_chwd: bool = False,
    skip_mkinitcpio: bool = False,
) -> int:
    """The main reconcile pass. Returns a process exit code."""
    # --- Snapshot current hardware ---
    pci_ids = _hw.detect_pci_ids()
    dmi = _hw.get_dmi_info()
    chwd_profile = _hw.detect_chwd_profile()
    current_fp = _hw.compute_fingerprint(pci_ids, dmi, chwd_profile)
    saved_fp = _hw.read_fingerprint(target_root)

    log.info(
        "current hardware: %d PCI devices, DMI=%r/%r, chwd=%r",
        len(pci_ids),
        dmi.sys_vendor,
        dmi.product_name,
        chwd_profile or "(none)",
    )

    # --- Disk-role validation (always run, even on the fast path) ---
    # Cheap (one lsblk call) and important: a swapped disk doesn't
    # change PCI IDs or DMI, so the fingerprint would say "no change"
    # while a filesystem mount is silently broken. Run validation
    # FIRST so the log makes the issue visible early on every boot.
    _validate_disk_roles(target_root)

    # --- Fast path: fingerprint unchanged ---
    if not force and saved_fp and saved_fp == current_fp:
        log.info("fingerprint unchanged — no reconcile needed")
        return EXIT_NO_CHANGE

    if saved_fp and saved_fp != current_fp:
        log.info("fingerprint CHANGED — reconciling")
        log.info("  old: %s", saved_fp)
        log.info("  new: %s", current_fp)
    elif not saved_fp:
        log.info("no prior fingerprint — recording baseline")
    elif force:
        log.info("--force given — reconciling regardless of fingerprint")

    # --- Re-resolve hardware config from current state ---
    all_quirks = _hw.discover_quirks()
    all_machines = _hw.discover_machines()
    matched = _hw.match_quirks(all_quirks, pci_ids, dmi.chassis_type)
    machine = _hw.suggest_machine(all_machines, dmi)
    hw = _hw.resolve_hardware(machine, matched, all_quirks=all_quirks)

    if machine:
        log.info("matched machine profile: %s", machine.slug)
    if hw.quirks:
        log.info(
            "applicable quirks: %s",
            ", ".join(sorted(q.slug for q in hw.quirks)),
        )
    else:
        log.info("applicable quirks: (none)")

    # --- Reconcile file deployment ---
    if dry_run:
        # Compute what would change without writing anything. We do this
        # by reading the current manifest and comparing against the new
        # resolved set.
        from arches_installer.core.hardware import _resolved_files

        desired = _resolved_files(hw)
        desired_names = {
            cat: set(entries.keys()) for cat, entries in desired.items()
        }
        existing = _hw.read_manifest(target_root)
        existing_names = {cat: set(v) for cat, v in existing.items()}

        any_change = False
        for cat in ("modprobe", "udev", "sysctl"):
            to_add = desired_names[cat] - existing_names.get(cat, set())
            to_remove = existing_names.get(cat, set()) - desired_names[cat]
            for f in sorted(to_add):
                log.info("[dry-run] would add:    %s/%s", cat, f)
                any_change = True
            for f in sorted(to_remove):
                log.info("[dry-run] would remove: %s/%s", cat, f)
                any_change = True

        if not skip_chwd:
            log.info("[dry-run] would run: chwd -a")
        if any_change and not skip_mkinitcpio:
            log.info("[dry-run] would run: mkinitcpio -P")
        log.info("[dry-run] would update fingerprint to: %s", current_fp)
        return EXIT_DRYRUN_DIFF if any_change else EXIT_NO_CHANGE

    added, removed = _hw.reconcile_hardware_files(
        hw,
        log=lambda msg: log.info(msg),
        target_root=target_root,
    )

    modules_changed = any(
        added.get(cat) or removed.get(cat)
        for cat in _INITRAMFS_AFFECTING_CATEGORIES
    )

    # --- chwd: refresh GPU driver selection ---
    chwd_ran_ok = True
    if not skip_chwd:
        chwd_ran_ok = _run_chwd(dry_run=False)
        # Re-read chwd profile so the fingerprint reflects post-chwd state
        if chwd_ran_ok:
            chwd_profile = _hw.detect_chwd_profile()

    # --- mkinitcpio: only if module config changed ---
    if modules_changed and not skip_mkinitcpio:
        _run_mkinitcpio(dry_run=False)
    elif not modules_changed:
        log.info("no module-affecting changes — skipping mkinitcpio")

    # --- Persist state ---
    # Rebuild the full manifest from disk (added/removed only tell us
    # the diff; we want the complete set).
    new_manifest = {
        "modprobe": sorted(
            f.name
            for f in (target_root / "etc/modprobe.d").iterdir()
            if f.is_file() and is_managed_file(f)
        )
        if (target_root / "etc/modprobe.d").is_dir()
        else [],
        "udev": sorted(
            f.name
            for f in (target_root / "etc/udev/rules.d").iterdir()
            if f.is_file() and is_managed_file(f)
        )
        if (target_root / "etc/udev/rules.d").is_dir()
        else [],
        "sysctl": sorted(
            f.name
            for f in (target_root / "etc/sysctl.d").iterdir()
            if f.is_file() and is_managed_file(f)
        )
        if (target_root / "etc/sysctl.d").is_dir()
        else [],
    }
    _hw.write_manifest(new_manifest, target_root, log=lambda msg: log.info(msg))

    # Recompute fingerprint with the (possibly updated) chwd profile
    new_fp = _hw.compute_fingerprint(pci_ids, dmi, chwd_profile)
    _hw.write_fingerprint(new_fp, target_root, log=lambda msg: log.info(msg))

    total_added = sum(len(v) for v in added.values())
    total_removed = sum(len(v) for v in removed.values())
    log.info(
        "reconcile complete: %d files added/updated, %d removed, "
        "modules_changed=%s, chwd_ok=%s",
        total_added,
        total_removed,
        modules_changed,
        chwd_ran_ok,
    )
    return EXIT_RECONCILED


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arches-hardware-rescan",
        description=(
            "Re-evaluate hardware quirks and GPU drivers on the running "
            "system. Runs idempotently — fast no-op when nothing changed."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reconcile even if the hardware fingerprint is unchanged.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without modifying any files.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print current hardware state and exit (no changes).",
    )
    parser.add_argument(
        "--skip-chwd",
        action="store_true",
        help="Skip the chwd -a invocation (driver selection unchanged).",
    )
    parser.add_argument(
        "--skip-mkinitcpio",
        action="store_true",
        help="Skip mkinitcpio -P even if module config changed.",
    )
    parser.add_argument(
        "--target-root",
        type=Path,
        default=Path("/"),
        help=(
            "Filesystem root to operate on (default: /). Useful for "
            "testing against a mounted chroot."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug-level logging.",
    )
    args = parser.parse_args(argv)

    # Configure logging. When invoked under systemd (no TTY on stdout)
    # the journald handler attaches via stdout/stderr automatically.
    # Use a plain format — systemd-journald adds its own timestamps.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )

    if args.status:
        return cmd_status(args.target_root)

    # All mutating commands need root
    if not args.dry_run:
        _ensure_root()

    try:
        return run_rescan(
            target_root=args.target_root,
            force=args.force,
            dry_run=args.dry_run,
            skip_chwd=args.skip_chwd,
            skip_mkinitcpio=args.skip_mkinitcpio,
        )
    except KeyboardInterrupt:
        log.error("interrupted")
        return EXIT_ERROR
    except Exception as e:
        log.exception("rescan failed: %s", e)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
