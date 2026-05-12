"""Disk layout loading, application, and RAID management.

A disk layout defines a partition scheme: a list of partition specs
(filesystem, size, mount point, subvolumes) that are applied to a block
device.  Layouts are defined in TOML files under ``disk-layouts/``.

This module also handles btrfs multi-device RAID and mdadm RAID setup,
and ESP mirroring for RAID 1/10 configurations.
"""

from __future__ import annotations

import enum
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arches_installer.core.disk import (
    MOUNT_ROOT,
    BlockDevice,
    PartitionMap,
    _part_name,
)
from arches_installer.core.disk_descriptor import (
    DescriptorError,
    DiskCriteria,
    describe_failure,
    match_disks,
    parse_descriptor,
)
from arches_installer.core.run import LogCallback, _log
from arches_installer.core.run import run as logged_run

# The default role name for layouts that don't declare any [[disks]]
# entries. The implicit single-disk default we synthesise for backward
# compatibility with basic.toml / flexible.toml uses this name.
DEFAULT_DISK_ROLE = "primary"

# Search paths for the disk-layouts directory, checked in order.
# On the live ISO, layouts are staged at /opt/arches/disk-layouts/.
# In development, they're at <project>/disk-layouts/ (relative to the
# installer package: installer/arches_installer/core/disk_layout.py -> ../../../../disk-layouts).
_LAYOUTS_SEARCH = [
    Path("/opt/arches/disk-layouts"),
    Path(__file__).resolve().parents[3] / "disk-layouts",
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class SubvolumeSpec:
    """A btrfs subvolume within a partition."""

    name: str  # e.g. "@", "@home", "@var"
    mount_point: str | None = None  # e.g. "/", "/home", "/var"; None = not mounted


@dataclass
class DiskRole:
    """A named role that one or more physical disks fulfil.

    Layouts declare roles in ``[[disks]]`` entries. Partitions then
    reference roles by name in their ``disk`` (singular, one device)
    or ``disks`` (plural, used with ``raid_level``) field.

    The actual physical-device assignment is done at install time by
    :func:`resolve_disk_roles` which evaluates the descriptor against
    the detected hardware. The same role name can appear once in a
    layout, optionally be overridden by a machine profile, and
    optionally be overridden again by the auto-install config.
    """

    name: str
    # The descriptor — natural-language string or structured dict.
    # Parsed lazily into a DiskCriteria so layouts can be loaded
    # without touching the filesystem.
    descriptor: str | dict[str, Any] = ""

    def criteria(self) -> DiskCriteria:
        """Parse the descriptor on demand."""
        return parse_descriptor(self.descriptor)


@dataclass
class PartitionSpec:
    """A single partition in a disk layout.

    For multi-disk layouts, ``disk`` references the role name of the
    target disk. When ``raid_level`` is set, the role is expected to
    resolve to multiple physical disks and the partition is created
    on all of them, joined by the appropriate RAID mechanism.

    When neither ``disk`` nor ``disks`` is set, the partition targets
    the implicit ``DEFAULT_DISK_ROLE`` ("primary") — keeping single-disk
    layouts (basic.toml, flexible.toml) working unchanged.
    """

    size: str  # "2G", "100G", "*" (fill rest)
    filesystem: str = ""  # "vfat", "ext4", "btrfs", "xfs", "swap", "" = raw
    mount_point: str | None = None  # "/boot", "/", "/home", None = not mounted
    label: str = ""
    mount_options: str = ""
    subvolumes: list[SubvolumeSpec] = field(default_factory=list)
    # Multi-disk addressing. Exactly one of these names a role:
    #   disk: single-disk partition (most common)
    #   disks: multi-disk partition forming a RAID volume
    disk: str = ""  # role name; defaults to DEFAULT_DISK_ROLE at resolve time
    disks: str = ""  # role name (same shape as `disk` but for RAID)
    raid_level: str = ""  # "0", "1", "5", "6", "10"; only valid with `disks`
    raid_backend: str = ""  # "btrfs" or "mdadm"; defaults to filesystem-native

    @property
    def target_role(self) -> str:
        """Resolve which role name this partition targets."""
        return self.disks or self.disk or DEFAULT_DISK_ROLE

    @property
    def is_multidisk(self) -> bool:
        """True if this partition needs multiple physical disks (RAID)."""
        return bool(self.disks) and bool(self.raid_level)


@dataclass
class DiskLayout:
    """A complete disk layout specification loaded from TOML."""

    name: str
    description: str
    bootloaders: list[str]  # ["limine"], ["grub"], ["limine", "grub"]
    partitions: list[PartitionSpec]
    disks: list[DiskRole] = field(default_factory=list)
    path: Path | None = None  # source TOML file path

    @property
    def role_names(self) -> set[str]:
        """All role names declared in this layout."""
        return {d.name for d in self.disks}

    def has_explicit_disks(self) -> bool:
        """True if the layout has at least one [[disks]] entry.

        Layouts without any [[disks]] block get an implicit single-disk
        default applied during resolution (see :func:`resolve_disk_roles`).
        """
        return bool(self.disks)


class RaidLevel(enum.Enum):
    """Supported RAID levels."""

    RAID0 = 0
    RAID1 = 1
    RAID10 = 10


class RaidBackend(enum.Enum):
    """RAID implementation backend."""

    MDADM = "mdadm"
    BTRFS = "btrfs"


@dataclass
class RaidConfig:
    """RAID configuration for multi-disk setups."""

    level: RaidLevel
    backend: RaidBackend
    devices: list[str]  # all physical device paths


# ---------------------------------------------------------------------------
# Size parsing
# ---------------------------------------------------------------------------

# Matches "2G", "512M", "100G", etc. — number + unit
_SIZE_RE = re.compile(r"^(\d+)([MGT])$", re.IGNORECASE)


def parse_size_spec(spec: str) -> str:
    """Convert a human-readable size spec to an sgdisk size argument.

    "2G"   -> "+2G"
    "512M" -> "+512M"
    "*"    -> "0"  (fill remaining space)

    Raises ValueError for unrecognized formats.
    """
    if spec == "*":
        return "0"
    m = _SIZE_RE.match(spec)
    if not m:
        raise ValueError(
            f"Invalid partition size: {spec!r}. "
            f"Expected a number with M/G/T suffix (e.g. '2G', '512M') or '*'."
        )
    return f"+{m.group(1)}{m.group(2).upper()}"


# ---------------------------------------------------------------------------
# TOML loading and discovery
# ---------------------------------------------------------------------------


def _parse_partition(data: dict[str, Any]) -> PartitionSpec:
    """Parse a single [[partitions]] entry from TOML."""
    subvols_raw = data.get("subvolumes", [])
    subvols = [
        SubvolumeSpec(
            name=sv["name"],
            mount_point=sv.get("mount_point"),
        )
        for sv in subvols_raw
    ]
    return PartitionSpec(
        size=data["size"],
        filesystem=data.get("filesystem", ""),
        mount_point=data.get("mount_point"),
        label=data.get("label", ""),
        mount_options=data.get("mount_options", ""),
        subvolumes=subvols,
        disk=data.get("disk", ""),
        disks=data.get("disks", ""),
        raid_level=str(data.get("raid_level", "")),
        raid_backend=data.get("raid_backend", ""),
    )


def _parse_disk_role(data: dict[str, Any]) -> DiskRole:
    """Parse a single [[disks]] entry from TOML.

    Accepts either:
        [[disks]]
        name = "root"
        device = "2TB NVMe SSD"           # string descriptor

    or:
        [[disks]]
        name = "root"
        device = { transport = "nvme", size = "2T" }  # structured descriptor
    """
    if "name" not in data:
        raise ValueError("[[disks]] entry missing required 'name' field")
    name = str(data["name"])
    if not name:
        raise ValueError("[[disks]] entry has empty 'name'")
    descriptor: str | dict[str, Any] = data.get("device", "")
    # device can be either a string or a TOML table (parsed to dict).
    # We pass it through unchanged; the descriptor parser handles both.
    return DiskRole(name=name, descriptor=descriptor)


def _validate_layout(layout: DiskLayout) -> list[str]:
    """Validate a disk layout, returning a list of error messages.

    Empty list means valid.
    """
    errors: list[str] = []

    # Duplicate role names — checked independently of partition state
    # so tests can verify role-only validation without supplying
    # partitions, and so a layout with only roles defined still flags
    # this fatal mistake.
    role_seen: set[str] = set()
    for r in layout.disks:
        if r.name in role_seen:
            errors.append(f"Duplicate disk role name: {r.name!r}")
        role_seen.add(r.name)

    if not layout.partitions:
        errors.append("Layout has no partitions defined.")
        return errors

    # ── Per-disk "*" rule: only one fill-rest per disk role ──
    # Old single-disk rule was "'*' must be the last partition".
    # In multi-disk layouts, "fill the rest" makes sense per-disk, so
    # we group partitions by their target role and enforce that '*'
    # only appears in the last partition for each role.
    by_role: dict[str, list[int]] = {}
    for i, part in enumerate(layout.partitions):
        by_role.setdefault(part.target_role, []).append(i)
    for role, indices in by_role.items():
        for pos_in_role, layout_idx in enumerate(indices):
            part = layout.partitions[layout_idx]
            if part.size == "*" and pos_in_role != len(indices) - 1:
                errors.append(
                    f"Partition {layout_idx + 1} ({part.label or 'unlabeled'}) "
                    f"uses size '*' but is not the LAST partition for disk "
                    f"role {role!r}. Only the final partition on each disk "
                    f"can fill remaining space."
                )

    # Check for duplicate mount points
    mount_points = [
        p.mount_point for p in layout.partitions if p.mount_point is not None
    ]
    seen: set[str] = set()
    for mp in mount_points:
        if mp in seen:
            errors.append(f"Duplicate mount point: {mp}")
        seen.add(mp)

    # Check subvolumes only on btrfs partitions
    for i, part in enumerate(layout.partitions):
        if part.subvolumes and part.filesystem != "btrfs":
            errors.append(
                f"Partition {i + 1} ({part.label or 'unlabeled'}) has "
                f"subvolumes but filesystem is {part.filesystem!r}, not 'btrfs'."
            )

    # ── Disk-role references must resolve to a declared role ──
    # When layout.disks is empty, every partition implicitly targets
    # DEFAULT_DISK_ROLE; that's fine. When layout.disks is non-empty,
    # every partition's target_role must be in that set.
    if layout.disks:
        declared = layout.role_names
        for i, part in enumerate(layout.partitions):
            role = part.target_role
            if role not in declared:
                errors.append(
                    f"Partition {i + 1} ({part.label or 'unlabeled'}) "
                    f"references disk role {role!r} which is not declared in "
                    f"[[disks]]. Available roles: {sorted(declared)}"
                )

    # ── RAID validation ──
    # raid_level must be paired with `disks` (plural); raid_level on a
    # single-disk partition is meaningless.
    for i, part in enumerate(layout.partitions):
        if part.raid_level and not part.disks:
            errors.append(
                f"Partition {i + 1} sets raid_level={part.raid_level!r} but "
                f"does not set `disks` (plural). RAID requires multiple disks."
            )
        if part.raid_level and part.raid_level not in {"0", "1", "5", "6", "10"}:
            errors.append(
                f"Partition {i + 1} has unsupported raid_level "
                f"{part.raid_level!r}. Supported: 0, 1, 5, 6, 10."
            )
        if part.raid_backend and part.raid_backend not in {"btrfs", "mdadm"}:
            errors.append(
                f"Partition {i + 1} has unsupported raid_backend "
                f"{part.raid_backend!r}. Supported: btrfs, mdadm."
            )

    return errors


def load_disk_layout(path: Path) -> DiskLayout:
    """Load a disk layout from a TOML file.

    Raises ``ValueError`` if the layout fails validation.
    """
    with open(path, "rb") as f:
        data = tomllib.load(f)

    meta = data.get("meta", {})
    partitions_raw = data.get("partitions", [])
    disks_raw = data.get("disks", [])

    layout = DiskLayout(
        name=meta.get("name", "Unknown"),
        description=meta.get("description", ""),
        bootloaders=meta.get("bootloaders", []),
        partitions=[_parse_partition(p) for p in partitions_raw],
        disks=[_parse_disk_role(d) for d in disks_raw],
        path=path,
    )

    errors = _validate_layout(layout)
    if errors:
        err_str = "; ".join(errors)
        raise ValueError(f"Invalid disk layout {path.name}: {err_str}")

    return layout


# ---------------------------------------------------------------------------
# Disk-role resolution (layout × machine × auto-install → device map)
# ---------------------------------------------------------------------------


class DiskRoleResolutionError(ValueError):
    """Raised when role resolution fails (zero / too many / wrong count)."""


def _required_count_for_partition(part: PartitionSpec) -> int | None:
    """Return the exact number of disks a partition requires for its role.

    None means "no fixed requirement" (single-disk partitions on a role
    that simply matches one disk). Used to validate RAID arity at
    resolution time.
    """
    if not part.is_multidisk:
        return None
    # Canonical disk counts per RAID level. Some RAID5/6/10 configs accept
    # more than the minimum (e.g. RAID-5 with 4 disks), but for simplicity
    # of explicit-intent matching we treat the minimum as the required
    # count. Users wanting wider RAIDs can declare multiple roles
    # ("mirror-a", "mirror-b") or set count via descriptor matching.
    minimums = {"0": 2, "1": 2, "5": 3, "6": 4, "10": 4}
    return minimums.get(part.raid_level)


def _required_count_for_role(layout: DiskLayout, role: str) -> int | None:
    """Return the max required count across all partitions on this role.

    If any partition on the role is multi-disk RAID, that count wins.
    """
    counts: list[int] = []
    for p in layout.partitions:
        if p.target_role != role:
            continue
        n = _required_count_for_partition(p)
        if n is not None:
            counts.append(n)
    if not counts:
        return None
    return max(counts)


def _merge_role_overrides(
    layout: DiskLayout,
    machine_disks: list[DiskRole] | None,
    auto_install_disks: list[DiskRole] | None,
) -> dict[str, DiskRole]:
    """Apply the CSS-like specificity stack to produce a final role map.

    Layout < machine < auto-install. Later sources override earlier
    ones by role name. Missing roles in higher layers fall through
    to the layout's spec.
    """
    merged: dict[str, DiskRole] = {r.name: r for r in layout.disks}
    for r in machine_disks or []:
        merged[r.name] = r
    for r in auto_install_disks or []:
        merged[r.name] = r

    # Synthesise the implicit default if the layout declared no disks.
    # This keeps basic.toml / flexible.toml working unchanged.
    if not layout.disks and DEFAULT_DISK_ROLE not in merged:
        merged[DEFAULT_DISK_ROLE] = DiskRole(
            name=DEFAULT_DISK_ROLE,
            descriptor={"removable": False},
        )

    return merged


@dataclass
class ResolvedDiskRoles:
    """Final role → physical-disk mapping for a single install."""

    # role name -> ordered list of BlockDevices fulfilling that role
    assignments: dict[str, list[BlockDevice]] = field(default_factory=dict)
    # role name -> the DiskRole spec that won the override stack
    roles: dict[str, DiskRole] = field(default_factory=dict)

    def primary(self, role: str) -> BlockDevice:
        """Convenience: the first device assigned to *role*.

        Raises KeyError if the role isn't assigned. Used by callers
        that only want the lead disk (ESP placement, mdadm primary, etc.).
        """
        return self.assignments[role][0]

    def devices(self, role: str) -> list[BlockDevice]:
        return self.assignments[role]

    def to_state_dict(self) -> dict[str, Any]:
        """Build a JSON-serialisable snapshot for /var/lib/arches/disk-roles.json.

        Records the *stable* identifiers for each role's assigned disks
        (serial, wwn, by-id symlink names) — NOT the /dev/... paths, which
        can change across reboots. The runtime arches-hardware-rescan
        validates that every recorded serial is still present and warns
        if a disk has been removed.
        """
        out: dict[str, Any] = {"version": 1, "roles": {}}
        for role_name, devices in self.assignments.items():
            spec = self.roles.get(role_name)
            out["roles"][role_name] = {
                "descriptor": spec.descriptor if spec else "",
                "devices": [
                    {
                        "serial": d.serial,
                        "wwn": d.wwn,
                        "model": d.model,
                        "size_bytes": d.size_bytes,
                        "transport": d.transport,
                        "by_id_links": list(d.by_id_links),
                        # Path at install time — useful for debugging,
                        # NOT for re-resolution. hardware-rescan
                        # re-derives the live path via by-id at runtime.
                        "path_at_install": d.path,
                    }
                    for d in devices
                ],
            }
        return out


def write_disk_roles_state(
    resolved: ResolvedDiskRoles,
    target_root: Path,
    log: LogCallback | None = None,
) -> None:
    """Persist the resolved-roles snapshot to <target>/var/lib/arches/disk-roles.json.

    Called by the install pipeline after a successful install. The
    runtime hardware-rescan service reads this file on every boot to
    validate that the disks are still where the layout said they
    should be.
    """
    import json

    state_dir = target_root / "var" / "lib" / "arches"
    state_dir.mkdir(parents=True, exist_ok=True)
    fp = state_dir / "disk-roles.json"
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(resolved.to_state_dict(), indent=2, sort_keys=True) + "\n"
    )
    tmp.replace(fp)
    _log(f"  wrote disk roles snapshot -> {fp}", log)


