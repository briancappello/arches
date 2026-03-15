"""Bootloader installation and configuration.

Supports multiple bootloader backends via the platform config:
- Limine (x86-64): UEFI + BIOS, snapshot boot entries
- GRUB (aarch64): UEFI-only, standard boot
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from arches_installer.core.disk import MOUNT_ROOT
from arches_installer.core.platform import PlatformConfig

LogCallback = Callable[[str], None]


def _log(msg: str, callback: LogCallback | None = None) -> None:
    if callback:
        callback(msg)


def run(cmd: list[str], log: LogCallback | None = None, **kwargs):
    """Run a command, logging output."""
    _log(f"$ {' '.join(cmd)}", log)
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        _log(f"ERROR: {result.stderr.strip()}", log)
        result.check_returncode()
    return result


def chroot_run(cmd: list[str], log: LogCallback | None = None):
    """Run a command inside the target chroot."""
    return run(["arch-chroot", str(MOUNT_ROOT)] + cmd, log=log)


def detect_firmware() -> str:
    """Detect firmware type — 'uefi' or 'bios'."""
    if Path("/sys/firmware/efi").exists():
        return "uefi"
    return "bios"


def get_root_uuid(root_partition: str) -> str:
    """Get UUID of the root partition."""
    result = subprocess.run(
        ["blkid", "-s", "UUID", "-o", "value", root_partition],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def get_root_partuuid(root_partition: str) -> str:
    """Get PARTUUID of the root partition."""
    result = subprocess.run(
        ["blkid", "-s", "PARTUUID", "-o", "value", root_partition],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


# ─── Limine ───────────────────────────────────────────────


def generate_limine_conf(
    platform: PlatformConfig,
    root_partition: str,
) -> str:
    """Generate limine.conf content."""
    root_uuid = get_root_uuid(root_partition)
    kernel_pkg = platform.kernel.package
    layout = platform.disk_layout

    # Build kernel cmdline
    cmdline_parts = [f"root=UUID={root_uuid}", "rw"]

    if layout.filesystem == "btrfs" and layout.subvolumes:
        cmdline_parts.append("rootflags=subvol=@")

    # Add common kernel parameters
    cmdline_parts.extend(
        [
            "quiet",
            "loglevel=3",
            "systemd.show_status=auto",
            "rd.udev.log_level=3",
        ]
    )

    cmdline = " ".join(cmdline_parts)

    conf = f"""timeout: 5

/Arches Linux
    protocol: linux
    path: boot():/vmlinuz-{kernel_pkg}
    cmdline: {cmdline}
    module_path: boot():/initramfs-{kernel_pkg}.img

/Arches Linux (fallback)
    protocol: linux
    path: boot():/vmlinuz-{kernel_pkg}
    cmdline: {cmdline}
    module_path: boot():/initramfs-{kernel_pkg}-fallback.img
"""

    if platform.bootloader.snapshot_boot:
        conf += """
/+Snapshots
    comment: Auto-populated by limine-snapper-sync
