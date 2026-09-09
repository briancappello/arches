#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# API setup and offline_mode captured from arch-install-scripts 31-2 /usr/bin/arch-chroot.
# mount, umount and chroot below are test doubles. No real mount or chroot runs.

ignore_error() {
  "$@" 2>/dev/null
  return 0
}

chroot_add_mount() {
  mount "$@" && CHROOT_ACTIVE_MOUNTS=("$2" "${CHROOT_ACTIVE_MOUNTS[@]}")
}

chroot_maybe_add_mount() {
  local cond=$1; shift
  if eval "$cond"; then
    chroot_add_mount "$@"
  fi
}

chroot_init() {
  CHROOT_ACTIVE_MOUNTS=()
  CHROOT_ACTIVE_LAZY=()
  CHROOT_ACTIVE_FILES=()
  [[ $(trap -p EXIT) ]] && die '(BUG): attempting to overwrite existing EXIT trap'
  trap 'chroot_teardown' EXIT
}

chroot_teardown() {
  if (( ${#CHROOT_ACTIVE_MOUNTS[@]} )); then
    umount "${CHROOT_ACTIVE_MOUNTS[@]}"
  fi
  unset CHROOT_ACTIVE_MOUNTS

  if (( ${#CHROOT_ACTIVE_LAZY[@]} )); then
    umount --lazy "${CHROOT_ACTIVE_LAZY[@]}"
  fi
  unset CHROOT_ACTIVE_LAZY

  if (( ${#CHROOT_ACTIVE_FILES[@]} )); then
    rm "${CHROOT_ACTIVE_FILES[@]}"
  fi
  unset CHROOT_ACTIVE_FILES
}

chroot_setup() {
  chroot_add_mount proc "$1/proc" -t proc -o nosuid,noexec,nodev &&
  chroot_add_mount sys "$1/sys" -t sysfs -o nosuid,noexec,nodev,ro &&
  ignore_error chroot_maybe_add_mount "[[ -d '$1/sys/firmware/efi/efivars' ]]" \
      efivarfs "$1/sys/firmware/efi/efivars" -t efivarfs -o nosuid,noexec,nodev &&
  chroot_add_mount udev "$1/dev" -t devtmpfs -o mode=0755,nosuid &&
  chroot_add_mount devpts "$1/dev/pts" -t devpts -o mode=0620,gid=5,nosuid,noexec &&
  chroot_add_mount shm "$1/dev/shm" -t tmpfs -o mode=1777,nosuid,nodev &&
  chroot_add_mount /run "$1/run" --bind --make-private &&
  chroot_add_mount tmp "$1/tmp" -t tmpfs -o mode=1777,strictatime,nodev,nosuid
}

offline_mode() {
  local chroot_args=()

  $setup "$chrootdir" || die "failed to setup chroot %s" "$chrootdir"

  [[ $userspec ]] && chroot_args+=(--userspec "$userspec")

  SHELL=/bin/bash SYSTEMD_IN_CHROOT=1 $pid_unshare chroot "${chroot_args[@]}" -- "$chrootdir" "${args[@]}"
}

# Test-only syscall boundary and simulated package hook.
declare -A mounted=()
mount() { mounted["$2"]="$*"; printf 'MOUNT %s\n' "$*"; }
umount() { for path in "$@"; do unset 'mounted[$path]'; printf 'UMOUNT %s\n' "$path"; done; }
die() { printf '%s\n' "$*" >&2; exit 1; }
chroot() {
  [[ $1 == -- && $2 == "$chrootdir" ]] || exit 70
  shift 2
  [[ $1 == pacman ]] || die 'Unstubbed chroot test command'
  "$@"
}
pacman() {
  local path
  for path in proc sys dev dev/pts dev/shm run tmp; do
    [[ -n ${mounted["$TEST_ROOT/$path"]} ]] || { printf 'HOOK_MISSING_API %s\n' "$path"; return 71; }
  done
  [[ $SYSTEMD_IN_CHROOT == 1 ]] || return 72
  printf 'HOOK_API_OK\n'
  return "${HOOK_EXIT:-0}"
}

if [[ $1 == arch-chroot ]]; then
  shift
  chrootdir=$1
  shift
  args=("$@")
  setup=chroot_setup
  userspec=
  pid_unshare=
  chroot_init
  offline_mode
else
  [[ $1 == pacman ]] || die 'Unstubbed test command'
  "$@"
fi
