#!/usr/bin/env bash
#
# Manage a persistent privileged build container for Arches ISO builds.
#
# The container runs under root's podman (required for --privileged).
# All podman commands use sudo transparently. The user's password is
# requested once and cached by sudo for subsequent calls.
#
# Usage:
#   ./scripts/builder.sh start      # Start the builder (prompts for sudo once)
#   ./scripts/builder.sh stop       # Stop the builder
#   ./scripts/builder.sh status     # Check if running
#   ./scripts/builder.sh build      # Build graphical ISO
#   ./scripts/builder.sh build-fb   # Build framebuffer-only ISO
#   ./scripts/builder.sh exec <cmd> # Run command in the builder
#   ./scripts/builder.sh log        # Show last build log
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONTAINER_NAME="arches-builder"

source "$SCRIPT_DIR/detect-platform.sh"

case "$CONTAINER_ARCH" in
    linux/amd64) BASE_IMAGE="docker.io/archlinux:latest"; TARGETARCH="amd64" ;;
    linux/arm64) BASE_IMAGE="docker.io/lopsided/archlinux:latest"; TARGETARCH="arm64" ;;
esac

IMAGE_NAME="arches-builder-${TARGETARCH}"
CACHE_DIR="$PROJECT_DIR/.pkg-cache/$PLATFORM"
BUILD_LOG="$PROJECT_DIR/builder.log"

# All podman operations go through root's podman.
# For start/stop, the user provides their password via normal sudo.
# For build/exec, passwordless sudo is configured for 'podman exec'.
_podman() {
    if [[ $EUID -eq 0 ]]; then
        podman "$@"
    else
        sudo podman "$@"
    fi
}

# Check that podman storage is functional before attempting any operations.
# Common failure: overlay driver on btrfs, which the kernel does not support.
_check_storage() {
    local err rc=0
    err=$(_podman info 2>&1) || rc=$?
    if [[ $rc -eq 0 ]]; then
        return 0
    fi

    if [[ "$err" == *"overwritten by graph driver"*"from database"* ]]; then
        echo "ERROR: Podman storage database conflicts with configured driver."
        echo ""
        echo "The old storage database is forcing a different driver than the one"
        echo "in your storage.conf. Delete the stale storage data:"
        echo ""
        echo "    sudo rm -rf /var/lib/containers/storage"
        echo ""
        echo "Then retry: make builder-start"
    elif [[ "$err" == *"overlay"*"btrfs"* || "$err" == *"is not supported over btrfs"* ]]; then
        echo "ERROR: Podman storage driver 'overlay' is not supported on btrfs."
        echo ""
        echo "Your root filesystem uses btrfs, which is incompatible with the"
        echo "default overlay storage driver. Configure the btrfs driver for podman:"
        echo ""
        echo "  # Root podman (used by the builder):"
        echo "  sudo mkdir -p /etc/containers"
        echo "  printf '[storage]\ndriver = \"btrfs\"\n' | sudo tee /etc/containers/storage.conf"
        echo "  sudo rm -rf /var/lib/containers/storage"
        echo ""
        echo "  # Rootless podman (your user):"
        echo "  mkdir -p ~/.config/containers"
        echo "  printf '[storage]\ndriver = \"btrfs\"\n' > ~/.config/containers/storage.conf"
        echo "  rm -rf ~/.local/share/containers/storage"
        echo ""
        echo "Then retry: make builder-start"
    else
        echo "ERROR: Podman storage is misconfigured."
        echo ""
        echo "$err"
        echo ""
        echo "Try deleting podman storage and retrying:"
        echo "    sudo rm -rf /var/lib/containers/storage"
        echo "    sudo podman system reset"
    fi
    exit 1
}

# Use sudo -n (non-interactive) for checks that should work without password.
# Falls back to podman exec with a simple test command.
_builder_running() {
    # Try non-interactive sudo first (works if sudo credentials are cached
    # or if broad NOPASSWD is configured)
    if sudo -n podman inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q true; then
        return 0
    fi
    # Fall back to checking via podman exec (covered by NOPASSWD rule)
    if sudo -n podman exec "$CONTAINER_NAME" true 2>/dev/null; then
        return 0
    fi
    return 1
}

_ensure_image() {
    if ! _podman image exists "$IMAGE_NAME"; then
        echo "══ Building container image ($IMAGE_NAME) ══"
        _podman build \
            --network=host \
            --platform="$CONTAINER_ARCH" \
            --build-arg BASE_IMAGE="$BASE_IMAGE" \
            --build-arg TARGETARCH="$TARGETARCH" \
            -t "$IMAGE_NAME" \
            -f "$PROJECT_DIR/Containerfile" \
            "$PROJECT_DIR"
    fi
}

