#!/usr/bin/env bash
#
# Build AUR packages and create a local pacman repository.
# This repo is embedded into the ISO so AUR packages can be
# installed offline during the install phase.
#
# Usage:
#   ./scripts/build-aur-repo.sh <platform>
#   ./scripts/build-aur-repo.sh x86-64
#   ./scripts/build-aur-repo.sh --force x86-64   # rebuild even if repo exists
#
# When run as root (via sudo), automatically drops privileges to
# SUDO_USER for makepkg, then fixes ownership afterward.
# Skips the build entirely if the repo is already populated (use --force).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REPO_DIR="$PROJECT_ROOT/iso/airootfs/opt/arches-repo"
REPO_NAME="arches-local"
BUILD_DIR="/tmp/arches-aur-build"

FORCE=false
PLATFORM=""
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=true ;;
        *)       PLATFORM="$arg" ;;
    esac
done

if [[ -z "$PLATFORM" ]]; then
    echo "Usage: $0 [--force] <platform>"
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

# Custom plasmoids built from local sibling repos.
# Each entry: local_dir:package_name
# Build deps beyond the base set are declared per-package below.
CUSTOM_CMAKE_PACKAGES=(
    "$PROJECT_ROOT/../kde-task-manager:arches-taskmanager-patched"
    "$PROJECT_ROOT/../plasma-ai-usage-monitor:plasma-ai-usage-monitor"
)

# Per-package extra makedepends (space-separated).
# The kde-task-manager build requires plasma-desktop sources at build time
# (fetched by its own build script), plus many KF6/Plasma dev libraries.
declare -A CUSTOM_EXTRA_MAKEDEPS=(
    ["arches-taskmanager-patched"]="git kconfig ki18n kio knotifications kservice kwindowsystem plasma-activities plasma-activities-stats libplasma libksysguard kitemmodels plasma-workspace"
    ["plasma-ai-usage-monitor"]="kwallet ki18n knotifications qt6-base"
)

# Per-package extra runtime depends (space-separated).
declare -A CUSTOM_EXTRA_DEPS=(
    ["arches-taskmanager-patched"]=""
    ["plasma-ai-usage-monitor"]="kwallet"
)

