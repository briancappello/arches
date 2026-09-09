"""Shared install pipeline -- the single source of truth for the install sequence.

Both the TUI progress screen and auto-install mode call ``run_install_pipeline``
to execute the install.  This eliminates duplication and ensures logging,
error handling, and phase ordering are consistent regardless of the entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from arches_installer.core.bootloader import install_bootloader
from arches_installer.core.disk import MOUNT_ROOT, PartitionMap
from arches_installer.core.disk_layout import (
    DiskLayout,
    RaidBackend,
    RaidConfig,
    RaidLevel,
    ResolvedDiskRoles,
    apply_disk_layout,
    apply_disk_layout_resolved,
    mirror_esp,
    setup_raid_mdadm,
    write_disk_roles_state,
    _part_name,
)
from arches_installer.core.firstboot import inject_firstboot_service
from arches_installer.core.hardware import HardwareConfig
from arches_installer.core.install import append_swap_fstab_entries, install_system
from arches_installer.core.platform import PlatformConfig
from arches_installer.core.provider import (
    ProviderConfig,
    ProviderInputs,
    resolve_provider,
    resolve_install_provider,
    validate_selection,
    check_provider_identity,
    write_provider_identity,
)
from arches_installer.core.run import LogCallback
from arches_installer.core.snapper import setup_snapshots
from arches_installer.core.template import InstallTemplate


@dataclass
class InstallParams:
    """All parameters needed to run the install pipeline."""

    platform: PlatformConfig
    template: InstallTemplate
    device: str
    hostname: str
    username: str
    password: str
    # Pre-existing partition map (manual partitioning).
    # If set, disk_layout is ignored and these mounts are used as-is.
    partition_map: PartitionMap | None = None
    # Disk layout to apply (layout-based partitioning).
    # Used when partition_map is None.
    disk_layout: DiskLayout | None = None
    # Optional RAID configuration (btrfs multi-device or mdadm).
    # Used by the legacy single-disk-or-homogeneous-RAID flow only.
    # Multi-disk role-based layouts use ``resolved_disk_roles`` instead.
    raid_config: RaidConfig | None = None
    # Resolved multi-disk role assignments. When set, the disk layout
    # is applied via :func:`apply_disk_layout_resolved` and the legacy
    # ``device`` + ``raid_config`` parameters are ignored.
    resolved_disk_roles: ResolvedDiskRoles | None = None
    # Hardware configuration (machine profile + auto-detected quirks).
    # When set, hardware-specific packages, services, modprobe/udev
    # configs, and Ansible roles are merged into the install.
    hardware: HardwareConfig | None = None
    # Extra Ansible variables from auto-install config [ansible_vars].
    # Forwarded as -e key=value to the firstboot ansible-playbook run.
    ansible_vars: dict[str, str] | None = None
    provider: ProviderConfig | None = None
    inputs: ProviderInputs | None = None
    install_mode: str = "fresh"


def detect_firmware() -> str:
    return "uefi" if Path("/sys/firmware/efi").is_dir() else "bios"


def install_identity(params: InstallParams) -> dict:
    provider = params.provider or resolve_provider(params.platform)
    return {
        "provider": provider.id,
        "platform": params.platform.name,
        "template": params.template.name,
        "channel": "stable",
        "bootstrap_revision": "4.0.2" if provider.id == "omarchy" else "legacy",
    }


def _inspect_disk(device: str) -> dict:
    if not re.fullmatch(r"/dev/[A-Za-z0-9_-]+", device):
        raise ValueError("Omarchy requires a physical whole disk, not a mapped device")
    result = subprocess.run(
        [
            "lsblk",
            "--json",
            "--tree",
            "--bytes",
            "--paths",
            "--output",
            "PATH,TYPE,PKNAME,PARTN,PARTTYPE,SIZE,FSTYPE,UUID,MOUNTPOINTS",
            device,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    devices = json.loads(result.stdout)["blockdevices"]
    if (
        len(devices) != 1
        or devices[0]["path"] != device
        or devices[0]["type"] != "disk"
    ):
        raise ValueError("Omarchy requires one physical disk")
    disk = devices[0]
    if any(p["type"] != "part" or p.get("children") for p in disk.get("children", [])):
        raise ValueError("Omarchy encryption/RAID/mapped devices are unsupported")
    return disk


def check_existing_disk_identity(device: str) -> None:
    """Inspect existing Linux roots read-only before a fresh layout can erase them."""
    result = subprocess.run(
        ["lsblk", "--json", "--tree", "--paths", "--output", "PATH,FSTYPE", device],
        check=True,
        capture_output=True,
        text=True,
    )
    devices = list(json.loads(result.stdout)["blockdevices"])
    if not devices:
        raise ValueError("Cannot inspect an empty block-device inventory")
    options = {
        "btrfs": "ro,nologreplay,subvolid=5",
        "ext4": "ro,noload",
        "xfs": "ro,norecovery",
        "vfat": "ro",
    }
    while devices:
        item = devices.pop()
        devices.extend(item.get("children", []))
        filesystem = item.get("fstype")
        if filesystem in (None, ""):
            if item["path"] != device:
                raise ValueError("Cannot inspect an unidentified existing partition")
            continue
        if filesystem == "swap":
            continue
        if filesystem not in options:
            raise ValueError(f"Cannot safely inspect existing filesystem: {filesystem}")
        directory = Path(tempfile.mkdtemp(prefix="arches-identity-", dir="/run"))
        mounted = False
        try:
            subprocess.run(
                [
                    "mount",
                    "-t",
                    filesystem,
                    "-o",
                    options[filesystem],
                    item["path"],
                    str(directory),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            mounted = True
            roots = [directory]
            if filesystem == "btrfs":
                listing = subprocess.run(
                    ["btrfs", "subvolume", "list", "-a", str(directory)],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                for line in listing.splitlines():
                    match = re.fullmatch(
                        r"ID \d+ gen \d+ top level \d+ path (.+)", line
                    )
                    if not match:
                        raise ValueError(
                            "Cannot inspect malformed Btrfs subvolume inventory"
                        )
                    relative = PurePosixPath(match[1].removeprefix("<FS_TREE>/"))
                    if (
                        relative.is_absolute()
                        or ".." in relative.parts
                        or not relative.parts
                    ):
                        raise ValueError(
                            "Unsafe Btrfs subvolume path during identity inspection"
                        )
                    roots.append(directory / relative)
            # A filesystem can back /, /var, /var/lib, or /var/lib/arches.
            # Inspect these logical locations, not arbitrary files on the disk.
            for root in roots:
                if root.resolve() != root or not root.is_dir():
                    raise ValueError("Cannot safely inspect existing subvolume root")
                for relative in (
                    "var/lib/arches/provider.json",
                    "lib/arches/provider.json",
                    "arches/provider.json",
                    "provider.json",
                ):
                    path = root / relative
                    if path.resolve() != path:
                        raise ValueError("Symlink in existing provider identity path")
                    try:
                        path.lstat()
                    except FileNotFoundError:
                        continue
                    raise ValueError(
                        "Existing on-disk provider identity; refusing a reinstall or provider switch"
                    )
        finally:
            if mounted:
                # Failed unmount deliberately retains scratch instead of removing it.
                subprocess.run(
                    ["umount", str(directory)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            directory.rmdir()


def inspect_omarchy_mounts(params, parts) -> str:
    """Read actual mount and partition identity, without creating target files."""
    disk = _inspect_disk(params.device)
    partitions = disk.get("children", [])
    if len(partitions) != 2:
        raise ValueError("Omarchy requires exactly ESP and root partitions")
    esp, root = partitions
    if (
        (esp["path"], root["path"]) != (parts.esp, parts.root)
        or str(esp.get("partn")) != "1"
        or str(root.get("partn")) != "2"
    ):
        raise ValueError("Omarchy partition identity mismatch")
    if (
        esp.get("parttype", "").lower() != "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
        or int(esp["size"]) != 2 * 1024**3
        or esp.get("fstype") != "vfat"
        or root.get("fstype") != "btrfs"
    ):
        raise ValueError("Omarchy requires a 2 GiB ESP and Btrfs root")
    uuid = root.get("uuid", "")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        raise ValueError("Omarchy root UUID missing")
    result = subprocess.run(
        [
            "findmnt",
            "--json",
            "--list",
            "--submounts",
            "--mountpoint",
            str(MOUNT_ROOT),
            "--output",
            "TARGET,SOURCE,FSTYPE,FSROOT,UUID,OPTIONS",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    records = json.loads(result.stdout)["filesystems"]
    expected = {
        str(MOUNT_ROOT / mount.lstrip("/")): subvol
        for mount, subvol in (
            ("/", "@"),
            ("/home", "@home"),
            ("/var/log", "@log"),
            ("/var/cache/pacman/pkg", "@pkg"),
            ("/boot", None),
        )
    }
    if len(records) != 5 or {r["target"] for r in records} != expected.keys():
        raise ValueError("Omarchy requires exact @/@home/@log/@pkg and /boot mounts")
    for record in records:
        subvol = expected[record["target"]]
        source = record["source"].split("[", 1)[0]
        if "rw" not in record["options"].split(","):
            raise ValueError("Omarchy target mounts must be writable")
        if subvol is None:
            if (
                source != parts.esp
                or record["fstype"] != "vfat"
                or record["fsroot"] != "/"
            ):
                raise ValueError("Omarchy ESP mount mismatch")
        elif (
            source != parts.root
            or record["fstype"] != "btrfs"
            or record["uuid"] != uuid
            or record["fsroot"] != "/" + subvol
        ):
            raise ValueError("Omarchy root subvolume binding mismatch")
    topology = subprocess.run(
        ["btrfs", "filesystem", "show", "--raw", str(MOUNT_ROOT)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if re.findall(r"Total devices\s+(\d+)", topology) != ["1"]:
        raise ValueError("Omarchy multi-device Btrfs/RAID is unsupported")
    subvolumes = subprocess.run(
        ["btrfs", "subvolume", "list", str(MOUNT_ROOT)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if sorted(re.findall(r" path (.+)$", subvolumes, re.MULTILINE)) != [
        "@",
        "@home",
        "@log",
        "@pkg",
    ]:
        raise ValueError(
            "Omarchy requires exactly four standard subvolumes, no hidden @var"
        )
    return uuid


def preflight_provider(params: InstallParams) -> None:
    """Resolve once, authenticate outside the target, then authorize disk setup."""
    try:
        selected, inputs = resolve_install_provider(
            params.platform, params.provider.id if params.provider else None
        )
        if params.provider is not None and params.provider != selected:
            raise ValueError("Provider policy differs from staged selection")
        if params.inputs is not None and params.inputs != inputs:
            raise ValueError(
                "Provider input pin must come from trusted staged metadata"
            )
        params.provider = params.provider or selected
        params.inputs = params.inputs or inputs
        provider = params.provider
        check_provider_identity(MOUNT_ROOT, install_identity(params))
        if provider.id == "arch":
            raise ValueError(
                "Stock Arch boot integration is unproven; no runnable template is enabled"
            )
        if params.template.providers and provider.id not in params.template.providers:
            raise ValueError("Template/provider mismatch")
        if (
            params.install_mode == "fresh"
            and params.partition_map is None
            and not params.platform.allow_auto_install
        ):
            raise ValueError("Platform prohibits automatic disk installation")
        if provider.id != "omarchy":
            if "zsh" in params.template.ansible.firstboot_roles:
                from arches_installer.core import ohmyzsh
                from arches_installer.core.provider import STAGED_ROOT

                ohmyzsh.preflight(STAGED_ROOT)
            if (
                params.install_mode == "fresh"
                and params.partition_map is None
                and params.disk_layout is not None
            ):
                devices = {params.device}
                if params.resolved_disk_roles is not None:
                    devices = {
                        d.path
                        for group in params.resolved_disk_roles.assignments.values()
                        for d in group
                    }
                elif params.raid_config is not None:
                    devices = set(params.raid_config.devices)
                for device in devices:
                    check_existing_disk_identity(device)
            return
        if params.platform.bootloader.type != "limine":
            raise ValueError("Omarchy requires its upstream Limine boot configuration")
        if [(v.package, v.headers) for v in provider.kernel.variants] != [
            ("linux", "linux-headers")
        ]:
            raise ValueError(
                "Omarchy requires the authenticated stock linux kernel selection"
            )
        layout = params.disk_layout
        filesystem = (
            params.partition_map.root_filesystem
            if params.partition_map
            else next(
                (p.filesystem for p in layout.partitions if p.mount_point == "/"), ""
            )
            if isinstance(layout, DiskLayout)
            else ""
        )
        validate_selection(
            params.platform,
            provider,
            params.template,
            firmware=detect_firmware(),
            filesystem=filesystem,
            install_mode=params.install_mode,
        )
        if (
            params.raid_config
            or params.ansible_vars
            or (params.hardware and (params.hardware.machine or params.hardware.quirks))
        ):
            raise ValueError(
                "Omarchy RAID, hardware profiles/quirks and Arches overrides are unsupported"
            )
        if params.partition_map is None:
            if not isinstance(layout, DiskLayout) or len(layout.partitions) != 2:
                raise ValueError("Omarchy requires an explicit standard layout")
            esp, root = layout.partitions
            if (
                esp.size,
                esp.filesystem,
                esp.mount_point,
                root.size,
                root.filesystem,
                root.mount_point,
            ) != ("2G", "vfat", "/boot", "*", "btrfs", "/") or esp.subvolumes:
                raise ValueError("Omarchy requires a 2 GiB ESP at /boot and Btrfs root")
            if [(s.name, s.mount_point) for s in root.subvolumes] != [
                ("@", "/"),
                ("@home", "/home"),
                ("@log", "/var/log"),
                ("@pkg", "/var/cache/pacman/pkg"),
            ]:
                raise ValueError(
                    "Omarchy requires exact @/@home/@log/@pkg bindings, no @var"
                )
            if (
                any(
                    p.disks or p.raid_level or p.raid_backend for p in layout.partitions
                )
                or esp.target_role != root.target_role
            ):
                raise ValueError("Omarchy multi-disk/RAID layout unsupported")
            if (
                any(
                    option not in ("compress=zstd:1", "noatime", "ssd", "discard=async")
                    for option in root.mount_options.split(",")
                    if option
                )
                or esp.mount_options
            ):
                raise ValueError("Unsupported Omarchy mount options")
            if params.resolved_disk_roles is not None:
                assignments = params.resolved_disk_roles.assignments
                if set(assignments) != {root.target_role} or [
                    d.path for d in assignments[root.target_role]
                ] != [params.device]:
                    raise ValueError("Omarchy requires one resolved disk role")
        from arches_installer.core.omarchy import validate_install_settings

        validate_install_settings(
            params.template, params.hostname, params.username, params.password
        )
        if inputs is None:
            raise ValueError(
                "Omarchy requires complete authenticated staged inputs; online acquisition is unavailable"
            )
        from arches_installer.core.omarchy import (
            prepare_stock_engine,
            validate_bootstrap_inputs,
            validate_fresh_target,
        )
        from arches_installer.core.install import package_transaction
        from arches_installer.core.run import run

        files, targets, _, _ = validate_bootstrap_inputs(
            params.platform, provider, inputs
        )
        # Reuse the isolated transaction in scratch, never in the installation root.
        with tempfile.TemporaryDirectory(
            prefix="arches-preflight-", dir="/run"
        ) as directory:
            with package_transaction(
                params.platform, provider=provider, inputs=inputs, root=Path(directory)
            ) as tx:
                # Pacman resolves virtual targets (such as nvim -> neovim) too.
                run(
                    ["pacman"]
                    + tx.pacman_args
                    + ["-Sw", "--noconfirm", "--ask=64"]
                    + targets
                )
                prepare_stock_engine(tx, files)
        # No physical-machine policy has been reviewed. Limit the acceptance path
        # to QEMU with virtual display; Task 7 still owns actual boot acceptance.
        from arches_installer.core.hardware import get_dmi_info

        if get_dmi_info().sys_vendor != "QEMU":
            raise ValueError(
                "Unverified Omarchy hardware: only the QEMU acceptance target is enabled"
            )
        pci = subprocess.run(
            ["lspci", "-Dn"], check=True, capture_output=True, text=True
        ).stdout.splitlines()
        displays = [
            line.split()[2]
            for line in pci
            if len(line.split()) >= 3 and line.split()[1].startswith("03")
        ]
        if not displays or any(device != "1af4:1050" for device in displays):
            raise ValueError("Unverified Omarchy GPU: virtio display required")
        if params.partition_map is not None:
            inspect_omarchy_mounts(params, params.partition_map)
            validate_fresh_target(MOUNT_ROOT)
        else:
            disk = _inspect_disk(params.device)
            if any(
                any(p.get("mountpoints") or [])
                for p in [disk, *disk.get("children", [])]
            ):
                raise ValueError("Omarchy automatic target must not be mounted")
            check_existing_disk_identity(params.device)
    except (
        ValueError,
        OSError,
        subprocess.SubprocessError,
        KeyError,
        TypeError,
    ) as exc:
        raise ValueError(f"Provider preflight: {exc}") from exc


def run_install_pipeline(
    params: InstallParams,
    log: LogCallback,
) -> PartitionMap:
    """Run the full install pipeline.

    Executes all 5 phases in order:
      1. Disk setup (manual map, layout-based, or RAID + layout)
      2. System install (pacstrap + chroot configuration)
      3. Bootloader (Limine or GRUB) + optional ESP mirroring
      4. Snapshots (if btrfs root)
      5. First-boot service (Ansible + firstboot packages)

    Returns the partition map (useful for the caller to store).
    Raises on failure -- the caller should catch and handle.
    """
    platform = params.platform
    template = params.template
    preflight_provider(params)
    provider = params.provider or resolve_provider(platform)

    # Phase 1: Disk
    log("[bold cyan]== Phase 1: Disk Setup ==[/bold cyan]")
    if params.partition_map is not None:
        parts = params.partition_map
        log("Using manually prepared mounts:")
        log(f"  Root: {parts.root}")
        log(f"  ESP:  {parts.esp}")
        if parts.boot:
            log(f"  Boot: {parts.boot}")
        if parts.home:
            log(f"  Home: {parts.home}")
        log("[green]Manual mounts verified.[/green]")
    elif params.disk_layout is not None:
        if params.resolved_disk_roles is not None:
            # Multi-disk role-based path: a separate resolver step
            # already mapped each layout role to physical disks.
            log(
                "[bold cyan]Applying multi-disk layout with "
                f"{len(params.resolved_disk_roles.assignments)} role(s)...[/bold cyan]"
            )
            for role, devs in params.resolved_disk_roles.assignments.items():
                paths = ", ".join(d.path for d in devs)
                log(f"  {role}: {paths}")
            parts = apply_disk_layout_resolved(
                params.disk_layout,
                params.resolved_disk_roles,
                log=log,
            )
            log("[green]Disk prepared successfully.[/green]")
        else:
            primary_device = params.device
            extra_devices: list[str] = []

            if params.raid_config is not None:
                if params.raid_config.backend == RaidBackend.MDADM:
                    # mdadm: assemble array first, then apply layout to /dev/md0
                    log(
                        f"[bold cyan]Setting up mdadm "
                        f"RAID{params.raid_config.level.value}...[/bold cyan]"
                    )
                    primary_device = setup_raid_mdadm(params.raid_config, log=log)
                elif params.raid_config.backend == RaidBackend.BTRFS:
                    # btrfs RAID: apply layout to all disks simultaneously
                    log(
                        f"[bold cyan]Preparing btrfs "
                        f"RAID{params.raid_config.level.value} across "
                        f"{len(params.raid_config.devices)} devices...[/bold cyan]"
                    )
                    primary_device = params.raid_config.devices[0]
                    extra_devices = params.raid_config.devices[1:]

            parts = apply_disk_layout(
                primary_device,
                params.disk_layout,
                extra_devices=extra_devices or None,
                raid_config=params.raid_config,
                log=log,
            )
            log("[green]Disk prepared successfully.[/green]")
    else:
        raise RuntimeError(
            "No partition_map or disk_layout provided. "
            "Either manually partition and mount, or select a disk layout."
        )

    # Phase 2: System install
    log("[bold cyan]== Phase 2: System Install ==[/bold cyan]")
    identity = install_identity(params)
    check_provider_identity(MOUNT_ROOT, identity)
    if provider.id == "omarchy":
        root_uuid = inspect_omarchy_mounts(params, parts)
    write_provider_identity(MOUNT_ROOT, identity, "installing")
    try:
        if provider.id == "omarchy":
            from arches_installer.core.omarchy import _install_omarchy

            _install_omarchy(
                platform,
                template,
                params.hostname,
                params.username,
                params.password,
                provider=provider,
                inputs=params.inputs,
                root=MOUNT_ROOT,
                root_uuid=root_uuid,
                disk=params.device,
                esp_number=1,
                log=log,
            )
        else:
            install_system(
                platform,
                template,
                params.hostname,
                params.username,
                params.password,
                hardware=params.hardware,
                log=log,
                provider=provider,
                inputs=params.inputs,
            )
        result = _finish_install(params, parts, provider, log)
        write_provider_identity(MOUNT_ROOT, identity, "complete")
    except BaseException:
        write_provider_identity(MOUNT_ROOT, identity, "failed")
        _persist_install_log(log)
        raise
    log("== Installation complete ==")
    return result


def _finish_install(params, parts, provider, log):
    """Omarchy already completed boot, snapshots, and user setup in its adapter."""
    if provider.lifecycle_owner == "omarchy":
        _persist_install_log(log)
        return parts
    platform = params.platform
    template = params.template
    # genfstab runs inside install_system before chroot config; swap
    # partitions need to be appended afterward because genfstab reads
    # /proc/swaps which is empty during install.
    if parts.swap_partitions:
        append_swap_fstab_entries(parts.swap_partitions, log=log)
    log("[green]System installed successfully.[/green]")

    # Phase 3: Bootloader
    log("[bold cyan]== Phase 3: Bootloader ==[/bold cyan]")
    install_bootloader(
        platform,
        params.device,
        parts.esp,
        parts.root,
        parts=parts,
        log=log,
    )
    log("[green]Bootloader installed.[/green]")

    # Phase 3 epilogue: mirror ESP for btrfs RAID 1/10
    if (
        params.raid_config is not None
        and params.raid_config.backend == RaidBackend.BTRFS
        and params.raid_config.level in (RaidLevel.RAID1, RaidLevel.RAID10)
    ):
        log("[bold cyan]-- ESP Mirroring --[/bold cyan]")
        secondary_esps = [_part_name(d, 1) for d in params.raid_config.devices[1:]]
        mirror_esp(parts.esp, secondary_esps, log=log)
    elif (
        params.raid_config is not None
        and params.raid_config.backend == RaidBackend.BTRFS
        and params.raid_config.level == RaidLevel.RAID0
    ):
        log(
            "[yellow]RAID 0: ESP is on primary disk only. "
            "No mirroring (RAID 0 has no redundancy).[/yellow]"
        )

    # Phase 4: Snapshots (if btrfs root)
    if parts.root_filesystem == "btrfs":
        log("[bold cyan]== Phase 4: Snapshots ==[/bold cyan]")
        setup_snapshots(platform, parts=parts, log=log)
        log("[green]Snapshot support configured.[/green]")

    # Phase 5: First-boot service
    log("[bold cyan]== Phase 5: First-Boot ==[/bold cyan]")
    inject_firstboot_service(
        template,
        params.username,
        platform=platform,
        hardware=params.hardware,
        extra_vars=params.ansible_vars,
        log=log,
    )

    # Persist resolved disk-roles state so arches-hardware-rescan can
    # validate disk presence on every subsequent boot. Only meaningful
    # when the multi-disk role path was used; harmless otherwise.
    if params.resolved_disk_roles is not None:
        from arches_installer.core.disk import MOUNT_ROOT

        write_disk_roles_state(params.resolved_disk_roles, MOUNT_ROOT, log=log)

    # Copy install log to the installed system for post-mortem debugging
    _persist_install_log(log)

    log("")

    return parts


def _persist_install_log(log: LogCallback | None = None) -> None:
    """Copy the install log from the live ISO tmpfs into the installed system."""
    from arches_installer.core.disk import MOUNT_ROOT
    from arches_installer.core.run import _log

    src = Path("/var/log/arches-install.log")
    if not src.exists():
        return

    target_log = MOUNT_ROOT / "var" / "log" / "arches-install.log"
    try:
        target_log.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.copy2(src, target_log)
        _log(f"Install log saved to {target_log}.", log)
    except OSError as e:
        _log(f"WARNING: Could not save install log: {e}", log)