_build_volumes() {
    local volumes=(
        -v "$PROJECT_DIR:/build"
        -v "$CACHE_DIR:/var/cache/pacman/pkg"
    )

    # SSH key embedding
    local build_user
    build_user="$(logname 2>/dev/null || whoami)"
    local build_home
    build_home=$(eval echo "~$build_user")
    if [[ -d "$build_home/.ssh" ]]; then
        volumes+=(-v "$build_home/.ssh:/home/builder/.ssh:ro")
    fi

    # Module build source mounts
    for mounts_file in "$PROJECT_DIR"/modules/*/build.mounts; do
        [[ -f "$mounts_file" ]] || continue
        while IFS= read -r rel_path; do
            [[ "$rel_path" =~ ^#.*$ || -z "$rel_path" ]] && continue
            local src_path="$PROJECT_DIR/$rel_path"
            if [[ -d "$src_path" ]]; then
                volumes+=(-v "$(cd "$src_path" && pwd):/build/$rel_path:ro")
            fi
        done < "$mounts_file"
    done

    echo "${volumes[@]}"
}

cmd_start() {
    _check_storage

    if _podman container exists "$CONTAINER_NAME" 2>/dev/null; then
        if _builder_running; then
            echo "Builder is already running."
            return 0
        fi
        echo "Removing stale builder container..."
        _podman rm -f "$CONTAINER_NAME" >/dev/null
    fi

    _ensure_image
    mkdir -p "$CACHE_DIR"

    local build_user
    build_user="$(logname 2>/dev/null || whoami)"
    local build_uid build_gid
    build_uid=$(id -u "$build_user")
    build_gid=$(id -g "$build_user")

    local volumes
    volumes=$(_build_volumes)

    echo "══ Starting persistent builder ══"
    echo "  Platform:  $PLATFORM ($ARCHES_ARCH)"
    echo "  Container: $CONTAINER_NAME"
    echo "  Image:     $IMAGE_NAME"

    # shellcheck disable=SC2086
    _podman run -d --privileged \
        --name "$CONTAINER_NAME" \
        --network=host \
        --security-opt label=disable \
        --pids-limit=-1 \
        $volumes \
        "$IMAGE_NAME" \
        /bin/bash -c "
            usermod -u $build_uid builder 2>/dev/null
            groupmod -g $build_gid builder 2>/dev/null
            chown builder:builder /tmp /home/builder 2>/dev/null
            trap 'exit 0' TERM
            sleep infinity &
            wait
        "

    echo ""
    echo "Builder started. Run builds with:"
    echo "  ./scripts/builder.sh build"
}

cmd_stop() {
    if _podman container exists "$CONTAINER_NAME" 2>/dev/null; then
        _podman rm -f "$CONTAINER_NAME" >/dev/null
        echo "Builder stopped."
    else
        echo "Builder is not running."
    fi
}

cmd_status() {
    if _podman container exists "$CONTAINER_NAME" 2>/dev/null; then
        if _builder_running; then
            echo "Builder is running ($CONTAINER_NAME)"
            return 0
        fi
        echo "Builder exists but is stopped."
        return 1
    fi
    echo "Builder is not running."
    echo "Start with: $0 start"
    return 1
}

cmd_exec() {
    if ! _builder_running; then
        echo "ERROR: Builder is not running. Start with: $0 start"
        exit 1
    fi
    _podman exec "$CONTAINER_NAME" "$@"
}

cmd_build() {
    local iso_mode="${1:-graphical}"
    shift 2>/dev/null || true

    if ! _builder_running; then
        echo "ERROR: Builder is not running. Start with: $0 start"
        exit 1
    fi

    local offline_flag=""
    [[ "${OFFLINE:-1}" == "1" ]] && offline_flag="OFFLINE=1"

    local force_flag=""
    [[ "${FORCE:-}" == "1" ]] && force_flag="FORCE=1"

    echo "══ Building ISO (mode: $iso_mode, platform: $PLATFORM) ══"
    echo "  Log: $BUILD_LOG"

    # shellcheck disable=SC2086
    _podman exec \
        -e SUDO_USER=builder \
        "$CONTAINER_NAME" \
        make -C /build _iso \
            PLATFORM="$PLATFORM" \
            ARCHES_ARCH="$ARCHES_ARCH" \
            ISO_MODE="$iso_mode" \
            $offline_flag \
            $force_flag \
            "$@" \
        2>&1 | tee "$BUILD_LOG"

    local exit_code=${PIPESTATUS[0]}

    if [[ $exit_code -eq 0 ]]; then
        echo ""
        echo "══ Build succeeded ══"
        for f in "$PROJECT_DIR"/out/arches-*.iso; do
            [[ -f "$f" ]] || continue
            local size
            size=$(ls -lh "$f" | awk '{print $5}')
            echo "  $(basename "$f") ($size)"
        done
    else
        echo ""
        echo "══ Build failed (exit code: $exit_code) ══"
        echo "  Check log: $BUILD_LOG"
    fi

    return $exit_code
}

cmd_log() {
    if [[ -f "$BUILD_LOG" ]]; then
        tail -50 "$BUILD_LOG"
    else
        echo "No build log found."
    fi
}

# ── Main ──────────────────────────────────────────────

case "${1:-help}" in
    start)    cmd_start ;;
    stop)     cmd_stop ;;
    status)   cmd_status ;;
    exec)     shift; cmd_exec "$@" ;;
    build)    shift; cmd_build graphical "$@" ;;
    build-fb) shift; cmd_build fb "$@" ;;
    log)      cmd_log ;;
    help|*)
        echo "Usage: $0 <command>"
        echo ""
        echo "  start       Start the persistent builder (prompts for sudo)"
        echo "  stop        Stop and remove the builder"
        echo "  status      Check if the builder is running"
        echo "  build       Build graphical ISO"
        echo "  build-fb    Build framebuffer-only ISO"
        echo "  exec <cmd>  Run command in the builder"
        echo "  log         Show last build log"
        echo ""
        echo "Environment:"
        echo "  OFFLINE=0   Skip offline package cache (default: 1)"
        echo "  FORCE=1     Force rebuild of AUR packages"
        ;;
esac
