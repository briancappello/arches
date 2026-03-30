"""Bootloader installation and configuration.

Supports multiple bootloader backends via the platform config:
- Limine (x86-64): UEFI + BIOS, snapshot boot entries via limine-snapper-sync
- GRUB (aarch64): UEFI-only, snapshot boot entries via grub-btrfs
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from arches_installer.core.disk import MOUNT_ROOT
from arches_installer.core.platform import PlatformConfig
from arches_installer.core.run import LogCallback, _log, chroot_run, run


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


def write_limine_defaults(
    platform: PlatformConfig,
    root_partition: str,
    log: LogCallback | None = None,
) -> None:
    """Write /etc/default/limine for limine-entry-tool to use."""
    root_uuid = get_root_uuid(root_partition)
    layout = platform.disk_layout

    # Build kernel cmdline
    cmdline_parts = [f"root=UUID={root_uuid}", "rw"]

    if layout.filesystem == "btrfs" and layout.subvolumes:
        cmdline_parts.append("rootflags=subvol=/@")

    # Framebuffer console — match the ISO live boot behavior
    cmdline_parts.append("video=1920x1080")

    # Add common kernel parameters
    cmdline_parts.extend(
        [
            "systemd.show_status=auto",
        ]
    )

    cmdline = " ".join(cmdline_parts)

    default_conf = 'ESP_PATH="/boot"\n'
    default_conf += f'KERNEL_CMDLINE[default]+="{cmdline}"\n'
    default_conf += 'BOOT_ORDER="*, *lts, *fallback'
    if platform.bootloader.snapshot_boot:
        default_conf += ", Snapshots"
    default_conf += '"\n'

    default_dir = MOUNT_ROOT / "etc" / "default"
    default_dir.mkdir(parents=True, exist_ok=True)
    (default_dir / "limine").write_text(default_conf)
    _log("Wrote /etc/default/limine", log)

    # Also write /etc/kernel/cmdline — the standard location that
    # mkinitcpio hooks (including limine-mkinitcpio-hook) look for.
    kernel_dir = MOUNT_ROOT / "etc" / "kernel"
    kernel_dir.mkdir(parents=True, exist_ok=True)
    (kernel_dir / "cmdline").write_text(cmdline + "\n")
    _log("Wrote /etc/kernel/cmdline", log)


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

    # Write /etc/default/limine config
    write_limine_defaults(platform, root_partition, log)

    # Install limine-mkinitcpio-hook BEFORE deploying the bootloader.
    # This package provides limine-install, limine-mkinitcpio, and
    # limine-update. It must be installed AFTER /etc/default/limine
    # exists, but we need its tools for the next steps.
    # Use -Sy to sync the arches-local repo first.
    _log("Installing limine-mkinitcpio-hook...", log)
    chroot_run(
        ["pacman", "-Sy", "--noconfirm", "--needed", "limine-mkinitcpio-hook"],
        log=log,
    )

    if firmware == "uefi":
        # Use limine-install to deploy the EFI binary and register
        # the NVRAM entry. This replaces our manual cp + efibootmgr.
        _log("Running limine-install...", log)
        chroot_run(["limine-install"], log=log)
    elif firmware == "bios" and platform.bootloader.supports_bios:
        install_limine_bios(device, log)
    else:
        _log(f"BIOS boot not supported on platform {platform.name}.", log)
        raise RuntimeError(
            f"Platform {platform.name} does not support BIOS boot, "
            "but no UEFI firmware was detected."
        )

    # Generate initramfs AND limine.conf entries together.
    # limine-mkinitcpio wraps mkinitcpio and limine-entry-tool to
    # create the initramfs and corresponding boot entries in one step.
    _log("Running limine-mkinitcpio to generate initramfs and boot entries...", log)
    chroot_run(["limine-mkinitcpio"], log=log)

    # Add Memtest86+ entry to limine.conf and install a pacman hook
    # so it persists across kernel upgrades (limine-mkinitcpio regenerates
    # limine.conf from scratch on each kernel update).
    _setup_memtest_limine(platform, log)


MEMTEST_APPEND_SCRIPT = """\
#!/usr/bin/env bash
# Append Memtest86+ entry to limine.conf if not already present.
# Called by the 95-memtest-limine pacman hook after kernel updates.
set -euo pipefail

