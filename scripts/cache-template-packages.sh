#!/usr/bin/env bash
# Cache all template packages (and their dependencies) into the ISO
# so the installer can run without downloading anything.
#
# Usage: ./scripts/cache-template-packages.sh <platform>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PLATFORM="${1:-}"

if [[ -z "$PLATFORM" ]]; then
    echo "Usage: $0 <platform>"
    exit 1
fi

PLATFORM_CONF="$PROJECT_ROOT/platforms/$PLATFORM/pacman.conf"
if [[ ! -f "$PLATFORM_CONF" ]]; then
    echo "ERROR: Platform pacman.conf not found: $PLATFORM_CONF"
    exit 1
fi

# Read the platform.toml for base packages (kernel, etc.)
PLATFORM_TOML="$PROJECT_ROOT/platforms/$PLATFORM/platform.toml"
ARCHES_PROVIDER=$(python3 "$SCRIPT_DIR/iso-config.py" provider-id "$PLATFORM_TOML")
export ARCHES_PROVIDER
case "$ARCHES_PROVIDER" in
    arch|omarchy)
        echo "ERROR: Alternative provider caches require authenticated ProviderInputs from Task 5."
        exit 1 ;;
esac
source "$SCRIPT_DIR/lib/build-cache.sh"

# Collect base packages from platform.toml (single source of truth).
# This matches what install.py:pacstrap() installs.
packages=$(python3 "$SCRIPT_DIR/iso-config.py" target-packages "$PLATFORM_TOML")
mapfile -t base_packages <<< "$packages"

# Collect ISO environment packages (both framebuffer and graphical layers).
# Even for an fb-only ISO build, we cache graphical packages because the
# installed system templates may include desktop modules.
echo "  Reading ISO environment packages (framebuffer + graphical)..."
packages=$(python3 "$SCRIPT_DIR/iso-config.py" packages "$PROJECT_ROOT/modules" graphical)
mapfile -t iso_packages <<< "$packages"
echo "  ISO environment packages: ${#iso_packages[@]}"

# Common hardware driver packages installed by chwd (hardware detection).
# These cover AMD, Intel, NVIDIA, and VM (virtio) GPUs.  Without these in
# the cache, chwd would need network access to install drivers.
hw_driver_packages=(
    # AMD
    mesa vulkan-radeon xf86-video-amdgpu libva-mesa-driver mesa-vdpau
    # Intel
    vulkan-intel intel-media-driver
    # NVIDIA (open kernel modules + proprietary userspace)
    nvidia-open nvidia-utils
    # VM / generic
    xf86-video-vesa xf86-video-fbdev
    # Input
    xf86-input-libinput
    # Xorg fallbacks
    xorg-server xorg-xinit
)

# Packages from hardware machine profiles (e.g. qemu-guest-agent for VMs,
# tuned/thermald for ThinkPads).  These are auto-detected at install time
# via DMI matching and must be pre-cached for offline installs.
machine_packages=()
echo "  Reading hardware machine profiles..."
for machine_toml in "$PROJECT_ROOT"/hardware/machines/*.toml; do
    [[ -f "$machine_toml" ]] || continue
    packages=$(python3 -c "
import tomllib
d = tomllib.load(open('$machine_toml', 'rb'))
for p in d.get('packages', {}).get('install', []):
    print(p)
")
    while IFS= read -r pkg; do
        [[ -n "$pkg" ]] && machine_packages+=("$pkg")
    done <<< "$packages"
done
if [[ ${#machine_packages[@]} -gt 0 ]]; then
    echo "  Machine profile packages: ${machine_packages[*]}"
fi

# Deduplicate
all_packages=($(printf '%s\n' "${base_packages[@]}" "${iso_packages[@]}" "${hw_driver_packages[@]}" "${machine_packages[@]}" | sort -u))

echo "  Packages to cache: ${#all_packages[@]}"
echo "  Package list: ${all_packages[*]}"

# Download packages (and dependencies) without installing.
# Use the platform pacman.conf so we get the right repos.
# We use the ISO's pacman.conf (with rewritten repo paths) for the build.
ISO_PACMAN_CONF="$PROJECT_ROOT/iso/pacman.conf"
if [[ -f "$ISO_PACMAN_CONF" ]]; then
    _conf="$ISO_PACMAN_CONF"
else
    _conf="$PLATFORM_CONF"
fi

echo "  Config: $_conf"

# Use a temporary dbpath so pacman resolves the full dependency tree
# as if nothing is installed (the host may already have these packages,
# which would cause -Sw to skip them).
TEMP_DB=$(mktemp -d)
cleanup() { rm -rf "$TEMP_DB" 2>/dev/null || true; }
trap cleanup EXIT

# Sync databases into the temp dbpath
echo ""
echo "  ── Syncing package databases ──"
pacman -Sy --noconfirm \
    --config "$_conf" \
    --dbpath "$TEMP_DB"

# New scopes never reuse the old flat cache, nor mix database generations.
dependencies=$(pacman -Q)
scope=$(compute_cache_hash "str:provider=$ARCHES_PROVIDER" "str:platform=$PLATFORM" \
    "str:architecture=$(uname -m)" "str:channel=stable" \
    "str:packages=${all_packages[*]}" "str:image=${ARCHES_BUILD_IMAGE:?Builder image identity is required}" \
    "str:dependencies=$dependencies" "$PLATFORM_TOML" "$_conf" \
    "str:snapshot=$(compute_cache_hash "$TEMP_DB/sync")")
CACHE_DIR="$PROJECT_ROOT/.offline-cache/$PLATFORM/$ARCHES_PROVIDER/$scope"
mkdir -p "$CACHE_DIR"
echo "  Downloading to: $CACHE_DIR"

# Download all packages AND their dependencies
echo ""
echo "  ── Downloading packages ──"
pacman -Sw --noconfirm \
    --config "$_conf" \
    --cachedir "$CACHE_DIR" \
    --dbpath "$TEMP_DB" \
    "${all_packages[@]}"

# Copy synced databases into the cache so the installer can use them
# offline. The installer reads these from $CACHE_DIR/sync/ to set up
# local file:// mirrors for pacstrap's -Sy.
echo ""
echo "  ── Copying pacman databases to cache ──"
mkdir -p "$CACHE_DIR/sync"
cp "$TEMP_DB/sync/"*.db "$CACHE_DIR/sync/" 2>/dev/null || true
echo "  Copied $(ls "$CACHE_DIR/sync/"*.db 2>/dev/null | wc -l) database files"

echo ""
echo "  Cached $(find "$CACHE_DIR" -name '*.pkg.tar.*' | wc -l) packages"
echo "  Cache size: $(du -sh "$CACHE_DIR" | cut -f1)"
# This pointer is build-local routing, not an authentication assertion.
printf '%s\n' "$CACHE_DIR" > "$PROJECT_ROOT/.offline-cache/$PLATFORM/$ARCHES_PROVIDER/current"