def read_disk_roles_state(target_root: Path) -> dict[str, Any]:
    """Read the persisted disk-roles snapshot. Returns {} if absent."""
    import json

    fp = target_root / "var" / "lib" / "arches" / "disk-roles.json"
    try:
        return json.loads(fp.read_text())
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return {}


@dataclass
class DiskRolesValidationReport:
    """Result of validating persisted disk-roles against live hardware.

    The hardware-rescan service consumes this to decide what to log /
    warn the operator about. We never auto-rebalance RAID arrays or
    re-resolve roles at runtime — that would invite data loss. Instead
    we surface drift loudly and let the operator decide.
    """

    # role name -> list of stable IDs (serials or by-id) that should be
    # present but are not findable on the live system.
    missing: dict[str, list[str]] = field(default_factory=dict)
    # role name -> list of (recorded_path, live_path) where the device
    # is still here but enumerated under a different /dev/... name.
    relocated: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # Roles whose recorded disks are all still present unchanged.
    ok: list[str] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return bool(self.missing) or bool(self.relocated)


def validate_disk_roles(
    target_root: Path,
    candidates: list[BlockDevice],
) -> DiskRolesValidationReport:
    """Compare the persisted disk-roles snapshot against current hardware.

    For each role recorded at install time, look for the same physical
    disks now (matched by serial OR by-id symlink — both are stable
    across reboots). Returns a report distinguishing:
      - OK: every recorded device is still present (path may differ)
      - relocated: device still here, /dev/... path changed
      - missing: device with that serial/wwn/by-id is gone

    Pure observation — never modifies disk state.
    """
    state = read_disk_roles_state(target_root)
    report = DiskRolesValidationReport()
    if not state or "roles" not in state:
        return report

    # Build lookup tables for fast matching.
    by_serial = {c.serial: c for c in candidates if c.serial}
    by_wwn = {c.wwn: c for c in candidates if c.wwn}
    by_byid: dict[str, BlockDevice] = {}
    for c in candidates:
        for link in c.by_id_links:
            by_byid[link] = c

    for role_name, role_data in state["roles"].items():
        role_devices = role_data.get("devices", [])
        if not role_devices:
            continue

        missing: list[str] = []
        relocated: list[tuple[str, str]] = []
        all_ok = True

        for rec in role_devices:
            # Find this recorded device in the live candidates.
            live: BlockDevice | None = None
            stable_id = rec.get("serial") or rec.get("wwn") or "(unknown)"

            if rec.get("serial") and rec["serial"] in by_serial:
                live = by_serial[rec["serial"]]
            elif rec.get("wwn") and rec["wwn"] in by_wwn:
                live = by_wwn[rec["wwn"]]
            else:
                for link in rec.get("by_id_links", []):
                    if link in by_byid:
                        live = by_byid[link]
                        break

            if live is None:
                missing.append(stable_id)
                all_ok = False
            else:
                old_path = rec.get("path_at_install", "")
                if old_path and old_path != live.path:
                    relocated.append((old_path, live.path))
                    # Relocations are not OK-or-not — record but don't
                    # flip the all_ok flag, since the device IS still
                    # here. The hardware-rescan logs them at info.

        if missing:
            report.missing[role_name] = missing
        if relocated:
            report.relocated[role_name] = relocated
        if all_ok and not relocated:
            report.ok.append(role_name)

    return report


