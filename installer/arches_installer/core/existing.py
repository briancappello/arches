"""Install into partitions that already exist (layout kind "existing").

An existing layout names its target partitions by GPT PARTLABEL on the
disks of a role. It writes no partition table. The only partitions it
writes to are the targets; the ESP it reuses is mounted, never formatted.

Before anything is written, :func:`plan_existing_layout` checks the guard
(design D1) and refuses unless all of these hold:

- every target partition carries the declared PARTLABEL, exactly once on
  every disk of its role;
- ``blkid -p`` finds no signature on it (no filesystem, LUKS or RAID
  member) and nothing holds or mounts it;
- the reused ESP exists, is vfat, and is the one ESP that holds
  ``limine.conf``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from arches_installer.core.disk import MOUNT_ROOT, EncryptedVolume, PartitionMap
from arches_installer.core.disk_layout import (
    DiskLayout,
    PartitionSpec,
    ResolvedDiskRoles,
    _create_and_mount_subvolumes,
    _format_partition,
    _mount_partition,
)
from arches_installer.core.encryption import (
    EncryptionSecrets,
    configure_mkinitcpio_hooks,
    create_luks_volume,
    tpm2_present,
    validate_pin,
    write_crypttab_initramfs,
    write_mdadm_conf,
)
from arches_installer.core.run import LogCallback, _log
from arches_installer.core.secureboot import PACKAGES as SECURE_BOOT_PACKAGES
from arches_installer.core.run import run as logged_run

# The reused ESP is the one that already holds the Limine config of the
# current system: its entries are merged, never replaced (design D3).
ESP_MARKER = "limine.conf"


class ExistingLayoutError(RuntimeError):
    """The D1 guard refused the install. Nothing was written."""


# ---------------------------------------------------------------------------
# Probing (read-only)
# ---------------------------------------------------------------------------


class Probe(Protocol):
    """Read-only questions the guard asks about block devices."""

    def partitions(self, disk: str) -> list[tuple[str, str]]:
        """Return (partition path, PARTLABEL) for every partition of *disk*."""

    def signatures(self, part: str) -> list[str]:
        """Return the signature types blkid finds on *part* (empty if none)."""

    def in_use(self, part: str) -> list[str]:
        """Return holders and mount points of *part* (empty if unused)."""

    def has_file(self, part: str, name: str) -> bool:
        """Return True if the filesystem on *part* has *name* at its root."""

    def has_tpm2(self) -> bool:
        """Return True if a TPM 2.0 is available."""


class SystemProbe:
    """Probe the live system. Every call is read-only."""

    def partitions(self, disk: str) -> list[tuple[str, str]]:
        out = subprocess.run(
            ["lsblk", "-J", "-p", "-o", "PATH,PARTLABEL,TYPE", disk],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        found: list[tuple[str, str]] = []

        def walk(nodes: list[dict]) -> None:
            for n in nodes:
                if n.get("type") == "part":
                    found.append((n["path"], n.get("partlabel") or ""))
                walk(n.get("children", []))

        walk(json.loads(out).get("blockdevices", []))
        return found

    def signatures(self, part: str) -> list[str]:
        # -p probes the device itself (no cache); exit code 2 means no
        # signature was found.
        result = subprocess.run(
            ["blkid", "-p", "-o", "export", part],
            capture_output=True,
            text=True,
        )
        if result.returncode == 2:
            return []
        if result.returncode != 0:
            raise ExistingLayoutError(
                f"blkid -p {part} failed ({result.returncode}): {result.stderr.strip()}"
            )
        # For a partition blkid -p also reports the GPT entry itself
        # (PART_ENTRY_*); that describes the slot, not its content.
        types: list[str] = []
        content = False
        for line in result.stdout.splitlines():
            key, _, value = line.partition("=")
            if key == "DEVNAME" or key.startswith("PART_ENTRY_"):
                continue
            content = True
            if key in ("TYPE", "PTTYPE"):
                types.append(value)
        if not content:
            return []
        # blkid found something it could not name: still a signature.
        return types or ["unknown"]

    def in_use(self, part: str) -> list[str]:
        name = Path(part).resolve().name
        users = [
            f"held by {h.name}"
            for h in sorted(Path(f"/sys/class/block/{name}/holders").glob("*"))
        ]
        out = subprocess.run(
            ["lsblk", "-n", "-o", "MOUNTPOINTS", part],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        users += [f"mounted at {m}" for m in out.split() if m]
        return users

    def has_file(self, part: str, name: str) -> bool:
        tmp = Path(tempfile.mkdtemp(prefix="arches-esp-probe-"))
        try:
            subprocess.run(
                ["mount", "-o", "ro", part, str(tmp)],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            tmp.rmdir()
            return False
        try:
            return (tmp / name).is_file()
        finally:
            subprocess.run(["umount", str(tmp)], check=False)
            tmp.rmdir()

    def has_tpm2(self) -> bool:
        return tpm2_present()


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


@dataclass
class ExistingVolume:
    """One target volume: a layout partition and the partitions behind it."""

    spec: PartitionSpec
    members: list[str]


@dataclass
class ExistingPlan:
    """What an existing layout will touch. Built only when the guard holds."""

    esp: str
    esp_spec: PartitionSpec
    volumes: list[ExistingVolume] = field(default_factory=list)

    @property
    def targets(self) -> list[str]:
        """Every partition the install writes to."""
        return [m for v in self.volumes for m in v.members]


def _labelled(
    probe: Probe, disks: list[str], partlabel: str, errors: list[str]
) -> list[str]:
    """Return the one partition with *partlabel* on each disk."""
    found: list[str] = []
    for disk in disks:
        matches = [p for p, label in probe.partitions(disk) if label == partlabel]
        if len(matches) != 1:
            errors.append(
                f"{disk}: expected exactly one partition with PARTLABEL "
                f"{partlabel!r}, found {len(matches)}"
                + (f" ({', '.join(matches)})" if matches else "")
            )
            continue
        found.append(matches[0])
    return found


def plan_existing_layout(
    layout: DiskLayout,
    resolved: ResolvedDiskRoles,
    *,
    probe: Probe | None = None,
) -> ExistingPlan:
    """Check the D1 guard and return what the install will touch.

    Raises :class:`ExistingLayoutError` listing every failed check. Only
    reads: it never writes to any device.
    """
    if not layout.is_existing:
        raise ValueError(f"layout {layout.name!r} is not an existing layout")
    probe = probe or SystemProbe()
    errors: list[str] = []

    def role_disks(part: PartitionSpec) -> list[str]:
        devs = resolved.assignments.get(part.target_role)
        if not devs:
            errors.append(f"disk role {part.target_role!r} has no disks")
            return []
        return [d.path for d in devs]

    esp = ""
    esp_spec: PartitionSpec | None = None
    volumes: list[ExistingVolume] = []

    for part in layout.partitions:
        disks = role_disks(part)
        if not disks:
            continue
        if part.reuse:
            esp_spec = part
            candidates = [
                p
                for d in disks
                for p, label in probe.partitions(d)
                if label == part.partlabel
            ]
            vfat = [p for p in candidates if "vfat" in probe.signatures(p)]
            marked = [p for p in vfat if probe.has_file(p, ESP_MARKER)]
            if len(marked) == 1:
                esp = marked[0]
            else:
                errors.append(
                    f"expected exactly one existing vfat ESP with PARTLABEL "
                    f"{part.partlabel!r} that holds {ESP_MARKER}; found "
                    f"{len(marked)} (candidates: {', '.join(candidates) or 'none'}"
                    f"; vfat: {', '.join(vfat) or 'none'})"
                )
            continue

        members = _labelled(probe, disks, part.partlabel, errors)
        for m in members:
            sigs = probe.signatures(m)
            if sigs:
                errors.append(
                    f"{m} (PARTLABEL {part.partlabel!r}) is not empty: "
                    f"blkid found {', '.join(sigs)}"
                )
            users = probe.in_use(m)
            if users:
                errors.append(f"{m} is in use: {', '.join(users)}")
        volumes.append(ExistingVolume(spec=part, members=members))

    if esp and esp in {m for v in volumes for m in v.members}:
        errors.append(f"{esp} is both the reused ESP and a target")

    if any(v.spec.encryption for v in volumes) and not probe.has_tpm2():
        errors.append(
            "the layout is encrypted with TPM2 + PIN, but no TPM 2.0 is present"
        )

    if errors:
        raise ExistingLayoutError(
            "Refusing to install: the existing-partition guard failed. "
            "Nothing was written.\n  - " + "\n  - ".join(errors)
        )
    assert esp_spec is not None
    return ExistingPlan(esp=esp, esp_spec=esp_spec, volumes=volumes)


def resolve_and_plan(
    layout: DiskLayout,
    machine_disks: list | None = None,
    auto_install_disks: list | None = None,
    probe: Probe | None = None,
) -> tuple[ResolvedDiskRoles, ExistingPlan]:
    """Resolve the layout's disk roles on this machine and check the guard."""
    from arches_installer.core.disk import detect_block_devices
    from arches_installer.core.disk_layout import resolve_disk_roles

    resolved = resolve_disk_roles(
        layout,
        detect_block_devices(),
        machine_disks=machine_disks,
        auto_install_disks=auto_install_disks,
    )
    return resolved, plan_existing_layout(layout, resolved, probe=probe)


