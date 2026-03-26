#!/usr/bin/env bash
#
# Convert an Arches ISO to a GPT + FAT32 USB disk image bootable via
# U-Boot's extlinux/syslinux boot protocol (no GRUB required).
#
# Apple Silicon Macs boot via m1n1 → U-Boot. U-Boot's "bootflow scan"
# searches every removable device for /extlinux/extlinux.conf (the
# syslinux/distro-boot standard). This is far more reliable than
# chainloading a GRUB EFI binary, which requires display drivers,
# embedded configs, and correct EFI variable setup.
#
# The resulting image contains:
#   - GPT with a single FAT32 partition (type EF00 / ESP)
#   - /extlinux/extlinux.conf  (U-Boot reads this directly)
#   - /arch/boot/<arch>/vmlinuz-*  + initramfs-*.img
#   - /arch/<arch>/airootfs.sfs
#
# Usage:
#   sudo ./scripts/iso-to-usb-image.sh out/arches-2026.03.26-aarch64.iso
#   sudo dd if=out/arches-2026.03.26-aarch64.usb.img of=/dev/sdX bs=4M status=progress
#
# Then on the Mac:
#   1. Plug the USB-C drive in
#   2. Reboot into the Asahi OS (where U-Boot runs)
#   3. Interrupt U-Boot and run: bootflow scan -b usb
#
set -euo pipefail

# ── Arguments ─────────────────────────────────────────
ISO="${1:-}"
if [[ -z "$ISO" || ! -f "$ISO" ]]; then
    echo "Usage: $0 <path-to-arches-iso>"
    echo ""
    echo "Converts an Arches ISO to a GPT+FAT32 USB disk image for"
    echo "booting on Apple Silicon via U-Boot (extlinux.conf)."
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script requires root (for loop mounts)."
    echo "       Run: sudo $0 $ISO"
    exit 1
fi

# ── Check dependencies ────────────────────────────────
MISSING=()
for cmd in sgdisk losetup mkfs.fat partprobe blkid; do
    command -v "$cmd" &>/dev/null || MISSING+=("$cmd")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "ERROR: Missing required commands: ${MISSING[*]}"
    echo "  Arch:   sudo pacman -S gptfdisk dosfstools util-linux"
    echo "  Fedora: sudo dnf install gdisk dosfstools util-linux"
    exit 1
fi

# ── Derived paths ─────────────────────────────────────
ISO_BASENAME="$(basename "$ISO" .iso)"
OUT_DIR="$(dirname "$ISO")"
USB_IMG="${OUT_DIR}/${ISO_BASENAME}.usb.img"

WORK="$(mktemp -d /tmp/arches-usb-XXXXXX)"
ISO_MNT="${WORK}/iso"
IMG_MNT="${WORK}/img"

