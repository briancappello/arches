#!/usr/bin/env bash
# Stage the arches-local public key into the ISO and refuse an unsigned
# repository (design D7). Called by the Makefile's _stage-platform target.
#
#   - /opt/arches/platform/arches-local.{asc,fpr}: the installer adds the
#     key to the target's pacman keyring.
#   - /usr/share/pacman/keyrings/arches{.gpg,-trusted}: the live ISO's
#     pacman-init populates its keyring from these.
#   - Run as root (inside the build container), it also trusts the key in
#     the build's own pacman keyring, so mkarchiso can verify arches-local.
#
# Usage: stage-repo-key.sh ISO_PROFILE_DIR
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_DIR="${ARCHES_REPO_KEY_DIR:-$PROJECT_ROOT/keys}"
PROFILE="${1:?usage: stage-repo-key.sh ISO_PROFILE_DIR}"
REPO_DIR="${ARCHES_REPO_DIR:-$PROFILE/airootfs/opt/arches-repo}"
REPO_NAME="arches-local"

die() {
	echo "ERROR: $*" >&2
	exit 1
}

[[ -f "$KEY_DIR/arches-local.asc" && -f "$KEY_DIR/arches-local.fpr" ]] ||
	die "arches-local is SigLevel = Required, but $KEY_DIR has no repository key. Create it on the YubiKey: keys/README.md"
fpr="$(tr -d ' \n' <"$KEY_DIR/arches-local.fpr" | tr '[:lower:]' '[:upper:]')"
[[ "$fpr" =~ ^[0-9A-F]{40}$ ]] || die "$KEY_DIR/arches-local.fpr is not a 40-hex fingerprint."

# Verify with a throwaway keyring that holds only the repository key.
home="$(mktemp -d)"
trap 'gpgconf --homedir "$home" --kill all >/dev/null 2>&1 || true; rm -rf "$home"' EXIT
gpg --homedir "$home" --batch --quiet --import "$KEY_DIR/arches-local.asc"
gpg --homedir "$home" --batch --with-colons --list-keys "$fpr" >/dev/null 2>&1 ||
	die "$KEY_DIR/arches-local.asc does not hold the key $fpr."

valid() {
	gpg --homedir "$home" --batch --status-fd 1 --verify "$1" "$2" 2>/dev/null |
		awk -v fpr="$fpr" '$2 == "VALIDSIG" && $NF == fpr { found = 1 } END { exit !found }'
}

bad=()
if [[ -d "$REPO_DIR" ]]; then
	while IFS= read -r f; do
		valid "$f.sig" "$f" || bad+=("${f##*/}")
	done < <(compgen -G "$REPO_DIR/*.pkg.tar.*" | grep -v '\.sig$' || true)
	if [[ -f "$REPO_DIR/$REPO_NAME.db.tar.gz" ]]; then
		valid "$REPO_DIR/$REPO_NAME.db.tar.gz.sig" "$REPO_DIR/$REPO_NAME.db.tar.gz" ||
			bad+=("$REPO_NAME.db.tar.gz")
	fi
fi
if ((${#bad[@]})); then
	printf '  not signed by %s: %s\n' "$fpr" "${bad[@]}" >&2
	die "arches-local is not signed. Sign it with the YubiKey: ./scripts/sign-local-repo.sh"
fi

platform_dir="$PROFILE/airootfs/opt/arches/platform"
keyrings="$PROFILE/airootfs/usr/share/pacman/keyrings"
mkdir -p "$platform_dir" "$keyrings"
cp "$KEY_DIR/arches-local.asc" "$platform_dir/arches-local.asc"
echo "$fpr" >"$platform_dir/arches-local.fpr"
cp "$KEY_DIR/arches-local.asc" "$keyrings/arches.gpg"
echo "$fpr:4:" >"$keyrings/arches-trusted"
echo "  arches-local key: $fpr (signed repository verified)"

if ((EUID == 0)) && [[ "${ARCHES_SKIP_BUILD_KEYRING:-}" != 1 ]]; then
	pacman-key --add "$KEY_DIR/arches-local.asc"
	pacman-key --lsign-key "$fpr"
fi