# ---------------------------------------------------------------------------
# Applying the plan
# ---------------------------------------------------------------------------


def layout_packages(layout: DiskLayout) -> list[str]:
    """Packages the target needs to assemble and unlock *layout* at boot."""
    pkgs: list[str] = []
    for p in layout.partitions:
        if p.reuse:
            pkgs.extend(pk for pk in SECURE_BOOT_PACKAGES if pk not in pkgs)
        if p.raid_backend == "mdadm" and "mdadm" not in pkgs:
            pkgs.append("mdadm")
        if p.encryption == "luks2":
            for pkg in ("cryptsetup", "tpm2-tss"):
                if pkg not in pkgs:
                    pkgs.append(pkg)
    return pkgs


def unmount_shared_esp(log: LogCallback | None = None) -> None:
    """Take the shared ESP away from the target before pacstrap.

    Installing kernels runs mkinitcpio's preset hook and limine's deploy
    hook; with the ESP at /mnt/boot they would write vmlinuz-*,
    initramfs-*.img (overwriting another system's files of that name),
    limine.conf entries and an NVRAM entry onto the shared ESP. Without
    it they write into the new root's own /boot directory instead.
    """
    logged_run(["umount", str(MOUNT_ROOT / "boot")], log=log)


def remount_shared_esp(esp: str, log: LogCallback | None = None) -> None:
    """After pacstrap: drop what it wrote into the root's /boot, mount the ESP.

    Also removes the mkinitcpio presets: this system boots UKIs built by
    limine-mkinitcpio, and a later ``mkinitcpio -P`` (chwd runs one) would
    otherwise build preset images onto the shared ESP.
    """
    boot = MOUNT_ROOT / "boot"
    if os.path.ismount(boot):
        raise ExistingLayoutError(f"{boot} is still a mount point; not cleaning it")
    for child in boot.iterdir() if boot.is_dir() else []:
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for preset in (MOUNT_ROOT / "etc" / "mkinitcpio.d").glob("*.preset"):
        preset.unlink()
        _log(f"Removed mkinitcpio preset {preset.name} (UKIs only)", log)
    boot.mkdir(parents=True, exist_ok=True)
    logged_run(["mount", esp, str(boot)], log=log)