LOOP=""
cleanup() {
    echo "Cleaning up..."
    umount "$IMG_MNT" 2>/dev/null || true
    umount "$ISO_MNT" 2>/dev/null || true
    [[ -n "$LOOP" ]] && losetup -d "$LOOP" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

# ── Mount the ISO ─────────────────────────────────────
echo "══ Mounting ISO ══"
mkdir -p "$ISO_MNT" "$IMG_MNT"
mount -o loop,ro "$ISO" "$ISO_MNT"

# ── Volume label ──────────────────────────────────────
# Read iso_label from profiledef.sh — single source of truth for the
# volume label used on both ISO and USB images. The archiso initramfs
# resolves archisolabel= via /dev/disk/by-label/, so the FAT32 label
# on the USB partition must match exactly.
# FAT32 labels are limited to 11 uppercase characters.
PROFILEDEF="$(dirname "$0")/../iso/profiledef.sh"
FAT_LABEL=$(grep '^iso_label=' "$PROFILEDEF" | head -1 | sed 's/^iso_label="\(.*\)"/\1/')
if [[ -z "$FAT_LABEL" ]]; then
    echo "ERROR: Could not read iso_label from $PROFILEDEF"
    exit 1
fi
if [[ ${#FAT_LABEL} -gt 11 ]]; then
    echo "ERROR: iso_label '${FAT_LABEL}' exceeds FAT32 11-char limit"
    echo "       Fix iso_label in iso/profiledef.sh"
    exit 1
fi
FAT_LABEL="${FAT_LABEL^^}"
echo "  Volume label: ${FAT_LABEL}"

# ── Detect archiso layout ────────────────────────────
# archiso uses /arch as the default install_dir
INSTALL_DIR=""
for candidate in arch; do
    if [[ -d "${ISO_MNT}/${candidate}/boot" ]]; then
        INSTALL_DIR="$candidate"
        break
    fi
done

if [[ -z "$INSTALL_DIR" ]]; then
    echo "ERROR: Could not find archiso install directory in ISO"
    ls -la "$ISO_MNT"/
    exit 1
fi

# Detect architecture from boot directory
ARCH=""
for candidate in aarch64 x86_64; do
    if [[ -d "${ISO_MNT}/${INSTALL_DIR}/boot/${candidate}" ]]; then
        ARCH="$candidate"
        break
    fi
done

if [[ -z "$ARCH" ]]; then
    echo "ERROR: Could not detect architecture from ISO"
    ls -la "${ISO_MNT}/${INSTALL_DIR}/boot/"
    exit 1
fi

echo "  Install dir: ${INSTALL_DIR}"
echo "  Architecture: ${ARCH}"

# Locate the key files
KERNEL=$(ls "${ISO_MNT}/${INSTALL_DIR}/boot/${ARCH}"/vmlinuz-* 2>/dev/null | head -1)
INITRD=$(ls "${ISO_MNT}/${INSTALL_DIR}/boot/${ARCH}"/initramfs-*.img 2>/dev/null | head -1)
SQUASHFS=$(ls "${ISO_MNT}/${INSTALL_DIR}/${ARCH}"/airootfs.sfs 2>/dev/null || \
           ls "${ISO_MNT}/${INSTALL_DIR}/${ARCH}"/airootfs.erofs 2>/dev/null || true)

for f in "$KERNEL" "$INITRD" "$SQUASHFS"; do
    if [[ ! -f "$f" ]]; then
        echo "ERROR: Required file not found: $f"
        echo ""
        echo "ISO contents:"
        find "$ISO_MNT" -maxdepth 3 -type f | head -30
        exit 1
    fi
done

KERNEL_BASENAME="$(basename "$KERNEL")"
INITRD_BASENAME="$(basename "$INITRD")"

echo "  Kernel:    ${KERNEL_BASENAME}"
echo "  Initrd:    ${INITRD_BASENAME}"
echo "  Rootfs:    $(basename "$SQUASHFS")"

# ── Calculate image size ──────────────────────────────
# Sum all file sizes + 256 MiB overhead for FAT32 metadata,
# GPT tables, extlinux.conf, and alignment padding.
TOTAL_BYTES=0
for f in "$KERNEL" "$INITRD" "$SQUASHFS"; do
    SIZE=$(stat -c%s "$f")
    TOTAL_BYTES=$((TOTAL_BYTES + SIZE))
done

# Check for signature file
if [[ -f "${SQUASHFS}.sig" ]]; then
    SIG_SIZE=$(stat -c%s "${SQUASHFS}.sig")
    TOTAL_BYTES=$((TOTAL_BYTES + SIG_SIZE))
fi

OVERHEAD_MIB=256
IMG_BYTES=$((TOTAL_BYTES + OVERHEAD_MIB * 1024 * 1024))

# Round up to nearest MiB
IMG_MIB=$(( (IMG_BYTES + 1048575) / 1048576 ))

echo ""
echo "══ Creating USB image (${IMG_MIB} MiB) ══"

# ── Create the disk image ─────────────────────────────
dd if=/dev/zero of="$USB_IMG" bs=1M count="$IMG_MIB" status=none

# GPT partition table with a single EFI System Partition.
# U-Boot's bootflow scan recognises ESP partitions on USB and
# searches them for /extlinux/extlinux.conf.
sgdisk --clear \
    --new=1:2048:0 --typecode=1:EF00 --change-name=1:"${FAT_LABEL}" \
    "$USB_IMG"

# ── Set up loop device and format ─────────────────────
LOOP=$(losetup --find --show --partscan "$USB_IMG")
PART="${LOOP}p1"

# Wait for partition device to appear
for i in $(seq 1 10); do
    [[ -b "$PART" ]] && break
    sleep 0.5
    partprobe "$LOOP" 2>/dev/null || true
done

if [[ ! -b "$PART" ]]; then
    echo "ERROR: Partition device $PART did not appear"
    exit 1
fi

mkfs.fat -F 32 -n "$FAT_LABEL" "$PART"

# ── Mount and populate ────────────────────────────────
echo "══ Populating USB image ══"
mount "$PART" "$IMG_MNT"

# Recreate the archiso directory structure so the archiso initramfs hooks
# can find the squashfs rootfs using the same paths as on the ISO.
mkdir -p "${IMG_MNT}/${INSTALL_DIR}/boot/${ARCH}"
mkdir -p "${IMG_MNT}/${INSTALL_DIR}/${ARCH}"

# Copy kernel + initramfs
cp -v "$KERNEL" "${IMG_MNT}/${INSTALL_DIR}/boot/${ARCH}/"
cp -v "$INITRD" "${IMG_MNT}/${INSTALL_DIR}/boot/${ARCH}/"

# Copy squashfs rootfs (this is the big one)
echo "  Copying rootfs ($(du -h "$SQUASHFS" | cut -f1))..."
cp "$SQUASHFS" "${IMG_MNT}/${INSTALL_DIR}/${ARCH}/"

# Copy squashfs signature if present
if [[ -f "${SQUASHFS}.sig" ]]; then
    cp "${SQUASHFS}.sig" "${IMG_MNT}/${INSTALL_DIR}/${ARCH}/"
fi

# Copy the archiso version/build info
for f in "${ISO_MNT}/${INSTALL_DIR}"/version "${ISO_MNT}/${INSTALL_DIR}"/pkglist.*.txt; do
    [[ -f "$f" ]] && cp "$f" "${IMG_MNT}/${INSTALL_DIR}/"
done

# ── Generate extlinux.conf ────────────────────────────
# U-Boot's bootflow scan looks for /extlinux/extlinux.conf on each
# bootable partition. This replaces GRUB entirely — no EFI binary,
# no display drivers, no embedded config fragility.
#
# Boot device discovery: we use archisolabel= (not archisosearchuuid=).
# The archiso initramfs hook resolves archisolabel to
# /dev/disk/by-label/<label>, then _mnt_dev() waits up to 30 seconds
# for the device to appear. This is critical on Apple Silicon where
# the USB controller takes ~15s to enumerate after kernel start.
#
# archisosearchuuid= is NOT used because:
#   1. Its UUID lookup checks /dev/disk/by-uuid/ which won't match
#      a FAT32 volume label
#   2. Its file-search fallback iterates devices at that instant with
#      no wait, so it misses the USB drive if it hasn't enumerated yet
echo "══ Generating extlinux.conf ══"
mkdir -p "${IMG_MNT}/extlinux"

cat > "${IMG_MNT}/extlinux/extlinux.conf" <<EOF
default arches
menu title Arches Install/Recovery
timeout 50

label arches
    menu label Arches Install/Recovery (${ARCH})
    kernel /${INSTALL_DIR}/boot/${ARCH}/${KERNEL_BASENAME}
    initrd /${INSTALL_DIR}/boot/${ARCH}/${INITRD_BASENAME}
    append archisobasedir=${INSTALL_DIR} archisolabel=${FAT_LABEL} rootdelay=30 console=ttyAMA0,115200 console=tty0
EOF

echo "  Created extlinux/extlinux.conf"
cat "${IMG_MNT}/extlinux/extlinux.conf"

# ── Finalize ──────────────────────────────────────────
sync
umount "$IMG_MNT"
losetup -d "$LOOP"
LOOP=""  # prevent cleanup trap from double-detaching

echo ""
echo "══ USB image built ══"
echo "  ${USB_IMG} ($(du -h "$USB_IMG" | cut -f1))"
echo ""
echo "Boot instructions:"
echo "  1. Write to USB:  sudo dd if=${USB_IMG} of=/dev/sdX bs=4M status=progress"
echo "  2. Plug into Mac, reboot into Asahi U-Boot"
echo "  3. Interrupt U-Boot and run: bootflow scan -b usb"
