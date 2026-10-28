#!/usr/bin/env bash
# Sign the arches-local repository with the YubiKey (design D7).
#
# Operator step, on the build host, between building and installing:
#
#   ./scripts/build-aur-repo.sh x86-64      # unsigned; agents may run this
#   ./scripts/sign-local-repo.sh            # you, with the YubiKey (touch per signature)
#   ./scripts/builder.sh build              # the ISO trusts only signed packages
#
# It signs every package that has no valid signature from the repository
# key, then signs the repository database. The key is the one named in
# keys/arches-local.fpr, and its signing key must live on a smartcard:
# a software key is refused (ARCHES_ALLOW_SOFTWARE_KEY=1 is for tests).
#
# Usage: sign-local-repo.sh [REPO_DIR]
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_DIR="${ARCHES_REPO_KEY_DIR:-$PROJECT_ROOT/keys}"
REPO_DIR="${1:-$PROJECT_ROOT/iso/airootfs/opt/arches-repo}"
REPO_NAME="arches-local"

die() {
	echo "sign-local-repo: $*" >&2
	exit 1
}

[[ -f "$KEY_DIR/arches-local.fpr" ]] ||
	die "no $KEY_DIR/arches-local.fpr. Generate the key on the YubiKey first (keys/README.md)."
fpr="$(tr -d ' \n' <"$KEY_DIR/arches-local.fpr" | tr '[:lower:]' '[:upper:]')"
[[ "$fpr" =~ ^[0-9A-F]{40}$ ]] || die "$KEY_DIR/arches-local.fpr is not a 40-hex fingerprint."

# The key that signs must be on a card: gpg puts the card's serial number
# in field 15 of the signing (sub)key's colon record ("+" means a software
# key, "#" a key that is not available).
if [[ "${ARCHES_ALLOW_SOFTWARE_KEY:-}" != 1 ]]; then
	if ! gpg --batch --with-colons --list-secret-keys "$fpr" 2>/dev/null |
		awk -F: '($1 == "sec" || $1 == "ssb") && $12 ~ /s/ && $15 != "" && $15 != "+" && $15 != "#" { found = 1 } END { exit !found }'; then
		die "the signing key for $fpr is not on a smartcard (insert the YubiKey)."
	fi
fi

[[ -d "$REPO_DIR" ]] || die "no repository at $REPO_DIR."

signed_by_us() {
	gpg --batch --status-fd 1 --verify "$1" "$2" 2>/dev/null |
		awk -v fpr="$fpr" '$2 == "VALIDSIG" && $NF == fpr { found = 1 } END { exit !found }'
}

mapfile -t pkgs < <(compgen -G "$REPO_DIR/*.pkg.tar.*" | grep -v '\.sig$' || true)
((${#pkgs[@]})) || die "no packages in $REPO_DIR."

for pkg in "${pkgs[@]}"; do
	if [[ -f "$pkg.sig" ]] && signed_by_us "$pkg.sig" "$pkg"; then
		echo "already signed: ${pkg##*/}"
		continue
	fi
	echo "signing: ${pkg##*/}"
	gpg --batch --yes --local-user "$fpr" --detach-sign --no-armor --output "$pkg.sig" "$pkg"
	signed_by_us "$pkg.sig" "$pkg" || die "signature check failed for $pkg"
done

(
	cd "$REPO_DIR"
	rm -f "$REPO_NAME".db* "$REPO_NAME".files*
	repo-add --sign --key "$fpr" "$REPO_NAME.db.tar.gz" "${pkgs[@]##*/}"
)
signed_by_us "$REPO_DIR/$REPO_NAME.db.tar.gz.sig" "$REPO_DIR/$REPO_NAME.db.tar.gz" ||
	die "the repository database signature does not verify."

echo "arches-local signed with $fpr: ${#pkgs[@]} package(s) and the database."
