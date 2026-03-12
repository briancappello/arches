#!/usr/bin/env bash
#
# Build AUR packages and create a local pacman repository.
# This repo is embedded into the ISO so AUR packages can be
# installed offline during the install phase.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REPO_DIR="$PROJECT_ROOT/iso/airootfs/opt/arches-repo"
REPO_NAME="arches-local"
BUILD_DIR="/tmp/arches-aur-build"

# AUR packages to pre-build
AUR_PACKAGES=(
    limine-snapper-sync
)

echo "=== Arches AUR Repo Builder ==="
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