def resolve_disk_roles(
    layout: DiskLayout,
    candidates: list[BlockDevice],
    *,
    machine_disks: list[DiskRole] | None = None,
    auto_install_disks: list[DiskRole] | None = None,
) -> ResolvedDiskRoles:
    """Resolve every role declared in *layout* to a list of physical disks.

    Selection rules:
        1. Build the effective role spec map (layout < machine < auto-install).
        2. For each role, evaluate the descriptor against *candidates*.
        3. Validate the match count against the role's partition usage:
            - Single-disk role: exactly 1 match required.
            - RAID role: exact count derived from raid_level required.
        4. Raise :class:`DiskRoleResolutionError` with a detailed message
           if any role fails to resolve.

    The match-evaluation order across roles is stable: sorted by role
    name. The within-role device ordering is the sort returned by
    :func:`match_disks` (by serial, then path), so RAID arrays use the
    same physical-disk → role-index mapping on every install.
    """
    effective = _merge_role_overrides(layout, machine_disks, auto_install_disks)

    # Collect each role's required device count.
    required: dict[str, int | None] = {
        role: _required_count_for_role(layout, role) for role in effective
    }
    # Roles that exist in `effective` but aren't referenced by any
    # partition still need to resolve (someone may add a partition
    # referencing it later via override), but we don't enforce a count.

    # Resolve roles. We sort by name so error messages are deterministic.
    assignments: dict[str, list[BlockDevice]] = {}
    errors: list[str] = []

    # Track which physical disks have been claimed by which role so we
    # can reject overlapping claims. A single physical disk can't fill
    # two distinct roles (would mean partitioning it twice).
    claimed: dict[str, str] = {}  # device path -> role name that claimed it

    for role_name in sorted(effective.keys()):
        role = effective[role_name]
        try:
            criteria = role.criteria()
        except DescriptorError as e:
            errors.append(f"role {role_name!r}: invalid descriptor: {e}")
            continue

        matched = match_disks(criteria, candidates)

        # Reject candidates already claimed by another role.
        unclaimed = [d for d in matched if d.path not in claimed]
        already = [d for d in matched if d.path in claimed]

        if not unclaimed:
            if already:
                conflicts = ", ".join(
                    f"{d.path} (claimed by role {claimed[d.path]!r})" for d in already
                )
                errors.append(
                    f"role {role_name!r} matched only disks claimed by "
                    f"earlier roles: {conflicts}. Refine descriptors so "
                    f"each role matches distinct disks."
                )
            else:
                errors.append(
                    f"role {role_name!r}: {describe_failure(criteria, candidates)}"
                )
            continue

        # Validate match count.
        n_required = required.get(role_name)
        if n_required is not None:
            if len(unclaimed) < n_required:
                errors.append(
                    f"role {role_name!r}: RAID needs {n_required} disks, "
                    f"only {len(unclaimed)} matched. Matched: "
                    f"{[d.path for d in unclaimed]}"
                )
                continue
            if len(unclaimed) > n_required:
                errors.append(
                    f"role {role_name!r}: descriptor matched "
                    f"{len(unclaimed)} disks but only {n_required} are "
                    f"required for the configured RAID. Matched: "
                    f"{[d.path for d in unclaimed]}. Refine the descriptor "
                    f"(add a serial, by-id pattern, or model variant) so "
                    f"it matches exactly {n_required}, or use multiple "
                    f"distinct roles."
                )
                continue
        else:
            # Single-disk role: must resolve to exactly 1.
            if len(unclaimed) > 1:
                errors.append(
                    f"role {role_name!r}: descriptor matched "
                    f"{len(unclaimed)} disks (ambiguous). Refine to "
                    f"pick exactly one. Matched: "
                    f"{[d.path for d in unclaimed]}"
                )
                continue

        # Take the leading N devices (or just 1 for single-disk roles).
        take = n_required if n_required is not None else 1
        chosen = unclaimed[:take]
        assignments[role_name] = chosen
        for d in chosen:
            claimed[d.path] = role_name

    if errors:
        raise DiskRoleResolutionError(
            "Failed to resolve disk roles:\n  - " + "\n  - ".join(errors)
        )

    return ResolvedDiskRoles(assignments=assignments, roles=effective)


