#!/usr/bin/env bash
#
# Build the Arches aarch64 ISO inside a Podman container.
#
# This provides the full Arch Linux ARM toolchain (mkarchiso, pacman,
# makepkg, etc.) on a non-Arch host (e.g. Fedora aarch64).
#
# Requires sudo — mkarchiso needs real root for devtmpfs mounts,
# loopback devices, and chroot operations (same as native ISO builds).
#
# Usage:
#   sudo ./scripts/build-in-container.sh              # default: aarch64-generic
#   sudo ./scripts/build-in-container.sh --rebuild     # force rebuild container image
#   sudo FORCE=1 ./scripts/build-in-container.sh       # pass FORCE to AUR repo build
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
IMAGE_NAME="arches-builder"
PLATFORM="aarch64-generic"

# ── Require root ──────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Container ISO build requires root (for mkarchiso chroot/mount operations)."
    echo "       Run: sudo make container-iso-aarch64"
    exit 1
fi

# Resolve the invoking user for UID mapping inside the container.
# When run via sudo, SUDO_USER is the original user.
BUILD_USER="${SUDO_USER:-$(logname 2>/dev/null || echo nobody)}"
BUILD_UID=$(id -u "$BUILD_USER")
BUILD_GID=$(id -g "$BUILD_USER")

REBUILD=false
for arg in "$@"; do
    case "$arg" in
        --rebuild) REBUILD=true ;;
    esac
done

# ── Build container image if needed ───────────────────
if [[ "$REBUILD" == true ]]; then
    echo "══ Rebuilding container image ══"
    podman rmi -f "$IMAGE_NAME" 2>/dev/null || true
fi

if ! podman image exists "$IMAGE_NAME"; then
    echo "══ Building container image ($IMAGE_NAME) ══"
    podman build -t "$IMAGE_NAME" -f "$PROJECT_DIR/Containerfile" "$PROJECT_DIR"
fi

# ── Volume mounts ─────────────────────────────────────
# Mount the project directory read-write.
# Mount sibling repos (custom plasmoid sources) read-only so
# build-aur-repo.sh can find them at their expected relative paths.
VOLUMES=(
    -v "$PROJECT_DIR:/build"
)

# Persistent pacman package cache — avoids re-downloading ~660MB of packages
# on every build. mkarchiso uses /var/cache/pacman/pkg/ as its CacheDir.
CACHE_DIR="$PROJECT_DIR/.pkg-cache"
mkdir -p "$CACHE_DIR"
VOLUMES+=(-v "$CACHE_DIR:/var/cache/pacman/pkg")

# Mount the build user's .ssh directory so stage-installer can embed their
# public key into the ISO (for passwordless SSH to installed systems).
BUILD_HOME=$(eval echo "~$BUILD_USER")
if [[ -d "$BUILD_HOME/.ssh" ]]; then
    VOLUMES+=(-v "$BUILD_HOME/.ssh:/home/builder/.ssh:ro")
fi

# Custom plasmoid sibling repos — mount if they exist
for sibling in kde-task-manager plasma-ai-usage-monitor; do
    sibling_path="$PROJECT_DIR/../$sibling"
    if [[ -d "$sibling_path" ]]; then
        VOLUMES+=(-v "$(cd "$sibling_path" && pwd):/build/../$sibling:ro")
    else
        echo "  WARNING: Sibling repo not found: $sibling_path"
        echo "           Custom plasmoid build for $sibling will fail."
    fi
done

# ── Run the build ─────────────────────────────────────
# --privileged: mkarchiso needs devtmpfs, loopback devices, mount, chroot.
# SUDO_USER=builder: build-aur-repo.sh uses this to drop privileges for makepkg.
echo "══ Starting container build (platform: $PLATFORM) ══"

FORCE_FLAG=""
[[ "${FORCE:-}" == "1" ]] && FORCE_FLAG="FORCE=1"

# The project dir is mounted from the host. Inside the container, 'builder'
# needs write access for makepkg (which refuses to run as root). We match
# the builder UID/GID to the invoking user so file ownership stays consistent.
exec podman run --rm --privileged \
    --security-opt label=disable \
    "${VOLUMES[@]}" \
    -e SUDO_USER=builder \
    "$IMAGE_NAME" \
    /bin/bash -c "\
        usermod -u $BUILD_UID builder && \
        groupmod -g $BUILD_GID builder && \
        chown -R builder:builder /tmp && \
        chown builder:builder /home/builder && \
        make iso-$PLATFORM $FORCE_FLAG"
