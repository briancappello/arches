#!/usr/bin/env bash
#
# Build cache helpers for per-package skip-if-unchanged caching.
#
# Source this file from build-aur-repo.sh or module build.sh scripts:
#   source "$(dirname "${BASH_SOURCE[0]}")/../scripts/lib/build-cache.sh"
#
# Usage:
#   # Hash relevant source files for a package
#   hash=$(compute_cache_hash file1 file2 dir/)
#
#   # Check if we can skip building
#   if pkg_cache_hit "$REPO_DIR" "my-package" "$hash"; then
#       echo "Skipping my-package (cached)"
#   else
#       # ... build ...
#       save_cache_hash "$REPO_DIR" "my-package" "$hash"
#   fi
#

# compute_cache_hash [files_dirs_and_strings...]
#
# Compute a sha256 hash over the contents of the given files,
# directories, and literal strings.  Directories are hashed
# recursively (all file contents + relative paths, sorted for
# determinism).
#
# Prefix an argument with "str:" to hash it as a literal string
# rather than a file path (e.g., "str:v6.6.4" to include a version
# tag in the hash).
#
# Prints the hex digest to stdout.
compute_cache_hash() {
    {
        for arg in "$@"; do
            if [[ "$arg" == str:* ]]; then
                # Literal string — hash the value directly
                echo "STR:${arg#str:}"
            elif [[ -d "$arg" ]]; then
                # Hash all files in the directory, sorted by path
                find "$arg" -type f | sort | while IFS= read -r f; do
                    # Include relative path in hash so renames are detected
                    echo "FILE:$f"
                    sha256sum "$f" | cut -d' ' -f1
                done
            elif [[ -f "$arg" ]]; then
                echo "FILE:$arg"
                sha256sum "$arg" | cut -d' ' -f1
            else
                # Include missing paths in hash so additions are detected
                echo "MISSING:$arg"
            fi
        done
    } | sha256sum | cut -d' ' -f1
}

# pkg_cache_hit <repo_dir> <pkg_name> <hash>
#
# Returns 0 (true) if the package has already been built with the same
# inputs.  Checks that:
#   1. A .cachehash sidecar file exists with matching hash
#   2. At least one .pkg.tar.* file exists for this package
pkg_cache_hit() {
    local repo_dir="$1"
    local pkg_name="$2"
    local hash="$3"

    local sidecar="$repo_dir/${pkg_name}.cachehash"

    # Sidecar must exist and match
    if [[ ! -f "$sidecar" ]]; then
        return 1
    fi

    local cached_hash
    cached_hash=$(<"$sidecar")
    if [[ "$cached_hash" != "$hash" ]]; then
        return 1
    fi

    # At least one package file must exist
    if ! compgen -G "$repo_dir/${pkg_name}-*.pkg.tar.*" &>/dev/null; then
        return 1
    fi

    return 0
}

# save_cache_hash <repo_dir> <pkg_name> <hash>
#
# Write the cache hash sidecar file after a successful build.
save_cache_hash() {
    local repo_dir="$1"
    local pkg_name="$2"
    local hash="$3"

    echo "$hash" > "$repo_dir/${pkg_name}.cachehash"
}

# remove_stale_packages <repo_dir> <pkg_name>
#
# Remove old .pkg.tar.* files for a package before rebuilding.
# This prevents version conflicts in the repo (old and new versions
# of the same package coexisting).
remove_stale_packages() {
    local repo_dir="$1"
    local pkg_name="$2"

    local stale
    stale=$(compgen -G "$repo_dir/${pkg_name}-*.pkg.tar.*" 2>/dev/null || true)
    if [[ -n "$stale" ]]; then
        echo "  Removing stale packages for $pkg_name"
        rm -f "$repo_dir"/${pkg_name}-*.pkg.tar.*
    fi
}
