# aarch64-apple Platform

Apple Silicon (M1/M2/M3/M4) platform using the Asahi Linux kernel and mesa stack.

## Configuration

| Setting                | Value                                        |
|------------------------|----------------------------------------------|
| **Kernel**             | `linux-asahi`                                |
| **Bootloader**         | m1n1 → U-Boot → GRUB                         |
| **Filesystem**         | btrfs with subvolumes (`@`, `@home`, `@var`) |
| **ESP**                | 512 MiB at `/boot/efi`                       |
| **Snapshot boot**      | No (Asahi boot chain limitation)             |
| **Hardware detection** | Disabled (Asahi mesa handles GPU)            |
| **Auto-install**       | Disabled (disk managed by Asahi installer)   |
| **Default template**   | `dev-workstation`                            |

## Install Paths

### 1. USB Boot

Write the Arches USB image to a USB-C drive, interrupt U-Boot at startup, and run `bootflow scan -b usb`. U-Boot finds `/extlinux/extlinux.conf` on the USB and boots the kernel directly.

**Known USB limitations (from Asahi U-Boot):**
- USB-A ports do not work (controller requires non-redistributable firmware)
- The two USB-C ports furthest from power on iMacs don't work
- Multi-function USB devices (hub+NIC combos) may not work
- USB hubs with empty SD card slots can cause a hard reset

### 2. Host Install (Recommended)

Install Arches into btrfs subvolumes alongside an existing Asahi Linux (e.g., Fedora) without touching the partition table:

```bash
sudo make host-install CONFIG=templates/host-install.toml
```

This runs inside a Podman container on the host. See `templates/host-install.toml` for configuration.

### 3. Fresh Install

Use the Asahi installer from macOS to create partitions and the boot chain (m1n1 + U-Boot), then install Arches into the prepared partition using the USB installer or host-install.

## Apple-Specific Features

- **Firmware copying**: The installer copies Apple Silicon firmware (`/lib/firmware/vendor/`) from the host into the target system for WiFi, Bluetooth, and GPU support.
- **Keyboard remapping**: `hid_apple` module options are configured to swap Fn↔Ctrl and Option↔Command for a standard PC keyboard layout.
- **vmlinuz symlink**: A pacman hook maintains a `/boot/vmlinuz-linux-asahi` symlink to `/boot/Image` (Arch Linux ARM convention) so GRUB can find the kernel.
