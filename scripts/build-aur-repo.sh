#!/usr/bin/env bash
#
# Build AUR packages and module custom packages, then create a local
# pacman repository.  This repo is embedded into the ISO so custom
# packages can be installed offline during the install phase.
#
# Usage:
#   ./scripts/build-aur-repo.sh <platform>
#   ./scripts/build-aur-repo.sh x86-64
#   ./scripts/build-aur-repo.sh --force x86-64   # rebuild even if repo exists
#
# Package sources:
#   1. Platform AUR packages — declared in platform.toml [aur_packages]
#   2. Module custom packages — each module may have a build.sh script
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
MODULES_DIR="$PROJECT_ROOT/modules"

# Cache helpers for per-package skip-if-unchanged
source "$SCRIPT_DIR/lib/build-cache.sh"

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

# Discover module build scripts — only for modules that are included in
# templates listed in iso.toml.  This avoids building custom packages
# for modules that won't be installed (e.g., KDE packages for a
# headless-only ISO).
MODULE_BUILD_SCRIPTS=()
while IFS= read -r slug; do
    [[ -n "$slug" ]] || continue
    build_script="$MODULES_DIR/$slug/build.sh"
    if [[ -f "$build_script" ]]; then
        MODULE_BUILD_SCRIPTS+=("$build_script")
    fi
done < <(python3 "$SCRIPT_DIR/iso-config.py" build-modules "$MODULES_DIR")

# Export FORCE so module build scripts can check it
export FORCE

# ── Privilege handling ────────────────────────────────
if [[ $EUID -eq 0 ]]; then
    if [[ -z "${SUDO_USER:-}" || "${SUDO_USER:-}" == "root" ]]; then
        echo "ERROR: Running as root without SUDO_USER set."
        echo "       Run via sudo so the build can drop privileges:"
        echo "         sudo make iso"
        exit 1
    fi
    echo "  (running as root — will drop to $SUDO_USER for makepkg)"
    mkdir -p "$REPO_DIR"
    chown -R "$SUDO_USER":"$(id -g "$SUDO_USER")" "$REPO_DIR"
    FORCE_FLAG=""
    [[ "$FORCE" == true ]] && FORCE_FLAG="--force"
    # Preserve env vars the child invocation needs: ARCHES_TEMPLATE
    # filters the build to a single template (so iso-config.py
    # build-modules returns only the relevant modules), ARCHES_GPU
    # selects the GPU compute stack (so only matching gpu-related
    # modules are built), and FORCE is picked up from the env in
    # addition to the flag. PATH is needed for makepkg to find tools.
    sudo -u "$SUDO_USER" --preserve-env=PATH,ARCHES_TEMPLATE,ARCHES_GPU,FORCE \
        "$0" $FORCE_FLAG "$PLATFORM"
    chown -R root:root "$REPO_DIR"
    exit 0
fi

echo "=== Arches AUR Repo Builder ==="
echo "Platform:       $PLATFORM"
echo "Build dir:      $BUILD_DIR"
echo "Repo dir:       $REPO_DIR"
echo "AUR packages:   ${AUR_PACKAGES[*]:-(none)}"
echo "Module builds:  ${MODULE_BUILD_SCRIPTS[*]:-(none)}"
echo ""

# Prepare
mkdir -p "$BUILD_DIR" "$REPO_DIR"

REBUILD_REPO=false

# --------------------------------------------------------------------------
# 1. Build AUR packages (from platform.toml)
# --------------------------------------------------------------------------
if [[ ${#AUR_PACKAGES[@]} -gt 0 ]]; then
    for pkg in "${AUR_PACKAGES[@]}"; do
        # Shallow-clone to check PKGBUILD, then hash it for caching
        aur_dir="$BUILD_DIR/$pkg"
        if [[ -d "$aur_dir" ]]; then
            git -C "$aur_dir" pull --ff-only --quiet 2>/dev/null || true
        else
            git clone --depth 1 "https://aur.archlinux.org/${pkg}.git" "$aur_dir"
        fi

        hash=$(compute_cache_hash "$aur_dir/PKGBUILD")

        if [[ "$FORCE" == false ]] && pkg_cache_hit "$REPO_DIR" "$pkg" "$hash"; then
            echo "── AUR: $pkg (cached, skipping) ──"
            continue
        fi

        echo "── Building AUR: $pkg ──"
        remove_stale_packages "$REPO_DIR" "$pkg"
        cd "$aur_dir"
        makepkg -sf --noconfirm --needed --cleanbuild
        cp ./*.pkg.tar.* "$REPO_DIR/"
        save_cache_hash "$REPO_DIR" "$pkg" "$hash"
        REBUILD_REPO=true
        echo "  Built: $pkg"
    done
else
    echo "── No AUR packages for $PLATFORM — skipping AUR builds ──"
fi

# --------------------------------------------------------------------------
# 2. Run module build scripts
# --------------------------------------------------------------------------
# Each module may have a build.sh that builds custom packages.
# We pass REPO_DIR, BUILD_DIR, PLATFORM, and the cache library path
# as environment variables.

export ARCHES_BUILD_CACHE_LIB="$SCRIPT_DIR/lib/build-cache.sh"

if [[ ${#MODULE_BUILD_SCRIPTS[@]} -gt 0 ]]; then
    for build_script in "${MODULE_BUILD_SCRIPTS[@]}"; do
        module_slug="$(basename "$(dirname "$build_script")")"
        echo ""
        echo "── Running module build: $module_slug ──"
        REPO_DIR="$REPO_DIR" BUILD_DIR="$BUILD_DIR" PLATFORM="$PLATFORM" \
            bash "$build_script"
    done
else
    echo "── No module build scripts found — skipping module builds ──"
fi

# --------------------------------------------------------------------------
# 3. Create/update the repo database
# --------------------------------------------------------------------------
if compgen -G "$REPO_DIR"/*.pkg.tar.* &>/dev/null; then
    echo ""
    echo "── Updating repo database ──"
    cd "$REPO_DIR"
    # Remove old db and recreate to ensure consistency
    rm -f "${REPO_NAME}.db" "${REPO_NAME}.db.tar.gz" \
          "${REPO_NAME}.files" "${REPO_NAME}.files.tar.gz"
    repo-add "${REPO_NAME}.db.tar.gz" ./*.pkg.tar.*

    echo ""
    echo "=== AUR repo ready at $REPO_DIR ==="
    echo "Packages:"
    ls -1 "$REPO_DIR"/*.pkg.tar.*
else
    echo ""
    echo "=== No packages to build — repo is empty ==="
fi
