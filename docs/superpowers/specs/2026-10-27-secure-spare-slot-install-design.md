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

- every target partition carries the declared PARTLABEL;
- `blkid` finds no signature on it (no filesystem, no LUKS, no RAID member);
- the ESP it reuses already exists and is vfat.

The ESP is mounted but never formatted. The guard is the reason the layout is
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

### D3. Boot on the shared ESP

- The new system adds its Limine entries next to the current ones. The
  installer merges into `limine.conf`; it never replaces the file, and it
  backs the file up first.
- Each kernel boots as a unified kernel image (UKI), so the kernel, initramfs
  and command line are one signed file. The ESP has 4.2G free; the proof
  records the UKI sizes so the budget is known.
- Under Secure Boot, Limine's own binary is signed and the hash of
  `limine.conf` is enrolled into it (`limine enroll-config`). Then an edited
  config or an unsigned image does not boot.

### D4. Secure Boot keys

- `sbctl create-keys` and `sbctl enroll-keys --microsoft`. Microsoft's
  certificates stay because the GPU's option ROM is signed by them; dropping
  them risks a black screen at boot.
- A pacman hook re-signs the UKIs and Limine after every update, and re-enrolls
  the config hash.
- Enrolling is a separate, attended step that happens only after the new
  system has booted correctly with Secure Boot off. After enrolling, the
  current system no longer boots unless Secure Boot is turned off in the
  firmware. That fallback is only a fallback if the operator sets a **BIOS
  supervisor password**.

### D5. Kernel lockdown and out-of-tree modules

Lockdown would refuse NVIDIA and `evdi` unless the kernel trusts their
signing key, and without shim our db key is not a module key. So this design
does not turn lockdown on. The implementation records whether CachyOS's
kernel can trust a module key we control; enabling lockdown is a follow-up.

### D6. Defaults that change for every install

| Today | After |
| --- | --- |
| The installer writes `%wheel ALL=(ALL:ALL) NOPASSWD: ALL` (`core/install.py`) | wheel needs a password. First boot runs as a root systemd service, so it needs no sudo. |
| `deploy_ssh_key` copies the build host's public key into `authorized_keys` | Only a key the install config names explicitly, and the confirm screen shows it |
| sshd follows the Arch defaults: password login on | Workstation and laptop templates leave sshd off. Where it is on: keys only (`PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin no`), and firewalld opens it only when declared |
| KDE never locks the screen | `kscreenlockerrc`: `Autolock=true`, `Timeout=5`, `LockOnResume=true` |
| `arches-local` is `SigLevel = Optional TrustAll` and comes before `core` | Packages are signed with an arches repository key and the repo is `SigLevel = Required`. Where that key lives is open (O3). |
| The builder needs root podman | Rootless (`fix/rootless-builder`) |

Each row gets a test that fails if the old default comes back.

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

## Operator steps on p1

1. Set a BIOS supervisor password.
2. Boot the Arches ISO from USB and install into the spare pair.
3. Validate the new system with Secure Boot off; set the TPM PIN; store the
   recovery key offline.
4. Enroll the Secure Boot keys and turn Secure Boot on.
5. Later, a separate plan: migrate data through external storage.

## Open decisions

- **O1.** Decided: mdadm RAID0, one LUKS2 device, 200G (D2).
- **O2.** Lockdown (D5): follow-up or blocker?
- **O3.** Where the `arches-local` signing key lives: not as a plain file on
  p1. A YubiKey OpenPGP slot is the candidate.
