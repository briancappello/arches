"""Hardware quirk and machine profile detection and deployment.

Quirks are device-class fixes auto-detected by scanning PCI IDs and
chassis type.  Machine profiles are model-specific configs matched by
DMI (vendor, product name).  Both are declared as TOML files under
``hardware/quirks/`` and ``hardware/machines/``.

At install time the resolved hardware config is deployed as modprobe
configs, udev rules, and sysctl tunables into the target filesystem.
Machine-specific packages, services, and Ansible roles are merged into
the install pipeline alongside the template's own lists.
"""

from __future__ import annotations

import fnmatch
import shutil
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


def deploy_hardware_files(
    hw: HardwareConfig,
    log: LogCallback | None = None,
) -> None:
    """Write hardware config files into the target filesystem.

    Deploys modprobe configs, udev rules, and sysctl tunables from
    all resolved quirks and the machine profile.
    """
    if not hw.quirks and not hw.machine:
        _log("No hardware config to deploy.", log)
        return

    _log("Deploying hardware configuration files...", log)

    # modprobe configs -> /etc/modprobe.d/
    modprobe_dir = MOUNT_ROOT / "etc" / "modprobe.d"
    modprobe_dir.mkdir(parents=True, exist_ok=True)
    for filename, value in hw.all_modprobe.items():
        # Resolve content from quirk/machine files_dir
        content = value
        for source in hw.quirks + ([hw.machine] if hw.machine else []):
            if filename in getattr(source, "modprobe", {}):
                content = _resolve_file_content(filename, value, source.files_dir)
                break
        target = modprobe_dir / filename
        target.write_text(content.rstrip("\n") + "\n")
        _log(f"  modprobe: {filename}", log)

    # udev rules -> /etc/udev/rules.d/
    udev_dir = MOUNT_ROOT / "etc" / "udev" / "rules.d"
    udev_dir.mkdir(parents=True, exist_ok=True)
    for filename, value in hw.all_udev.items():
        content = value
        for source in hw.quirks + ([hw.machine] if hw.machine else []):
            if filename in getattr(source, "udev", {}):
                content = _resolve_file_content(filename, value, source.files_dir)
                break
        target = udev_dir / filename
        target.write_text(content.rstrip("\n") + "\n")
        _log(f"  udev:    {filename}", log)

    # sysctl configs -> /etc/sysctl.d/
    if hw.all_sysctl:
        sysctl_dir = MOUNT_ROOT / "etc" / "sysctl.d"
        sysctl_dir.mkdir(parents=True, exist_ok=True)
        for filename, value in hw.all_sysctl.items():
            content = value
            for source in hw.quirks + ([hw.machine] if hw.machine else []):
                if filename in getattr(source, "sysctl", {}):
                    content = _resolve_file_content(filename, value, source.files_dir)
                    break
            target = sysctl_dir / filename
            target.write_text(content.rstrip("\n") + "\n")
            _log(f"  sysctl:  {filename}", log)

    _log("Hardware configuration deployed.", log)