"""

    return conf


def install_limine_uefi(
    platform: PlatformConfig,
    esp_partition: str,
    log: LogCallback | None = None,
) -> None:
    """Install Limine for UEFI boot."""
    _log("Installing Limine (UEFI)...", log)
    boot_dir = MOUNT_ROOT / "boot"
    efi_binary = platform.bootloader.efi_binary

    # Copy Limine EFI binary
    limine_efi_src = MOUNT_ROOT / "usr" / "share" / "limine" / efi_binary
    efi_dest = boot_dir / "EFI" / "BOOT"
    efi_dest.mkdir(parents=True, exist_ok=True)

    # Also install to EFI/limine for explicit entry
    limine_dest = boot_dir / "EFI" / "limine"
    limine_dest.mkdir(parents=True, exist_ok=True)

    run(["cp", str(limine_efi_src), str(efi_dest / efi_binary)], log=log)
    run(["cp", str(limine_efi_src), str(limine_dest / "limine.efi")], log=log)

    # Create NVRAM entry via efibootmgr
    try:
        chroot_run(
            [
                "efibootmgr",
                "--create",
                "--disk",
                esp_partition.rstrip("0123456789p"),
                "--part",
                "1",
                "--label",
                "Arches Linux",
                "--loader",
                "/EFI/limine/limine.efi",
            ],
            log=log,
        )
    except subprocess.CalledProcessError:
        _log("efibootmgr failed — UEFI NVRAM entry not created.", log)
        _log(f"Falling back to {platform.bootloader.efi_fallback_path}.", log)


def install_limine_bios(
    device: str,
    log: LogCallback | None = None,
) -> None:
    """Install Limine for BIOS boot."""
    _log("Installing Limine (BIOS)...", log)
    boot_dir = MOUNT_ROOT / "boot"
    limine_dir = boot_dir / "limine"
    limine_dir.mkdir(parents=True, exist_ok=True)

    # Copy BIOS files
    limine_sys = MOUNT_ROOT / "usr" / "share" / "limine" / "limine-bios.sys"
    run(["cp", str(limine_sys), str(limine_dir / "limine-bios.sys")], log=log)

    # Install BIOS boot sector
    chroot_run(["limine", "bios-install", device], log=log)


def _install_limine(
    platform: PlatformConfig,
    device: str,
    esp_partition: str,
    root_partition: str,
    log: LogCallback | None = None,
) -> None:
    """Full Limine install pipeline."""
    firmware = detect_firmware()
    _log(f"Detected firmware: {firmware}", log)

    # Generate and write limine.conf
    conf = generate_limine_conf(platform, root_partition)
    conf_path = MOUNT_ROOT / "boot" / "limine.conf"
    conf_path.write_text(conf)
    _log("Wrote limine.conf", log)

    if firmware == "uefi":
        install_limine_uefi(platform, esp_partition, log)
    elif firmware == "bios" and platform.bootloader.supports_bios:
        install_limine_bios(device, log)
    else:
        _log(f"BIOS boot not supported on platform {platform.name}.", log)
        raise RuntimeError(
            f"Platform {platform.name} does not support BIOS boot, "
            "but no UEFI firmware was detected."
        )


# ─── GRUB ─────────────────────────────────────────────────


def _install_grub(
    platform: PlatformConfig,
    device: str,
    esp_partition: str,
    root_partition: str,
    log: LogCallback | None = None,
) -> None:
    """Full GRUB install pipeline (UEFI-only)."""
    firmware = detect_firmware()
    _log(f"Detected firmware: {firmware}", log)

    if firmware != "uefi":
        raise RuntimeError(
            f"Platform {platform.name} uses GRUB and requires UEFI firmware, "
            "but no UEFI firmware was detected."
        )

    # Determine GRUB target based on architecture
    if platform.arch == "aarch64":
        grub_target = "arm64-efi"
    else:
        grub_target = "x86_64-efi"

    _log(f"Installing GRUB ({grub_target})...", log)

    # grub-install into the chroot
    chroot_run(
        [
            "grub-install",
            "--target",
            grub_target,
            "--efi-directory",
            "/boot/efi",
            "--bootloader-id",
            "Arches",
            "--removable",
        ],
        log=log,
    )

    # Generate grub.cfg
    _log("Generating GRUB config...", log)
    chroot_run(["grub-mkconfig", "-o", "/boot/grub/grub.cfg"], log=log)

    _log("GRUB installation complete.", log)


# ─── Public API ───────────────────────────────────────────


def install_bootloader(
    platform: PlatformConfig,
    device: str,
    esp_partition: str,
    root_partition: str,
    log: LogCallback | None = None,
) -> None:
    """Install the bootloader specified by the platform config."""
    bootloader_type = platform.bootloader.type

    if bootloader_type == "limine":
        _install_limine(platform, device, esp_partition, root_partition, log)
    elif bootloader_type == "grub":
        _install_grub(platform, device, esp_partition, root_partition, log)
    else:
        raise ValueError(f"Unknown bootloader type: {bootloader_type}")

    _log("Bootloader installation complete.", log)
