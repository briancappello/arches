#!/usr/bin/env bash
#
# Manage a persistent privileged build container for Arches ISO builds.
#
# The container runs under the user's rootless Podman. No sudo is used:
# container root is the host user, so files the build writes into the
# bind-mounted project are owned by the user on the host.
#
# Usage:
#   ./scripts/builder.sh start      # Start the builder
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

# The builder must run under the user's rootless Podman. Root's Podman
# would write root-owned files into the project and needs sudo.
_check_rootless() {
    if [[ $EUID -eq 0 ]]; then
        echo "ERROR: Run the builder as your user, not as root."
        echo "       It uses rootless Podman and needs no sudo."
        exit 1
    fi
}

# Check that podman storage is functional before attempting any operations.
# Common failure: overlay driver on btrfs, which the kernel does not support.
_check_storage() {
    local err rc=0
    err=$(podman info 2>&1) || rc=$?
    if [[ $rc -eq 0 ]]; then
        return 0
    fi

    if [[ "$err" == *"overwritten by graph driver"*"from database"* ]]; then
        echo "ERROR: Podman storage database conflicts with configured driver."
        echo ""
        echo "The old storage database is forcing a different driver than the one"
        echo "in your storage.conf. Delete the stale storage data:"
        echo ""
        echo "    rm -rf ~/.local/share/containers/storage"
        echo ""
        echo "Then retry: make builder-start"
    elif [[ "$err" == *"overlay"*"btrfs"* || "$err" == *"is not supported over btrfs"* ]]; then
        echo "ERROR: Podman storage driver 'overlay' is not supported on btrfs."
        echo ""
        echo "Your root filesystem uses btrfs, which is incompatible with the"
        echo "default overlay storage driver. Configure the btrfs driver for podman:"
        echo ""
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
        echo "    podman system reset"
    fi
    exit 1
}

_builder_running() {
    [[ "$(podman inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null)" == true ]]
}

_ensure_image() {
    if ! podman image exists "$IMAGE_NAME"; then
        echo "══ Building container image ($IMAGE_NAME) ══"
        podman build \
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

    # Module build source mounts (scripts/lib/build-mounts.sh resolves
    # them from the main checkout when this is a worktree)
    source "$PROJECT_DIR/scripts/lib/build-mounts.sh"
    for mounts_file in "$PROJECT_DIR"/modules/*/build.mounts; do
        [[ -f "$mounts_file" ]] || continue
        while IFS= read -r rel_path; do
            [[ "$rel_path" =~ ^#.*$ || -z "$rel_path" ]] && continue
            local src_path
            if src_path="$(resolve_build_mount "$PROJECT_DIR" "$rel_path")"; then
                volumes+=(-v "$src_path:/build/$rel_path:ro")
            fi
        done < "$mounts_file"
    done

    echo "${volumes[@]}"
}

cmd_start() {
    _check_storage

    if podman container exists "$CONTAINER_NAME" 2>/dev/null; then
        if _builder_running; then
            echo "Builder is already running."
            return 0
        fi
        echo "Removing stale builder container..."
        podman rm -f "$CONTAINER_NAME" >/dev/null
    fi

    _ensure_image
    mkdir -p "$CACHE_DIR"

    local volumes
    volumes=$(_build_volumes)

    echo "══ Starting persistent builder ══"
    echo "  Platform:  $PLATFORM ($ARCHES_ARCH)"
    echo "  Container: $CONTAINER_NAME"
    echo "  Image:     $IMAGE_NAME"

    # Rootless: container root maps to the host user, and --privileged
    # grants capabilities only inside the user namespace. Podman's default
    # rootless network is used: with --network=host the container cannot
    # mount sysfs, because the host network namespace is not ours.
    #
    # shellcheck disable=SC2086
    podman run -d --privileged \
        --name "$CONTAINER_NAME" \
        --security-opt label=disable \
        --pids-limit=-1 \
        $volumes \
        "$IMAGE_NAME" \
        /bin/bash -c "
            trap 'exit 0' TERM
            sleep infinity &
            wait
        "

    echo ""
    echo "Builder started. Run builds with:"
    echo "  ./scripts/builder.sh build"
}

cmd_stop() {
    if podman container exists "$CONTAINER_NAME" 2>/dev/null; then
        podman rm -f "$CONTAINER_NAME" >/dev/null
        echo "Builder stopped."
    else
        echo "Builder is not running."
    fi
}

cmd_status() {
    if podman container exists "$CONTAINER_NAME" 2>/dev/null; then
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
    podman exec "$CONTAINER_NAME" "$@"
}

cmd_build() {
    local iso_mode="${1:-graphical}"
    shift 2>/dev/null || true

    if ! _builder_running; then
        echo "ERROR: Builder is not running. Start with: $0 start"
        exit 1
    fi

    # Verify the package cache bind-mount is still functional. If the
    # host directory was deleted and recreated (e.g. make clean-caches)
    # while the container was running, the mount goes stale and pacman
    # will fail with "No such file or directory" deep inside the build.
    if ! podman exec "$CONTAINER_NAME" test -d /var/cache/pacman/pkg 2>/dev/null; then
        echo "ERROR: Package cache mount is stale inside the builder container."
        echo ""
        echo "  This usually happens when the host .pkg-cache/ directory was"
        echo "  deleted while the builder was running (e.g. make clean-caches)."
        echo ""
        echo "  Fix: restart the builder to refresh the bind-mounts:"
        echo "    make builder-stop"
        echo "    make builder-start"
        exit 1
    fi

    local offline_flag="OFFLINE=${OFFLINE:-1}"

    local force_flag=""
    [[ "${FORCE:-}" == "1" ]] && force_flag="FORCE=1"

    # TEMPLATE filters iso.toml [install].templates to a single workload.
    # Passed both as a make argument (so the Makefile's TEMPLATE ?= picks
    # it up) and as ARCHES_TEMPLATE in the env (so subprocesses inherit
    # the filter without re-parsing make args).
    local template_flag=""
    [[ -n "${TEMPLATE:-}" ]] && template_flag="TEMPLATE=$TEMPLATE"

    echo "══ Building ISO (mode: $iso_mode, platform: $PLATFORM${TEMPLATE:+, template: $TEMPLATE}) ══"
    echo "  Log: $BUILD_LOG"

    # Run the build under a bash wrapper that chowns artifacts back
    # to container root on every exit (success, failure, signal).
    # Container root is the host user. makepkg runs as 'builder', whose
    # files would otherwise belong to a subordinate uid on the host,
    # which the host user cannot remove without root.
    #
    # shellcheck disable=SC2086
    podman exec \
        -e SUDO_USER=builder \
        -e ARCHES_TEMPLATE="${TEMPLATE:-}" \
        -e ARCHES_GPU="${ARCHES_GPU:-}" \
        -e ARCHES_ALLOW_DEFAULT_PASSWORD="${ARCHES_ALLOW_DEFAULT_PASSWORD:-}" \
        -e ARCHES_TEST_BUILD="${ARCHES_TEST_BUILD:-}" \
        "$CONTAINER_NAME" \
        bash -c '
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
            trap "_chown_back" EXIT INT TERM
            make -C /build _iso \
                PLATFORM='"$PLATFORM"' \
                ARCHES_ARCH='"$ARCHES_ARCH"' \
                ISO_MODE='"$iso_mode"' \
                '"$offline_flag"' \
                '"$force_flag"' \
                '"$template_flag"' \
                '"$*"'
        ' \
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
    start|stop|status|exec|build|build-fb) _check_rootless ;;
esac

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
        echo "  start       Start the persistent builder"
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