def _find_layouts_dir() -> Path:
    """Locate the disk-layouts directory from the search path."""
    for d in _LAYOUTS_SEARCH:
        if d.is_dir():
            return d
    searched = ", ".join(str(d) for d in _LAYOUTS_SEARCH)
    raise FileNotFoundError(f"Disk layouts directory not found (searched: {searched})")


def resolve_disk_layout(filename: str) -> Path:
    """Resolve a disk layout filename to its full path.

    Accepts a bare filename like ``"basic.toml"`` and returns the absolute
    path.  Raises ``FileNotFoundError`` if the file does not exist.
    """
    layouts_dir = _find_layouts_dir()
    path = layouts_dir / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"Disk layout not found: {filename} (looked in {layouts_dir})"
        )
    return path


def discover_disk_layouts() -> list[DiskLayout]:
    """Discover all available disk layout files.

    Returns layouts sorted by name.
    """
    layouts_dir = _find_layouts_dir()
    layouts: list[DiskLayout] = []

    for item in sorted(layouts_dir.iterdir()):
        if item.suffix == ".toml":
            try:
                layout = load_disk_layout(item)
                if layout.name != "Unknown":
                    layouts.append(layout)
            except (KeyError, ValueError, tomllib.TOMLDecodeError):
                pass  # Skip malformed layout files

    return sorted(layouts, key=lambda la: la.name)


# ---------------------------------------------------------------------------
# RAID setup
# ---------------------------------------------------------------------------


def setup_raid_mdadm(
    config: RaidConfig,
    log: LogCallback | None = None,
) -> str:
    """Create an mdadm RAID array from the given config.

    Partitions each physical device identically (one data partition filling
    the whole disk), then assembles them into /dev/md0.

    Returns the virtual block device path (e.g. ``/dev/md0``).
    """
    md_device = "/dev/md0"
    level_str = str(config.level.value)
    device_count = len(config.devices)

    _log(
        f"[bold cyan]Setting up mdadm RAID{level_str} across "
        f"{device_count} devices: {', '.join(config.devices)}[/bold cyan]",
        log,
    )

    # Wipe each device and create a single partition for RAID
    for dev in config.devices:
        _log(f"  Wiping {dev}...", log)
        logged_run(["wipefs", "--all", "--force", dev], log=log)
        logged_run(["sgdisk", "--zap-all", dev], log=log)
        logged_run(["sgdisk", "--clear", dev], log=log)
        _log(f"  Creating RAID partition on {dev}...", log)
        logged_run(
            ["sgdisk", "-n", "1:0:0", "-t", "1:FD00", dev],
            log=log,
        )

    # Wait for partition nodes
    _log("  Waiting for partition device nodes...", log)
    for dev in config.devices:
        logged_run(["partprobe", dev], log=log)
    logged_run(["udevadm", "settle", "--timeout=10"], log=log)

    # Assemble the array
    raid_parts = [_part_name(dev, 1) for dev in config.devices]
    _log(
        f"  Creating mdadm array {md_device} with {', '.join(raid_parts)}...",
        log,
    )
    logged_run(
        [
            "mdadm",
            "--create",
            md_device,
            f"--level={level_str}",
            f"--raid-devices={device_count}",
            "--metadata=1.2",
            "--run",
        ]
        + raid_parts,
        log=log,
    )

    _log(
        f"[green]mdadm RAID{level_str} array ready at {md_device}.[/green]",
        log,
    )
    return md_device


