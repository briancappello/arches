#!/usr/bin/env bash
#
# Auto-detect the build platform from host hardware.
#
# Detection logic:
#   1. uname -m → x86_64 or aarch64
#   2. On aarch64, check for Apple Silicon via device-tree
#      → aarch64-apple vs aarch64-generic
#
# Override: set PLATFORM=<name> before sourcing.
#
# Usage (source, don't execute — sets $PLATFORM):
#   source scripts/detect-platform.sh
#
# The following variables are set:
#   PLATFORM       — one of: x86-64, aarch64-generic, aarch64-apple
#   CONTAINER_ARCH — podman --platform value: linux/amd64 or linux/arm64
#   ARCHES_ARCH    — archiso architecture: x86_64 or aarch64
#

if [[ -n "${PLATFORM:-}" ]]; then
    # Caller already set PLATFORM — validate it
    case "$PLATFORM" in
        x86-64|aarch64-generic|aarch64-apple) ;;
        *)
            echo "ERROR: Unknown PLATFORM='$PLATFORM'"
            echo "       Valid values: x86-64, aarch64-generic, aarch64-apple"
            exit 1
            ;;
    esac
else
    # Auto-detect from host hardware
    HOST_ARCH="$(uname -m)"
    case "$HOST_ARCH" in
        x86_64)
            PLATFORM="x86-64"
            ;;
        aarch64)
            # Check for Apple Silicon via device-tree
            if [[ -f /sys/firmware/devicetree/base/compatible ]] &&
               tr '\0' '\n' < /sys/firmware/devicetree/base/compatible | grep -q '^apple,'; then
                PLATFORM="aarch64-apple"
            else
                PLATFORM="aarch64-generic"
            fi
            ;;
        *)
            echo "ERROR: Unsupported architecture: $HOST_ARCH"
            echo "       Supported: x86_64, aarch64"
            echo "       Override with: PLATFORM=x86-64|aarch64-generic|aarch64-apple"
            exit 1
            ;;
    esac
fi

# Derived values
case "$PLATFORM" in
    x86-64)
        CONTAINER_ARCH="linux/amd64"
        ARCHES_ARCH="x86_64"
        ;;
    aarch64-generic|aarch64-apple)
        CONTAINER_ARCH="linux/arm64"
        ARCHES_ARCH="aarch64"
        ;;
esac

export PLATFORM CONTAINER_ARCH ARCHES_ARCH
