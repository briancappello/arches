#!/usr/bin/env bash
#
# Install Arches from a running Linux host via Podman container.
#
# This script builds the arches-installer container image (Arch Linux ARM
# with pacstrap + Asahi repos) and runs the installer inside it, targeting
# the host's btrfs partition.
#
# The container is given access to:
#   - /dev (block devices for mounting partitions)
#   - /mnt (shared mount namespace for the install target)
#   - Host firmware (/lib/firmware/vendor, read-only, for Apple Silicon)
#   - The install config file
#
# Two install modes:
#   alongside  — create new subvolumes next to existing OS (safe, dual-boot)
#   replace    — replace existing subvolumes (destructive)
#
# Usage:
#   sudo ./scripts/host-install.sh config.toml
#   sudo ./scripts/host-install.sh --dry-run config.toml
#   sudo ./scripts/host-install.sh --rebuild config.toml
#
# Or via Makefile:
#   sudo make host-install CONFIG=examples/host-install.toml
#   sudo make host-install-dry CONFIG=examples/host-install.toml
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
IMAGE_NAME="arches-installer"

# ── Logging ───────────────────────────────────────────
# All output goes to both the terminal and a log file (overwritten each run).
LOG_FILE="$PROJECT_DIR/host-install.log"
exec > >(tee "$LOG_FILE") 2>&1
echo "Log: $LOG_FILE"

# ── Parse arguments ───────────────────────────────────
REBUILD=false
DRY_RUN=""
CONFIG_FILE=""

for arg in "$@"; do
    case "$arg" in
        --rebuild)  REBUILD=true ;;
        --dry-run)  DRY_RUN="--dry-run" ;;
        *)          CONFIG_FILE="$arg" ;;
    esac
done

if [[ -z "$CONFIG_FILE" ]]; then
    echo "Usage: $0 [--rebuild] [--dry-run] <config.toml>"
    echo ""
    echo "Install Arches into btrfs subvolumes from a running Linux host."
    echo ""
    echo "Options:"
    echo "  --rebuild    Force rebuild of the container image"
    echo "  --dry-run    Validate config and print plan without executing"
    echo ""
    echo "The config file specifies the target partition, ESP, template,"
    echo "install mode (alongside/replace), and user settings."
    echo "See examples/host-install.toml for a complete example."
    exit 1
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: Config file not found: $CONFIG_FILE"
    exit 1
fi

# Resolve to absolute path
CONFIG_FILE="$(cd "$(dirname "$CONFIG_FILE")" && pwd)/$(basename "$CONFIG_FILE")"

# ── Require root ──────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Host install requires root (for mounting partitions and pacstrap)."
    echo "       Run: sudo $0 $*"
    exit 1
fi

# ── Persistent pacman cache ───────────────────────────
# Shared .pkg-cache/ — used by podman build (image layers) and podman run
# (pacstrap), so packages downloaded during either step are reused.
CACHE_DIR="$PROJECT_DIR/.pkg-cache"
mkdir -p "$CACHE_DIR"

# ── Build container image if needed ───────────────────
if [[ "$REBUILD" == true ]]; then
    echo "══ Rebuilding installer container image ══"
    podman rmi -f "$IMAGE_NAME" 2>/dev/null || true
fi

if ! podman image exists "$IMAGE_NAME"; then
    echo "══ Building installer container image ($IMAGE_NAME) ══"
    podman build -t "$IMAGE_NAME" -f "$PROJECT_DIR/Containerfile.install" "$PROJECT_DIR"
fi

# ── Volume mounts ─────────────────────────────────────
VOLUMES=(
    # Config file
    -v "$CONFIG_FILE:/opt/arches/config.toml:ro"

    # Ansible playbooks (from project, in case they've been updated)
    -v "$PROJECT_DIR/ansible:/opt/arches/ansible:ro"

    # Installer source (from project, for latest code)
    -v "$PROJECT_DIR/installer:/opt/arches/installer:ro"
)

# Host firmware for Apple Silicon
for fw_path in /lib/firmware/vendor /usr/lib/firmware/vendor; do
    if [[ -d "$fw_path" ]]; then
        VOLUMES+=(-v "$fw_path:/host-firmware:ro")
        break
    fi
done

# SSH key embedding (optional)
REAL_HOME="$(eval echo ~"${SUDO_USER:-$USER}")"
if [[ -f "$REAL_HOME/.ssh/id_ed25519.pub" ]]; then
    VOLUMES+=(-v "$REAL_HOME/.ssh/id_ed25519.pub:/opt/arches/build-host.pub:ro")
