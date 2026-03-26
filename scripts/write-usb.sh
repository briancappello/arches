#!/usr/bin/env bash
#
# Write an Arches USB image to a USB drive.
#
# Interactively prompts the user to select a USB device, confirms
# the write, and prints U-Boot instructions on completion.
#
# Usage:
#   sudo ./scripts/write-usb.sh out/arches-2026.03.26-aarch64.usb.img
#
# Requires: python3, lsblk, dd, udevadm
#
set -euo pipefail

# ── Arguments ─────────────────────────────────────────
IMG="${1:-}"
if [[ -z "$IMG" || ! -f "$IMG" ]]; then
    echo "Usage: $0 <path-to-usb-image>"
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script requires root."
    echo "       Run: sudo $0 $IMG"
    exit 1
fi

# ── Check dependencies ────────────────────────────────
for cmd in python3 lsblk dd udevadm; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: Missing required command: $cmd"
        exit 1
    fi
done

IMG_SIZE=$(du -h "$IMG" | cut -f1)

# ── Helper: query USB block devices ───────────────────
# Returns one line per USB disk: "name<TAB>size<TAB>model"
# Using lsblk --json + python avoids all column-width / whitespace
# parsing issues with multi-word model names like "SanDisk 3.2Gen1".
list_usb_devices() {
    lsblk -dno NAME,SIZE,MODEL,TRAN,TYPE --json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for d in data.get('blockdevices', []):
    if d.get('tran') == 'usb' and d.get('type') == 'disk':
        print(f\"{d['name']}\t{d['size']}\t{d.get('model', '') or ''}\")
"
}

# ── Helper: wait for USB hotplug ──────────────────────
# On Apple Silicon, USB-C hotplug triggers a full XHCI controller
# teardown/reinit cycle. The timeline from dmesg:
#   +0s   XHCI deregisters (bus gone, all devices vanish)
#   +5s   XHCI re-registers (new bus, hubs detected)
#   +10s  USB mass storage device enumerated
#   +11s  SCSI/block device (/dev/sdX) appears
#
# We poll every 2s and use udevadm settle after detection to ensure
# the block device and partition table are fully ready.
wait_for_usb() {
    local known_before="$1"

    while true; do
        sleep 2
        local current
        current=$(list_usb_devices)
        if [[ -n "$current" && "$current" != "$known_before" ]]; then
            # New device appeared — wait for udev to finish processing
            # (partition table scan, symlink creation, etc.)
            udevadm settle --timeout=15 2>/dev/null || sleep 3
            return 0
        fi
    done
}

# ── Device selection ──────────────────────────────────
echo "══ Write USB Image ══"
echo "  Image: $IMG ($IMG_SIZE)"
echo ""

while true; do
    DEVICES=$(list_usb_devices)

    if [[ -z "$DEVICES" ]]; then
        echo "  No USB drives detected."
        echo ""
        echo "  [w] Wait for USB drive (hotplug)"
        echo "  [q] Quit"
        echo ""
        read -rp "  > " choice
        case "$choice" in
            w|W)
                echo ""
                echo "  Waiting for USB drive... (plug in a drive, Ctrl-C to cancel)"
                wait_for_usb ""
                DEVICES=$(list_usb_devices)
                if [[ -n "$DEVICES" ]]; then
                    echo ""
                    echo "  Drive detected!"
                    echo ""
                else
                    echo ""
                    echo "  Still no drives found. Try again."
                    echo ""
                    continue
                fi
                ;;
            q|Q)
                echo "  Aborted."
                exit 0
                ;;
            *)
                echo "  Invalid choice."
                echo ""
                continue
                ;;
        esac
    fi

    # Build a numbered list of USB devices
    DEVS=()
    i=1
    echo "  USB drives:"
    echo ""
    while IFS=$'\t' read -r name size model; do
        DEVS+=("/dev/$name")
        printf "  [%d] /dev/%-8s %6s  %s\n" "$i" "$name" "$size" "$model"
        ((i++))
    done <<< "$DEVICES"

    echo ""
    echo "  [w] Wait for another USB drive (hotplug)"
    echo "  [q] Quit"
    echo ""
    read -rp "  Select drive: " choice

    case "$choice" in
        w|W)
            echo ""
            echo "  Waiting for USB drive... (plug in a drive, Ctrl-C to cancel)"
            wait_for_usb "$DEVICES"
            DEVICES=$(list_usb_devices)
            if [[ -n "$DEVICES" ]]; then
                echo ""
                echo "  Drive detected!"
                echo ""
            else
                echo ""
                echo "  Still no drives found. Try again."
                echo ""
            fi
            continue
            ;;
        q|Q)
            echo "  Aborted."
            exit 0
            ;;
        *)
            if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#DEVS[@]} )); then
                TARGET="${DEVS[$((choice - 1))]}"
                break
            else
                echo "  Invalid choice."
                echo ""
                continue
            fi
            ;;
    esac
