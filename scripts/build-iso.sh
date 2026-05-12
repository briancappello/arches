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

# ── Require root ──────────────────────────────────────
# Check this FIRST, before opening the log file. Otherwise a non-root
# invocation that already had a root-owned log file from a previous run
# would fail at `tee` with a misleading "Permission denied" before
# reaching the actual "must run as root" error message.
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: ISO build requires root (for mkarchiso chroot/mount operations)."
    echo "       Run: sudo $0"
    echo ""
    echo "       Alternative: use the persistent builder which does NOT require sudo:"
    echo "         make builder-start    # one-time setup"
    echo "         make builder-iso      # build (no sudo)"
    exit 1
fi

# Resolve the invoking user for UID mapping inside the container.
BUILD_USER="${SUDO_USER:-$(logname 2>/dev/null || echo nobody)}"
BUILD_UID=$(id -u "$BUILD_USER")
BUILD_GID=$(id -g "$BUILD_USER")

# ── Logging ───────────────────────────────────────────
# We must write to the log as root (we ARE root here), but we want the
# invoking user to be able to `tail`/`rm` the file afterwards without
# sudo. Two-step trick:
#   1. If a stale file exists, fix its ownership BEFORE redirecting
#      stdout to it — otherwise a previous root-owned file could fail
#      to open even for append-via-tee under some umasks.
#   2. After the build (success or failure), chown the final log
#      file back to the invoking user via an EXIT trap.
LOG_FILE="$PROJECT_DIR/container-build.log"
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    if [[ -e "$LOG_FILE" ]]; then
        chown "$SUDO_USER:$(id -gn "$SUDO_USER")" "$LOG_FILE" 2>/dev/null || true
    fi
    # Ensure the log ends up user-owned regardless of how we exit
    # (success, error, signal). The trap is set BEFORE the redirect
    # so even a failure inside `tee` itself triggers cleanup.
    trap '
        if [[ -f "'"$LOG_FILE"'" ]]; then
            chown "'"$SUDO_USER"':$(id -gn "'"$SUDO_USER"'")" "'"$LOG_FILE"'" 2>/dev/null || true
        fi
    ' EXIT
fi
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

# Module build source mounts — discover from modules/*/build.mounts
for mounts_file in "$PROJECT_DIR"/modules/*/build.mounts; do
    [[ -f "$mounts_file" ]] || continue
    module_name=$(basename "$(dirname "$mounts_file")")
    while IFS= read -r rel_path; do
        [[ "$rel_path" =~ ^#.*$ || -z "$rel_path" ]] && continue
        src_path="$PROJECT_DIR/$rel_path"
        if [[ -d "$src_path" ]]; then
            VOLUMES+=(-v "$(cd "$src_path" && pwd):/build/$rel_path:ro")
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
podman run --rm --privileged \
    --network=host \
    --security-opt label=disable \
    "${VOLUMES[@]}" \
    -e SUDO_USER=builder \
    -e ARCHES_TEMPLATE="${TEMPLATE:-}" \
    -e ARCHES_GPU="${ARCHES_GPU:-}" \
    -e ARCHES_ALLOW_DEFAULT_PASSWORD="${ARCHES_ALLOW_DEFAULT_PASSWORD:-}" \
    "$IMAGE_NAME" \
    /bin/bash -c '
        # On ANY exit (success, failure, SIGINT, SIGTERM, OOM), chown
        # all build artifacts back to the host user so they can be
        # managed without sudo on the host afterwards. The Makefile
        # staging targets run as root (mkarchiso needs it) and write
        # into the bind-mounted /build, which would otherwise leak
        # root-owned files into the host workspace and break next
        # `git checkout` / `make clean` runs.
        _chown_back() {
            chown -R builder:builder \
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
        usermod -u '"$BUILD_UID"' builder &&
        groupmod -g '"$BUILD_GID"' builder &&
        chown -R builder:builder /tmp &&
        chown builder:builder /home/builder &&
        make _iso PLATFORM='"$PLATFORM"' ARCHES_ARCH='"$ARCHES_ARCH"' '"$FORCE_FLAG"' '"$TEMPLATE_FLAG"' '"$OFFLINE_FLAG"' '"$ISO_MODE_FLAG"' &
        wait $!
    '
