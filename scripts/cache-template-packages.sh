#!/usr/bin/env bash
# Cache all template packages (and their dependencies) into the ISO
# so the installer can run without downloading anything.
#
# Usage: ./scripts/cache-template-packages.sh <platform>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
INSTALLER_DIR="$PROJECT_ROOT/installer/arches_installer"
TEMPLATES_DIR="$INSTALLER_DIR/templates"
CACHE_DIR="$PROJECT_ROOT/iso/airootfs/opt/arches/pkg-cache"
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

# Collect base packages that the installer always installs
base_packages=(base linux-firmware mkinitcpio sudo)

# Read all kernel variant packages from platform.toml
while IFS= read -r pkg; do
    [[ -n "$pkg" ]] && base_packages+=("$pkg")
done < <(python3 -c "
import tomllib
d = tomllib.load(open('$PLATFORM_TOML', 'rb'))
for v in d['kernel']['variants']:
    print(v['package'])
    print(v['headers'])
")

# Read base_packages.install from platform.toml
while IFS= read -r pkg; do
    pkg=$(echo "$pkg" | tr -d '", ')
    [[ -n "$pkg" && "$pkg" != "]" ]] && base_packages+=("$pkg")
done < <(sed -n '/^\[base_packages\]/,/^\[/p' "$PLATFORM_TOML" | grep -E '^\s+"' | sed 's/.*"\(.*\)".*/\1/')

# Collect all template packages (from all install phases)
template_packages=()
for tmpl in "$TEMPLATES_DIR"/*.toml; do
    echo "  Reading template: $(basename "$tmpl")"
    # Read packages from all [install.*] sections
    for section in 'install\.pacstrap' 'install\.override' 'install\.firstboot'; do
        while IFS= read -r pkg; do
            pkg=$(echo "$pkg" | tr -d '", ')
            [[ -n "$pkg" && "$pkg" != "]" ]] && template_packages+=("$pkg")
        done < <(sed -n "/^\[${section}\]/,/^\[/p" "$tmpl" | grep -E '^\s+"' | sed 's/.*"\(.*\)".*/\1/')
    done
    # Also support old format: [system] packages = [...]
    while IFS= read -r pkg; do
        pkg=$(echo "$pkg" | tr -d '", ')
        [[ -n "$pkg" && "$pkg" != "]" ]] && template_packages+=("$pkg")
    done < <(sed -n '/^\[system\]/,/^\[/p' "$tmpl" | sed -n '/^packages/,/^\]/p' | grep -E '^\s+"' | sed 's/.*"\(.*\)".*/\1/')
done

# Deduplicate
all_packages=($(printf '%s\n' "${base_packages[@]}" "${template_packages[@]}" | sort -u))

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

echo ""
echo "  Cached $(find "$CACHE_DIR" -name '*.pkg.tar.*' | wc -l) packages"
echo "  Cache size: $(du -sh "$CACHE_DIR" | cut -f1)"
