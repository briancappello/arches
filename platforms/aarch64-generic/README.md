# aarch64-generic Platform

Generic ARM64 platform for Arch Linux ARM on UEFI-capable hardware (Raspberry Pi 4/5 with UEFI firmware, server boards, ARM VMs, etc.).

## Configuration

| Setting                | Value                                        |
|------------------------|----------------------------------------------|
| **Kernel**             | `linux-aarch64`                              |
| **Bootloader**         | GRUB (UEFI-only)                             |
| **Filesystem**         | btrfs with subvolumes (`@`, `@home`, `@var`) |
| **ESP**                | 512 MiB at `/boot/efi`                       |
| **Snapshot boot**      | Yes, via `grub-btrfs`                        |
| **Hardware detection** | Disabled                                     |
| **Default template**   | `dev-workstation`                            |

## Disk Layout

GRUB reads kernels from btrfs natively, so `/boot` lives on the `@` subvolume — no separate `/boot` partition is needed. The ESP mounts at `/boot/efi` and only holds the GRUB EFI binary.

## QEMU Testing

```bash
make qemu-install   # Build ISO + boot in QEMU (aarch64)
make qemu-boot      # Boot installed disk
```

Requires `qemu-system-aarch64` and EDK2 UEFI firmware (`edk2-aarch64` on Fedora, `edk2-ovmf` on Arch).
