#!/usr/bin/env bash
# A QEMU stand-in for p1 (spec Validation 1): two NVMe disks with p1's
# partition structure, OVMF with Secure Boot (starts in Setup Mode), and a
# software TPM 2.0 (swtpm). Runs as the user; needs /dev/kvm.
#
#   vm.sh disks                 create the two disks (sparse qcow2)
#   vm.sh start [ISO]           boot (from ISO if given); returns at once
#   vm.sh stop                  power off (QMP quit) and stop swtpm
#   vm.sh key KEY...            press keys (QMP send-key: ret, down, a, shift-a)
#   vm.sh type TEXT             type text on the VGA console
#   vm.sh shot FILE.png         screenshot of the VGA console
#   vm.sh serial TEXT           send TEXT + newline to the serial console
#   vm.sh snap NAME | restore NAME   copy / put back disks, OVMF vars, TPM state
#   vm.sh ssh CMD...            ssh to the guest (port 2222)
#
# State lives in $P1VM_DIR (default ~/.cache/arches-p1vm): disks, OVMF vars,
# TPM state, serial.log, PID files. Every process is tracked by its PID.
set -euo pipefail

# Not under /tmp: on tmpfs the qcow2 disks fill RAM and the guest sees write faults.
DIR="${P1VM_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/arches-p1vm}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVMF_CODE=/usr/share/edk2/x64/OVMF_CODE.secboot.4m.fd
OVMF_VARS_TEMPLATE=/usr/share/edk2/x64/OVMF_VARS.4m.fd
DISK_SIZE="${P1VM_DISK_SIZE:-40G}"
MEM="${P1VM_MEM:-8G}"
SMP="${P1VM_SMP:-6}"
SSH_PORT="${P1VM_SSH_PORT:-2222}"

mkdir -p "$DIR"
qmp() { uv run --no-project python "$HERE/qmp.py" "$DIR/qmp.sock" "$@"; }

alive() { [[ -f "$DIR/$1.pid" ]] && kill -0 "$(cat "$DIR/$1.pid")" 2>/dev/null; }

case "${1:-}" in
disks)
	for d in disk0 disk1; do
		qemu-img create -f qcow2 "$DIR/$d.qcow2" "$DISK_SIZE" >/dev/null
	done
	cp "$OVMF_VARS_TEMPLATE" "$DIR/OVMF_VARS.fd"
	rm -rf "$DIR/tpm" && mkdir -p "$DIR/tpm"
	echo "created $DIR/disk{0,1}.qcow2 ($DISK_SIZE), fresh OVMF vars (Setup Mode), empty TPM"
	;;
start)
	iso="${2:-}"
	alive qemu && { echo "already running"; exit 1; }
	if ! alive swtpm; then
		swtpm socket --tpm2 --tpmstate dir="$DIR/tpm" \
			--ctrl type=unixio,path="$DIR/swtpm.sock" \
			--flags startup-clear --daemon --pid file="$DIR/swtpm.pid"
	fi
	args=(
		-name p1test -machine q35,smm=on,accel=kvm -cpu host -m "$MEM" -smp "$SMP"
		-global driver=cfi.pflash01,property=secure,value=on
		-drive if=pflash,format=raw,unit=0,readonly=on,file="$OVMF_CODE"
		-drive if=pflash,format=raw,unit=1,file="$DIR/OVMF_VARS.fd"
		-chardev socket,id=chrtpm,path="$DIR/swtpm.sock"
		-tpmdev emulator,id=tpm0,chardev=chrtpm -device tpm-tis,tpmdev=tpm0
		-drive file="$DIR/disk0.qcow2",if=none,id=d0,format=qcow2,cache=unsafe
		-device nvme,drive=d0,serial=MICRON0001
		-drive file="$DIR/disk1.qcow2",if=none,id=d1,format=qcow2,cache=unsafe
		-device nvme,drive=d1,serial=SABRENT0001
		-netdev user,id=n0,hostfwd=tcp:127.0.0.1:"$SSH_PORT"-:22 -device virtio-net-pci,netdev=n0
		-vga std -display none
		-chardev socket,id=ser0,path="$DIR/serial.sock",server=on,wait=off,logfile="$DIR/serial.log",logappend=on
		-serial chardev:ser0
		-qmp unix:"$DIR/qmp.sock",server=on,wait=off
		-pidfile "$DIR/qemu.pid" -daemonize
	)
	if [[ -n "$iso" ]]; then
		args+=(-drive file="$iso",media=cdrom,if=none,id=cd0,readonly=on -device ide-cd,drive=cd0,bootindex=0)
	fi
	echo "==== start $(date -u +%FT%TZ) iso=${iso:-none} ====" >>"$DIR/serial.log"
	qemu-system-x86_64 "${args[@]}"
	echo "qemu pid $(cat "$DIR/qemu.pid"), swtpm pid $(cat "$DIR/swtpm.pid")"
	;;
stop)
	if alive qemu; then qmp quit || true; fi
	for _ in $(seq 1 30); do alive qemu || break; sleep 1; done
	alive qemu && kill "$(cat "$DIR/qemu.pid")"
	alive swtpm && kill "$(cat "$DIR/swtpm.pid")"
	rm -f "$DIR/qemu.pid" "$DIR/swtpm.pid"
	echo stopped
	;;
key) shift; for k in "$@"; do qmp key "$k"; done ;;
type) shift; qmp type "$*" ;;
shot) qmp shot "$(realpath -m "$2")" ;;
serial) shift; printf '%s\r' "$*" | socat - UNIX-CONNECT:"$DIR/serial.sock" ;;
snap)
	alive qemu && { echo "stop the VM first"; exit 1; }
	mkdir -p "$DIR/snap/$2"
	cp --reflink=auto "$DIR"/disk0.qcow2 "$DIR"/disk1.qcow2 "$DIR"/OVMF_VARS.fd "$DIR/snap/$2/"
	rm -rf "$DIR/snap/$2/tpm" && cp -a "$DIR/tpm" "$DIR/snap/$2/tpm"
	echo "snapshot $2"
	;;
restore)
	alive qemu && { echo "stop the VM first"; exit 1; }
	cp --reflink=auto "$DIR/snap/$2"/disk0.qcow2 "$DIR/snap/$2"/disk1.qcow2 "$DIR/snap/$2"/OVMF_VARS.fd "$DIR/"
	rm -rf "$DIR/tpm" && cp -a "$DIR/snap/$2/tpm" "$DIR/tpm"
	echo "restored $2"
	;;
ssh)
	shift
	ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
		-o LogLevel=ERROR -o ConnectTimeout=10 "${P1VM_USER:-root}"@127.0.0.1 "$@"
	;;
*)
	sed -n '2,17p' "$0"
	exit 2
	;;
esac