# ---------------------------------------------------------------------------
# Disk layout application
# ---------------------------------------------------------------------------


def _wipe_device(device: str, log: LogCallback | None = None) -> None:
    """Wipe partition table and create a fresh GPT label on a device."""
    _log(f"  Wiping {device}...", log)
    logged_run(["wipefs", "--all", "--force", device], log=log)
    logged_run(["sgdisk", "--zap-all", device], log=log)
    logged_run(["sgdisk", "--clear", device], log=log)


def _create_partitions(
    device: str,
    layout: DiskLayout,
    log: LogCallback | None = None,
) -> list[str]:
    """Create GPT partitions on a device according to the layout.

    Returns a list of partition device paths (e.g. ["/dev/sda1", "/dev/sda2"]).
    """
    part_count = len(layout.partitions)
    _log(f"  Creating {part_count} partitions on {device}...", log)

    part_paths: list[str] = []
    for i, part in enumerate(layout.partitions):
        part_num = i + 1
        size_arg = parse_size_spec(part.size)

        # Determine GPT type code
        if part.filesystem == "vfat":
            type_code = "EF00"  # EFI System Partition
        elif part.filesystem == "swap":
            type_code = "8200"  # Linux swap
        else:
            type_code = "8300"  # Linux filesystem (default for ext4/btrfs/xfs/raw)

        # Build sgdisk size spec: "start:end"
        if size_arg == "0":
            # Fill remaining space
            size_spec = "0:0"
            _log(
                f"    Partition {part_num}: {part.filesystem or 'raw'}  *"
                f"   -> {part.mount_point or '(none)'}  [{part.label or 'unlabeled'}]",
                log,
            )
        else:
            size_spec = f"0:{size_arg}"
            _log(
                f"    Partition {part_num}: {part.filesystem or 'raw'}  {part.size}"
                f"   -> {part.mount_point or '(none)'}  [{part.label or 'unlabeled'}]",
                log,
            )

        cmd = [
            "sgdisk",
            "-n",
            f"{part_num}:{size_spec}",
            "-t",
            f"{part_num}:{type_code}",
        ]
        if part.label:
            cmd.extend(["-c", f"{part_num}:{part.label}"])
        cmd.append(device)
        logged_run(cmd, log=log)

        part_paths.append(_part_name(device, part_num))

    return part_paths


def _format_partition(
    part_path: str,
    spec: PartitionSpec,
    part_num: int,
    btrfs_extra_devices: list[str] | None = None,
    raid_level: RaidLevel | None = None,
    log: LogCallback | None = None,
) -> None:
    """Format a single partition according to its spec.

    For btrfs partitions that are part of a multi-device RAID,
    ``btrfs_extra_devices`` contains the matching partition paths on the
    secondary disks, and ``raid_level`` determines the data/metadata profile.
    """
    if not spec.filesystem:
        _log(
            f"  Partition {part_num} ({spec.label or 'unlabeled'}): "
            f"raw, unformatted -- skipping mkfs.",
            log,
        )
        return

    if spec.filesystem == "vfat":
        _log(f"  Formatting {part_path} as FAT32...", log)
        cmd = ["mkfs.fat", "-F", "32"]
        if spec.label:
            cmd.extend(["-n", spec.label.upper()])
        cmd.append(part_path)
        logged_run(cmd, log=log)

    elif spec.filesystem == "ext4":
        _log(f"  Formatting {part_path} as ext4...", log)
        cmd = ["mkfs.ext4", "-F"]
        if spec.label:
            cmd.extend(["-L", spec.label])
        cmd.append(part_path)
        logged_run(cmd, log=log)

    elif spec.filesystem == "btrfs":
        if btrfs_extra_devices:
            # Multi-device btrfs RAID
            assert raid_level is not None
            raid_str = f"raid{raid_level.value}"
            all_devices = [part_path] + btrfs_extra_devices
            _log(
                f"  Formatting btrfs {raid_str} across {', '.join(all_devices)}...",
                log,
            )
            cmd = [
                "mkfs.btrfs",
                "-f",
                "-d",
                raid_str,
                "-m",
                raid_str,
            ]
            if spec.label:
                cmd.extend(["-L", spec.label])
            cmd.extend(all_devices)
            logged_run(cmd, log=log)
        else:
            _log(f"  Formatting {part_path} as btrfs...", log)
            cmd = ["mkfs.btrfs", "-f"]
            if spec.label:
                cmd.extend(["-L", spec.label])
            cmd.append(part_path)
            logged_run(cmd, log=log)

    elif spec.filesystem == "xfs":
        # XFS is the canonical bulk-storage filesystem for large
        # contiguous datasets (model weights, dataset archives, video).
        # It scales much better than ext4 for files >100 GB and has
        # cheaper extent allocation under fragmentation pressure.
        _log(f"  Formatting {part_path} as xfs...", log)
        cmd = ["mkfs.xfs", "-f"]
        if spec.label:
            # XFS labels are limited to 12 chars; truncate if needed.
            cmd.extend(["-L", spec.label[:12]])
        cmd.append(part_path)
        logged_run(cmd, log=log)

    elif spec.filesystem == "swap":
        # Swap partitions on disk are still useful even with zram:
        # they back swap pressure beyond what fits in compressed RAM,
        # and they survive hibernation (zram does not). Most installs
        # won't need this but the schema should support it.
        _log(f"  Setting up swap on {part_path}...", log)
        cmd = ["mkswap"]
        if spec.label:
            cmd.extend(["-L", spec.label[:15]])  # swap labels max 15 chars
        cmd.append(part_path)
        logged_run(cmd, log=log)

    else:
        _log(
            f"[yellow]  Warning: unknown filesystem {spec.filesystem!r} "
            f"for partition {part_num} -- skipping mkfs.[/yellow]",
            log,
        )


def _create_and_mount_subvolumes(
    part_path: str,
    spec: PartitionSpec,
    log: LogCallback | None = None,
) -> list[str]:
    """Create btrfs subvolumes and mount them under MOUNT_ROOT.

    Returns the list of subvolume names created (e.g. ["@", "@home", "@var"]).
    """
    if not spec.subvolumes:
        return []

    _log(f"  Creating btrfs subvolumes on {part_path}...", log)

    # Temporarily mount the top-level subvolume to create children
    tmp_mount = Path(tempfile.mkdtemp(prefix="arches-btrfs-layout-"))
    tmp_mount.mkdir(parents=True, exist_ok=True)
    logged_run(["mount", part_path, str(tmp_mount)], log=log)

    subvol_names: list[str] = []
    try:
        for sv in spec.subvolumes:
            _log(
                f"    {sv.name} -> {sv.mount_point or '(not mounted)'}",
                log,
            )
            logged_run(
                ["btrfs", "subvolume", "create", str(tmp_mount / sv.name)],
                log=log,
            )
            subvol_names.append(sv.name)
    finally:
        logged_run(["umount", str(tmp_mount)], log=log)

    # Mount subvolumes in order: root subvol (@) first, then children
    root_sv = None
    child_svs = []
    for sv in spec.subvolumes:
        if sv.mount_point == "/":
            root_sv = sv
        elif sv.mount_point is not None:
            child_svs.append(sv)

    if root_sv:
        root_mount = MOUNT_ROOT
        root_mount.mkdir(parents=True, exist_ok=True)
        opts = f"subvol={root_sv.name}"
        if spec.mount_options:
            opts = f"{opts},{spec.mount_options}"
        _log(
            f"  Mounting subvolume {root_sv.name} at {root_mount}...",
            log,
        )
        logged_run(
            ["mount", "-o", opts, part_path, str(root_mount)],
            log=log,
        )

    for sv in child_svs:
        assert sv.mount_point is not None
        mount_target = MOUNT_ROOT / sv.mount_point.lstrip("/")
        mount_target.mkdir(parents=True, exist_ok=True)
        opts = f"subvol={sv.name}"
        if spec.mount_options:
            opts = f"{opts},{spec.mount_options}"
        _log(
            f"  Mounting subvolume {sv.name} at {mount_target}...",
            log,
        )
        logged_run(
            ["mount", "-o", opts, part_path, str(mount_target)],
            log=log,
        )

    return subvol_names


