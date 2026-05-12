#!/usr/bin/env bash
#
# Build an Arches ISO and boot a QEMU VM with one or more virtual
# disks attached for installation.
#
# Auto-detects the host platform and uses appropriate QEMU settings.
# If the ISO doesn't exist, builds it first. Creates qcow2 disks if
# they don't exist.
#
# The VM boots the ISO with the disk(s) attached. If auto-install.toml
# is embedded in the ISO, it runs unattended; otherwise the TUI
# installer starts.
#
# === SINGLE-DISK MODE (default) ===
#   ./scripts/qemu-install.sh                    # interactive install (online)
#   ./scripts/qemu-install.sh --rebuild           # force ISO rebuild
#   OFFLINE=1 ./scripts/qemu-install.sh          # offline install
#   ./scripts/qemu-install.sh --log <file>        # capture installer log
#   ./scripts/qemu-install.sh --fresh-disk        # recreate the disk image
#
# === MULTI-DISK MODE (for testing the disk-role system) ===
# Pass --disk <size> one or more times. The first disk becomes the
# "root" role candidate; subsequent disks are extra storage. Each
# disk gets a distinct virtio serial so the disk-descriptor matcher
# can pick the right one in [[disks]] declarations.
#
#   # Two-disk LLM workstation simulation:
#   ./scripts/qemu-install.sh --disk 20G --disk 60G
#
#   # Three disks for a more complex setup:
#   ./scripts/qemu-install.sh --disk 20G --disk 60G --disk 100G
#
# Inside the auto-install.toml or layout, address the disks with
# descriptors like:
#   device = "20G virtio"   # matches the first disk
#   device = "60G virtio"   # matches the second
# Or by serial (each disk gets serial "ARCHES-TEST-NN" with NN = 01..NN):
#   device = "ARCHES-TEST-01"
#
# After install completes, use `make qemu-boot` to boot the installed disk.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$PROJECT_DIR/out"

# Disk paths follow a numbered convention so the existing `make qemu-boot`
# wrapper finds the primary disk at the canonical path. In multi-disk
# mode, the FIRST disk is the canonical one (used for boot) and additional
# disks are numbered -2, -3, ...
DISK_PRIMARY="/tmp/arches-test-disk.qcow2"
DISK_EXTRA_PREFIX="/tmp/arches-test-disk"
DEFAULT_DISK_SIZE="60G"
EFI_VARS="/tmp/arches-efi-vars.raw"
MEM="4G"
SMP="4"
SSH_PORT="2222"
LOG_FILE=""
FRESH_DISK=false
OFFLINE="${OFFLINE:-0}"

# Disk sizes for multi-disk mode. Empty array means "single disk with
# DEFAULT_DISK_SIZE" — the original behaviour. Otherwise, length of
# this array determines how many disks we attach.
DISK_SIZES=()

# ── Parse arguments ───────────────────────────────────
BUILD_ISO_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --rebuild) BUILD_ISO_ARGS+=(--rebuild) ;;
        --platform) BUILD_ISO_ARGS+=(--platform "$2"); PLATFORM="$2"; shift ;;
        --log) LOG_FILE="$2"; shift ;;
        --fresh-disk) FRESH_DISK=true ;;
        --disk)
            # Multi-disk mode: each --disk adds one disk of the given size.
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --disk requires a size argument (e.g. --disk 20G)" >&2
                exit 2
            fi
            DISK_SIZES+=("$2")
            shift
            ;;
        --help|-h)
            sed -n '2,40p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            echo "       Run with --help for usage." >&2
            exit 2
            ;;
    esac
    shift
done