# ── Skip if repo already populated ────────────────────
# When called from `sudo make iso-x86-64`, avoid re-building packages that
# are already present.  Pass --force to rebuild regardless.
if [[ "$FORCE" == false && -f "$REPO_DIR/${REPO_NAME}.db.tar.gz" ]] \
   && compgen -G "$REPO_DIR"/*.pkg.tar.* &>/dev/null; then
    echo "=== AUR repo already populated at $REPO_DIR — skipping build ==="
    echo "    (use --force to rebuild)"
    ls -1 "$REPO_DIR"/*.pkg.tar.*
    exit 0
fi

# ── Privilege handling ────────────────────────────────
# makepkg refuses to run as root.  When invoked via sudo (e.g. from
# `sudo make iso-x86-64`), drop back to the invoking user for the
# actual build, then fix up ownership afterward.
if [[ $EUID -eq 0 ]]; then
    if [[ -z "${SUDO_USER:-}" || "${SUDO_USER:-}" == "root" ]]; then
        echo "ERROR: Running as root without SUDO_USER set."
        echo "       Run via sudo so the build can drop privileges:"
        echo "         sudo make iso-x86-64  (or iso-aarch64-generic)"
        echo "       Or build the AUR repo first as a normal user:"
        echo "         make aur-repo-x86-64  (or aur-repo-aarch64)"
        exit 1
    fi
    echo "  (running as root — will drop to $SUDO_USER for makepkg)"
    # Create repo dir as root (the unprivileged user may not be able to)
    mkdir -p "$REPO_DIR"
    chown "$SUDO_USER":"$(id -g "$SUDO_USER")" "$REPO_DIR"
    # Re-exec the entire script as the original user, preserving --force
    FORCE_FLAG=""
    [[ "$FORCE" == true ]] && FORCE_FLAG="--force"
    sudo -u "$SUDO_USER" --preserve-env=PATH \
        "$0" $FORCE_FLAG "$PLATFORM"
    # Fix ownership so root-owned mkarchiso can read everything
    chown -R root:root "$REPO_DIR"
    exit 0
fi

echo "=== Arches AUR Repo Builder ==="
echo "Platform:   $PLATFORM"
echo "Build dir:  $BUILD_DIR"
echo "Repo dir:   $REPO_DIR"
echo "AUR:        ${AUR_PACKAGES[*]:-(none)}"
echo "Custom:     ${CUSTOM_CMAKE_PACKAGES[*]}"
echo ""

# Clean and prepare
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR" "$REPO_DIR"

# --------------------------------------------------------------------------
# 1. Build AUR packages (skip if none defined for this platform)
# --------------------------------------------------------------------------
if [[ ${#AUR_PACKAGES[@]} -gt 0 ]]; then
    for pkg in "${AUR_PACKAGES[@]}"; do
        echo "── Building AUR: $pkg ──"
        cd "$BUILD_DIR"
        git clone "https://aur.archlinux.org/${pkg}.git"
        cd "$pkg"
        makepkg -s --noconfirm --needed
        cp ./*.pkg.tar.* "$REPO_DIR/"
        echo "  Built: $pkg"
    done
else
    echo "── No AUR packages for $PLATFORM — skipping AUR builds ──"
fi

# --------------------------------------------------------------------------
# 2. Build custom cmake plasmoids into pacman packages
# --------------------------------------------------------------------------
# We generate a minimal PKGBUILD for each, then run makepkg.

build_cmake_package() {
    local src_dir="$1"
    local pkg_name="$2"

    if [[ ! -d "$src_dir" ]]; then
        echo "ERROR: Source directory not found: $src_dir" >&2
        return 1
    fi

    local src_dir_abs
    src_dir_abs="$(cd "$src_dir" && pwd)"

    # Extract version from CMakeLists.txt (PROJECT_VERSION or project VERSION)
    local version
    version=$(grep -oP '(?:PROJECT_VERSION\s+"|project\([^)]*VERSION\s+)\K[0-9]+\.[0-9]+\.[0-9]+' \
              "$src_dir_abs/CMakeLists.txt" | head -1)
    version="${version:-0.0.1}"

    local pkg_build_dir="$BUILD_DIR/$pkg_name"
    mkdir -p "$pkg_build_dir"

    # Gather per-package deps
    local extra_makedeps="${CUSTOM_EXTRA_MAKEDEPS[$pkg_name]:-}"
    local extra_deps="${CUSTOM_EXTRA_DEPS[$pkg_name]:-}"
    local makedeps_str="'cmake' 'extra-cmake-modules' 'gcc' 'qt6-base' 'qt6-declarative'"
    local deps_str="'plasma-workspace'"
    for d in $extra_makedeps; do makedeps_str+=" '$d'"; done
    for d in $extra_deps; do deps_str+=" '$d'"; done

    # The kde-task-manager has its own build script that fetches upstream
    # sources and applies the patch. We call it, then package the result.
    # For other cmake packages, we do a standard cmake build.
    local is_taskmanager=false
    [[ "$pkg_name" == "arches-taskmanager-patched" ]] && is_taskmanager=true

    # Generate PKGBUILD
    if $is_taskmanager; then
        # The task manager build script produces a .so plugin. We need to
        # capture it and install it system-wide instead of to ~/.local.
        cat > "$pkg_build_dir/PKGBUILD" <<EOF
# Auto-generated by arches build-aur-repo.sh
pkgname=$pkg_name
pkgver=$version
pkgrel=1
pkgdesc="Patched KDE Task Manager — full-height launchers on multi-row panels"
arch=('x86_64' 'aarch64')
license=('GPL-3.0-or-later')
depends=($deps_str)
makedepends=($makedeps_str 'git')

build() {
    # Use the project's own CMakeLists.txt with source assembly
    local upstream_clone="/tmp/plasma-desktop-upstream-src"
    local upstream_tag="v\$pkgver"
    if [[ ! -d "\$upstream_clone" ]]; then
        git clone --depth 1 --branch "\$upstream_tag" --filter=blob:none --sparse \\
            "https://invent.kde.org/plasma/plasma-desktop" "\$upstream_clone"
        git -C "\$upstream_clone" sparse-checkout set applets/taskmanager kcms/recentFiles
    fi

    local qs="\$upstream_clone/applets/taskmanager"
    mkdir -p src/qml/code

    # C++ sources and resources from upstream
    for f in backend.cpp backend.h smartlauncherbackend.cpp smartlauncherbackend.h \\
              smartlauncheritem.cpp smartlauncheritem.h main.xml metadata.json; do
        cp "\$qs/\$f" src/
    done
    cp "\$upstream_clone/kcms/recentFiles/kactivitymanagerd_plugins_settings.kcfgc" src/
    cp "\$upstream_clone/kcms/recentFiles/kactivitymanagerd_plugins_settings.kcfg" src/

    # QML from upstream + our patch
    cp "\$qs"/qml/*.qml src/qml/
    cp "\$qs"/qml/code/*.js src/qml/code/
    cp "$src_dir_abs/CMakeLists.txt" src/CMakeLists.txt
    patch -p1 -d src < "$src_dir_abs/fullheight-launchers.patch"

    cmake -S src -B build \\
        -DCMAKE_BUILD_TYPE=Release \\
        -DCMAKE_INSTALL_PREFIX=/usr \\
        -DBUILD_TESTING=OFF
    cmake --build build --parallel
}

package() {
    DESTDIR="\$pkgdir" cmake --install build
}
EOF
    else
        cat > "$pkg_build_dir/PKGBUILD" <<EOF
# Auto-generated by arches build-aur-repo.sh
pkgname=$pkg_name
pkgver=$version
pkgrel=1
pkgdesc="Custom Arches plasmoid: $pkg_name"
arch=('x86_64' 'aarch64')
license=('GPL-3.0-or-later')
depends=($deps_str)
makedepends=($makedeps_str)

build() {
    cmake -S "$src_dir_abs" -B build \\
        -DCMAKE_BUILD_TYPE=Release \\
        -DCMAKE_INSTALL_PREFIX=/usr \\
        -DBUILD_TESTING=OFF
    cmake --build build --parallel
}

package() {
    DESTDIR="\$pkgdir" cmake --install build
}
EOF
    fi

    echo "── Building custom: $pkg_name ($version) ──"
    cd "$pkg_build_dir"
    makepkg -s --noconfirm --skipchecksums
    cp ./*.pkg.tar.* "$REPO_DIR/"
    echo "  Built: $pkg_name"
}

for entry in "${CUSTOM_CMAKE_PACKAGES[@]}"; do
    IFS=':' read -r src_dir pkg_name <<< "$entry"
    build_cmake_package "$src_dir" "$pkg_name"
done

# --------------------------------------------------------------------------
# 3. Create/update the repo database
# --------------------------------------------------------------------------
echo ""
echo "── Creating repo database ──"
cd "$REPO_DIR"
repo-add "${REPO_NAME}.db.tar.gz" ./*.pkg.tar.*

echo ""
echo "=== AUR repo built at $REPO_DIR ==="
echo "Packages:"
ls -1 "$REPO_DIR"/*.pkg.tar.* 2>/dev/null || echo "  (none)"