def _mount_partition(
    part_path: str,
    spec: PartitionSpec,
    log: LogCallback | None = None,
) -> None:
    """Mount a non-subvolume partition under MOUNT_ROOT."""
    if spec.mount_point is None:
        _log(
            f"  Partition {part_path} ({spec.label or 'unlabeled'}): "
            f"no mount point -- skipping mount.",
            log,
        )
        return

    mount_target = MOUNT_ROOT / spec.mount_point.lstrip("/")
    mount_target.mkdir(parents=True, exist_ok=True)

    _log(f"  Mounting {part_path} at {mount_target}...", log)

    if spec.mount_options:
        logged_run(
            ["mount", "-o", spec.mount_options, part_path, str(mount_target)],
            log=log,
        )
    else:
        logged_run(["mount", part_path, str(mount_target)], log=log)


def _create_partitions_for_role(
    device: str,
    role_partitions: list[tuple[int, PartitionSpec]],
    log: LogCallback | None = None,
) -> dict[int, str]:
    """Create the partitions for a single disk role on one physical disk.

    ``role_partitions`` is a list of ``(global_partition_index, spec)`` pairs.
    The global index is the position of the partition in the original
    layout.partitions list — we use it as the key in the returned map so
    callers can correlate partitions across disks (for RAID and ESP mirror).

    Returns a dict ``{global_partition_index: partition_device_path}``.
    """
    if not role_partitions:
        return {}

    _log(
        f"  Creating {len(role_partitions)} partition(s) on {device}...",
        log,
    )

    # Local partition number is 1-based and dense for THIS disk, even
    # though the layout-global index may have gaps (when partitions
    # for other roles are interleaved).
    out: dict[int, str] = {}
    for local_num, (global_idx, part) in enumerate(role_partitions, start=1):
        size_arg = parse_size_spec(part.size)

        if part.filesystem == "vfat":
            type_code = "EF00"
        elif part.filesystem == "swap":
            type_code = "8200"
        else:
            type_code = "8300"

        if size_arg == "0":
            size_spec = "0:0"
            size_display = "*"
        else:
            size_spec = f"0:{size_arg}"
            size_display = part.size

        _log(
            f"    Partition {local_num}: {part.filesystem or 'raw'}  "
            f"{size_display}   -> {part.mount_point or '(none)'}  "
            f"[{part.label or 'unlabeled'}]",
            log,
        )

        cmd = [
            "sgdisk",
            "-n",
            f"{local_num}:{size_spec}",
            "-t",
            f"{local_num}:{type_code}",
        ]
        if part.label:
            cmd.extend(["-c", f"{local_num}:{part.label}"])
        cmd.append(device)
        logged_run(cmd, log=log)

        out[global_idx] = _part_name(device, local_num)

    return out


