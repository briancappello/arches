#!/usr/bin/env bash
#
# Build the Arches ISO inside a Podman container.
#
# Auto-detects the host platform and builds the appropriate ISO.
# Works from any Linux distro — only requires Podman and sudo.
#
# Usage:
#   sudo ./scripts/build-iso.sh                       # auto-detect platform + default template
#   sudo PLATFORM=x86-64 ./scripts/build-iso.sh       # explicit platform
#   sudo ./scripts/build-iso.sh --template dev-workstation  # explicit template
#   sudo ./scripts/build-iso.sh --rebuild              # force container rebuild
#   sudo FORCE=1 ./scripts/build-iso.sh               # force AUR repo rebuild
#   sudo OFFLINE=1 ./scripts/build-iso.sh             # pre-cache all packages for offline install
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# ── Parse arguments ───────────────────────────────────
REBUILD=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --rebuild) REBUILD=true ;;
        --platform) PLATFORM="$2"; shift ;;
        --template) TEMPLATE="$2"; shift ;;
    esac
    shift
done

# ── Detect platform ──────────────────────────────────
source "$SCRIPT_DIR/detect-platform.sh"

# Map platform to container base image.
# Image name includes arch so x86-64 and aarch64 images don't collide.
case "$CONTAINER_ARCH" in
    linux/amd64) BASE_IMAGE="docker.io/archlinux:latest"; TARGETARCH="amd64" ;;
    linux/arm64) BASE_IMAGE="docker.io/lopsided/archlinux:latest"; TARGETARCH="arm64" ;;
esac

# Container image is the same for all x86-64 optimization tiers — the
# tier-specific repos are in the platform's pacman.conf, not the container.
IMAGE_NAME="arches-builder-${TARGETARCH}"

# Template: use explicit value, TEMPLATE env var, or let Make resolve from platform.toml
TEMPLATE="${TEMPLATE:-}"

echo "Platform: $PLATFORM (arch: $ARCHES_ARCH, container: $CONTAINER_ARCH, template: ${TEMPLATE:-<default>})"

# ── Logging ───────────────────────────────────────────
LOG_FILE="$PROJECT_DIR/container-build.log"
exec > >(tee "$LOG_FILE") 2>&1
echo "Log: $LOG_FILE"

# ── Require root ──────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: ISO build requires root (for mkarchiso chroot/mount operations)."
    echo "       Run: sudo $0"
    exit 1
fi

# Resolve the invoking user for UID mapping inside the container.
BUILD_USER="${SUDO_USER:-$(logname 2>/dev/null || echo nobody)}"
BUILD_UID=$(id -u "$BUILD_USER")
BUILD_GID=$(id -g "$BUILD_USER")

# ── Persistent pacman cache ───────────────────────────
# Partitioned by platform so different optimization tiers don't pollute
# each other's caches (e.g., v3 and v4 builds on the same host).
CACHE_DIR="$PROJECT_DIR/.pkg-cache/$PLATFORM"
mkdir -p "$CACHE_DIR"

# ── Build container image if needed ───────────────────
if [[ "$REBUILD" == true ]]; then
    echo "══ Rebuilding container image ══"
    podman rmi -f "$IMAGE_NAME" 2>/dev/null || true
fi

if ! podman image exists "$IMAGE_NAME"; then
    echo "══ Building container image ($IMAGE_NAME for $CONTAINER_ARCH) ══"
    podman build \
        --network=host \
        --platform="$CONTAINER_ARCH" \
        --build-arg BASE_IMAGE="$BASE_IMAGE" \
        --build-arg TARGETARCH="$TARGETARCH" \
        -t "$IMAGE_NAME" \
        -f "$PROJECT_DIR/Containerfile" \
        "$PROJECT_DIR"
fi

# ── Volume mounts ─────────────────────────────────────
VOLUMES=(
    -v "$PROJECT_DIR:/build"
    -v "$CACHE_DIR:/var/cache/pacman/pkg"
)

# SSH key embedding — mount the build user's .ssh so stage-installer
# can embed their public key into the ISO.
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
echo "══ Starting container build (platform: $PLATFORM, template: ${TEMPLATE:-<default>}) ══"

FORCE_FLAG=""
[[ "${FORCE:-}" == "1" ]] && FORCE_FLAG="FORCE=1"
TEMPLATE_FLAG=""
[[ -n "$TEMPLATE" ]] && TEMPLATE_FLAG="TEMPLATE=$TEMPLATE"
OFFLINE_FLAG=""
[[ "${OFFLINE:-0}" == "1" ]] && OFFLINE_FLAG="OFFLINE=1"

# Bash as PID 1 inside a container ignores SIGINT/SIGTERM by default.
# We register a trap that forwards the signal to all child processes,
# then run make in the background and wait for it.
podman run --rm --privileged \
    --network=host \
    --security-opt label=disable \
    "${VOLUMES[@]}" \
    -e SUDO_USER=builder \
    "$IMAGE_NAME" \
    /bin/bash -c '
        trap "kill 0; exit 130" INT
        trap "kill 0; exit 143" TERM
        usermod -u '"$BUILD_UID"' builder &&
        groupmod -g '"$BUILD_GID"' builder &&
        chown -R builder:builder /tmp &&
        chown builder:builder /home/builder &&
        make _iso PLATFORM='"$PLATFORM"' ARCHES_ARCH='"$ARCHES_ARCH"' '"$FORCE_FLAG"' '"$TEMPLATE_FLAG"' '"$OFFLINE_FLAG"' &
        wait $!
    '

# Fix ownership of build output so non-root user can access it
if [[ -n "${SUDO_USER:-}" && -d "$PROJECT_DIR/out" ]]; then
    chown -R "$SUDO_USER:$SUDO_USER" "$PROJECT_DIR/out"
fi
