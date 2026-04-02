#!/usr/bin/env bash
#
# Boot a QEMU VM with multiple virtual disks for RAID testing.
#
# Creates 2 (or more) qcow2 disk images and attaches them as additional
# virtio drives alongside the ISO. The installer TUI can then be used
# to configure mdadm or btrfs RAID across the disks.
#
# Usage:
#   ./scripts/qemu-raid.sh                         # 2x 120G disks
#   RAID_DISKS=3 ./scripts/qemu-raid.sh            # 3 disks
#   RAID_DISK_SIZE=60G ./scripts/qemu-raid.sh      # smaller disks
#   ./scripts/qemu-raid.sh --fresh                 # recreate disk images
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$PROJECT_DIR/out"

RAID_DISKS="${RAID_DISKS:-2}"
RAID_DISK_SIZE="${RAID_DISK_SIZE:-30G}"
DISK_PREFIX="/tmp/arches-raid-disk"
EFI_VARS="/tmp/arches-raid-efi-vars.raw"
MEM="4G"
SMP="4"
SSH_PORT="2222"
FRESH=false
LOG_FILE=""

# ── Parse arguments ───────────────────────────────────
BUILD_ISO_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fresh) FRESH=true ;;
        --rebuild) BUILD_ISO_ARGS+=(--rebuild) ;;
        --log) LOG_FILE="$2"; shift ;;
    esac
    shift
done

# ── Detect platform ──────────────────────────────────
source "$SCRIPT_DIR/detect-platform.sh"
if [[ "$PLATFORM" == "aarch64-apple" ]]; then
    PLATFORM="aarch64-generic"
    source "$SCRIPT_DIR/detect-platform.sh"
fi
echo "Platform: $PLATFORM"

# ── Step 1: Ensure ISO exists ────────────────────────
ISO=$(ls -t "$OUT_DIR"/arches-"${PLATFORM}"-*.iso 2>/dev/null | head -1 || true)

if [[ -z "$ISO" ]] || [[ "${#BUILD_ISO_ARGS[@]}" -gt 0 ]]; then
    if [[ -z "$ISO" ]]; then
        echo "No ISO found for $PLATFORM — build one first with: make iso"
        exit 1
    fi
fi

echo "ISO: $ISO"

# ── Step 2: Create disk images ───────────────────────
if [[ "$FRESH" == true ]]; then
    echo "Removing old RAID disk images..."
    rm -f "${DISK_PREFIX}"-*.qcow2 "$EFI_VARS"
fi

echo ""
echo "== Creating ${RAID_DISKS} virtual disks (${RAID_DISK_SIZE} each) =="

DISK_ARGS=()
for i in $(seq 1 "$RAID_DISKS"); do
    disk="${DISK_PREFIX}-${i}.qcow2"
    if [[ ! -f "$disk" ]]; then
        qemu-img create -f qcow2 "$disk" "$RAID_DISK_SIZE"
        echo "  Created: $disk"
    else
        echo "  Exists:  $disk (use --fresh to recreate)"
    fi
    # Use explicit drive+device so we can set a serial for lsblk identification
    DISK_ARGS+=(-drive "file=$disk,format=qcow2,if=none,id=raid${i}" -device "virtio-blk-pci,drive=raid${i},serial=ARCHES-RAID-${i}")
done

# ── Step 3: EFI vars ─────────────────────────────────
if [[ ! -f "$EFI_VARS" ]]; then
    case "$PLATFORM" in
        x86-64)
            cp /usr/share/edk2/x64/OVMF_VARS.4m.fd "$EFI_VARS"
            ;;
        aarch64-generic)
            cp /usr/share/edk2/aarch64/vars-template-pflash.raw "$EFI_VARS"
            ;;
    esac
fi

# ── Step 4: Log capture ──────────────────────────────
LOG_ARGS=()
if [[ -n "$LOG_FILE" ]]; then
    : > "$LOG_FILE"
    LOG_ARGS=(
        -device virtio-serial-pci
        -chardev "file,id=logfile,path=$LOG_FILE"
        -device "virtserialport,chardev=logfile,name=arches-log"
    )
fi

# ── Step 5: Launch QEMU ──────────────────────────────
echo ""
echo "== Launching QEMU with ${RAID_DISKS} disks =="
echo "  Disks: $(for i in $(seq 1 "$RAID_DISKS"); do echo -n "${DISK_PREFIX}-${i}.qcow2 "; done)"
echo "  SSH:   ssh -p $SSH_PORT <user>@localhost"
echo "  Quit:  Ctrl-A X (serial) or close window"
if [[ -n "$LOG_FILE" ]]; then
    echo "  Log:   $LOG_FILE"
fi
echo ""
echo "  In the installer:"
echo "    1. Welcome -> Continue"
echo "    2. Disk Select -> Configure RAID"
echo "    3. RAID Config -> pick backend, level, select all disks"
echo "    4. Layout Select -> pick Flexible (or Basic)"
echo "    5. Continue through template, user setup, confirm, install"
echo ""

case "$PLATFORM" in
    x86-64)
        exec qemu-system-x86_64 \
            -enable-kvm \
            -cpu host \
            -m "$MEM" \
            -smp "$SMP" \
            -drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/x64/OVMF_CODE.4m.fd \
            -drive if=pflash,format=raw,file="$EFI_VARS" \
            -vga virtio \
            -serial mon:stdio \
            -drive file="$ISO",format=raw,media=cdrom \
            "${DISK_ARGS[@]}" \
            -net nic -net user,hostfwd=tcp::${SSH_PORT}-:22 \
            "${LOG_ARGS[@]}" \
        ;;

    aarch64-generic)
        exec qemu-system-aarch64 \
            -M virt \
            -enable-kvm \
            -cpu host \
            -m "$MEM" \
            -smp "$SMP" \
            -drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/aarch64/QEMU_EFI-pflash.raw \
            -drive if=pflash,format=raw,file="$EFI_VARS" \
            -device virtio-gpu-pci \
            -device qemu-xhci -device usb-kbd -device usb-tablet \
            -device usb-storage,drive=cdrom0,bootindex=2 \
            -drive id=cdrom0,file="$ISO",format=raw,if=none,media=cdrom,readonly=on \
            "${DISK_ARGS[@]}" \
            -net nic -net user,hostfwd=tcp::${SSH_PORT}-:22 \
            "${LOG_ARGS[@]}" \
        ;;
esac
