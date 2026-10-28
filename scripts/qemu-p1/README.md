# QEMU stand-in for p1 (spec Validation 1)

Runs as the user; needs `/dev/kvm`, `swtpm`, `socat`, and
`/usr/share/edk2/x64/OVMF_CODE.secboot.4m.fd` + `OVMF_VARS.4m.fd`.
State lives in `$P1VM_DIR` (default `~/.cache/arches-p1vm`); not under
`/tmp`, which is tmpfs: the growing disks fill RAM and the guest sees
write faults.

```bash
ARCHES_TEST_BUILD=1 OFFLINE=1 ISO_MODE=fb ARCHES_ALLOW_DEFAULT_PASSWORD=1 \
    ./scripts/build-iso.sh --template kde-workstation
ISO=$PWD/out/arches-x86-64-*-TEST-ONLY.iso
S=scripts/qemu-p1/vm.sh
$S disks && $S start $ISO                      # live ISO; root over: $S ssh
$S ssh 'cat > /root/old.sh' < scripts/qemu-p1/make-old-system.sh
$S ssh 'bash /root/old.sh'                    # p1's partition table + a current system
$S ssh 'cat > /root/sum.sh' < scripts/qemu-p1/checksums.sh
$S ssh 'bash /root/sum.sh' > before.txt
$S ssh -tt 'arches-install --auto /root/p1.toml'   # layout p1-spare.toml, [encryption] tpm_pin
$S ssh 'bash /root/sum.sh' > after.txt        # after umount, cryptsetup close, mdadm --stop
comm -23 <(sort before.txt) <(sort after.txt) # must list only EFI/limine/limine_x64.efi (signed)
```

The recovery key is printed once on the serial console (`serial.log`).
Boot prompts (Limine menu, PIN) are on both the VGA console (`$S shot`,
`$S key`, `$S type`) and the serial console (`$S serial TEXT`).
`$S snap NAME` / `$S restore NAME` copy the disks, OVMF vars and TPM state.
