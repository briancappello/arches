"""Hardware quirk and machine profile detection and deployment.

Quirks are device-class fixes auto-detected by scanning PCI IDs and
chassis type.  Machine profiles are model-specific configs matched by
DMI (vendor, product name).  Both are declared as TOML files under
``hardware/quirks/`` and ``hardware/machines/``.

At install time the resolved hardware config is deployed as modprobe
configs, udev rules, and sysctl tunables into the target filesystem.
Machine-specific packages, services, and Ansible roles are merged into
the install pipeline alongside the template's own lists.

The same deployment code is reused at runtime by
``arches-hardware-rescan``, which re-evaluates the hardware after a
GPU/peripheral swap and reconciles the deployed files (removing
orphans, adding newcomers). To make that safe, every file we write
carries a management header so we can distinguish files we own from
files a user or another package wrote.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arches_installer.core.disk import MOUNT_ROOT
from arches_installer.core.run import LogCallback, _log

# Search paths for the hardware directory, checked in order.
_HARDWARE_SEARCH = [
    Path("/opt/arches/hardware"),
    Path(__file__).resolve().parents[3] / "hardware",
]

# State directory on the installed system. Holds the hardware
# fingerprint and the manifest of files we currently manage.
STATE_DIR_REL = Path("var/lib/arches")
FINGERPRINT_FILE = "hardware-fingerprint"
MANIFEST_FILE = "hardware-manifest.json"

# Sentinel comment placed at the top of every file we write so the
# rescan tool can safely identify and remove orphans. Format chosen to
# be valid as a comment in modprobe.d (#), udev rules (#), and
# sysctl.d (#) — all three accept '#' line comments.
MANAGED_HEADER_PREFIX = "# arches-hardware-rescan: managed quirk="


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Quirk:
    """A device-class hardware fix loaded from a TOML file."""

    slug: str  # filename stem, e.g. "intel-wifi7-disable-be"
    name: str
    description: str
    # Match criteria (any match triggers the quirk)
    pci_ids: list[str] = field(default_factory=list)
    pci_vendor: str = ""
    chassis_type: list[int] = field(default_factory=list)
    # File drops
    modprobe: dict[str, str] = field(default_factory=dict)
    udev: dict[str, str] = field(default_factory=dict)
    sysctl: dict[str, str] = field(default_factory=dict)
    # Path to the quirk's companion files directory (if it exists)
    files_dir: Path | None = None

    def matches(self, pci_ids: set[str], chassis: int) -> bool:
        """Return True if this quirk applies to the given hardware."""
        conditions: list[bool] = []

        if self.pci_ids:
            conditions.append(any(pid.lower() in pci_ids for pid in self.pci_ids))

        if self.pci_vendor:
            vendor = self.pci_vendor.lower()
            conditions.append(
                any(pid.lower().startswith(vendor + ":") for pid in pci_ids)
            )

        if self.chassis_type:
            conditions.append(chassis in self.chassis_type)

        if not conditions:
            return False

        # All specified conditions must be true (AND logic).
        # pci_ids and pci_vendor are device-presence tests;
        # chassis_type is an environment test.  A quirk like
        # "NVIDIA RTD3 for laptops" needs BOTH the GPU AND a laptop
        # chassis.
        return all(conditions)


@dataclass
class MachineProfile:
    """A model-specific hardware profile loaded from a TOML file."""

    slug: str  # filename stem, e.g. "thinkpad-p1-gen7"
    name: str
    description: str
    platform: str = ""
    # DMI match criteria
    sys_vendor: str = ""
    sys_vendor_pattern: str = ""
    product_name: str = ""
    product_name_pattern: str = ""
    chassis_type: list[int] = field(default_factory=list)
    # Quirks to include
    quirk_includes: list[str] = field(default_factory=list)
    # Machine-specific config
    packages: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    services_disable: list[str] = field(default_factory=list)
    modprobe: dict[str, str] = field(default_factory=dict)
    udev: dict[str, str] = field(default_factory=dict)
    sysctl: dict[str, str] = field(default_factory=dict)
    ansible_firstboot_roles: list[str] = field(default_factory=list)
    ansible_vars: dict[str, Any] = field(default_factory=dict)
    # Per-host disk-role overrides. Same shape as DiskRole in
    # disk_layout.py — kept as untyped dicts here to avoid a circular
    # import; the resolver converts them at use time. Stored as the
    # raw [[disks]] table list from TOML.
    disks: list[dict[str, Any]] = field(default_factory=list)
    # Path to companion files (if exists)
    files_dir: Path | None = None

    def matches_dmi(self, dmi: DMIInfo) -> bool:
        """Return True if this profile matches the given DMI info."""
        # Exact vendor match
        if self.sys_vendor and dmi.sys_vendor != self.sys_vendor:
            return False
        # Vendor pattern match
        if self.sys_vendor_pattern and not fnmatch.fnmatch(
            dmi.sys_vendor, self.sys_vendor_pattern
        ):
            return False
        # Exact product match
        if self.product_name and dmi.product_name != self.product_name:
            return False
        # Product pattern match
        if self.product_name_pattern and not fnmatch.fnmatch(
            dmi.product_name, self.product_name_pattern
        ):
            return False
        # Chassis type match (if specified)
        if self.chassis_type and dmi.chassis_type not in self.chassis_type:
            return False
        # Must have at least one match criterion
        if not any(
            [
                self.sys_vendor,
                self.sys_vendor_pattern,
                self.product_name,
                self.product_name_pattern,
                self.chassis_type,
            ]
        ):
            return False
        return True

    @property
    def is_generic(self) -> bool:
        """True if this is a generic fallback profile (no DMI vendor/product)."""
        return not any(
            [
                self.sys_vendor,
                self.sys_vendor_pattern,
                self.product_name,
                self.product_name_pattern,
            ]
        )


@dataclass
class DMIInfo:
    """System identification from DMI/SMBIOS tables."""

    sys_vendor: str = ""
    product_name: str = ""
    board_name: str = ""
    chassis_type: int = 0


@dataclass
class HardwareConfig:
    """Resolved hardware config: selected machine + all applicable quirks.

    This is the single object passed through the install pipeline.
    """

    machine: MachineProfile | None = None
    quirks: list[Quirk] = field(default_factory=list)

    @property
    def all_packages(self) -> list[str]:
        """All packages from the machine profile."""
        if self.machine:
            return list(self.machine.packages)
        return []

    @property
    def all_services(self) -> list[str]:
        """All services from the machine profile."""
        if self.machine:
            return list(self.machine.services)
        return []

    @property
    def all_services_disable(self) -> list[str]:
        """Services to disable (suppress from template's enable list)."""
        if self.machine:
            return list(self.machine.services_disable)
        return []

    @property
    def all_firstboot_roles(self) -> list[str]:
        """All Ansible firstboot roles from the machine profile."""
        if self.machine:
            return list(self.machine.ansible_firstboot_roles)
        return []

    def disk_role_overrides(self) -> list[Any]:
        """Return the machine profile's disk-role overrides as DiskRole objects.

        Defers the import to avoid a circular dependency between
        ``hardware.py`` and ``disk_layout.py`` at module-load time.
        """
        if not self.machine or not self.machine.disks:
            return []
        from arches_installer.core.disk_layout import DiskRole

        out: list[Any] = []
        for d in self.machine.disks:
            if "name" not in d:
                continue  # malformed; skip silently (loader warned)
            out.append(DiskRole(name=str(d["name"]), descriptor=d.get("device", "")))
        return out

    @property
    def all_modprobe(self) -> dict[str, str]:
        """Merged modprobe configs from all quirks + machine."""
        merged: dict[str, str] = {}
        for q in self.quirks:
            merged.update(q.modprobe)
        if self.machine:
            merged.update(self.machine.modprobe)
        return merged

    @property
    def all_udev(self) -> dict[str, str]:
        """Merged udev rules from all quirks + machine."""
        merged: dict[str, str] = {}
        for q in self.quirks:
            merged.update(q.udev)
        if self.machine:
            merged.update(self.machine.udev)
        return merged

    @property
    def all_sysctl(self) -> dict[str, str]:
        """Merged sysctl configs from all quirks + machine."""
        merged: dict[str, str] = {}
        for q in self.quirks:
            merged.update(q.sysctl)
        if self.machine:
            merged.update(self.machine.sysctl)
        return merged


# ---------------------------------------------------------------------------
# TOML loading
# ---------------------------------------------------------------------------


def _find_hardware_dir() -> Path:
    """Locate the hardware directory from the search path."""
    for d in _HARDWARE_SEARCH:
        if d.is_dir():
            return d
    searched = ", ".join(str(d) for d in _HARDWARE_SEARCH)
    raise FileNotFoundError(f"Hardware directory not found (searched: {searched})")


def load_quirk(path: Path) -> Quirk:
    """Load a single quirk from a TOML file."""
    with open(path, "rb") as f:
        data = tomllib.load(f)

    q = data.get("quirk", {})
    match = data.get("match", {})

    slug = path.stem
    files_dir = path.parent / slug
    if not files_dir.is_dir():
        files_dir = None

    return Quirk(
        slug=slug,
        name=q.get("name", slug),
        description=q.get("description", ""),
        pci_ids=match.get("pci_ids", []),
        pci_vendor=match.get("pci_vendor", ""),
        chassis_type=match.get("chassis_type", []),
        modprobe=data.get("modprobe", {}),
        udev=data.get("udev", {}),
        sysctl=data.get("sysctl", {}),
        files_dir=files_dir,
    )


def load_machine(path: Path) -> MachineProfile:
    """Load a single machine profile from a TOML file."""
    with open(path, "rb") as f:
        data = tomllib.load(f)

    m = data.get("machine", {})
    match = data.get("match", {})
    pkgs = data.get("packages", {})
    svcs = data.get("services", {})
    ans = data.get("ansible", {})
    quirks_sec = data.get("quirks", {})

    slug = path.stem
    files_dir = path.parent / slug
    if not files_dir.is_dir():
        files_dir = None

    return MachineProfile(
        slug=slug,
        name=m.get("name", slug),
        description=m.get("description", ""),
        platform=m.get("platform", ""),
        sys_vendor=match.get("sys_vendor", ""),
        sys_vendor_pattern=match.get("sys_vendor_pattern", ""),
        product_name=match.get("product_name", ""),
        product_name_pattern=match.get("product_name_pattern", ""),
        chassis_type=match.get("chassis_type", []),
        quirk_includes=quirks_sec.get("include", []),
        packages=pkgs.get("install", []),
        services=svcs.get("enable", []),
        services_disable=svcs.get("disable", []),
        modprobe=data.get("modprobe", {}),
        udev=data.get("udev", {}),
        sysctl=data.get("sysctl", {}),
        ansible_firstboot_roles=ans.get("firstboot_roles", []),
        ansible_vars=ans.get("vars", {}),
        # Disk-role overrides — stored as raw dicts so loading this
        # module doesn't pull in the disk_layout dependency cycle.
        disks=data.get("disks", []),
        files_dir=files_dir,
    )


def discover_quirks(hw_dir: Path | None = None) -> list[Quirk]:
    """Load all quirks from the hardware/quirks/ directory."""
    if hw_dir is None:
        hw_dir = _find_hardware_dir()
    quirks_dir = hw_dir / "quirks"
    if not quirks_dir.is_dir():
        return []
    return [load_quirk(p) for p in sorted(quirks_dir.glob("*.toml"))]


def discover_machines(hw_dir: Path | None = None) -> list[MachineProfile]:
    """Load all machine profiles from the hardware/machines/ directory."""
    if hw_dir is None:
        hw_dir = _find_hardware_dir()
    machines_dir = hw_dir / "machines"
    if not machines_dir.is_dir():
        return []
    return [load_machine(p) for p in sorted(machines_dir.glob("*.toml"))]


# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------


def detect_pci_ids() -> set[str]:
    """Scan the PCI bus and return a set of 'vendor:device' ID strings.

    Returns lowercase strings like ``{'8086:272b', '10de:2838'}``.
    """
    try:
        result = subprocess.run(
            ["lspci", "-nn"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()

    ids: set[str] = set()
    for line in result.stdout.splitlines():
        # Extract [vendor:device] patterns from lspci -nn output.
        # Example: "09:00.0 Network controller [0280]: Intel ... [8086:272b]"
        start = line.rfind("[")
        end = line.rfind("]")
        if start != -1 and end != -1 and ":" in line[start:end]:
            pair = line[start + 1 : end]
            if len(pair.split(":")) == 2:
                ids.add(pair.lower())
    return ids


def get_dmi_info() -> DMIInfo:
    """Read system identification from DMI/SMBIOS tables."""

    def _read(name: str) -> str:
        path = Path(f"/sys/class/dmi/id/{name}")
        try:
            return path.read_text().strip()
        except (FileNotFoundError, PermissionError):
            return ""

    chassis_str = _read("chassis_type")
    try:
        chassis = int(chassis_str)
    except (ValueError, TypeError):
        chassis = 0

    return DMIInfo(
        sys_vendor=_read("sys_vendor"),
        product_name=_read("product_name"),
        board_name=_read("board_name"),
        chassis_type=chassis,
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def match_quirks(
    quirks: list[Quirk],
    pci_ids: set[str],
    chassis_type: int,
) -> list[Quirk]:
    """Filter quirks that match the given hardware."""
    return [q for q in quirks if q.matches(pci_ids, chassis_type)]


def suggest_machine(
    machines: list[MachineProfile],
    dmi: DMIInfo,
) -> MachineProfile | None:
    """Find the best matching machine profile for the given DMI info.

    Prefers specific profiles (vendor + product) over generic ones
    (chassis-type-only fallbacks).  Returns None if nothing matches.
    """
    specific: list[MachineProfile] = []
    generic: list[MachineProfile] = []

    for m in machines:
        if m.matches_dmi(dmi):
            if m.is_generic:
                generic.append(m)
            else:
                specific.append(m)

    if specific:
        return specific[0]
    if generic:
        return generic[0]
    return None


def resolve_hardware(
    machine: MachineProfile | None,
    matched_quirks: list[Quirk],
    all_quirks: list[Quirk] | None = None,
) -> HardwareConfig:
    """Build a resolved HardwareConfig from a machine + auto-detected quirks.

    Merges the machine's explicitly included quirks with the auto-detected
    ones, deduplicating by slug.

    Parameters
    ----------
    machine:
        Selected machine profile (may be None for quirks-only installs).
    matched_quirks:
        Quirks that matched via hardware auto-detection.
    all_quirks:
        Full list of available quirks (needed to resolve machine's
        ``quirk_includes`` by slug).  If None, only ``matched_quirks``
        are used.
    """
    quirk_map: dict[str, Quirk] = {}

    # 1. Auto-detected quirks
    for q in matched_quirks:
        quirk_map[q.slug] = q

    # 2. Machine's explicitly included quirks
    if machine and all_quirks:
        slug_lookup = {q.slug: q for q in all_quirks}
        for slug in machine.quirk_includes:
            if slug in slug_lookup and slug not in quirk_map:
                quirk_map[slug] = slug_lookup[slug]

    return HardwareConfig(
        machine=machine,
        quirks=list(quirk_map.values()),
    )


# ---------------------------------------------------------------------------
# Deployment (writes files into the target filesystem)
# ---------------------------------------------------------------------------


def _resolve_file_content(
    filename: str,
    value: str,
    files_dir: Path | None,
) -> str:
    """Resolve file content: inline string or reference to a companion file.

    If the value looks like a filename (no newline, no '=', ends in a
    common extension), treat it as a reference to a file in the
    companion directory.  Otherwise treat it as inline content.
    """
    is_reference = (
        files_dir is not None
        and "\n" not in value
        and "=" not in value
        and len(value) < 256
    )
    if is_reference:
        ref_path = files_dir / value
        if ref_path.is_file():
            return ref_path.read_text()

    return value


def _tag_content(quirk_slug: str, content: str) -> str:
    """Prepend the management header so we can identify our files later.

    Idempotent: if the content already starts with the header (e.g. we
    are rewriting a file we previously wrote), it is replaced rather
    than stacked.
    """
    header = f"{MANAGED_HEADER_PREFIX}{quirk_slug}\n"
    body = content
    if body.startswith(MANAGED_HEADER_PREFIX):
        # Strip the existing header (everything up to and including the
        # first newline) before adding the new one.
        _, _, body = body.partition("\n")
    return header + body.lstrip("\n").rstrip("\n") + "\n"


def is_managed_file(path: Path) -> str | None:
    """Return the quirk slug owning this file, or None if not managed.

    Reads only the first 256 bytes — enough to find the header without
    slurping large files.
    """
    try:
        with open(path, "rb") as f:
            first = f.read(256).decode("utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if not first.startswith(MANAGED_HEADER_PREFIX):
        return None
    line = first.split("\n", 1)[0]
    return line[len(MANAGED_HEADER_PREFIX) :].strip() or None


def _resolved_files(hw: HardwareConfig) -> dict[str, dict[str, tuple[str, str]]]:
    """Build the {category: {filename: (quirk_slug, content)}} map.

    Each leaf is a (owning quirk slug, raw content without management
    header) tuple. Categories are 'modprobe', 'udev', 'sysctl' with
    target directories ``/etc/modprobe.d``, ``/etc/udev/rules.d``, and
    ``/etc/sysctl.d``.
    """
    out: dict[str, dict[str, tuple[str, str]]] = {
        "modprobe": {},
        "udev": {},
        "sysctl": {},
    }

    sources: list[Quirk | MachineProfile] = list(hw.quirks)
    if hw.machine:
        sources.append(hw.machine)

    for source in sources:
        slug = getattr(source, "slug", "machine")
        for category in ("modprobe", "udev", "sysctl"):
            mapping = getattr(source, category, {}) or {}
            for filename, value in mapping.items():
                content = _resolve_file_content(filename, value, source.files_dir)
                # Later sources override earlier ones for the same filename
                # (machine wins over quirk, matching the merge order in
                # HardwareConfig.all_modprobe et al).
                out[category][filename] = (slug, content.rstrip("\n") + "\n")

    return out


# Relative paths (under target_root) for each category. Kept here so
# the rescan tool and the installer agree on layout.
_CATEGORY_DIRS: dict[str, Path] = {
    "modprobe": Path("etc/modprobe.d"),
    "udev": Path("etc/udev/rules.d"),
    "sysctl": Path("etc/sysctl.d"),
}


def deploy_hardware_files(
    hw: HardwareConfig,
    log: LogCallback | None = None,
    *,
    target_root: Path | None = None,
) -> dict[str, list[str]]:
    """Write hardware config files into a target filesystem.

    Deploys modprobe configs, udev rules, and sysctl tunables from all
    resolved quirks and the machine profile. Every file gets the
    arches-hardware-rescan management header so the runtime rescan
    tool can identify and reconcile them later.

    Parameters
    ----------
    hw:
        Resolved hardware config (quirks + optional machine profile).
    log:
        Optional log callback.
    target_root:
        Filesystem root to write into. Defaults to ``MOUNT_ROOT``
        (install-time chroot target). Pass ``Path("/")`` for runtime
        rescan on the live system.

    Returns
    -------
    Manifest dict ``{category: [filename, ...]}`` listing every file
    we wrote, so callers can persist it for later reconciliation.
    """
    root = target_root if target_root is not None else MOUNT_ROOT
    manifest: dict[str, list[str]] = {"modprobe": [], "udev": [], "sysctl": []}

    if not hw.quirks and not hw.machine:
        _log("No hardware config to deploy.", log)
        return manifest

    _log(f"Deploying hardware configuration files to {root}...", log)

    resolved = _resolved_files(hw)

    for category, entries in resolved.items():
        if not entries:
            continue
        target_dir = root / _CATEGORY_DIRS[category]
        target_dir.mkdir(parents=True, exist_ok=True)
        for filename, (slug, content) in entries.items():
            target = target_dir / filename
            target.write_text(_tag_content(slug, content))
            manifest[category].append(filename)
            _log(f"  {category}: {filename}  (quirk: {slug})", log)

    _log("Hardware configuration deployed.", log)
    return manifest


def reconcile_hardware_files(
    hw: HardwareConfig,
    log: LogCallback | None = None,
    *,
    target_root: Path,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Reconcile deployed files on a live system against ``hw``.

    For each managed file currently on disk (identified by the
    ``arches-hardware-rescan: managed`` header), one of three
    transitions happens:

    1. Still applies, content unchanged — leave alone.
    2. Still applies, content changed (quirk file updated upstream) —
       rewrite.
    3. No longer applies (orphan) — delete.

    Newly-applicable quirks that have no on-disk file are written.

    This function is safe to run on the live system. It does NOT
    touch files lacking the management header — those are assumed to
    belong to the user or another package.

    Returns
    -------
    Two manifests: ``(added_or_updated, removed)``, each
    ``{category: [filename, ...]}``.
    """
    desired = _resolved_files(hw)

    added: dict[str, list[str]] = {"modprobe": [], "udev": [], "sysctl": []}
    removed: dict[str, list[str]] = {"modprobe": [], "udev": [], "sysctl": []}

    for category in ("modprobe", "udev", "sysctl"):
        cat_dir = target_root / _CATEGORY_DIRS[category]
        desired_entries = desired[category]

        # --- 1. Remove orphans: managed files not in desired set ---
        if cat_dir.is_dir():
            for existing in sorted(cat_dir.iterdir()):
                if not existing.is_file():
                    continue
                owner = is_managed_file(existing)
                if owner is None:
                    continue  # not ours
                if existing.name not in desired_entries:
                    existing.unlink()
                    removed[category].append(existing.name)
                    _log(
                        f"  {category}: removed {existing.name} "
                        f"(was: {owner}, no longer applies)",
                        log,
                    )

        # --- 2. Write desired entries (skip if already up-to-date) ---
        if desired_entries:
            cat_dir.mkdir(parents=True, exist_ok=True)
        for filename, (slug, content) in desired_entries.items():
            target = cat_dir / filename
            new_content = _tag_content(slug, content)
            existing_owner = is_managed_file(target)
            try:
                current = target.read_text() if existing_owner is not None else None
            except OSError:
                current = None
            if existing_owner is not None and current == new_content:
                continue  # already correct
            if existing_owner is None and target.exists():
                # File exists but isn't ours — refuse to overwrite.
                _log(
                    f"  {category}: SKIP {filename} — exists and is not "
                    f"managed by arches-hardware-rescan (would clobber "
                    f"user/package file)",
                    log,
                )
                continue
            target.write_text(new_content)
            added[category].append(filename)
            verb = "updated" if existing_owner else "added"
            _log(f"  {category}: {verb} {filename}  (quirk: {slug})", log)

    return added, removed


# ---------------------------------------------------------------------------
# Hardware fingerprint — used by the runtime rescan to skip work on
# boots where nothing has changed.
# ---------------------------------------------------------------------------


def compute_fingerprint(
    pci_ids: set[str],
    dmi: DMIInfo,
    chwd_profile: str = "",
) -> str:
    """Stable, order-independent hash of the salient hardware identity.

    Includes:
      - sorted PCI vendor:device pairs
      - DMI sys_vendor, product_name, chassis_type
      - the currently-applied chwd profile name (so a manual driver
        switch via ``chwd -i`` triggers a reconcile next boot too).
    """
    payload = {
        "pci": sorted(pci_ids),
        "dmi": {
            "sys_vendor": dmi.sys_vendor,
            "product_name": dmi.product_name,
            "chassis_type": dmi.chassis_type,
        },
        "chwd_profile": chwd_profile,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def detect_chwd_profile() -> str:
    """Return the name of the currently-installed chwd profile, or "".

    ``chwd --list-installed`` prints one profile per line on success.
    The output format varies by chwd version: some print bare slugs
    one per line, others tabulate them with headers, and on a system
    with no profile installed it prints ``Warning: No installed
    profiles!``.

    Returns the first plausible profile name, or empty string if chwd
    is not available, errors out, or has no installed profile.
    """
    try:
        result = subprocess.run(
            ["chwd", "--list-installed"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    import re

    # A real chwd profile slug looks like "nvidia-open-dkms", "amd",
    # "intel", "macbook-t2", "virtualmachine" — lowercase letters,
    # digits, hyphens, and dots only. Anything else (warning lines,
    # banners, headers) is rejected.
    _PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")
    for line in result.stdout.splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if token and _PROFILE_RE.match(token):
            return token
    return ""


def read_fingerprint(target_root: Path) -> str:
    """Read the persisted fingerprint, or return "" if absent."""
    fp = target_root / STATE_DIR_REL / FINGERPRINT_FILE
    try:
        return fp.read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return ""


def write_fingerprint(
    fingerprint: str,
    target_root: Path,
    log: LogCallback | None = None,
) -> None:
    """Write the fingerprint atomically to ``<state>/hardware-fingerprint``."""
    state_dir = target_root / STATE_DIR_REL
    state_dir.mkdir(parents=True, exist_ok=True)
    fp = state_dir / FINGERPRINT_FILE
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(fingerprint + "\n")
    tmp.replace(fp)
    _log(f"  wrote fingerprint -> {fp}", log)


def read_manifest(target_root: Path) -> dict[str, list[str]]:
    """Read the persisted file manifest, or return an empty one."""
    fp = target_root / STATE_DIR_REL / MANIFEST_FILE
    try:
        return json.loads(fp.read_text())
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return {"modprobe": [], "udev": [], "sysctl": []}


def write_manifest(
    manifest: dict[str, list[str]],
    target_root: Path,
    log: LogCallback | None = None,
) -> None:
    """Write the manifest atomically to ``<state>/hardware-manifest.json``."""
    state_dir = target_root / STATE_DIR_REL
    state_dir.mkdir(parents=True, exist_ok=True)
    fp = state_dir / MANIFEST_FILE
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    tmp.replace(fp)
    _log(f"  wrote manifest    -> {fp}", log)