def apply_disk_layout_resolved(
    layout: DiskLayout,
    resolved: ResolvedDiskRoles,
    log: LogCallback | None = None,
) -> PartitionMap:
    """Apply a disk layout using a resolved role -> devices map.

    Group the layout's partitions by their target role. For each role
    and each device assigned to that role, create matching partitions
    via sgdisk, then format/RAID/mount as the layout dictates. This is
    the multi-disk-capable counterpart to :func:`apply_disk_layout`;
    the latter is now a backward-compatible wrapper around this
    function for single-disk use.

    Partitioning order: roles are processed alphabetically by name for
    determinism. Within a role, layout-original partition order is
    preserved. The first device in each role's device list is treated
    as the "lead" — its ESP (if any) becomes the bootable ESP.
    """
    if not resolved.assignments:
        raise ValueError(
            "apply_disk_layout_resolved called with empty role assignments"
        )

    _log(
        f'[bold cyan]Applying layout "{layout.name}" '
        f"({len(resolved.assignments)} role(s))[/bold cyan]",
        log,
    )

    # 1. Group partitions by target role. Preserve layout order within
    # each role.
    parts_by_role: dict[str, list[tuple[int, PartitionSpec]]] = {}
    for idx, part in enumerate(layout.partitions):
        parts_by_role.setdefault(part.target_role, []).append((idx, part))

    # Validate every role referenced by a partition has device assignments.
    for role in parts_by_role:
        if role not in resolved.assignments:
            raise ValueError(
                f"Partitions reference role {role!r} but it was not "
                f"resolved to any physical disks"
            )

    # 2. Wipe every physical device exactly once. A single physical
    # disk might serve multiple roles — but our resolver rejects that.
    all_devices: list[str] = []
    seen: set[str] = set()
    for devs in resolved.assignments.values():
        for d in devs:
            if d.path not in seen:
                all_devices.append(d.path)
                seen.add(d.path)
    for dev_path in all_devices:
        _wipe_device(dev_path, log=log)

    # 3. Create partitions on every physical device. The created paths
    # are indexed by (role_name, device_path, global_partition_idx)
    # so we can correlate them later for RAID and ESP mirror.
    # part_paths[role][device][global_idx] = "/dev/sdX1"
    part_paths: dict[str, dict[str, dict[int, str]]] = {}
    for role_name in sorted(parts_by_role.keys()):
        role_parts = parts_by_role[role_name]
        part_paths[role_name] = {}
        for dev in resolved.assignments[role_name]:
            created = _create_partitions_for_role(dev.path, role_parts, log=log)
            part_paths[role_name][dev.path] = created

    # 4. Probe partition tables.
    _log("  Probing partition tables...", log)
    for dev_path in all_devices:
        logged_run(["partprobe", dev_path], log=log)
    logged_run(["udevadm", "settle", "--timeout=10"], log=log)

    # 5. Format partitions. For each layout partition:
    #    - if single-disk (raid_level not set): format the one part_path
    #    - if RAID with raid_level set:
    #        - btrfs raid: mkfs.btrfs across all member partitions
    #        - mdadm raid: assemble an md device, then format that
    #    Track the partition map for the bootloader/install pipeline.
    esp_path = ""
    root_path = ""
    boot_path = ""
    home_path = ""
    root_filesystem = ""
    root_subvolumes: list[str] = []
    swap_parts: list[str] = []
    extra_mounts: list[tuple[str, str, str]] = []

    # Roles we'll mount during the second pass, keyed by global idx →
    # the device path the install pipeline should treat as "the
    # partition". For RAID, this is the assembled volume (mdadm) or
    # the lead member (btrfs treats any member as identity for mount).
    effective_part: dict[int, str] = {}

    for idx, part in enumerate(layout.partitions):
        role_name = part.target_role
        per_device = part_paths[role_name]

        if part.is_multidisk:
            # RAID partition: collect every member partition (the same
            # global idx from each device in the role) and format
            # together.
            members = [per_device[d.path][idx] for d in resolved.assignments[role_name]]
            backend = (
                part.raid_backend
                or ("btrfs" if part.filesystem == "btrfs" else "mdadm")
            )

            if backend == "btrfs" and part.filesystem == "btrfs":
                # Multi-device btrfs handles its own array assembly.
                level = RaidLevel(int(part.raid_level))
                _format_partition(
                    members[0],
                    part,
                    idx + 1,
                    btrfs_extra_devices=members[1:],
                    raid_level=level,
                    log=log,
                )
                # Mount target is the first member; btrfs auto-detects
                # the other members via UUID.
                effective_part[idx] = members[0]
            elif backend == "mdadm":
                # Assemble an mdadm array, then format the resulting
                # /dev/mdX as the requested filesystem.
                md_dev = _assemble_mdadm_array(
                    members,
                    raid_level=part.raid_level,
                    array_index=idx,
                    log=log,
                )
                _format_partition(md_dev, part, idx + 1, log=log)
                effective_part[idx] = md_dev
            else:
                raise ValueError(
                    f"unsupported raid_backend={backend!r} with "
                    f"filesystem={part.filesystem!r} on partition {idx + 1}"
                )
        else:
            # Single-disk partition. The role MUST have exactly one
            # device (the resolver enforces this for single-disk roles).
            devices = resolved.assignments[role_name]
            if len(devices) > 1:
                # Multi-device role being used by a single-disk partition.
                # We replicate the same partition shape onto every device
                # (e.g. an ESP that needs mirroring across the same disks
                # as a RAID-1 array). Format each, but the bootable one
                # is the first member.
                paths = [per_device[d.path][idx] for d in devices]
                _format_partition(paths[0], part, idx + 1, log=log)
                for extra in paths[1:]:
                    _log(
                        f"  Formatting secondary {part.filesystem or 'raw'} "
                        f"{extra} (mirror of {paths[0]})...",
                        log,
                    )
                    _format_partition(extra, part, idx + 1, log=log)
                effective_part[idx] = paths[0]
            else:
                pth = per_device[devices[0].path][idx]
                _format_partition(pth, part, idx + 1, log=log)
                effective_part[idx] = pth

        # Build the PartitionMap on the fly.
        ep = effective_part[idx]
        if part.filesystem == "vfat" and part.mount_point in (
            "/boot",
            "/boot/efi",
        ):
            esp_path = ep
        if part.mount_point == "/":
            root_path = ep
            root_filesystem = part.filesystem
        if part.mount_point == "/boot/efi":
            boot_path = ""
        if part.mount_point == "/home":
            home_path = ep
        if part.filesystem == "swap":
            swap_parts.append(ep)
        # Auxiliary mount points (anything that isn't one of the
        # canonical roles above). Used by the install pipeline to
        # validate fstab entries and by hardware-rescan to verify
        # the disks are still present on subsequent boots.
        elif part.mount_point and part.mount_point not in (
            "/",
            "/boot",
            "/boot/efi",
            "/home",
        ):
            extra_mounts.append((ep, part.mount_point, part.filesystem))

    # 6. Mount partitions. Root first (so subdirs work), then everything
    # else in layout order.
    for idx, part in enumerate(layout.partitions):
        if part.mount_point != "/":
            continue
        pth = effective_part[idx]
        if part.filesystem == "btrfs" and part.subvolumes:
            root_subvolumes = _create_and_mount_subvolumes(pth, part, log=log)
        else:
            _mount_partition(pth, part, log=log)
        break

    for idx, part in enumerate(layout.partitions):
        if part.mount_point == "/":
            continue
        if part.filesystem == "swap":
            # Swap doesn't get mounted; fstab + swapon handle it.
            continue
        pth = effective_part[idx]
        if part.filesystem == "btrfs" and part.subvolumes:
            _create_and_mount_subvolumes(pth, part, log=log)
        elif part.mount_point is not None:
            _mount_partition(pth, part, log=log)
        elif not part.filesystem:
            pass
        else:
            _log(
                f"  Partition {pth} ({part.label or 'unlabeled'}): "
                f"no mount point — skipping mount.",
                log,
            )

    _log(
        f'[green]Disk layout "{layout.name}" applied successfully.[/green]',
        log,
    )

    return PartitionMap(
        esp=esp_path,
        root=root_path,
        boot=boot_path,
        home=home_path,
        root_filesystem=root_filesystem,
        root_subvolumes=root_subvolumes,
        swap_partitions=swap_parts,
        extra_mounts=extra_mounts,
    )


def _assemble_mdadm_array(
    members: list[str],
    raid_level: str,
    array_index: int,
    log: LogCallback | None = None,
) -> str:
    """Assemble an mdadm array from partition members.

    Returns the resulting /dev/mdX path. The array index is the layout
    partition index so multiple per-partition arrays don't collide on
    the same md number.
    """
    md_device = f"/dev/md{array_index}"
    _log(
        f"  Assembling mdadm RAID{raid_level} array {md_device} from "
        f"{len(members)} members: {', '.join(members)}",
        log,
    )
    logged_run(
        [
            "mdadm",
            "--create",
            md_device,
            f"--level={raid_level}",
            f"--raid-devices={len(members)}",
            "--metadata=1.2",
            "--run",
        ]
        + members,
        log=log,
    )
    return md_device