done

# ── Show what's on the drive ──────────────────────────
echo ""
echo "══ Selected: $TARGET ══"
echo ""

# Device info
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS "$TARGET" 2>/dev/null || true

echo ""

# Partition table
PTTYPE=$(blkid -o value -s PTTYPE "$TARGET" 2>/dev/null || echo "unknown")
echo "  Partition table: $PTTYPE"

# Check if anything is mounted from this device
MOUNTS=$(lsblk -nro MOUNTPOINTS "$TARGET" 2>/dev/null | grep -v '^$' || true)
if [[ -n "$MOUNTS" ]]; then
    echo ""
    echo "  WARNING: The following mountpoints are active on this device:"
    echo "$MOUNTS" | sed 's/^/    /'
    echo ""
    echo "  Unmount them first, or choose a different device."
    echo ""
    read -rp "  Unmount and continue? [y/N] " umount_choice
    if [[ "$umount_choice" =~ ^[yY]$ ]]; then
        echo "  Unmounting..."
        for mp in $MOUNTS; do
            umount "$mp" 2>/dev/null || umount -l "$mp" 2>/dev/null || true
        done
    else
        echo "  Aborted."
        exit 0
    fi
fi

# ── Confirm ───────────────────────────────────────────
TARGET_SIZE=$(lsblk -dno SIZE "$TARGET" 2>/dev/null | tr -d ' ')
echo ""
echo "  ┌─────────────────────────────────────────────────┐"
echo "  │  ALL DATA ON $TARGET ($TARGET_SIZE) WILL BE DESTROYED  │"
echo "  └─────────────────────────────────────────────────┘"
echo ""
echo "  Image: $IMG ($IMG_SIZE)"
echo "  Target: $TARGET ($TARGET_SIZE)"
echo ""
read -rp "  Type 'yes' to confirm: " confirm

if [[ "$confirm" != "yes" ]]; then
    echo "  Aborted."
    exit 0
fi

# ── Write ─────────────────────────────────────────────
echo ""
echo "══ Writing image to $TARGET ══"
echo ""

dd if="$IMG" of="$TARGET" bs=4M status=progress conv=fsync

sync

echo ""
echo "══ Write complete ══"
echo ""
echo "  You can safely remove the USB drive."
echo ""
echo "  ┌─────────────────────────────────────────────────┐"
echo "  │  Boot on Apple Silicon                          │"
echo "  │                                                 │"
echo "  │  1. Plug USB-C drive into a working port        │"
echo "  │     (use the port closest to the power cable)   │"
echo "  │                                                 │"
echo "  │  2. Reboot into Asahi U-Boot                    │"
echo "  │                                                 │"
echo "  │  3. Interrupt U-Boot and run:                   │"
echo "  │                                                 │"
echo "  │       bootflow scan -b usb                      │"
echo "  │                                                 │"
echo "  └─────────────────────────────────────────────────┘"
