#!/usr/bin/env bash
#
# Remove Arches subvolumes and GRUB entry created by host-install.
#
# This deletes the @arches, @arches-home, and @arches-var subvolumes
# (or whatever prefix was used) from the btrfs partition, and removes
# the host GRUB entry. The existing OS is not affected.
#
# Usage:
#   sudo ./scripts/host-clean.sh examples/host-install.toml
#   sudo make host-clean CONFIG=examples/host-install.toml
#
set -euo pipefail

CONFIG_FILE="${1:-}"

if [[ -z "$CONFIG_FILE" ]]; then
    echo "Usage: $0 <config.toml>"
    echo ""
    echo "Remove Arches subvolumes and GRUB entry created by host-install."
    echo "Uses the same config file as host-install.sh to determine which"
    echo "partition and subvolume prefix to clean."
    exit 1
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: Config file not found: $CONFIG_FILE"
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Must be run as root."
    echo "       Run: sudo $0 $*"
    exit 1
fi

# ── Parse config ──────────────────────────────────────
PARTITION=$(grep '^partition' "$CONFIG_FILE" | sed 's/.*= *"\?\([^"]*\)"\?/\1/' | tr -d ' ')
SUBVOL_PREFIX=$(grep '^subvol_prefix' "$CONFIG_FILE" 2>/dev/null | sed 's/.*= *"\?\([^"]*\)"\?/\1/' | tr -d ' ' || true)
SUBVOL_PREFIX="${SUBVOL_PREFIX:-@arches}"

if [[ -z "$PARTITION" ]]; then
    echo "ERROR: Could not read 'partition' from config."
    exit 1
fi

SUBVOLS=(
    "$SUBVOL_PREFIX"
    "${SUBVOL_PREFIX}-home"
    "${SUBVOL_PREFIX}-var"
)

echo "══ Arches Host Clean ══"
echo "  Partition:  $PARTITION"
echo "  Subvolumes: ${SUBVOLS[*]}"
echo ""

# ── Unmount anything under /mnt ───────────────────────
echo "Unmounting /mnt..."
for mp in $(awk '{print $2}' /proc/mounts | grep '^/mnt' | sort -r); do
    umount "$mp" 2>/dev/null || true
done

# ── Delete subvolumes ─────────────────────────────────
TOP_MNT=$(mktemp -d)
mount -o subvolid=5 "$PARTITION" "$TOP_MNT"

deleted=0
for subvol in "${SUBVOLS[@]}"; do
    subvol_path="$TOP_MNT/$subvol"
    if [[ -d "$subvol_path" ]]; then
        echo "Deleting subvolume: $subvol"
        # Delete any nested subvolumes first (e.g., snapper snapshots)
        btrfs subvolume list -o "$subvol_path" 2>/dev/null | awk '{print $NF}' | sort -r | while read -r nested; do
            nested_path="$TOP_MNT/$nested"
            if [[ -d "$nested_path" ]]; then
                btrfs subvolume delete "$nested_path" 2>/dev/null || true
            fi
        done
        btrfs subvolume delete "$subvol_path"
        ((deleted++)) || true
    else
        echo "  $subvol — not found, skipping"
    fi
done

umount "$TOP_MNT"
rmdir "$TOP_MNT"

# ── Remove GRUB entry ────────────────────────────────
if [[ -f /etc/grub.d/41_arches ]]; then
    echo "Removing GRUB entry: /etc/grub.d/41_arches"
    rm -f /etc/grub.d/41_arches
    if command -v grub2-mkconfig &>/dev/null; then
        grub2-mkconfig -o /boot/grub2/grub.cfg 2>/dev/null && echo "Regenerated /boot/grub2/grub.cfg"
    elif command -v grub-mkconfig &>/dev/null; then
        grub-mkconfig -o /boot/grub/grub.cfg 2>/dev/null && echo "Regenerated /boot/grub/grub.cfg"
    fi
fi

echo ""
if [[ $deleted -gt 0 ]]; then
    echo "Cleaned $deleted subvolume(s). Disk space reclaimed."
else
    echo "No subvolumes found to clean."
fi
echo "Done."