def apply_disk_layout(
    device: str,
    layout: DiskLayout,
    extra_devices: list[str] | None = None,
    raid_config: RaidConfig | None = None,
    log: LogCallback | None = None,
) -> PartitionMap:
    """Apply a disk layout to a device (or devices for btrfs RAID).

    This is the main entry point for the layout-based partitioning flow.

    1. Wipes the device(s)
    2. Creates GPT partitions via sgdisk
    3. Formats each partition (handling btrfs multi-device RAID)
    4. Creates btrfs subvolumes if specified
    5. Mounts everything under MOUNT_ROOT

    For btrfs RAID, ``extra_devices`` contains the secondary disk paths.
    Each device is partitioned identically; btrfs data partitions from all
    devices are passed to ``mkfs.btrfs -d raidN``.  The ESP from the primary
    device is the boot ESP; ESPs on secondary devices are formatted but not
    mounted (they are mirrored in a separate step after bootloader install).

    Returns a ``PartitionMap`` describing the resulting mount layout.

    .. note::
       This is the legacy single-disk + homogeneous-RAID entry point.
       New code with multi-disk role-keyed layouts should call
       :func:`apply_disk_layout_resolved` directly. This function still
       works unchanged for ``basic.toml``-style single-disk installs.
    """
    _log(
        f'[bold cyan]Applying layout "{layout.name}" to {device}[/bold cyan]',
        log,
    )

    # Step 1: Wipe primary device
    _wipe_device(device, log=log)

    # Wipe extra devices for btrfs RAID
    if extra_devices:
        for dev in extra_devices:
            _wipe_device(dev, log=log)

    # Step 2: Create partitions on primary device
    primary_parts = _create_partitions(device, layout, log=log)

    # Create identical partitions on extra devices (btrfs RAID)
    extra_part_lists: list[list[str]] = []
    if extra_devices:
        for dev in extra_devices:
            _log(f"  Creating matching partitions on {dev}...", log)
            extra_parts = _create_partitions(dev, layout, log=log)
            extra_part_lists.append(extra_parts)

    # Step 3: Probe partition tables and wait for device nodes
    _log("  Probing partition table...", log)
    logged_run(["partprobe", device], log=log)
    if extra_devices:
        for dev in extra_devices:
            logged_run(["partprobe", dev], log=log)
    logged_run(["udevadm", "settle", "--timeout=10"], log=log)

    # Step 4: Format partitions
    # Track which partition is the root for building the PartitionMap
    esp_path = ""
    root_path = ""
    boot_path = ""
    home_path = ""
    root_filesystem = ""
    root_subvolumes: list[str] = []

    # Determine btrfs RAID level for multi-device formatting
    btrfs_raid_level = None
    if raid_config and raid_config.backend == RaidBackend.BTRFS:
        btrfs_raid_level = raid_config.level

    for i, spec in enumerate(layout.partitions):
        part_path = primary_parts[i]

        # Collect matching partitions from extra devices for btrfs RAID
        btrfs_extra: list[str] | None = None
        if (
            extra_part_lists
            and spec.filesystem == "btrfs"
            and btrfs_raid_level is not None
        ):
            btrfs_extra = [ep[i] for ep in extra_part_lists]

        # Format ESPs on extra devices separately (mirrored, not RAID)
        if spec.filesystem == "vfat" and extra_part_lists:
            _format_partition(part_path, spec, i + 1, log=log)
            for ep in extra_part_lists:
                _log(f"  Formatting secondary ESP {ep[i]} as FAT32...", log)
                _format_partition(ep[i], spec, i + 1, log=log)
        else:
            _format_partition(
                part_path,
                spec,
                i + 1,
                btrfs_extra_devices=btrfs_extra,
                raid_level=btrfs_raid_level,
                log=log,
            )

        # Identify partition roles
        if spec.filesystem == "vfat" and spec.mount_point in ("/boot", "/boot/efi"):
            esp_path = part_path
        if spec.mount_point == "/":
            root_path = part_path
            root_filesystem = spec.filesystem
        if spec.mount_point == "/boot/efi":
            boot_path = ""  # ESP at /boot/efi: no separate /boot partition
        if spec.mount_point == "/home":
            home_path = part_path

    # Step 5: Mount partitions (root first, then others in order)
    # First pass: mount root (with subvolumes if btrfs)
    for i, spec in enumerate(layout.partitions):
        if spec.mount_point == "/":
            if spec.filesystem == "btrfs" and spec.subvolumes:
                root_subvolumes = _create_and_mount_subvolumes(
                    primary_parts[i], spec, log=log
                )
            else:
                _mount_partition(primary_parts[i], spec, log=log)
            break

    # Second pass: mount everything else (non-root, non-subvolume-managed)
    for i, spec in enumerate(layout.partitions):
        if spec.mount_point == "/":
            continue  # Already mounted
        if spec.filesystem == "btrfs" and spec.subvolumes:
            # Subvolumes on non-root btrfs partitions
            _create_and_mount_subvolumes(primary_parts[i], spec, log=log)
        elif spec.mount_point is not None:
            _mount_partition(primary_parts[i], spec, log=log)
        elif not spec.filesystem:
            # Raw partition — already logged during format
            pass
        else:
            _log(
                f"  Partition {primary_parts[i]} ({spec.label or 'unlabeled'}): "
                f"no mount point -- skipping mount.",
                log,
            )

    _log(
        f'[green]Disk layout "{layout.name}" applied successfully.[/green]',
        log,
    )

    return PartitionMap(
        esp=esp_path,
        root=root_path,
        boot=boot_path,
        home=home_path,
        root_filesystem=root_filesystem,
        root_subvolumes=root_subvolumes,
    )


# ---------------------------------------------------------------------------
# ESP mirroring (btrfs RAID 1/10 post-bootloader step)
# ---------------------------------------------------------------------------


def mirror_esp(
    primary_esp: str,
    secondary_esps: list[str],
    log: LogCallback | None = None,
) -> None:
    """Mirror ESP contents from the primary ESP to secondary disk ESPs.

    This provides boot redundancy for btrfs RAID 1/10 configurations.
    Each secondary ESP is formatted, mounted at a temporary location,
    and synced from the primary ESP mount at MOUNT_ROOT/boot.

    After syncing, an EFI boot entry is created for each secondary disk.
    """
    if not secondary_esps:
        _log("  No secondary ESPs to mirror.", log)
        return

    _log(
        f"[bold cyan]Mirroring ESP from {primary_esp} to "
        f"{len(secondary_esps)} secondary disk(s)...[/bold cyan]",
        log,
    )

    primary_mount = MOUNT_ROOT / "boot"

    for sec_esp in secondary_esps:
        # Mount secondary ESP to a temp dir
        tmp_mount = Path(tempfile.mkdtemp(prefix="arches-esp-mirror-"))
        tmp_mount.mkdir(parents=True, exist_ok=True)

        _log(f"  Syncing ESP contents to {sec_esp}...", log)
        logged_run(["mount", sec_esp, str(tmp_mount)], log=log)
        try:
            logged_run(
                ["rsync", "-a", "--delete", f"{primary_mount}/", f"{tmp_mount}/"],
                log=log,
            )
        finally:
            logged_run(["umount", str(tmp_mount)], log=log)

        # Add UEFI boot entry for this secondary ESP
        # Extract the disk and partition number for efibootmgr
        # e.g. /dev/sdb1 -> disk=/dev/sdb, part=1
        _log(f"  Adding UEFI boot entry for {sec_esp}...", log)
        try:
            # Strip trailing digits (and 'p' for nvme) to get the disk
            disk = sec_esp.rstrip("0123456789")
            if disk.endswith("p"):
                disk = disk[:-1]
            part_num = sec_esp[len(disk) :]
            if part_num.startswith("p"):
                part_num = part_num[1:]
            logged_run(
                [
                    "efibootmgr",
                    "--create",
                    "--disk",
                    disk,
                    "--part",
                    part_num,
                    "--label",
                    f"Arches Linux (mirror {sec_esp})",
                    "--loader",
                    "/EFI/BOOT/BOOTX64.EFI",
                ],
                log=log,
            )
        except Exception as e:
            _log(
                f"[yellow]  Warning: failed to create UEFI boot entry "
                f"for {sec_esp}: {e}[/yellow]",
                log,
            )

    _log(
        f"[green]ESP mirrored to {len(secondary_esps)} secondary disk(s).[/green]",
        log,
    )