elif [[ -f "$REAL_HOME/.ssh/id_rsa.pub" ]]; then
    VOLUMES+=(-v "$REAL_HOME/.ssh/id_rsa.pub:/opt/arches/build-host.pub:ro")
fi

# ── AUR / custom package repo ─────────────────────────
# The platform pacman.conf references [arches-local] at /opt/arches-repo.
# For ISO builds, build-aur-repo.sh populates this during `make iso`.
# For host-install, we reuse a prior build or run the build in-container.
AUR_REPO_DIR="$PROJECT_DIR/iso/airootfs/opt/arches-repo"

if [[ -d "$AUR_REPO_DIR" ]] && ls "$AUR_REPO_DIR"/*.pkg.tar.* &>/dev/null; then
    echo "  AUR repo: reusing pre-built ($(ls "$AUR_REPO_DIR"/*.pkg.tar.* | wc -l) packages)"
else
    echo "══ Building custom packages in container ══"
    # build-aur-repo.sh runs makepkg, which refuses to run as root.
    # We create a 'builder' user inside the container, and the script's
    # privilege-drop logic (SUDO_USER) handles the rest.
    #
    # Mount layout inside the container:
    #   /build              → project root (rw, for writing to iso/airootfs/opt/arches-repo)
    #   /build/../<sibling> → sibling repos (ro, source for custom plasmoids)

    BUILD_VOLUMES=(
        -v "$PROJECT_DIR:/build"
    )

    # Mount sibling repos at the paths build-aur-repo.sh expects.
    # The script uses $PROJECT_ROOT/../<sibling>, where PROJECT_ROOT=/build,
    # so the sibling path resolves to /<sibling> inside the container.
    for sibling in kde-task-manager plasma-ai-usage-monitor; do
        sibling_path="$PROJECT_DIR/../$sibling"
        if [[ -d "$sibling_path" ]]; then
            abs_sibling="$(cd "$sibling_path" && pwd)"
            BUILD_VOLUMES+=(-v "$abs_sibling:/$sibling:ro")
            echo "  Sibling: $sibling (found)"
        else
            echo "  WARNING: Sibling repo not found: $sibling_path"
            echo "           Custom package build for $sibling will fail."
        fi
    done

    # CACHE_DIR set above (shared .pkg-cache/ for all container builds).
    BUILD_VOLUMES+=(-v "$CACHE_DIR:/var/cache/pacman/pkg")

    # Resolve the invoking user's UID/GID for consistent file ownership
    BUILD_USER="${SUDO_USER:-$(logname 2>/dev/null || echo nobody)}"
    BUILD_UID=$(id -u "$BUILD_USER")
    BUILD_GID=$(id -g "$BUILD_USER")

    podman run --rm \
        --security-opt label=disable \
        "${BUILD_VOLUMES[@]}" \
        "$IMAGE_NAME" \
        /bin/bash -c "
            useradd -m -u $BUILD_UID builder 2>/dev/null || usermod -u $BUILD_UID builder 2>/dev/null
            groupmod -g $BUILD_GID builder 2>/dev/null
            echo 'builder ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers
            chown builder:builder /tmp
            export SUDO_USER=builder
            cd /build
            sudo -u builder --preserve-env=PATH /build/scripts/build-aur-repo.sh aarch64-apple
            chown -R root:root /build/iso/airootfs/opt/arches-repo 2>/dev/null || true"

    echo "  AUR repo: built ($(ls "$AUR_REPO_DIR"/*.pkg.tar.* 2>/dev/null | wc -l) packages)"
fi

VOLUMES+=(-v "$AUR_REPO_DIR:/opt/arches-repo:ro")

# ── Persistent pacman cache ───────────────────────────
# CACHE_DIR set above (shared .pkg-cache/ for all container builds).
VOLUMES+=(-v "$CACHE_DIR:/var/cache/pacman/pkg")

# ── Run the installer ─────────────────────────────────
echo "══ Starting host install ══"
echo "  Config: $CONFIG_FILE"
echo "  Image:  $IMAGE_NAME"
[[ -n "$DRY_RUN" ]] && echo "  Mode:   DRY RUN"
echo ""

# The container needs:
# --privileged: for mount, arch-chroot, pacstrap
# --pid=host: so /proc/mounts reflects actual mounts
# Shared mount propagation: so mounts created inside the container
#   are visible to the host and persist after container exit.
#
# We mount /mnt as a shared mount so the installer's mounts at /mnt
# are propagated to the host. For GRUB entry generation (which runs
# on the host side after the container exits), the script handles that
# separately.

podman run --rm --privileged \
    --security-opt label=disable \
    --pid=host \
    --mount type=bind,src=/dev,dst=/dev,rslave \
    --mount type=bind,src=/mnt,dst=/mnt,bind-propagation=shared \
    --mount type=bind,src=/proc,dst=/host-proc,ro \
    "${VOLUMES[@]}" \
    "$IMAGE_NAME" \
    arches-install --host /opt/arches/config.toml \
        --platform /opt/arches/platform/platform.toml \
        $DRY_RUN

INSTALL_RC=$?
if [[ $INSTALL_RC -ne 0 ]]; then
    echo "ERROR: Container install failed (exit code $INSTALL_RC)"
    exit $INSTALL_RC
fi

# ── Apply GRUB entry on the host ──────────────────────
# The installer saves a GRUB snippet to the target filesystem.
# We need to mount the target btrfs to read it, then write to the host GRUB.
PARTITION=$(grep '^partition' "$CONFIG_FILE" | sed 's/.*= *"\?\([^"]*\)"\?/\1/' | tr -d ' ')
SUBVOL_PREFIX=$(grep '^subvol_prefix' "$CONFIG_FILE" 2>/dev/null | sed 's/.*= *"\?\([^"]*\)"\?/\1/' | tr -d ' ' || true)
SUBVOL_PREFIX="${SUBVOL_PREFIX:-@arches}"

# Mount the target subvolume to read the GRUB snippet
GRUB_MNT=$(mktemp -d)
mount -o "subvol=${SUBVOL_PREFIX},ro" "$PARTITION" "$GRUB_MNT" 2>/dev/null

GRUB_SNIPPET="$GRUB_MNT/opt/arches/grub-entry.cfg"
if [[ -f "$GRUB_SNIPPET" ]]; then
    echo "══ Applying GRUB entry on host ══"

    # Ensure GRUB shows the menu so the user can select Arches.
    # Fedora defaults to GRUB_TIMEOUT_STYLE=hidden which skips the menu.
    if [[ -f /etc/default/grub ]]; then
        if grep -q 'GRUB_TIMEOUT_STYLE=hidden' /etc/default/grub; then
            sed -i 's/GRUB_TIMEOUT_STYLE=hidden/GRUB_TIMEOUT_STYLE=menu/' /etc/default/grub
            echo "  Changed GRUB_TIMEOUT_STYLE to menu"
        fi
        if grep -q 'GRUB_TIMEOUT=0\b\|GRUB_TIMEOUT=1\b' /etc/default/grub; then
            sed -i 's/GRUB_TIMEOUT=[01]\b/GRUB_TIMEOUT=5/' /etc/default/grub
            echo "  Set GRUB_TIMEOUT to 5 seconds"
        fi
    fi

    # Write as /etc/grub.d/41_arches (Fedora-style)
    if [[ -d /etc/grub.d ]]; then
        GRUB_SCRIPT="/etc/grub.d/41_arches"
        {
            echo '#!/bin/bash'
            echo '# Added by arches host-install'
            echo "cat <<'ARCHES_EOF'"
            cat "$GRUB_SNIPPET"
            echo "ARCHES_EOF"
        } > "$GRUB_SCRIPT"
        chmod +x "$GRUB_SCRIPT"
        echo "  Wrote: $GRUB_SCRIPT"

        # Regenerate grub.cfg
        if command -v grub2-mkconfig &>/dev/null; then
            grub2-mkconfig -o /boot/grub2/grub.cfg 2>/dev/null && echo "  Regenerated /boot/grub2/grub.cfg"
        elif command -v grub-mkconfig &>/dev/null; then
            grub-mkconfig -o /boot/grub/grub.cfg 2>/dev/null && echo "  Regenerated /boot/grub/grub.cfg"
        else
            echo "  WARNING: grub-mkconfig not found. Run manually:"
            echo "    sudo grub2-mkconfig -o /boot/grub2/grub.cfg"
        fi
    else
        echo "  WARNING: /etc/grub.d not found. Add GRUB entry manually."
        echo "  Snippet content:"
        cat "$GRUB_SNIPPET"
    fi
else
    echo "  No GRUB snippet found in target — skipping GRUB entry."
fi

umount "$GRUB_MNT" 2>/dev/null
rmdir "$GRUB_MNT" 2>/dev/null

echo ""
echo "══ Done ══"
