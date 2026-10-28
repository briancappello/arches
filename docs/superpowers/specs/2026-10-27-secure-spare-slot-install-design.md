# Secure install into the spare slot (design)

**Date:** 2026-10-27. **Base:** `feat/p1-gen7` (6563e7e). **Branch:** `feat/secure-p1-install`.

## Goal

p1 boots a new, self-contained Arches system from its two spare partitions. The
system has:

- disk encryption (LUKS2), unlocked by the TPM and a PIN;
- Secure Boot with our own keys;
- no passwordless root, and no SSH password login;
- a screen that locks by itself;
- only signed packages.

The current system stays bootable and untouched. Moving data from the current
`/home` is a later, separate step that uses external storage.

## Non-goals

- Formatting, resizing or deleting any partition other than the two spares.
- Migrating `/home` data, or reclaiming the old root and `/home` partitions.
- Isolating AI agents from the user's secrets (a separate decision).

## Facts (p1, checked 2026-10-27)

| Partitions (on both `nvme0n1` and `nvme1n1`) | Size | Now |
| --- | --- | --- |
| `p1` ESP (vfat) | 8G each | `nvme1n1p1` is `/boot` (Limine), 4.2G free |
| `p2` `archroot` | 100G each | current `/`: one btrfs, data and metadata RAID0 across both disks |
| `p3` `spare` | 100G each | raw, no signature: **the target** |
| `p4` `archhome` | 746G each | current `/home`: one btrfs, RAID0 across both disks, 768G used |

- Firmware: ThinkPad P1 Gen 7, BIOS N48ET34W 1.21. Secure Boot is disabled and
  in **setup mode**. There is a TPM 2.0. fwupd rates the platform HSI:3.
- Kernel: CachyOS. Out-of-tree modules: NVIDIA (RTX 3000 Ada), `evdi`
  (DisplayLink).
- Today the installer only partitions whole disks, or installs into
  filesystems the operator mounted by hand (manual mode). It has no LUKS and no
  Secure Boot support.

## Design

### D1. Install into existing partitions, never anything else

A new layout kind, `existing`, names its target partitions by PARTLABEL
(`spare`) on disks chosen by the normal disk roles. It creates no partition
table. Before it writes anything, the installer refuses to continue unless:

- every target partition carries the declared PARTLABEL, exactly once on
  each disk of the role;
- `blkid -p` finds no signature on it (no filesystem, no LUKS, no RAID
  member), and nothing holds or mounts it;
- the ESP it reuses already exists and is vfat. Both p1 disks have a vfat
  partition labelled `ESP`, so the reused one is the only one that holds
  `limine.conf`; zero or two such ESPs is a refusal;
- for an encrypted layout, a TPM 2.0 is present.

The guard runs again right before the first write, and it reports every
failed check at once. For a partition, `blkid -p` always reports the GPT
entry itself (`PART_ENTRY_*`); only other keys count as a signature
(found in QEMU: an empty `spare` was first refused). NVMe names are not
stable across boots (QEMU swapped `nvme0n1`/`nvme1n1`); the guard never
relies on them. The ESP is mounted but never formatted. The guard is the reason the layout is
safe to point at a disk that holds the current system, so it is tested: the
QEMU proof checksums every other partition before and after the install.

### D2. Encryption

- Decided (operator, 2026-10-27): **one** encrypted device. The two spare
  partitions form an mdadm RAID0 array (200G). One LUKS2 (argon2id) device
  sits on the array, and one btrfs filesystem on that holds everything except
  `/boot`: subvolumes `@` (`/`), `@home`, `@var` and `@snapshots`. `/home`
  lives inside, so the system stands alone.
- Unlock: `systemd-cryptenroll --tpm2-device=auto --tpm2-with-pin=yes`,
  bound to PCR 7 (Secure Boot state and keys). The initramfs uses the
  `systemd`, `mdadm_udev` and `sd-encrypt` hooks with `crypttab.initramfs`.
  One device means one PIN prompt.