LIMINE_CONF="$(bootctl --print-esp-path 2>/dev/null || echo /boot)/limine.conf"
MEMTEST_EFI="/boot/memtest86+/memtest.efi"

[[ -f "$MEMTEST_EFI" && -f "$LIMINE_CONF" ]] || exit 0

if ! grep -q 'Memtest86+' "$LIMINE_CONF"; then
    printf '\\n/Memtest86+\\n    protocol: efi\\n    path: boot():/memtest86+/memtest.efi\\n' >> "$LIMINE_CONF"
fi
"""


def _memtest_pacman_hook(kernel_packages: list[str]) -> str:
    """Generate the Memtest86+ pacman hook with targets for all kernel variants.

    limine-mkinitcpio regenerates limine.conf from scratch on each kernel
    update, which wipes the Memtest entry.  This hook re-appends it after
    any kernel variant is installed or upgraded.
    """
    kernel_targets = "\n".join(f"Target = {pkg}" for pkg in kernel_packages)
    return f"""\
[Trigger]
Type = Package
Operation = Install
Operation = Upgrade
Target = memtest86+-efi
Target = limine-mkinitcpio-hook
{kernel_targets}

[Trigger]
Type = Path
Operation = Install
Operation = Upgrade
Target = usr/lib/modules/*/vmlinuz

[Action]
Description = Adding Memtest86+ entry to limine.conf...
When = PostTransaction
Exec = /usr/local/bin/arches-memtest-limine
Depends = bash
Depends = grep
"""


def _setup_memtest_limine(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Install a script and pacman hook to keep Memtest86+ in limine.conf."""
    memtest_efi = MOUNT_ROOT / "boot" / "memtest86+" / "memtest.efi"
    if not memtest_efi.exists():
        _log("Memtest86+ EFI not found, skipping limine entry.", log)
        return

    # Install the append script
    script_path = MOUNT_ROOT / "usr" / "local" / "bin" / "arches-memtest-limine"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(MEMTEST_APPEND_SCRIPT)
    script_path.chmod(0o755)

    # Install the pacman hook — trigger on all kernel variants
    kernel_packages = [v.package for v in platform.kernel.variants]
    hook_dir = MOUNT_ROOT / "etc" / "pacman.d" / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "95-memtest-limine.hook").write_text(
        _memtest_pacman_hook(kernel_packages)
    )

    # Run it now to add the entry to the current limine.conf
    chroot_run(["/usr/local/bin/arches-memtest-limine"], log=log)
    _log("Installed Memtest86+ limine entry and pacman hook.", log)


# ─── GRUB ─────────────────────────────────────────────────


def write_grub_defaults(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Write /etc/default/grub with kernel cmdline parameters."""
    cmdline_parts = []

    # aarch64 kernels may default to serial console only; ensure
    # framebuffer console is enabled so output is visible on screen.
    if platform.arch == "aarch64":
        cmdline_parts.append("console=tty0")

    # Framebuffer console — match the ISO live boot behavior
    cmdline_parts.append("video=1920x1080")
    cmdline_parts.append("systemd.show_status=auto")

    cmdline = " ".join(cmdline_parts)

    grub_default = MOUNT_ROOT / "etc" / "default" / "grub"
    if grub_default.exists():
        import re

        content = grub_default.read_text()
        # Update GRUB_CMDLINE_LINUX_DEFAULT
        content = re.sub(
            r"^GRUB_CMDLINE_LINUX_DEFAULT=.*$",
            f'GRUB_CMDLINE_LINUX_DEFAULT="{cmdline}"',
            content,
            flags=re.MULTILINE,
        )
        grub_default.write_text(content)
    else:
        grub_default.parent.mkdir(parents=True, exist_ok=True)
        grub_default.write_text(
            f'GRUB_CMDLINE_LINUX_DEFAULT="{cmdline}"\n'
            f"GRUB_TIMEOUT=5\n"
            f'GRUB_GFXMODE="auto"\n'
            f'GRUB_GFXPAYLOAD_LINUX="keep"\n'
        )
    _log("Wrote /etc/default/grub", log)


def _install_alarm_vmlinuz_hook(
    platform: PlatformConfig, log: LogCallback | None = None
) -> None:
    """Install a pacman hook that maintains the vmlinuz-* symlink.

    Arch Linux ARM kernels install /boot/Image instead of /boot/vmlinuz-*.
    GRUB's grub-mkconfig only searches for vmlinuz-*, so we need a persistent
    symlink. This hook recreates it after every kernel upgrade.
    """
    if len(platform.kernel.variants) > 1:
        raise NotImplementedError(
            "aarch64 vmlinuz symlink hook only supports a single kernel variant. "
            "With multiple variants, each needs its own hook creating a "
            "vmlinuz-<package> symlink and a corresponding pacman trigger. "
            "See _install_alarm_vmlinuz_hook in bootloader.py."
        )

    kernel_pkg = platform.kernel.package
    hooks_dir = MOUNT_ROOT / "etc" / "pacman.d" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    hook_content = f"""[Trigger]
Operation = Install
Operation = Upgrade
Type = Package
Target = {kernel_pkg}

[Action]
Description = Creating vmlinuz symlink for {kernel_pkg}...
When = PostTransaction
Depends = coreutils
Exec = /bin/ln -sf Image /boot/vmlinuz-{kernel_pkg}
"""
    (hooks_dir / "90-vmlinuz-symlink.hook").write_text(hook_content)
    _log("Installed pacman hook for vmlinuz symlink.", log)


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

    # Determine EFI directory: with btrfs, ESP is at /boot/efi (GRUB reads
    # kernels from btrfs natively). With ext4 + separate /boot, ESP is also
    # at /boot/efi. In both cases the EFI directory is /boot/efi.
    efi_directory = "/boot/efi"

    # Arch Linux ARM kernels install /boot/Image instead of /boot/vmlinuz-*.
    # GRUB's grub-mkconfig only searches for vmlinuz-*/vmlinux-*, so create
    # a symlink. This also needs a pacman hook to maintain it across upgrades.
    if platform.arch == "aarch64":
        kernel_pkg = platform.kernel.package
        vmlinuz = MOUNT_ROOT / "boot" / f"vmlinuz-{kernel_pkg}"
        if not vmlinuz.exists() and (MOUNT_ROOT / "boot" / "Image").exists():
            _log(f"Creating vmlinuz symlink for {kernel_pkg}...", log)
            vmlinuz.symlink_to("Image")
        _install_alarm_vmlinuz_hook(platform, log)

    # On aarch64, remove efi_uga from grub's video setup script.
    # efi_uga is an x86-only module that doesn't exist on arm64-efi;
    # its absence causes a "not found" error and a "Press any key" pause.
    if platform.arch == "aarch64":
        header = MOUNT_ROOT / "etc" / "grub.d" / "00_header"
        if header.exists():
            content = header.read_text()
            content = content.replace("    insmod efi_uga\n", "")
            header.write_text(content)
            _log("Removed efi_uga from GRUB 00_header (not available on arm64).", log)

    # Write /etc/default/grub
    write_grub_defaults(platform, log)

    _log(f"Installing GRUB ({grub_target})...", log)

    # grub-install into the chroot
    chroot_run(
        [
            "grub-install",
            "--target",
            grub_target,
            "--efi-directory",
            efi_directory,
            "--bootloader-id",
            "Arches",
            "--removable",
        ],
        log=log,
    )

    # Generate grub.cfg — grub-mkconfig auto-detects btrfs subvolumes
    # and adds the correct rootflags=subvol=/@ to the boot entry.
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
