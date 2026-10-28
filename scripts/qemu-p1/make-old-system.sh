#!/usr/bin/env bash
# Inside the live ISO, as root, on the TEST VM ONLY: give both NVMe disks
# p1's partition structure and install a small "current system" the way p1
# has it (spec "Facts"):
#
#   p1 ESP (vfat, PARTLABEL ESP)        nvme1n1p1 is /boot, Limine + limine.conf
#   p2 archroot  btrfs RAID0 over both disks, subvol @  -> /
#   p3 spare     raw, no signature                      -> the install target
#   p4 archhome  btrfs RAID0 over both disks            -> /home
#
# The old system runs limine-entry-tool (hashed linux-protocol entries,
# machine-id keyed), limine-snapper-sync, and the limine reset/enroll hooks,
# like p1. Its console is the serial port and root's password is "old".
#
# Sizes come from ESP_SIZE, ROOT_SIZE, SPARE_SIZE (archhome takes the rest).
set -euo pipefail

[[ "$(systemd-detect-virt --vm)" =~ ^(kvm|qemu)$ ]] || { echo "refusing: not a VM" >&2; exit 1; }
[[ "$(cat /sys/class/dmi/id/sys_vendor)" == QEMU ]] || { echo "refusing: not QEMU" >&2; exit 1; }

ESP_SIZE="${ESP_SIZE:-2G}"
ROOT_SIZE="${ROOT_SIZE:-8G}"
SPARE_SIZE="${SPARE_SIZE:-24G}"
D0=/dev/nvme0n1
D1=/dev/nvme1n1
MNT=/mnt/old

for d in "$D0" "$D1"; do
	sgdisk --zap-all "$d" >/dev/null
	sgdisk -n "1:0:+$ESP_SIZE" -t 1:EF00 -c 1:ESP \
		-n "2:0:+$ROOT_SIZE" -t 2:8300 -c 2:archroot \
		-n "3:0:+$SPARE_SIZE" -t 3:8300 -c 3:spare \
		-n 4:0:0 -t 4:8300 -c 4:archhome "$d" >/dev/null
done
partprobe "$D0" "$D1"
udevadm settle
for d in "$D0" "$D1"; do wipefs -a "${d}p3" >/dev/null; done

mkfs.fat -F 32 -n ESP "${D0}p1" >/dev/null
mkfs.fat -F 32 -n ESP "${D1}p1" >/dev/null
mkfs.btrfs -f -L archroot -d raid0 -m raid0 "${D0}p2" "${D1}p2" >/dev/null
mkfs.btrfs -f -L archhome -d raid0 -m raid0 "${D0}p4" "${D1}p4" >/dev/null
udevadm settle

mkdir -p "$MNT"
mount "${D0}p2" "$MNT"
btrfs subvolume create "$MNT/@" >/dev/null
umount "$MNT"
mount -o subvol=@ "${D0}p2" "$MNT"
mkdir -p "$MNT/boot" "$MNT/home"
mount "${D1}p1" "$MNT/boot"
mount "${D0}p4" "$MNT/home"

# The installer's own offline pacman.conf (ISO cache + local mirror).
conf="$(cd /opt/arches/installer && python -c '
from arches_installer.core import install
from pathlib import Path
install.MOUNT_ROOT = Path("/mnt/old")
print(install._make_pacman_conf_with_cache())')"

pacstrap -C "$conf" -c -K "$MNT" base linux-cachyos mkinitcpio btrfs-progs \
	limine limine-mkinitcpio-hook limine-snapper-sync snapper efibootmgr openssh >/dev/null

genfstab -U "$MNT" >"$MNT/etc/fstab"
echo oldsys >"$MNT/etc/hostname"
echo root:old | arch-chroot "$MNT" chpasswd
arch-chroot "$MNT" systemctl enable sshd >/dev/null 2>&1
install -d -m 700 "$MNT/root/.ssh"
[[ -f /root/.ssh/authorized_keys ]] && install -m 600 /root/.ssh/authorized_keys "$MNT/root/.ssh/"

uuid="$(blkid -s UUID -o value "${D0}p2")"
cat >"$MNT/etc/default/limine" <<EOF
ESP_PATH="/boot"
KERNEL_CMDLINE[default]+="root=UUID=$uuid rw rootflags=subvol=/@ console=tty0 console=ttyS0,115200"
BOOT_ORDER="*, *fallback, Snapshots"
EOF
echo "root=UUID=$uuid rw rootflags=subvol=/@ console=tty0 console=ttyS0,115200" >"$MNT/etc/kernel/cmdline"
arch-chroot "$MNT" limine-install >/dev/null
arch-chroot "$MNT" limine-mkinitcpio >/dev/null
# limine-entry-tool's header already says hash_mismatch_panic: no, as p1's.

# snapper + limine-snapper-sync, as on p1
arch-chroot "$MNT" snapper --no-dbus create-config / >/dev/null 2>&1 || true
arch-chroot "$MNT" systemctl enable limine-snapper-sync.service >/dev/null 2>&1

# Data to checksum: the install must not change any of it.
head -c 64M /dev/urandom >"$MNT/root/old-data.bin"
head -c 64M /dev/urandom >"$MNT/home/old-home-data.bin"
mkdir -p /tmp/esp0 && mount "${D0}p1" /tmp/esp0 && head -c 8M /dev/urandom >/tmp/esp0/other-esp-data.bin && umount /tmp/esp0

machine_id="$(cat "$MNT/etc/machine-id")"
umount -R "$MNT"
echo "old system ready: machine-id $machine_id, root UUID $uuid"
