#!/usr/bin/env bash
#
# Build an Arches ISO and boot a QEMU VM with a virtual disk attached
# for installation.
#
# Auto-detects the host platform and uses appropriate QEMU settings.
# If the ISO doesn't exist, builds it first. Creates a qcow2 disk if
# one doesn't exist.
#
# The VM boots the ISO with the disk attached. If auto-install.toml is
# embedded in the ISO, it runs unattended; otherwise the TUI installer
# starts.
#
# Usage:
#   ./scripts/qemu-install.sh                    # interactive install (online)
#   ./scripts/qemu-install.sh --rebuild           # force ISO rebuild
#   OFFLINE=1 ./scripts/qemu-install.sh          # offline install (cache + no network)
#   ./scripts/qemu-install.sh --log <file>        # installer log via virtio-serial
#   ./scripts/qemu-install.sh --fresh-disk        # fresh disk (don't reuse)
#
# After install completes, use `make qemu-boot` to boot the installed disk.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$PROJECT_DIR/out"

DISK="/tmp/arches-test-disk.qcow2"
DISK_SIZE="60G"
EFI_VARS="/tmp/arches-efi-vars.raw"
MEM="4G"
SMP="4"
SSH_PORT="2222"
LOG_FILE=""
FRESH_DISK=false
OFFLINE="${OFFLINE:-0}"

# ── Parse arguments ───────────────────────────────────
BUILD_ISO_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --rebuild) BUILD_ISO_ARGS+=(--rebuild) ;;
        --platform) BUILD_ISO_ARGS+=(--platform "$2"); PLATFORM="$2"; shift ;;
        --log) LOG_FILE="$2"; shift ;;
        --fresh-disk) FRESH_DISK=true ;;
    esac
    shift
done

# Pass OFFLINE to the ISO build
export OFFLINE

# ── Detect platform ──────────────────────────────────
# For QEMU, override aarch64-apple → aarch64-generic. The host is Apple
# Silicon but the VM is a standard aarch64 virt machine with KVM — no
# Apple-specific hardware to emulate.
source "$SCRIPT_DIR/detect-platform.sh"
if [[ "$PLATFORM" == "aarch64-apple" ]]; then
    echo "  (Apple Silicon host — using aarch64-generic for QEMU VM)"
    PLATFORM="aarch64-generic"
    source "$SCRIPT_DIR/detect-platform.sh"
fi
echo "Platform: $PLATFORM"

# ── Step 1: Ensure ISO exists ────────────────────────
ISO=$(ls -t "$OUT_DIR"/arches-"${PLATFORM}"-*.iso 2>/dev/null | head -1 || true)

if [[ -z "$ISO" ]] || [[ "${#BUILD_ISO_ARGS[@]}" -gt 0 ]]; then
    if [[ -z "$ISO" ]]; then
        echo "══ No ISO found for $PLATFORM — building ══"
    else
        echo "══ Rebuilding ISO ══"
    fi
    echo ""
    if [[ $EUID -eq 0 ]]; then
        PLATFORM="$PLATFORM" "$SCRIPT_DIR/build-iso.sh" ${BUILD_ISO_ARGS[@]+"${BUILD_ISO_ARGS[@]}"}
    else
        echo "  (This requires sudo for the container build)"
        sudo PLATFORM="$PLATFORM" SUDO_USER="${SUDO_USER:-$USER}" "$SCRIPT_DIR/build-iso.sh" ${BUILD_ISO_ARGS[@]+"${BUILD_ISO_ARGS[@]}"}
    fi
    ISO=$(ls -t "$OUT_DIR"/arches-"${PLATFORM}"-*.iso 2>/dev/null | head -1 || true)
    if [[ -z "$ISO" ]]; then
        echo "ERROR: ISO build failed — no ISO found in $OUT_DIR/"
        exit 1
    fi
fi

echo "ISO: $ISO"

# ── Step 2: Create disk if needed ────────────────────
if [[ "$FRESH_DISK" == true ]]; then
    rm -f "$DISK" "$EFI_VARS"
fi
if [[ ! -f "$DISK" ]]; then
    echo "══ Creating test disk ($DISK_SIZE) ══"
    qemu-img create -f qcow2 "$DISK" "$DISK_SIZE"
fi

# ── Fix ownership ────────────────────────────────────
# When run via sudo, the disk/EFI vars end up owned by root. Chown them
# back to the invoking user so `make qemu-boot` works without sudo.
if [[ $EUID -eq 0 && -n "${SUDO_USER:-}" ]]; then
    chown "$SUDO_USER:$(id -gn "$SUDO_USER")" "$DISK"
    [[ -f "$EFI_VARS" ]] && chown "$SUDO_USER:$(id -gn "$SUDO_USER")" "$EFI_VARS"
fi

# ── Step 3: Build QEMU arguments ─────────────────────

# Network — OFFLINE=1 disables the virtual NIC
NET_ARGS=()
if [[ "$OFFLINE" == "1" ]]; then
    NET_ARGS=(-nic none)
else
    NET_ARGS=(-net nic -net user,hostfwd=tcp::${SSH_PORT}-:22)
fi

# Virtio-serial log (installer output piped to host file)
LOG_ARGS=()
if [[ -n "$LOG_FILE" ]]; then
    : > "$LOG_FILE"
    LOG_ARGS=(
        -device virtio-serial-pci
        -chardev "file,id=logfile,path=$LOG_FILE"
        -device "virtserialport,chardev=logfile,name=arches-log"
    )
fi

echo ""
echo "══ Launching QEMU ══"
if [[ "$OFFLINE" != "1" ]]; then
    echo "  SSH: ssh -p $SSH_PORT <user>@localhost"
else
    echo "  Mode: OFFLINE (no network, packages from ISO cache)"
fi
echo "  Quit: Ctrl-A X (serial) or close window"
if [[ -n "$LOG_FILE" ]]; then
    echo "  Log: $LOG_FILE"
fi
echo ""

case "$PLATFORM" in
    x86-64)
        # Persistent EFI vars so boot entries survive across QEMU sessions
        if [[ ! -f "$EFI_VARS" ]]; then
            cp /usr/share/edk2/x64/OVMF_VARS.4m.fd "$EFI_VARS"
        fi

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
            -drive file="$DISK",format=qcow2,if=virtio \
            "${NET_ARGS[@]}" \
            "${LOG_ARGS[@]}" \
        ;;

    aarch64-generic)
        if [[ ! -f "$EFI_VARS" ]]; then
            cp /usr/share/edk2/aarch64/vars-template-pflash.raw "$EFI_VARS"
        fi

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
            -drive file="$DISK",format=qcow2,if=virtio \
            "${NET_ARGS[@]}" \
            "${LOG_ARGS[@]}" \
        ;;
esac
