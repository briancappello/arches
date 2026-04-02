#!/usr/bin/env bash
# Cache all template packages (and their dependencies) into the ISO
# so the installer can run without downloading anything.
#
# Usage: ./scripts/cache-template-packages.sh <platform>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
INSTALLER_DIR="$PROJECT_ROOT/installer/arches_installer"
TEMPLATES_DIR="$PROJECT_ROOT/templates"
CACHE_DIR="$PROJECT_ROOT/.offline-cache"
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

# Collect base packages from platform.toml (single source of truth).
# This matches what install.py:pacstrap() installs.
base_packages=()
while IFS= read -r pkg; do
    [[ -n "$pkg" ]] && base_packages+=("$pkg")
done < <(PYTHONPATH="$INSTALLER_DIR/.." python3 -c "
from arches_installer.core.platform import load_platform

platform = load_platform('$PLATFORM_TOML')

# Base packages (core system + platform-specific)
for p in platform.base_packages:
    print(p)

# Kernel variants
for v in platform.kernel.variants:
    print(v.package)
    print(v.headers)
")

# Collect all template packages (from all install phases)
template_packages=()
for tmpl in "$TEMPLATES_DIR"/*.toml; do
    echo "  Reading template: $(basename "$tmpl")"
    while IFS= read -r pkg; do
        [[ -n "$pkg" ]] && template_packages+=("$pkg")
    done < <(python3 -c "
import tomllib
d = tomllib.load(open('$tmpl', 'rb'))
i = d.get('install', {})
# New format: [install.pacstrap], [install.override], [install.firstboot]
for phase in ('pacstrap', 'override', 'firstboot'):
    for p in i.get(phase, {}).get('packages', []):
        print(p)
# Old format: [system] packages = [...]
for p in d.get('system', {}).get('packages', []):
    print(p)
")
done

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

# Deduplicate
all_packages=($(printf '%s\n' "${base_packages[@]}" "${template_packages[@]}" "${hw_driver_packages[@]}" | sort -u))

echo "  Packages to cache: ${#all_packages[@]}"
echo "  Package list: ${all_packages[*]}"

# Create cache dir
mkdir -p "$CACHE_DIR"

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
echo "  Downloading to: $CACHE_DIR"

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