- RAID0 has no redundancy: losing either disk loses the system, as today.
- A recovery key is generated at install, shown once, and never written to
  disk. The operator stores it offline.
- The installer enrolls the TPM2 + PIN slot itself (the TUI asks for the
  PIN). A random key in `/run` formats the device and enrolls both slots;
  its own slot is then wiped, so the device ends with exactly two slots.
  Neither the PIN nor the recovery key reaches a log.
- Found in QEMU (2026-10-27): systemd 262 extends PCR 7 with an
  `os-separator` event in the initrd of every UKI boot, before cryptsetup
  (`systemd-pcrosseparator.service`). The live ISO is not a UKI boot, so a
  slot bound to its PCR 7 never unlocks. The installer therefore enrolls
  the TPM2 slot after pacstrap, with an explicit value:
  `--tpm2-pcrs=7:sha256=<sha256(PCR7 || digest)>`, the digest read from the
  target's `/usr/lib/pcrlock.d/750-os-separator.pcrlock`. Without that file
  it binds the current PCR 7. To reverse, bind PCR 7 as is in
  `enroll_tpm2` (and expect the first boot to need the recovery key).
- The initrd asks for the PIN twice when the TPM refuses (the libcryptsetup
  token plugin, then systemd's own TPM2 path), then for the recovery key.
- PCR 7 changes when Secure Boot is turned on, so the first boot after D4
  asks for the recovery key; `arches-tpm-reenroll` then binds the TPM
  slot to the new PCR 7.

### D3. Boot on the shared ESP

- The new system adds its Limine entries next to the current ones. The
  installer merges into `limine.conf`; it never replaces the file, and it
  backs the file up first.
- Each kernel boots as a unified kernel image (UKI), so the kernel, initramfs
  and command line are one signed file. The ESP has 4.2G free; the proof
  records the UKI sizes so the budget is known: in QEMU 41 MB
  (`linux-cachyos`) and 43 MB (`-lts`). Without `autodetect` they were
  229 MB each, so the shared-ESP install adds `autodetect` (with `keyboard`
  before it, so every keyboard driver stays for the PIN prompt). To
  reverse: drop `autodetect` from HOOKS and run `limine-update`. NVIDIA
  modules and firmware on p1 add to the 41 MB; measure on p1.
- pacstrap runs with the ESP unmounted. With it mounted, the kernel
  packages' mkinitcpio preset hook wrote `vmlinuz-*` and `initramfs-*.img`
  onto the shared ESP, over the current system's files of the same name
  (QEMU). After pacstrap the installer empties the new root's own `/boot`,
  removes the mkinitcpio presets (UKIs only; a later `mkinitcpio -P`, e.g.
  from chwd, builds nothing then) and mounts the ESP.
- `limine-install` adds a "Limine" UEFI boot entry, first in the boot
  order, only if none exists for `EFI/limine/limine_x64.efi`. p1 has one.
- Under Secure Boot, Limine's own binary is signed and the hash of
  `limine.conf` is enrolled into it (`limine enroll-config`). Then an edited
  config or an unsigned image does not boot.
- How the merge works: limine-entry-tool (both systems run it) keys each OS
  entry on its machine-id, so the new system adds `/+Arches Linux` and
  leaves the others alone. The installer backs `limine.conf` up before
  pacstrap (the kernel install already runs the tool), and after the
  bootloader phase checks that the global options and every entry that is
  not ours are byte-identical to the backup; if not, it restores the backup
  and fails. UKIs are `EFI/Linux/arches_<kernel>.efi`.
- The config hash is enrolled only in the D4 step, not at install: the
  current system rewrites `limine.conf` (limine-snapper-sync, hourly) until
  then, and an enrolled hash would stop every entry from booting.

### D4. Secure Boot keys

- `sbctl create-keys` and `sbctl enroll-keys --microsoft`. Microsoft's
  certificates stay because the GPU's option ROM is signed by them; dropping
  them risks a black screen at boot.
- A pacman hook re-signs the UKIs and Limine after every update, and re-enrolls
  the config hash.
- Enrolling is a separate, attended step that happens only after the new
  system has booted correctly with Secure Boot off
  (`arches-secureboot-enroll`). The fallback (turning Secure Boot off) is
  only a fallback if the operator sets a **BIOS supervisor password**.
- The keys are created at install (`sbctl create-keys`, on the encrypted
  root), so mkinitcpio signs each UKI as it builds it. A UKI is never
  re-signed in place: that changes its bytes after `limine.conf` recorded
  their hash. `arches-secureboot-resign` rebuilds unsigned UKIs instead.
- Correction (Limine 12 `USAGE.md`, 2026-10-27): with Secure Boot active and
  a config hash enrolled, Limine still boots `linux`-protocol entries whose
  paths carry a BLAKE2 hash; only `efi` chainloads (our UKIs) rely on the
  firmware's signature check. So the current system's entries **keep
  booting** under Secure Boot. Its writes to the shared ESP then break the
  chain for every entry: limine-snapper-sync changes `limine.conf` (the
  enrolled hash no longer matches), and its `10-limine-reset-enroll` hook
  restores an unsigned `limine_x64.efi`. See O4.

### D5. Kernel lockdown and out-of-tree modules

Lockdown would refuse NVIDIA and `evdi` unless the kernel trusts their
signing key, and without shim our db key is not a module key. So this design
does not turn lockdown on (a follow-up, O2). The implementation records whether CachyOS's
kernel can trust a module key we control; enabling lockdown is a follow-up.

Findings (p1, kernel 7.3.0-4-cachyos, `/proc/config.gz`, 2026-10-27):

- `MODULE_SIG=y`, `MODULE_SIG_FORCE` off, `MODULE_SIG_ALL=y` with a
  per-build key: in-tree modules are signed; unsigned ones load and taint.
- `SECURITY_LOCKDOWN_LSM=y`, `LOCK_DOWN_KERNEL_FORCE_NONE=y`: lockdown is
  available (`lockdown=integrity`) but off.
- `INTEGRITY_PLATFORM_KEYRING=y`, `LOAD_UEFI_KEYS=y`: firmware db keys go
  to the `.platform` keyring, which verifies kexec images, **not** modules.
- `INTEGRITY_MACHINE_KEYRING=y`: MOK keys (`MokListRT`, set by shim) go to
  `.machine`, which modules do trust.
- On p1, `nvidia` and `evdi` are DKMS builds under `updates/dkms/`, signed
  with the "DKMS module signing key" (`1F:44:72:...`), which the kernel
  does not trust. In-tree modules carry the "Build time autogenerated
  kernel key".
- So a module key we control is trusted only through shim + MOK, or by
  building it into the kernel. Without one of those, lockdown refuses the
  DKMS-built NVIDIA and `evdi` modules. CachyOS ships
  `linux-cachyos-nvidia-open`, built with the kernel; if it is signed with
  the kernel's build key it would cover NVIDIA (to check), but nothing
  covers `evdi`.

### D6. Defaults that change for every install

| Today | After |
| --- | --- |
| The installer writes `%wheel ALL=(ALL:ALL) NOPASSWD: ALL` (`core/install.py`) | wheel needs a password. First boot runs as a root systemd service, so it needs no sudo. |
| `deploy_ssh_key` copies the build host's public key into `authorized_keys` | Only a key the install config names explicitly, and the confirm screen shows it |
| sshd follows the Arch defaults: password login on | Workstation and laptop templates leave sshd off. Where it is on: keys only (`PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin no`), and firewalld opens it only when declared |
| KDE never locks the screen | `kscreenlockerrc`: `Autolock=true`, `Timeout=5`, `LockOnResume=true` |
| `arches-local` is `SigLevel = Optional TrustAll` and comes before `core` | Packages are signed with the YubiKey repository key and the repo is `SigLevel = Required` (D7) |
| The builder needs root podman | Rootless (`fix/rootless-builder`) |

Each row gets a test that fails if the old default comes back.

### D7. Signing `arches-local` with the YubiKey

- The repository key is an OpenPGP signing key generated **on** the YubiKey
  (OpenPGP applet, touch required). The private key never exists outside
  the card. Only its public key and fingerprint are in this repository, and
  the installer adds them to the target's pacman keyring.
- Builds stay unprivileged and unsigned: agents and the rootless builder
  produce packages but cannot sign them. Signing is a separate host step that
  the operator runs with the YubiKey (`gpg` with the card, one touch per
  signature) before the packages enter the repository. That step is the
  human gate between agent-built code and p1's root.
- `pacman.conf` gives `[arches-local]` `SigLevel = Required`, and the
  installer adds and locally signs only that key. An unsigned package, or
  one signed by an unknown key, fails to install. pacman cannot pin a key
  to one repository: a signature by any key the keyring already trusts
  (Arch, CachyOS) also passes. Agents hold none of those keys.
- Tooling: the public key and fingerprint are `keys/arches-local.{asc,fpr}`
  (`keys/README.md` shows how to create the key on the card).
  `scripts/sign-local-repo.sh` signs packages and the database and refuses
  a key that is not on a smartcard. The ISO build
  (`scripts/stage-repo-key.sh`, from `_stage-platform`) refuses an
  unsigned repository and stages the key for the live ISO and the
  installer. `build-aur-repo.sh` keeps a signed database while no package
  changed.
- Until the key exists, every normal ISO build fails at that check:
  nothing could verify arches-local.
- TEST-ONLY builds (decided 2026-10-27, operator's recommended option):
  `ARCHES_TEST_BUILD=1` makes `stage-repo-key.sh` generate a throwaway key
  for that build (valid one day), sign arches-local with it and stage it.
  The ISO is marked: its file name ends in `-TEST-ONLY.iso`, it carries
  `/opt/arches/TEST-BUILD` and an `/etc/issue.d` banner, and its installer
  refuses to run unless `systemd-detect-virt --vm` says kvm/qemu and DMI
  says QEMU. A normal build removes any marker and still needs `keys/`.
  To reverse: delete the `ARCHES_TEST_BUILD` block in `stage-repo-key.sh`
  and `refuse_test_build_on_hardware` in `__main__.py`.
- Known limitation: `aarch64-apple` keeps `SigLevel = Optional TrustAll`
  for arches-local. Its host-install flow (`Containerfile.install`) creates
  an empty, unsigned repository inside a container and never passes
  through `stage-repo-key.sh`, so `Required` would make every Apple
  host-install fail. Signing that flow needs its own design.
- Losing the YubiKey means a new key and re-signing; nothing already
  installed stops working.

## Validation

1. **QEMU, the whole path:** two virtual disks with p1's partition table,
   OVMF with Secure Boot in setup mode, and swtpm. Fill the non-spare
   partitions with data and checksum them.
   - Install into the spare pair; the checksums must not change.
   - Boot with TPM and PIN; boot the old system from the same Limine menu.
   - Enroll the keys; boot with Secure Boot enforcing.
   - Tamper checks: an edited `limine.conf` and an unsigned UKI must both
     fail to boot. A changed Secure Boot state must ask for the recovery key.
2. **Offline:** installer unit tests for D1's guard, the `limine.conf`
   merge, crypttab, and every D6 row.
3. **p1, attended:** see "Operator steps".

### Validation 1 results (QEMU, 2026-10-27)

Harness: `scripts/qemu-p1/` (README there). Two 40G NVMe disks with p1's
structure (ESP 2G, archroot 8G btrfs RAID0, spare 24G, archhome rest btrfs
RAID0), a small current system on archroot with limine-entry-tool,
limine-snapper-sync and the reset/enroll hooks, OVMF Secure Boot in Setup
Mode, swtpm. TEST-ONLY ISO, `kde-workstation`, layout `p1-spare.toml`.
The fixes it forced are in D1 to D3; the last run used an ISO built at
commit 4199c28 with no installer code copied in, and repeated install,
checksums, TPM2 + PIN, first boot, enroll, both Secure Boot boots and the
TPM re-enroll. The tamper checks ran on the run before it (same
bootloader and Secure Boot code).

- Checksums: partition tables, the other ESP, archroot and archhome (raw,
  both disks, by serial) and every file of the current system on the
  shared ESP are unchanged. The one changed file is the shared
  `EFI/limine/limine_x64.efi`: the new system signs it.
- The new system boots with TPM2 + PIN from the shared Limine menu; the
  current system boots from the same menu. First boot: Ansible
  `failed=0`, postgresql active, sshd off, sudo asks for a password, KDE
  locks after 5 minutes, `arches-kwin-patched 6.8.1-2` installed offline.
- Recovery key: printed once on the console, found in no log or file of
  the live system or the target.
- `arches-secureboot-enroll`: froze the current system, enrolled the
  config hash, signed Limine and both UKIs, enrolled the keys with
  Microsoft's. With Secure Boot enforcing (Limine shows "Secure Boot", no
  editor), the current system boots, frozen (`/boot` not mounted,
  limine-snapper-sync masked, both hooks `.disabled`), and the new one
  boots its signed UKI.
- Changed Secure Boot state: the TPM refused (PCR 7), the initrd asked for
  the recovery key, which unlocked; `arches-tpm-reenroll` bound the slot
  again and the next boot unlocked with the PIN.
- Tamper: an edited `limine.conf` (cmdline + `init=/bin/sh`) stops Limine
  with "PANIC: CHECKSUM MISMATCH FOR CONFIG FILE"; an unsigned UKI with a
  correct hash and a re-enrolled, re-signed config stops with "efi:
  LoadImage failure (0x800000000000000f)" (firmware: access denied).
- UKI sizes: 41 MB and 43 MB; ESP use with both systems 145 MB of 2 GB.

## Operator steps on p1

1. Set a BIOS supervisor password.
2. Create the arches-local key on the YubiKey, commit its public half, sign
   the repository, build the ISO (`keys/README.md`).
3. Boot the Arches ISO from USB and install into the spare pair with the
   `p1 Spare Slot` layout. Set the TPM PIN in the installer; write the
   recovery key down when it is shown; store it offline.
4. Validate the new system with Secure Boot off (it boots from the same
   Limine menu as the current one).
5. On the new system: `sudo arches-secureboot-enroll`. It freezes the
   current system (O4a) and enrolls the keys. Turn Secure Boot on in the
   firmware; boot, enter the recovery key once; `sudo arches-tpm-reenroll`.
6. Later, a separate plan: migrate data through external storage.

## Open decisions

- **O1.** Decided: mdadm RAID0, one LUKS2 device, 200G (D2).
- **O2.** Decided: lockdown is a follow-up (D5), not part of this work.
- **O3.** Decided: the `arches-local` signing key lives on the YubiKey (D7).
- **O4.** Decided (2026-10-27, option a): `arches-secureboot-enroll` runs
  `arches-freeze-other-systems` before it enrolls. For every other OS entry
  in `limine.conf` it backs up `limine.conf` and the Limine binary (to
  `<ESP>/arches-freeze-<time>/` and `/var/lib/arches/freeze-<time>/`),
  mounts that system's root (from `root=UUID=` in its entry), and:
  gives `/boot` in its fstab `noauto,nofail` (the old fstab is kept as
  `/etc/fstab.arches-freeze-<time>`), masks `limine-snapper-sync.service`,
  and renames its `10-limine-reset-enroll` and `90-limine-enroll-config`
  hooks to `*.disabled`. The frozen system still boots from the menu
  (hashed entries), but writes nothing to the ESP, so it must not be
  updated while frozen. To reverse: boot it and run
  `sudo arches-unfreeze` (written into it by the freeze), then reboot;
  with Secure Boot on, its next ESP write breaks the boot chain, so turn
  Secure Boot off first or stay frozen.
