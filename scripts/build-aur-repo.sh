#!/usr/bin/env bash
#
# Build AUR packages and create a local pacman repository.
# This repo is embedded into the ISO so AUR packages can be
# installed offline during the install phase.
#
# Usage:
#   ./scripts/build-aur-repo.sh <platform>
#   ./scripts/build-aur-repo.sh x86-64
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REPO_DIR="$PROJECT_ROOT/iso/airootfs/opt/arches-repo"
REPO_NAME="arches-local"
BUILD_DIR="/tmp/arches-aur-build"

PLATFORM="${1:-}"
if [[ -z "$PLATFORM" ]]; then
    echo "Usage: $0 <platform>"
    echo "Available platforms:"
    ls -1 "$PROJECT_ROOT/platforms/"
    exit 1
fi

PLATFORM_DIR="$PROJECT_ROOT/platforms/$PLATFORM"
if [[ ! -f "$PLATFORM_DIR/platform.toml" ]]; then
    echo "ERROR: Platform config not found: $PLATFORM_DIR/platform.toml"
    exit 1
fi

# Read AUR packages from platform.toml
# Parse the [aur_packages] build array from TOML
AUR_PACKAGES=()
in_aur=false
while IFS= read -r line; do
    if [[ "$line" =~ ^\[aur_packages\] ]]; then
        in_aur=true
        continue
    fi
    if [[ "$in_aur" == true && "$line" =~ ^\[ ]]; then
        break
    fi
    if [[ "$in_aur" == true && "$line" =~ \"([^\"]+)\" ]]; then
        AUR_PACKAGES+=("${BASH_REMATCH[1]}")
    fi
done < "$PLATFORM_DIR/platform.toml"

if [[ ${#AUR_PACKAGES[@]} -eq 0 ]]; then
    echo "No AUR packages defined for platform $PLATFORM. Skipping."
    exit 0
fi

echo "=== Arches AUR Repo Builder ==="
echo "Platform:   $PLATFORM"
echo "Build dir:  $BUILD_DIR"
echo "Repo dir:   $REPO_DIR"
echo "Packages:   ${AUR_PACKAGES[*]}"
echo ""

# Clean and prepare
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR" "$REPO_DIR"

# Build each AUR package
for pkg in "${AUR_PACKAGES[@]}"; do
    echo "── Building $pkg ──"
    cd "$BUILD_DIR"
    git clone "https://aur.archlinux.org/${pkg}.git"
    cd "$pkg"
    makepkg -s --noconfirm --needed
    cp ./*.pkg.tar.zst "$REPO_DIR/"
    echo "  Built: $pkg"
done

# Create/update the repo database
echo ""
echo "── Creating repo database ──"
cd "$REPO_DIR"
repo-add "${REPO_NAME}.db.tar.gz" ./*.pkg.tar.zst

echo ""
echo "=== AUR repo built at $REPO_DIR ==="
echo "Packages:"
ls -1 "$REPO_DIR"/*.pkg.tar.zst 2>/dev/null || echo "  (none)"
