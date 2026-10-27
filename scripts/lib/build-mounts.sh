#!/usr/bin/env bash
# Resolve the host source directories that modules/*/build.mounts names.
#
# build.mounts paths are relative to the project root, e.g. ../kde-kwin.
# The sibling checkouts live next to the MAIN checkout (~/dev/arches ->
# ~/dev/kde-kwin), so from a worktree such as
# ~/dev/arches-worktrees/<name> the project-relative path misses them.
#
# Lookup order for a relative path REL:
#   1. $ARCHES_SOURCES_ROOT/REL   (explicit override)
#   2. <main checkout>/REL        (git rev-parse --git-common-dir)
#   3. <project root>/REL         (a plain checkout, or inside the builder)
#
# Usage: source this file, then: resolve_build_mount PROJECT_DIR REL

# Print the main checkout's root for PROJECT_DIR, or nothing.
main_checkout_root() {
	local project_dir="$1" common
	common="$(git -C "$project_dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || return 0
	[[ -d "$common" ]] || return 0
	# The common dir of a normal repository is <root>/.git.
	[[ "$(basename "$common")" == .git ]] && dirname "$common"
}

resolve_build_mount() {
	local project_dir="$1" rel="$2" base candidate
	for base in "${ARCHES_SOURCES_ROOT:-}" "$(main_checkout_root "$project_dir")" "$project_dir"; do
		[[ -n "$base" ]] || continue
		candidate="$base/$rel"
		if [[ -d "$candidate" ]]; then
			(cd "$candidate" && pwd)
			return 0
		fi
	done
	# Not found anywhere: the project-relative path, for the caller's message.
	echo "$project_dir/$rel"
	return 1
}