def configure_target_storage(
    parts: PartitionMap, log: LogCallback | None = None
) -> None:
    """Make the installed system assemble and unlock its volumes at boot."""
    if parts.md_devices:
        write_mdadm_conf(parts.md_devices, log=log)
    if parts.encrypted_volumes:
        write_crypttab_initramfs(parts.encrypted_volumes, log=log)
    if parts.md_devices or parts.encrypted_volumes:
        configure_mkinitcpio_hooks(mdadm=bool(parts.md_devices), log=log)


def _assemble(vol: ExistingVolume, log: LogCallback | None) -> str:
    """Return the block device for *vol*: its one member, or a new md array."""
    spec = vol.spec
    if not spec.raid_level:
        return vol.members[0]
    name = spec.label or spec.partlabel
    md = f"/dev/md/{name}"
    _log(
        f"  Creating mdadm RAID{spec.raid_level} {md} from {', '.join(vol.members)}...",
        log,
    )
    logged_run(
        [
            "mdadm",
            "--create",
            md,
            f"--level={spec.raid_level}",
            f"--raid-devices={len(vol.members)}",
            "--metadata=1.2",
            f"--name={name}",
            "--run",
        ]
        + vol.members,
        log=log,
    )
    logged_run(["udevadm", "settle", "--timeout=10"], log=log)
    return md