# Multi-disk mode flag. Used below to switch between single-disk
# (legacy, untouched) and multi-disk attachment.
if [[ ${#DISK_SIZES[@]} -eq 0 ]]; then
    MULTI_DISK=false
else
    MULTI_DISK=true
fi

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

# Dry-run mode (ARCHES_QEMU_DRY_RUN=1) skips ISO building and QEMU
# launch — useful for CI smoke-testing the disk-creation + argument
# assembly logic without requiring a working ISO or root.
if [[ "${ARCHES_QEMU_DRY_RUN:-0}" != "1" ]]; then
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
else
    ISO="${ISO:-/dev/null}"
    echo "ISO: $ISO (DRY-RUN)"
fi

if [[ "${ARCHES_QEMU_DRY_RUN:-0}" != "1" ]]; then
    echo "ISO: $ISO"
fi

# ── Step 2: Create disk(s) if needed ─────────────────
# DISK_PATHS lists the disk image paths in attach order; index 0 is
# the canonical "primary" disk that survives across runs at the
# original /tmp/arches-test-disk.qcow2 path (so `make qemu-boot`
# keeps working unchanged).
DISK_PATHS=()

if [[ "$MULTI_DISK" == true ]]; then
    if [[ "$FRESH_DISK" == true ]]; then
        echo "  Removing existing test disks..."
        rm -f "$DISK_PRIMARY" "${DISK_EXTRA_PREFIX}"-*.qcow2 "$EFI_VARS"
    fi

    echo "══ Creating ${#DISK_SIZES[@]} test disk(s) ══"
    for i in "${!DISK_SIZES[@]}"; do
        size="${DISK_SIZES[$i]}"
        # Path 0 is the canonical primary; the rest are numbered -2, -3, ...
        if [[ $i -eq 0 ]]; then
            path="$DISK_PRIMARY"
        else
            path="${DISK_EXTRA_PREFIX}-$((i + 1)).qcow2"
        fi
        if [[ ! -f "$path" ]]; then
            qemu-img create -f qcow2 "$path" "$size" >/dev/null
            echo "  Created: $path  ($size)"
        else
            actual_size=$(qemu-img info "$path" 2>/dev/null | awk -F'[(:] *' '/virtual size/ {print $2; exit}')
            echo "  Exists:  $path  (requested $size, actual $actual_size — use --fresh-disk to recreate)"
        fi
        DISK_PATHS+=("$path")
    done
else
    # Single-disk mode (default). Preserves the original behaviour.
    if [[ "$FRESH_DISK" == true ]]; then
        rm -f "$DISK_PRIMARY" "$EFI_VARS"
    fi
    if [[ ! -f "$DISK_PRIMARY" ]]; then
        echo "══ Creating test disk ($DEFAULT_DISK_SIZE) ══"
        qemu-img create -f qcow2 "$DISK_PRIMARY" "$DEFAULT_DISK_SIZE"
    fi
    DISK_PATHS+=("$DISK_PRIMARY")
fi

# ── Fix ownership ────────────────────────────────────
# When run via sudo, disk files end up owned by root. Chown them back
# to the invoking user so `make qemu-boot` works without sudo.
if [[ $EUID -eq 0 && -n "${SUDO_USER:-}" ]]; then
    for p in "${DISK_PATHS[@]}"; do
        chown "$SUDO_USER:$(id -gn "$SUDO_USER")" "$p"
    done
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

# Disk attachment. Single-disk mode uses the legacy short-form
# `if=virtio` to match the original behaviour exactly. Multi-disk
# mode uses the explicit drive+device form so each disk gets a
# distinct virtio serial (visible in `lsblk -o SERIAL`), which lets
# the descriptor matcher pick the right one in [[disks]] declarations.
DISK_QEMU_ARGS=()
if [[ "$MULTI_DISK" == true ]]; then
    for i in "${!DISK_PATHS[@]}"; do
        path="${DISK_PATHS[$i]}"
        idx=$((i + 1))
        # Serial format: ARCHES-TEST-NN (zero-padded). Pick this up
        # in a descriptor with `device = "ARCHES-TEST-01"`. The size
        # token in the descriptor (e.g. "20G virtio") is the most
        # natural way to disambiguate; the serial is the escape hatch.
        serial=$(printf "ARCHES-TEST-%02d" "$idx")
        # bootindex on the first disk only — secondary disks are data,
        # not bootable. (The ISO has bootindex=1 implicitly via media=cdrom.)
        bootindex_arg=""
        if [[ $i -eq 0 ]]; then
            bootindex_arg=",bootindex=3"
        fi
        DISK_QEMU_ARGS+=(
            -drive "file=${path},format=qcow2,if=none,id=disk${idx}"
            -device "virtio-blk-pci,drive=disk${idx},serial=${serial}${bootindex_arg}"
        )
    done
else
    DISK_QEMU_ARGS+=(-drive "file=${DISK_PATHS[0]},format=qcow2,if=virtio")
fi

echo ""
echo "══ Launching QEMU ══"
if [[ "$OFFLINE" != "1" ]]; then
    echo "  SSH:    ssh -p $SSH_PORT <user>@localhost"
else
    echo "  Mode:   OFFLINE (no network, packages from ISO cache)"
fi
echo "  Quit:   Ctrl-A X (serial) or close window"
if [[ -n "$LOG_FILE" ]]; then
    echo "  Log:    $LOG_FILE"
fi
if [[ "$MULTI_DISK" == true ]]; then
    echo "  Disks:  ${#DISK_PATHS[@]} virtio devices"
    for i in "${!DISK_PATHS[@]}"; do
        idx=$((i + 1))
        size="${DISK_SIZES[$i]}"
        serial=$(printf "ARCHES-TEST-%02d" "$idx")
        path="${DISK_PATHS[$i]}"
        echo "          $(basename "$path") ($size, serial=$serial)"
    done
    echo ""
    echo "  Descriptors that will match each disk:"
    for i in "${!DISK_PATHS[@]}"; do
        idx=$((i + 1))
        size="${DISK_SIZES[$i]}"
        serial=$(printf "ARCHES-TEST-%02d" "$idx")
        echo "    disk #${idx}: device = \"${size} virtio\"   # or device = \"${serial}\""
    done
fi
echo ""

case "$PLATFORM" in
    x86-64)
        # Persistent EFI vars so boot entries survive across QEMU sessions
        if [[ ! -f "$EFI_VARS" ]]; then
            cp /usr/share/edk2/x64/OVMF_VARS.4m.fd "$EFI_VARS"
        fi

        if [[ "${ARCHES_QEMU_DRY_RUN:-0}" == "1" ]]; then
            echo "DRY-RUN: would exec qemu-system-x86_64 with disks:"
            printf '  %s\n' "${DISK_QEMU_ARGS[@]}"
            exit 0
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
            "${DISK_QEMU_ARGS[@]}" \
            "${NET_ARGS[@]}" \
            "${LOG_ARGS[@]}" \
        ;;

    aarch64-generic)
        if [[ ! -f "$EFI_VARS" ]]; then
            cp /usr/share/edk2/aarch64/vars-template-pflash.raw "$EFI_VARS"
        fi

        if [[ "${ARCHES_QEMU_DRY_RUN:-0}" == "1" ]]; then
            echo "DRY-RUN: would exec qemu-system-aarch64 with disks:"
            printf '  %s\n' "${DISK_QEMU_ARGS[@]}"
            exit 0
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
            "${DISK_QEMU_ARGS[@]}" \
            "${NET_ARGS[@]}" \
            "${LOG_ARGS[@]}" \
        ;;
esac
