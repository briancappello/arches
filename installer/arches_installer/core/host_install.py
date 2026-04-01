"""Host-install runner for Apple Silicon (and other pre-installed systems).

Installs Arches into btrfs subvolumes on an existing Linux system, either
alongside the current OS or replacing it. Designed to run inside a Podman
container that provides the Arch Linux ARM toolchain (pacstrap, keyrings).

Unlike the ISO-based auto.py flow, host-install:
  - Never wipes the disk or touches the partition table
  - Creates btrfs subvolumes on the existing partition
  - Optionally generates a GRUB boot entry on the host for dual-boot
  - Copies Apple Silicon firmware from the host into the target
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arches_installer.core.bootloader import install_bootloader
from arches_installer.core.disk import (
    MOUNT_ROOT,
    cleanup_mounts,
    prepare_subvolume,
)
from arches_installer.core.firstboot import inject_firstboot_service
from arches_installer.core.install import install_system
from arches_installer.core.platform import PlatformConfig
from arches_installer.core.snapper import setup_snapshots
from arches_installer.core.template import (
    InstallTemplate,
    load_template,
    resolve_template,
)


@dataclass
class HostInstallConfig:
    """Configuration for a host-install (non-ISO, non-destructive)."""

    template: InstallTemplate
    hostname: str
    username: str
    password: str
    # Target disk partition (btrfs) — e.g. /dev/nvme0n1p6
    partition: str
    # Existing ESP partition — e.g. /dev/nvme0n1p4
    esp_partition: str
    # "alongside" or "replace"
    mode: str
    # Subvolume prefix for alongside mode (default: @arches)
    subvol_prefix: str
    # Whether to add a GRUB entry on the host for dual-boot
    add_grub_entry: bool
    # Whether to install GRUB inside the new system's chroot.
    # In alongside mode this is typically False (host GRUB is used).
    # In replace mode this is typically True.
    install_bootloader: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HostInstallConfig:
        """Build config from a parsed TOML dict."""
        install = data.get("install", {})

        template_name = install.get("template")
        if not template_name:
            raise ValueError("install.template is required")
        template = load_template(resolve_template(template_name))

        hostname = install.get("hostname", "arches")
        username = install.get("username")
        if not username:
            raise ValueError("install.username is required")
        password = install.get("password")
        if not password:
            raise ValueError("install.password is required")

        partition = install.get("partition")
        if not partition:
            raise ValueError("install.partition is required (e.g. /dev/nvme0n1p6)")
        esp_partition = install.get("esp_partition")
        if not esp_partition:
            raise ValueError("install.esp_partition is required (e.g. /dev/nvme0n1p4)")

        mode = install.get("mode", "alongside")
        if mode not in ("alongside", "replace"):
            raise ValueError(
                f"install.mode must be 'alongside' or 'replace', got '{mode}'"
            )

        return cls(
            template=template,
            hostname=hostname,
            username=username,
            password=password,
            partition=partition,
            esp_partition=esp_partition,
            mode=mode,
            subvol_prefix=install.get("subvol_prefix", "@arches"),
            add_grub_entry=install.get("add_grub_entry", mode == "alongside"),
            install_bootloader=install.get("install_bootloader", mode == "replace"),
        )

    @classmethod
    def from_file(cls, path: Path) -> HostInstallConfig:
        """Load config from a TOML file."""
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls.from_dict(data)


# ─── GRUB dual-boot entry ────────────────────────────────


GRUB_ENTRY_TEMPLATE = """\
menuentry "Arches Linux" {{
    search --no-floppy --fs-uuid --set=root {btrfs_uuid}
    linux /{subvol}/boot/vmlinuz-{kernel} root=UUID={btrfs_uuid} rootflags=subvol={subvol} {cmdline}
    initrd /{subvol}/boot/initramfs-{kernel}.img
}}
"""


def generate_grub_entry(
    platform: PlatformConfig,
    partition: str,
    subvol_prefix: str,
) -> str:
    """Generate a GRUB menuentry snippet for booting the Arches subvolume."""
    from arches_installer.core.bootloader import get_root_uuid

    btrfs_uuid = get_root_uuid(partition)
    kernel = platform.kernel.package

    # Build kernel cmdline
    cmdline_parts = ["rw"]
    # Platform-specific kernel flags (console, loglevel, video, etc.)
    cmdline_parts.extend(platform.kernel_flags)
    cmdline_parts.append("systemd.show_status=auto")
    cmdline = " ".join(cmdline_parts)

    return GRUB_ENTRY_TEMPLATE.format(
        btrfs_uuid=btrfs_uuid,
        subvol=subvol_prefix,
        kernel=kernel,
        cmdline=cmdline,
    )


def write_host_grub_entry(
    platform: PlatformConfig,
    partition: str,
    subvol_prefix: str,
    log=None,
) -> Path | None:
    """Write a GRUB entry file for the host's bootloader.

    Supports two host GRUB configurations:
    1. Fedora-style: /etc/grub.d/40_custom + grub2-mkconfig
    2. Direct: /boot/grub/custom.cfg (if /etc/grub.d doesn't exist)

    Returns the path to the written file, or None if writing failed.
    """
    from arches_installer.core.run import _log

    entry = generate_grub_entry(platform, partition, subvol_prefix)

    # Try Fedora-style first (/etc/grub.d/40_custom)
    grub_d = Path("/etc/grub.d")
    custom_script = grub_d / "41_arches"

    if grub_d.exists():
        script_content = "#!/bin/bash\n"
        script_content += "# Added by arches host-install\n"
        script_content += f"cat <<'ARCHES_EOF'\n{entry}ARCHES_EOF\n"

        custom_script.write_text(script_content)
        custom_script.chmod(0o755)
        _log(f"Wrote GRUB entry to {custom_script}", log)

        # Regenerate grub.cfg
        grub_cfg_paths = [
            Path("/boot/grub2/grub.cfg"),  # Fedora
            Path("/boot/grub/grub.cfg"),  # Arch/generic
        ]
        for grub_cfg in grub_cfg_paths:
            if grub_cfg.parent.exists():
                try:
                    subprocess.run(
                        ["grub2-mkconfig", "-o", str(grub_cfg)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    _log(f"Regenerated {grub_cfg}", log)
                    return custom_script
                except (subprocess.CalledProcessError, FileNotFoundError):
                    try:
                        subprocess.run(
                            ["grub-mkconfig", "-o", str(grub_cfg)],
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                        _log(f"Regenerated {grub_cfg}", log)
                        return custom_script
                    except (subprocess.CalledProcessError, FileNotFoundError):
                        continue

        _log(
            "WARNING: Could not regenerate grub.cfg. Run grub2-mkconfig manually.", log
        )
        return custom_script

    # Fallback: write directly to custom.cfg (included by most GRUB configs)
    for custom_cfg in [Path("/boot/grub2/custom.cfg"), Path("/boot/grub/custom.cfg")]:
        if custom_cfg.parent.exists():
            # Append, don't overwrite
            with open(custom_cfg, "a") as f:
                f.write(f"\n# Added by arches host-install\n{entry}")
            _log(f"Appended GRUB entry to {custom_cfg}", log)
            return custom_cfg

    _log("WARNING: Could not find a GRUB config location to write the boot entry.", log)
    return None


# ─── Main install runner ──────────────────────────────────


def log_stdout(msg: str) -> None:
    """Log to stdout, stripping Rich markup for plain text output."""
    clean = msg
    for tag in (
        "[bold cyan]",
        "[/bold cyan]",
        "[bold green]",
        "[/bold green]",
        "[bold red]",
        "[/bold red]",
        "[green]",
        "[/green]",
        "[red]",
        "[/red]",
    ):
        clean = clean.replace(tag, "")
    print(clean, flush=True)


def run_host_install(platform: PlatformConfig, config: HostInstallConfig) -> int:
    """Run the host-install pipeline. Returns exit code."""
    log = log_stdout

    log("== Arches Host Install ==")
    log(f"  Platform:    {platform.name} ({platform.arch})")
    log(f"  Template:    {config.template.name}")
    log(f"  Hostname:    {config.hostname}")
    log(f"  User:        {config.username}")
    log(f"  Partition:   {config.partition}")
    log(f"  ESP:         {config.esp_partition}")
    log(f"  Mode:        {config.mode}")
    if config.mode == "alongside":
        log(
            f"  Subvolumes:  {config.subvol_prefix}, {config.subvol_prefix}-home, {config.subvol_prefix}-var"
        )
    log(f"  GRUB entry:  {config.add_grub_entry}")
    log(
        f"  Bootloader:  {'install in chroot' if config.install_bootloader else 'skip (host GRUB)'}"
    )
    log("")

    try:
        # Phase 1: Prepare subvolumes
        log("-- Phase 1: Subvolume Setup --")
        cleanup_mounts()  # Clean up any leftover mounts from a previous attempt
        parts = prepare_subvolume(
            partition=config.partition,
            esp_partition=config.esp_partition,
            platform=platform,
            mode=config.mode,
            subvol_prefix=config.subvol_prefix,
        )
        log(f"Subvolumes prepared ({config.mode} mode).")

        # Phase 2: System install
        log("-- Phase 2: System Install --")
        install_system(
            platform,
            config.template,
            config.hostname,
            config.username,
            config.password,
            log=log,
        )
        log("System installed successfully.")

        # Phase 3: Bootloader
        log("-- Phase 3: Bootloader --")
        if config.install_bootloader:
            install_bootloader(
                platform,
                # device is the whole disk — derive from partition
                _device_from_partition(config.partition),
                parts.esp,
                parts.root,
                log=log,
            )
            log("Bootloader installed in chroot.")
        else:
            # In alongside mode, we still need mkinitcpio for the initramfs
            log("Skipping bootloader install (host GRUB will be used).")
            log("Running mkinitcpio...")
            from arches_installer.core.run import chroot_run

            # Create vmlinuz symlink for GRUB (Arch ARM uses /boot/Image)
            if platform.arch == "aarch64":
                kernel_pkg = platform.kernel.package
                vmlinuz = MOUNT_ROOT / "boot" / f"vmlinuz-{kernel_pkg}"
                if not vmlinuz.exists() and (MOUNT_ROOT / "boot" / "Image").exists():
                    vmlinuz.symlink_to("Image")
                    log(f"Created vmlinuz symlink for {kernel_pkg}.")
                # Install the pacman hook for persistence
                from arches_installer.core.bootloader import _install_alarm_vmlinuz_hook

                _install_alarm_vmlinuz_hook(platform, log)

            chroot_run(["mkinitcpio", "-P"], log=log)
            log("Initramfs generated.")

        # Phase 4: Snapshots (if btrfs platform with snapshot support)
        if (
            platform.disk_layout.filesystem == "btrfs"
            and platform.bootloader.snapshot_boot
        ):
            log("-- Phase 4: Snapshots --")
            setup_snapshots(platform, log=log)
            log("Snapshot support configured.")

        # Phase 5: First-boot service
        log("-- Phase 5: First-Boot --")
        inject_firstboot_service(
            config.template,
            config.username,
            log=log,
        )

        # Phase 6: Host GRUB entry (alongside mode)
        # The GRUB entry must be written on the HOST filesystem, not inside
        # the container. We generate the snippet and save it to the target
        # so host-install.sh can apply it after the container exits.
        if config.add_grub_entry:
            log("-- Phase 6: Host GRUB Entry --")
            entry = generate_grub_entry(
                platform,
                config.partition,
                config.subvol_prefix,
            )
            grub_snippet = MOUNT_ROOT / "opt" / "arches" / "grub-entry.cfg"
            grub_snippet.parent.mkdir(parents=True, exist_ok=True)
            grub_snippet.write_text(entry)
            log("GRUB entry snippet saved to target at /opt/arches/grub-entry.cfg")
            log("host-install.sh will apply it to the host GRUB after container exits.")

        log("")
        log("== Host install complete ==")
        if config.mode == "alongside":
            log("")
            log("Your existing system is untouched. Reboot and select")
            log("'Arches Linux' from the GRUB menu to boot into Arches.")
            log("Select your current OS to return to it.")

        return 0

    except Exception as e:
        log(f"\nINSTALL FAILED: {e}")
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        # Always try to clean up mounts
        try:
            cleanup_mounts()
        except Exception as cleanup_err:
            log(f"WARNING: Cleanup failed: {cleanup_err}")


def _device_from_partition(partition: str) -> str:
    """Derive the whole-disk device from a partition path.

    /dev/nvme0n1p6 → /dev/nvme0n1
    /dev/sda2      → /dev/sda
    """
    # NVMe/MMC: strip trailing pN
    m = re.match(r"(.+?)p\d+$", partition)
    if m and ("nvme" in partition or "mmcblk" in partition):
        return m.group(1)
    # SCSI/SATA: strip trailing digits
    m = re.match(r"(.+?)\d+$", partition)
    if m:
        return m.group(1)
    return partition
