#!/usr/bin/env bash
#
# Build the Arches ISO inside a Podman container.
#
# Auto-detects the host platform and builds the appropriate ISO.
# Works from any Linux distro — only requires rootless Podman, no sudo.
# Container root is the invoking user, so build output is user-owned.
#
# Usage:
#   ./scripts/build-iso.sh                       # auto-detect platform + default template
#   PLATFORM=x86-64 ./scripts/build-iso.sh       # explicit platform
#   ./scripts/build-iso.sh --template dev-workstation  # explicit template
#   ./scripts/build-iso.sh --rebuild              # force container rebuild
#   FORCE=1 ./scripts/build-iso.sh               # force AUR repo rebuild
#   OFFLINE=1 ./scripts/build-iso.sh             # pre-cache all packages for offline install
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

# ── Require a regular user ────────────────────────────
# The build runs under the user's rootless Podman. Under root's Podman it
# would write root-owned files into the project.
if [[ $EUID -eq 0 ]]; then
    echo "ERROR: Run the ISO build as your user, not as root."
    echo "       It uses rootless Podman and needs no sudo."
    exit 1
fi

# ── Logging ───────────────────────────────────────────
LOG_FILE="$PROJECT_DIR/container-build.log"
exec > >(tee "$LOG_FILE") 2>&1
echo "Log: $LOG_FILE"

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
if [[ -d "$HOME/.ssh" ]]; then
    VOLUMES+=(-v "$HOME/.ssh:/home/builder/.ssh:ro")
fi

# Module build source mounts — discover from modules/*/build.mounts.
# From a worktree they resolve against the main checkout
# (scripts/lib/build-mounts.sh; override with ARCHES_SOURCES_ROOT).
source "$SCRIPT_DIR/lib/build-mounts.sh"
for mounts_file in "$PROJECT_DIR"/modules/*/build.mounts; do
    [[ -f "$mounts_file" ]] || continue
    module_name=$(basename "$(dirname "$mounts_file")")
    while IFS= read -r rel_path; do
        [[ "$rel_path" =~ ^#.*$ || -z "$rel_path" ]] && continue
        if src_path="$(resolve_build_mount "$PROJECT_DIR" "$rel_path")"; then
            VOLUMES+=(-v "$src_path:/build/$rel_path:ro")
        else
            echo "  WARNING: Build source not found: $src_path (module: $module_name)"
        fi
    done < "$mounts_file"
done

# ── Run the build ─────────────────────────────────────
echo "══ Starting container build (platform: $PLATFORM, template: ${TEMPLATE:-<default>}) ══"

FORCE_FLAG=""
[[ "${FORCE:-}" == "1" ]] && FORCE_FLAG="FORCE=1"
TEMPLATE_FLAG=""
# TEMPLATE filters iso.toml's [install].templates list down to a single
# template across the whole build (see iso-config.py and the Makefile's
# ARCHES_TEMPLATE variable). Pass it as a make argument AND export it
# into the container env so subprocesses (build-aur-repo.sh,
# cache-template-packages.sh) inherit it transparently.
[[ -n "$TEMPLATE" ]] && TEMPLATE_FLAG="TEMPLATE=$TEMPLATE"
OFFLINE_FLAG="OFFLINE=${OFFLINE:-0}"
ISO_MODE_FLAG="ISO_MODE=${ISO_MODE:-graphical}"

# Bash as PID 1 inside a container ignores SIGINT/SIGTERM by default.
# We register a trap that forwards the signal to all child processes,
# then run make in the background and wait for it.
# Rootless: --privileged grants capabilities only inside the user
# namespace. The default rootless network is used: with --network=host
# the container cannot mount sysfs.
podman run --rm --privileged \
    --security-opt label=disable \
    "${VOLUMES[@]}" \
    -e SUDO_USER=builder \
    -e ARCHES_TEMPLATE="${TEMPLATE:-}" \
    -e ARCHES_GPU="${ARCHES_GPU:-}" \
    -e ARCHES_ALLOW_DEFAULT_PASSWORD="${ARCHES_ALLOW_DEFAULT_PASSWORD:-}" \
    -e ARCHES_TEST_BUILD="${ARCHES_TEST_BUILD:-}" \
    "$IMAGE_NAME" \
    /bin/bash -c '
        # On ANY exit (success, failure, SIGINT, SIGTERM, OOM), chown
        # all build artifacts to container root, which is the host
        # user. makepkg runs as builder, whose files would otherwise
        # belong to a subordinate uid on the host.
        _chown_back() {
            chown -R root:root \
                /build/iso/airootfs \
                /build/iso/grub \
                /build/iso/syslinux \
                /build/iso/pacman.conf \
                /build/iso/packages.x86_64 \
                /build/iso/packages.aarch64 \
                /build/out \
                /build/.pkg-cache \
                /build/.offline-cache \
                /build/.aur-repo \
                2>/dev/null || true
        }
        trap "_chown_back; kill 0; exit 130" INT
        trap "_chown_back; kill 0; exit 143" TERM
        trap "_chown_back" EXIT
        make _iso PLATFORM='"$PLATFORM"' ARCHES_ARCH='"$ARCHES_ARCH"' '"$FORCE_FLAG"' '"$TEMPLATE_FLAG"' '"$OFFLINE_FLAG"' '"$ISO_MODE_FLAG"' &
        wait $!
    '
