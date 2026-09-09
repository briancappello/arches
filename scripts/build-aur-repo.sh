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
STAGE_DIR="$PROJECT_ROOT/iso/airootfs/opt/arches-repo"
REPO_NAME="arches-local"
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

ARCHES_PROVIDER=$(python3 "$SCRIPT_DIR/iso-config.py" provider-id "$PLATFORM_DIR/platform.toml")
export ARCHES_PROVIDER
export ARCHES_BUILD_PURPOSE="${ARCHES_BUILD_PURPOSE:-target}"
case "$ARCHES_BUILD_PURPOSE" in
    target) module_command=build-modules ;;
    live) module_command=live-build-modules ;;
    *) echo "ERROR: Unknown build purpose: $ARCHES_BUILD_PURPOSE"; exit 1 ;;
esac
case "$ARCHES_PROVIDER:$ARCHES_BUILD_PURPOSE" in
    arch:target|omarchy:target)
        echo "ERROR: Alternative provider custom builds require reviewed, authenticated provenance."
        exit 1 ;;
esac
# The live builder remains the platform default. Its binaries never enter an
# alternative target's signed cache, even when the target uses the same names.
selection=$(python3 "$SCRIPT_DIR/iso-config.py" build-selection "$MODULES_DIR")
CACHE_ROOT="$PROJECT_ROOT/.aur-repo/$PLATFORM/$ARCHES_PROVIDER"
echo "Scoped cache root: $CACHE_ROOT"

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
build_slugs=$(python3 "$SCRIPT_DIR/iso-config.py" "$module_command" "$MODULES_DIR")
while IFS= read -r slug; do
    [[ -n "$slug" ]] || continue
    build_script="$MODULES_DIR/$slug/build.sh"
    if [[ -f "$build_script" ]]; then
        MODULE_BUILD_SCRIPTS+=("$build_script")
    fi
done <<< "$build_slugs"

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
    mkdir -p "$CACHE_ROOT" "$STAGE_DIR"
    chown -R "$SUDO_USER":"$(id -g "$SUDO_USER")" "$CACHE_ROOT" "$STAGE_DIR"
    FORCE_FLAG=""
    [[ "$FORCE" == true ]] && FORCE_FLAG="--force"
    # Preserve env vars the child invocation needs: ARCHES_TEMPLATE
    # filters the build to a single template (so iso-config.py
    # build-modules returns only the relevant modules), ARCHES_GPU
    # selects the GPU compute stack (so only matching gpu-related
    # modules are built), and FORCE is picked up from the env in
    # addition to the flag. PATH is needed for makepkg to find tools.
    sudo -u "$SUDO_USER" --preserve-env=PATH,ARCHES_TEMPLATE,ARCHES_GPU,ARCHES_PROVIDER,ARCHES_BUILD_IMAGE,ARCHES_BUILD_PURPOSE,ISO_MODE,FORCE \
        "$0" $FORCE_FLAG "$PLATFORM"
    chown -R root:root "$CACHE_ROOT"
    chown -R root:root "$STAGE_DIR"
    exit 0
fi