def apply_existing_layout(
    layout: DiskLayout,
    plan: ExistingPlan,
    log: LogCallback | None = None,
    *,
    encryption: EncryptionSecrets | None = None,
) -> PartitionMap:
    """Build the planned volumes and mount everything under MOUNT_ROOT.

    Writes only to ``plan.targets`` (and devices built on them). The ESP
    is mounted as-is. An encrypted volume needs *encryption*.
    """
    if any(v.spec.encryption for v in plan.volumes):
        if encryption is None:
            raise ExistingLayoutError(
                f"Layout {layout.name!r} is encrypted; a TPM PIN is required."
            )
        validate_pin(encryption.pin)
    _log(
        f'[bold cyan]Applying existing layout "{layout.name}"[/bold cyan]',
        log,
    )
    _log(f"  Targets: {', '.join(plan.targets)}", log)
    _log(f"  Reused ESP (not formatted): {plan.esp}", log)

    root_path = ""
    root_fs = ""
    root_subvolumes: list[str] = []
    mounted_later: list[tuple[str, PartitionSpec]] = []
    encrypted: list[EncryptedVolume] = []
    md_devices: list[str] = []

    for idx, vol in enumerate(plan.volumes):
        spec = vol.spec
        dev = _assemble(vol, log)
        if dev not in vol.members:
            md_devices.append(dev)
        if spec.encryption:
            assert encryption is not None
            crypt = create_luks_volume(
                dev, spec.label or spec.partlabel, encryption, log
            )
            encrypted.append(crypt)
            dev = crypt.mapper
        _format_partition(dev, spec, idx + 1, log=log)
        if spec.mount_point == "/":
            root_path, root_fs = dev, spec.filesystem
            if spec.filesystem == "btrfs" and spec.subvolumes:
                root_subvolumes = _create_and_mount_subvolumes(dev, spec, log=log)
            else:
                _mount_partition(dev, spec, log=log)
        else:
            mounted_later.append((dev, spec))

    for dev, spec in mounted_later:
        if spec.filesystem == "btrfs" and spec.subvolumes:
            _create_and_mount_subvolumes(dev, spec, log=log)
        else:
            _mount_partition(dev, spec, log=log)

    _mount_partition(plan.esp, plan.esp_spec, log=log)

    return PartitionMap(
        esp=plan.esp,
        root=root_path,
        root_filesystem=root_fs,
        root_subvolumes=root_subvolumes,
        esp_reused=True,
        encrypted_volumes=encrypted,
        md_devices=md_devices,
    )
