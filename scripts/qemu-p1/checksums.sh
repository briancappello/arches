#!/usr/bin/env bash
# Inside the live ISO (TEST VM): checksum everything the install must not
# change. Raw partitions other than the spare pair and the shared ESP, the
# partition tables, and every file on the shared ESP that is not ours.
#
# Disks are named by serial, not /dev/nvmeXn1: the kernel does not keep
# NVMe names stable across boots (QEMU swapped them, 2026-10-27).
#
# Usage: checksums.sh > before.txt ; ... ; checksums.sh > after.txt ; diff
set -euo pipefail

mapfile -t disks < <(lsblk -dn -o PATH,SERIAL,TRAN | awk '$3 == "nvme" { print $1 " " $2 }')
shared_esp=""
for entry in "${disks[@]}"; do
	dev="${entry% *}" serial="${entry#* }"
	# Partition table without the disk's name.
	echo "table $serial $(sfdisk -d "$dev" | grep -v -e '^last-lba' -e '^device:' | sed "s|$dev||g" | sha256sum | cut -d' ' -f1)"
	for n in 1 2 4; do
		part="${dev}p$n"
		if [[ $n == 1 ]]; then
			mkdir -p /tmp/esp-probe
			mount -o ro "$part" /tmp/esp-probe
			has_conf=0
			[[ -f /tmp/esp-probe/limine.conf ]] && has_conf=1
			umount /tmp/esp-probe
			if ((has_conf)); then
				shared_esp="$part"
				continue
			fi
		fi
		echo "raw $serial-p$n $(sha256sum <"$part" | cut -d' ' -f1)"
	done
done

# Shared ESP: files of the old system and the shared loader, not ours.
mkdir -p /tmp/esp-ro
mount -o ro "$shared_esp" /tmp/esp-ro
(
	cd /tmp/esp-ro
	find . -type f ! -path './EFI/Linux/arches_*' ! -name 'limine.conf*' \
		! -path './arches-freeze-*' -print0 | sort -z | xargs -0 sha256sum | sed 's/^/esp /'
)
umount /tmp/esp-ro
