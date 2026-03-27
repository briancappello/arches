#!/usr/bin/env bash
#
# Build Arches install media and write to USB drive.
#
# Auto-detects the host platform:
#   x86-64:        Build ISO → dd directly to USB (hybrid ISO)
#   aarch64-apple: Build ISO → convert to USB image (U-Boot/extlinux) → write
#   aarch64-generic: Same as aarch64-apple (U-Boot/extlinux)
#
# Usage:
#   sudo ./scripts/build-usb.sh                       # auto-detect
#   sudo PLATFORM=x86-64 ./scripts/build-usb.sh       # explicit platform
#   sudo ./scripts/build-usb.sh --rebuild              # force container rebuild
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$PROJECT_DIR/out"

# ── Parse arguments ───────────────────────────────────
BUILD_ISO_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --rebuild) BUILD_ISO_ARGS+=(--rebuild) ;;
        --platform) BUILD_ISO_ARGS+=(--platform "$2"); PLATFORM="$2"; shift ;;
    esac
    shift
done

# ── Detect platform ──────────────────────────────────
source "$SCRIPT_DIR/detect-platform.sh"
echo "Platform: $PLATFORM"

# ── Require root ──────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: USB build requires root."
    echo "       Run: sudo make usb"
    exit 1
fi

# ── Step 1: Build ISO ────────────────────────────────
# Check if a recent ISO already exists for this platform
ISO=$(ls -t "$OUT_DIR"/arches-"${PLATFORM}"-*.iso 2>/dev/null | head -1 || true)

if [[ -z "$ISO" ]]; then
    echo "══ No ISO found for $PLATFORM — building ══"
    "$SCRIPT_DIR/build-iso.sh" ${BUILD_ISO_ARGS[@]+"${BUILD_ISO_ARGS[@]}"}
    ISO=$(ls -t "$OUT_DIR"/arches-"${PLATFORM}"-*.iso 2>/dev/null | head -1 || true)
    if [[ -z "$ISO" ]]; then
        echo "ERROR: ISO build failed — no ISO found in $OUT_DIR/"
        exit 1
    fi
else
    echo "══ Using existing ISO: $ISO ══"
fi

# ── Step 2: Platform-specific post-processing ────────
case "$PLATFORM" in
    x86-64)
        # archiso ISOs are hybrid (ISO 9660 + GPT) — dd directly to USB.
        echo ""
        echo "══ x86-64: ISO is hybrid — writing directly to USB ══"
        "$SCRIPT_DIR/write-usb.sh" "$ISO"
        ;;

    aarch64-generic|aarch64-apple)
        # Convert to USB image for U-Boot/extlinux boot, then write.
        echo ""
        echo "══ aarch64: Converting ISO to USB image ══"
        "$SCRIPT_DIR/iso-to-usb-image.sh" "$ISO"

        IMG=$(ls -t "$OUT_DIR"/arches-"${PLATFORM}"-*.usb.img 2>/dev/null | head -1 || true)
        if [[ -z "$IMG" ]]; then
            echo "ERROR: USB image conversion failed"
            exit 1
        fi

        echo ""
        "$SCRIPT_DIR/write-usb.sh" "$IMG"
        ;;
esac