# ponytail: generated PKGBUILDs prevent a complete upfront dependency query.
# Allow one same-container stabilization pass, then rebuild every output once.
for attempt in 1 2; do
    dependencies=$(pacman -Q)
    ARCHES_BUILD_IDENTITY=$(ARCHES_BUILD_IDENTITY= compute_cache_hash \
        "str:cache-format=verified-attempts-v2" \
        "str:provider=$ARCHES_PROVIDER" "str:platform=$PLATFORM" \
        "str:architecture=$(uname -m)" "str:channel=stable" \
        "str:purpose=$ARCHES_BUILD_PURPOSE" \
        "str:image=${ARCHES_BUILD_IMAGE:?Builder image identity is required}" \
        "str:dependencies=$dependencies" /var/lib/pacman/sync \
        "$PROJECT_ROOT/Containerfile" "$PLATFORM_DIR" "$PROJECT_ROOT/providers" \
        "$PROJECT_ROOT/modules" "$PROJECT_ROOT/templates" \
        "$SCRIPT_DIR/gpu-stacks.toml" "str:selection=$selection" \
        "str:gpu=${ARCHES_GPU:-}")
    export ARCHES_BUILD_IDENTITY
    CACHE_SCOPE="$CACHE_ROOT/$ARCHES_BUILD_IDENTITY"
    mkdir -p "$CACHE_SCOPE"
    ATTEMPT_DIR=$(mktemp -d "$CACHE_SCOPE/attempt-XXXXXX")
    REPO_DIR="$ATTEMPT_DIR/repo"
    BUILD_DIR="$ATTEMPT_DIR/build"
    mkdir -p "$BUILD_DIR" "$REPO_DIR"
    # Only completed canonical caches can seed pass one. Pass two is always fresh.
    if [[ "$attempt" == 1 && "$FORCE" == false && -d "$CACHE_SCOPE/repo" ]]; then
        cp -a --reflink=auto "$CACHE_SCOPE/repo/." "$REPO_DIR/"
    fi

    echo "=== Arches AUR Repo Builder (pass $attempt/2) ==="
    echo "Platform:       $PLATFORM"
    echo "Build dir:      $BUILD_DIR"
    echo "Repo dir:       $REPO_DIR"
    echo "AUR packages:   ${AUR_PACKAGES[*]:-(none)}"
    echo "Module builds:  ${MODULE_BUILD_SCRIPTS[*]:-(none)}"
    echo ""

    # 1. Build AUR packages from platform.toml.
    if [[ ${#AUR_PACKAGES[@]} -gt 0 ]]; then
        for pkg in "${AUR_PACKAGES[@]}"; do
            aur_dir="$BUILD_DIR/$pkg"
            git clone --depth 1 "https://aur.archlinux.org/${pkg}.git" "$aur_dir"
            hash=$(compute_cache_hash "str:PKGBUILD=$(<"$aur_dir/PKGBUILD")")

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
            echo "  Built: $pkg"
        done
    else
        echo "── No AUR packages for $PLATFORM — skipping AUR builds ──"
    fi

    # 2. Run the selected module build scripts with this attempt's paths.
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

    final_dependencies=$(pacman -Q)
    if [[ "$dependencies" != "$final_dependencies" ]]; then
        echo "Build dependencies changed; unverified outputs retained at $ATTEMPT_DIR"
        if [[ "$attempt" == 2 ]]; then
            echo "ERROR: Build environment did not stabilize within two passes."
            exit 1
        fi
        FORCE=true
        continue
    fi

    # 3. Generate the database only after the whole build has a stable ABI.
    if compgen -G "$REPO_DIR"/*.pkg.tar.* &>/dev/null; then
        echo ""
        echo "── Updating repo database ──"
        cd "$REPO_DIR"
        rm -f "${REPO_NAME}.db" "${REPO_NAME}.db.tar.gz" \
              "${REPO_NAME}.files" "${REPO_NAME}.files.tar.gz"
        repo-add "${REPO_NAME}.db.tar.gz" ./*.pkg.tar.*
        echo "=== AUR repo ready at $REPO_DIR ==="
        ls -1 "$REPO_DIR"/*.pkg.tar.*
    else
        echo "=== No packages to build — repo is empty ==="
    fi

    # Failure before this point leaves only an unpublishable attempt directory.
    if [[ -d "$CACHE_SCOPE/repo" ]]; then
        mv "$CACHE_SCOPE/repo" "$CACHE_SCOPE/repo.previous-$(date +%s%N)"
    fi
    mv "$REPO_DIR" "$CACHE_SCOPE/repo"
    REPO_DIR="$CACHE_SCOPE/repo"
    break
done

# Replace only this build stage; retain every source and provider cache scope.
mkdir -p "$STAGE_DIR"
find "$STAGE_DIR" -maxdepth 1 -type f -delete
find "$STAGE_DIR" -maxdepth 1 -type l -delete
cp -a "$REPO_DIR/." "$STAGE_DIR/"
