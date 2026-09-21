# Omarchy Provider Execution Evidence

Date: 2026-09-07
Plan: `docs/superpowers/plans/2026-09-07-provider-selection.md`, Task 1.
Status: Scoped M1 acceptance passed for x86-64 QEMU Omarchy and the CachyOS VM Server offline workload. M2 remains pending the stock Arch boot-policy choice.

## Baseline

Repository HEAD: `771102ee0cc22a01bf1cdd32f81f43fceac30eb6`.
Existing edits to power, hardware, KDE, llama.cpp, and builder files were preserved.
No provider implementation, commits, or changes to existing application files occurred.
No isolated implementation worktree was created yet.

| Command | Exit | Passed | Failed | Skipped | Errors |
|---|---|---|---|---|---|
| `make test-unit` | 2 | 514 | 2 | 1 | 0 |
| `make test` | 2 | 551 | 2 | 1 | 0 |
| `make test-template` | 0 | Four templates loaded | N/A | N/A | 0 |
| `make dry-run` | 0 | Successful | N/A | N/A | 0 |
| Focused installer pytest | 0 | 129 | 0 | 0 | 0 |

Counts overlap across commands. The full suite also emitted one thread-lifecycle warning.

Focused command:

```bash
uv run pytest -q installer/tests/core/test_platform.py installer/tests/core/test_install.py installer/tests/core/test_pipeline.py installer/tests/core/test_host_install.py installer/tests/core/test_firstboot.py
```

### Existing Failures

Two tests in `installer/tests/core/test_gpu_stacks.py` expect `llama.cpp-vulkan-arches`:

- `TestResolveAndMergeWithGpu::test_amd_vulkan_default_pulls_llama_cpp`
- `TestResolveAndMergeWithGpu::test_vulkan_wins_over_cuda_in_full_resolve`

Existing edits to `modules/llama-cpp/module.toml` remove that package in favor of build dependencies.
This is a Tier 4 intent conflict, not a provider regression.
Neither the module changes nor the assertions were modified.
The user approved continuing with this recorded baseline while leaving the concurrent llama.cpp changes untouched.

The skipped test cannot write `/opt/arches` without privileges.
The full-suite warning reports an install-thread callback after Textual UI teardown (`NoMatches`, followed by `NoActiveAppError`).
This is a separate Tier 4 lifecycle investigation. Its root cause was not established by the baseline run.

### Preserved Defaults

| Platform | Kernels | Bootloader | Detection | Repositories |
|---|---|---|---|---|
| x86-64 | linux-cachyos and linux-cachyos-lts | Limine | chwd, optional | CachyOS v3/base, local, Arch |
| Generic ARM | linux-aarch64 | GRUB | Disabled | ALARM, local |
| Apple Silicon | linux-asahi | GRUB in installed platform config | Disabled | Asahi ALARM, local, ALARM |

Apple auto-install remains disabled. Its installed-platform GRUB configuration differs from the USB boot-chain description in the README.
The full baseline and resolved package lists are recorded in `/tmp/opencode/arches-provider-baseline.md` and `/tmp/opencode/arches-provider-platforms.log`.
Suite logs use `/tmp/opencode/arches-provider-{test-unit,test,test-template,dry-run,focused}.log`.

## Upstream Trust Results

| Source | Immutable revision |
|---|---|
| omacom/omarchy-iso | `2673c613d9a71e23920e43fbb951238145e0f1e8` |
| omacom/omarchy v4.0.2 | `346e69e1cec6c4e8924531874af6ba010a1bc99e` |
| omacom/omarchy quattro | `e848f1df97bbbe23db42fc2b1fb04937d7a0eae0` |
| omacom/omarchy-pkgs master | `2b15c13e86303cfcc9a8f94974bbaaa802270029` |

The earlier runtime-policy concern is resolved for the inspected non-T2 4.0.2 configuration.
Published settings and source templates use `SigLevel = Required DatabaseOptional` without an Omarchy repository override.
Refresh, channel selection, and reinstall use those templates. No repeated Arches policy-restoration hook is needed for repository signatures.
Unsigned local-file transactions remain permitted by `LocalFileSigLevel = Optional`, as in upstream Arch defaults.

The ISO build templates still use `Optional TrustAll`, and the offline installer uses `Never` for its bundled repository.
These are distinct from the installed runtime configuration.
A signed ISO authenticates its enclosed bytes, but does not make offline pacman enforce individual package signatures.
At the initial boot-only checkpoint, the Arches integration's mandatory-signature bootstrap remained unproven. The signed-closure follow-up below resolves package authentication for the selected targets. No bypass was added or executed by Arches.

### Verified Artifacts

Signing fingerprint: `40DFB630FF42BCFFB047046CF0134EE680CAC571`, UID `Omarchy <pkgs@omarchy.org>`.
Identity was corroborated through the official security manual, upstream key files, and keys.openpgp.org email lookup.
No key was imported into a host trust store.

| Artifact | SHA-256 | Verification |
|---|---|---|
| omarchy-4.0.2.iso | `2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8` | Full payload digest and detached signature passed |
| omarchy-keyring-20251027-1-any.pkg.tar.zst | `f4bc258879f493462736f9a072c4f4c7f753eb559f6b346f5bdf8ed816f75f66` | Detached signature passed |
| omarchy-settings-4.0.2-1-any.pkg.tar.zst | `8f594aabc9d96cf136bd1192d65e7e2437901b7525f4a3cbf280125e6e6a6869` | Detached signature passed |

ISO source: `https://iso.omarchy.org/omarchy-4.0.2.iso`, with `.sig` and `.sha256` sidecars.
The local ISO is 6,227,752,960 bytes.
`gpgv` returned exit 0 and `VALIDSIG` with the expected full fingerprint before VM boot.

Re-run local verification:

```bash
sha256sum out/omarchy-provider-proof-20260907/omarchy-4.0.2.iso
gpgv --homedir /tmp/opencode/omarchy-proof-20260907-wP9Pn1 --keyring /tmp/opencode/omarchy-proof-20260907-wP9Pn1/omarchy-public.gpg --status-fd 1 /tmp/opencode/omarchy-proof-20260907-wP9Pn1/omarchy-4.0.2.iso.sig out/omarchy-provider-proof-20260907/omarchy-4.0.2.iso
```

The isolated key/signature directory is temporary. Re-download authenticated material if it is absent in a later session.
The complete source trace and package evidence are in `/tmp/opencode/omarchy-upstream-proof.md`.

### Lifecycle Findings

The inspected ISO sequence installs settings and Neovim defaults before user creation.
It establishes initial Limine configuration before runtime-package boot hooks.
It then installs runtime packages and runs system finalization as root.
Final boot generation follows hardware, hibernation, Snapper, and provisioning configuration.
User provisioning runs separately as the actual target user and marks shipped migrations complete.

The source entry points include `omarchy-apply-system --install-user USER --first-install` and `omarchy-provision-user --force --first-install`.
Their environment, package prerequisites, and chroot context are part of the contract.
These commands have not yet been executed or accepted as a complete Arches adapter.

Upstream source describes `@`, `@home`, `@log`, and `@pkg` subvolumes.
Arches' `@var` and bind-mount choices require explicit rollback verification, particularly for `/var/lib/pacman`.

## Disposable VM Proof

QEMU/KVM is usable by the current user. System libvirt lists existing `seed-vm` and `fin-stack-dev` domains, which were left untouched.
The reference VM ran directly through QEMU, not as a libvirt domain.

| Property | Value |
|---|---|
| Name | arches-omarchy-reference-20260907 |
| CPU and RAM | 4 vCPU, 4 GiB |
| Machine | q35, KVM enabled, host CPU |
| Firmware | OVMF 4 MiB, read-only code, ephemeral variable overlay |
| Disk | New 64 GiB sparse qcow2 at `out/omarchy-provider-proof-20260907/reference.qcow2` |
| Media | Fully verified upstream Omarchy 4.0.2 ISO |
| Network | QEMU user network with `restrict=on`, no forwarding |
| Host exposure | No shared directories, host disks, or device passthrough |
| Control | Local QMP Unix socket, no VNC listener |
| Current state | Stopped through QMP after boot evidence capture |

QMP reported `running=true` and KVM `enabled=true`.
The screenshot at `out/omarchy-provider-proof-20260907/boot.png` shows the Omarchy welcome screen: `Press Return to Start Install`.
At this initial checkpoint, no installation was started. This proved VM access and authenticated live-media boot only. The follow-up below records the subsequent reference installation.
The ISO, sparse disk, and screenshot remain available for the next session.

Recreate the same reference boot using the existing new disk:

```bash
qemu-system-x86_64 \
  -name arches-omarchy-reference-20260907 \
  -machine q35,accel=kvm -cpu host -smp 4 -m 4096 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/x64/OVMF_CODE.4m.fd \
  -drive if=pflash,format=raw,snapshot=on,file=/usr/share/edk2/x64/OVMF_VARS.4m.fd \
  -drive file=/home/brian/dev/arches/out/omarchy-provider-proof-20260907/reference.qcow2,format=qcow2,if=virtio \
  -cdrom /home/brian/dev/arches/out/omarchy-provider-proof-20260907/omarchy-4.0.2.iso \
  -boot order=d,menu=on -device virtio-vga -display none \
  -nic user,restrict=on,model=virtio-net-pci \
  -qmp unix:/home/brian/dev/arches/out/omarchy-provider-proof-20260907/qmp.sock,server=on,wait=off \
  -serial file:/home/brian/dev/arches/out/omarchy-provider-proof-20260907/serial.log \
  -pidfile /home/brian/dev/arches/out/omarchy-provider-proof-20260907/qemu.pid -daemonize
```

This boot-only configuration does not preserve NVRAM across process restarts and has no external network access.
Installation/update proof needs a private persistent NVRAM file and an explicitly configured guest network.
It must continue to use disposable storage without host shares.

## Reference Installation Follow-Up

The authenticated ISO completed all 14 installation phases in 114.67 seconds on the disposable disk.
Automatic reboot and a later cold boot without installation or cidata media passed.
SSH and SDDM password login passed for the generated guest-only account.
Hyprland, Quickshell, and a terminal ran with software rendering. No claim of accelerated graphics follows.
The reference VM is stopped after clean guest poweroff. Its private persistent `OVMF_VARS.fd` and disk are retained.

The actual ISO's `orchestrator/main.py`, `phases_impl.py`, and `archinstall_adapter.py` exactly match the inspected ISO repository revision.
This establishes the decisive orchestration correspondence, not a reproducible build attestation for every package.
The ISO reports archinstall `4.4-1`; the target reports Omarchy `4.0.2-1` and Linux `7.1.9.arch1-2`.

Observed account policy: UID/GID 1000, supplementary `wheel`, `/usr/bin/bash`, and a 0700 home owned by the account.
Sudo requires a password except for narrow upstream helpers. User finalization and migration markers exist.
Observed layout: `@`, `@home`, `@log`, `@pkg`, read-only `@factory`, and a 2 GiB ESP at `/boot`.
Runtime repository signatures remain required without a per-repository override.

One initial graphical session ended with `sddm-helper exited with 2`.
Later login and cold-boot desktop checks passed. The cause remains a Tier 4 upstream session-lifecycle question, not an established GPU fault.
Hardware acceleration and long-running desktop stability remain unverified.

Reference evidence: `/tmp/opencode/omarchy-reference-install.md` and `out/omarchy-provider-proof-20260907/`.
Important artifacts include `cold-desktop-terminal.png`, `cold-boot-check-final.txt`, `cold-boot-blocks.json`, `source-hashes.txt`, and installation timing/log files.
The retained `check-installed.sh` is a runnable installed-state check.

CAUTION: `start-reference.sh install` uses a full-disk unattended configuration and must not run against the preserved installed disk.
`start-reference.sh disk` restarts the installed guest without media.
Scratch credential files remain mode 0600 and are not source artifacts.

## Signed Closure Follow-Up

A rootless disposable Arch container resolved 166 explicit targets into 941 package archives using only Omarchy stable and its stable Arch mirror.
The targets came from the signed runtime package's default list and the actual reference bootstrap selections.
No T2, optional Tailscale, or speculative GPU-driver packages were added.

Pacman reported 941 valid and fully trusted signatures, with 941 matching selected SHA-256 digests.
A fresh offline verification repeated the result. Invalid-signature and unknown-key controls were rejected without importing the unknown key.
No target packages or target scriptlets were installed or executed in the closure check.

The proof used separate target configuration, database, cache, root, and GnuPG paths.
Only named Arch and Omarchy public keyring material entered the new trust store.
The Arch material came from the authenticated ISO and matched the independently verified keyring package.
No host/live initialized trust store, CachyOS state, or host package database participated.

Effective target policy: `Required DatabaseOptional`, with `Required` for local and remote file signatures.
The proof container was removed. All evidence and 2,887,071,783 bytes of package archives remain under `out/omarchy-provider-proof-20260907/signed-closure/`.

| Retained database | SHA-256 |
|---|---|
| core | `c904c80bedd387beb86021d20e90c8519785c8b1ab7e94f27b75066a566e8648` |
| extra | `69cca941b3da9958ca6802a6f9ef3327158ee00f1104f4ceda9410f6e5ed701b` |
| multilib | `955ca1fb0dcad09d95b7cd65d0a2b4513f17e1cc445d0a9b94bb0146dbc7333f` |
| omarchy | `3105b5ab098c05a95bb61ab2bcb999f13fabd758e4fd03e90b181726e77fae00` |

All four databases matched a second online download during the proof window.
`selected-manifest.tsv` has SHA-256 `33eeb0938949a0e2950bb116af17c0d5bb7b7b4992de9b7ff456358aa093a297`.
It records repository, name, version, architecture, filename, URL, and digest for each package.

This is a point-in-time coherent set, not an upstream promise of immutable URLs or indefinite retention.
Package signatures authenticate bytes, not repository freshness or source-to-binary reproducibility.
Future builds must resolve a coherent set and fail on unavailable or unauthenticated inputs rather than mix snapshots.

Detailed report: `/tmp/opencode/omarchy-signed-closure.md`.

Review found that the original combined checker exited early on `pacman -Q` against the deliberately empty installed database.
Earlier standalone signature/digest controls were valid, but the claimed complete offline-check result was incorrect.
The checker now requires a successful database consistency check and an empty inventory query with exactly the expected status and no diagnostics.
A new network-disabled container completed the entire corrected check with exit 0 at `2026-09-08T00:33:42Z`:

```text
SIGNED_CLOSURE_CHECK_PASS packages=941 signatures=941 digests=941 negative_controls=2 keyring_comparisons=3 installed=0
```

Retained evidence: `signed-closure/corrected-offline-console.log` and `corrected-offline-status.txt`.
The adapter must also stage the authenticated ISO's Node payload before `iso-chroot` user provisioning.
This non-pacman artifact is not covered by the 941-package count and must retain its own authenticated identity.

## Remaining Gates

- Baseline continuation permission is resolved. Preserve the concurrent llama.cpp work and record the same two existing failures separately.
- Task 1 review of the reference install, decisive source correspondence, and signed closure.
- Arches adapter implementation and its own installation proof; the reference is not an Arches installation.
- Update, migration, kernel boot, and snapshot restoration evidence for M1 acceptance in Task 7.

No provider-model implementation began during the proof work.

## Arches Installation Acceptance

Tasks 2 through 6 are implemented and independently reviewed in `/home/brian/dev/arches-worktrees/provider-selection`, branch `feat/provider-selection`.
The original checkout retains the plans and task reports. Original user changes remain untouched and copied into the isolated worktree baseline.
No commits occurred.

Task 7 built actual Arches installation media with the selected authenticated Omarchy inputs.
The final exercised ISO has SHA-256 `5665b74ef95ac30556b03f07115f38ea3bbef84023ed070dc0c8e9166cabf843`.
The public installer passed its signed preflight, installed Omarchy, and recorded successful provider finalization.
The installed system booted without media and passed SDDM, desktop, and terminal checks with software rendering.

The generated Limine snapshot entry booted successfully. Upstream snapshot restoration removed the test marker.
Two restored normal boots passed desktop and package-database checks.
This proves restoration of the exercised state, not rollback across different package or kernel versions.

The actual `omarchy-update -y` command returned success but reported `there is nothing to do`.
No new migration or kernel transition occurred. Those M1 gates remain blocked; M2 has not started.

### Native Engine Correction

Review identified a Tier 2 integration defect: live CachyOS pacman wrote `%INSTALLED_DB%` metadata that target Omarchy pacman did not recognize.
Isolated database paths did not isolate the executable that wrote them.
The correction authenticates the pinned stock pacman archive and extracts only pacman, pacman-conf, and libalpm into private transaction storage.
Scoped executable and library paths select that engine for every Omarchy bootstrap transaction, beginning with the first installed database write.
Preflight checks executable compatibility before disk operations. No foreign glibc or database scrub is used.

Native controls in a disposable diskless live VM reproduced the original field with the live engine.
The stock engine wrote zero custom fields in the first transaction and in a subsequent 137-package base installation.
Target pacman then checked that database without metadata warnings.
Independent review passed the correction's scope, signature checks, and environment isolation.

The installed-state checker also now asserts identity, complete status, Bash shell, UID, and zero failed units instead of merely printing them.
Negative fixtures and an installed clone passed their expected outcomes.
The original accepted disk and firmware hashes remained unchanged.

The engine correction has not yet passed a new full ISO build and complete installation.
The earlier accepted installation retains its original metadata warnings. It is not evidence that those records were repaired.

### Current Results

- Full suite after the engine correction: 898 passed, 2 accepted baseline failures, 1 skipped.
- Focused correction suite: 334 passed.
- Actual Arches installation, disk-only desktop, marker-based snapshot restoration, and two restored boots: passed on the preceding acceptance ISO.
- Real package/migration/kernel transition and cross-version rollback: blocked or unverified.
- Fresh fault injection, native CachyOS offline regression, and ARM/hardware acceptance: still outstanding.

All task-owned VMs and the dedicated builder are stopped. Disk images, private firmware variables, logs, source archives, and screenshots are retained.
Existing `seed-vm`, `fin-stack-dev`, `arches-builder`, and upstream reference artifacts were left untouched.

Reports: `.superpowers/sdd/task-7-report.md` and `.superpowers/sdd/task-7-engine-report.md`.
Artifacts: `out/omarchy-provider-proof-20260907/arches-acceptance/`.
The final gate remains partial. No release-readiness claim follows from these results.

## Reusable Clean Baseline

The engine-corrected ISO was subsequently rebuilt and installed through the public pipeline.
ISO SHA-256: `387a00c3a83faf3070b400e3a12b86e5accfc3b132a9dc99f760d0145b1411d9`.
Disk-only checks passed with 941 package records, zero incompatible metadata fields, and no pacman metadata warnings.
This resolves the preceding full-installation gap for the stock-engine correction.

At the user's request, the stopped installation is preserved outside build outputs:
`/home/brian/dev/arches-vm-baselines/omarchy-stable-4.0.2-stock-engine/`.
The self-contained qcow2, matching UEFI variables, firmware code, test credentials, and check scripts are owner-read-only.
Image checks, source comparison, and saved SHA-256 verification passed.
It occupies about 6.2 GiB with a 60 GiB virtual capacity.

An empty copy-on-write clone and private writable UEFI variables are ready at `out/omarchy-edge-test.Rc0HrW/`.
Future tests must use clones rather than modify the baseline. No rebuild or reinstall is needed for the approved edge test.
The stable-to-edge transition has not yet occurred. M1's remaining update and regression gates are unchanged.

Full baseline provenance and restart instructions: `.superpowers/sdd/task-7-clean-engine-report.md` and the saved baseline's `README.md`.

## Edge Transition and Recovery

The user approved an official stable-to-edge transition on the prepared disposable clone only.
The installed baseline and shipped stable defaults remained unchanged.

The clone changed 124 package versions, added ten packages, removed two, and ran 16 actual new migrations.
The guest then booted kernel `7.2.3-arch1-3`, compared with baseline `7.1.9-arch1-2`.
The initial channel command was interrupted at its final reboot prompt, so its final exit status is unavailable.
A separate official updater completed on the updated system with exit zero.

An upstream snapshot was created before channel refresh, because refresh can upgrade packages before the updater creates its own snapshot.
The generated Limine recovery entry booted that snapshot. Upstream restoration recovered all 941 original package versions and the original UKI.
Two complete ready-state checks after restored normal boots passed, and the restored updater returned zero.
Home configuration and the 16 new migration markers remained, consistent with upstream root-only restoration.

Independent review accepted the scoped package, migration, kernel, and cross-version recovery evidence.
The earlier same-version marker experiment remains documented separately; it is not presented as part of this edge run.
Upstream encrypt/resume, random-seed permissions, login-keyring, and recovery-overlay warnings remain Tier 4 policy follow-ups.

The clone is stopped in restored stable root/package state, with migrated user-home state.
All saved baseline checksums and permissions still match.
Report: `.superpowers/sdd/task-7-edge-report.md`. Evidence: `out/omarchy-edge-test.Rc0HrW/`.

## Native Fault Results

The unmodified public CLI rejected three injected faults with exit 1 before disk writes:

- Corrupted manifest bytes with the original trusted digest.
- A missing authenticated input with external IPv4 and IPv6 networking blocked.
- An interrupted native preflight `pacman -Sw` process.

Every case displayed failure without success or automatic reboot.
The disk's written-sector counter remained zero. An independent comparison of the entire logical 32 GiB disk against a pristine image passed.
The fault VM is stopped; the blank disks and logs are retained.

These checks prove digest rejection, missing-input rejection under offline conditions, and preflight interruption handling.
They do not claim GPG rejection, network-loss-only rejection with a complete cache, or interruption during target package extraction.
Independent review accepted these scoped results.
Report: `.superpowers/sdd/task-7-fault-report.md`.

## CachyOS Offline Follow-Up

A fresh provider-aware CachyOS ISO installed the shipped VM Server workload with no virtual NIC.
All 418 target packages installed from the selected cache. Disk-only boot, password login, package-database checks, and default repositories passed.
ISO SHA-256: `4f4900d93c44d0d20f4bcd5c7e421b4561e0ffa2f0923596d70d9d3d294d820f`.

Complete offline provisioning failed: Oh My Zsh is a mandatory Git checkout outside the package cache.
Its firstboot clone failed without network access, and the Ansible abort prevented PostgreSQL initialization.
The package-installation and boot results are valid, but the server was not fully provisioned.

Review also identified two Tier 2 status defects in the shared firstboot generator.
It recorded Ansible failure but returned zero and printed a ready banner; its recovery hint contained literal escaped `&&`.
The source now preserves the Ansible exit status, selects the correct banner, and prints an executable manual recovery hint.
Pending-marker removal and manual retry policy remain unchanged.
Actual generated-script execution tests cover rc 0 and rc 2. Independent source review passed.
The full suite reports 900 passed, the two accepted baseline failures, and one skipped test.
The corrected status handling has not yet been exercised through a newly built native installation.

The remaining Tier 4 decision is the offline firstboot contract: bundle pinned, verified non-package resources at build time, or require network for firstboot.
Neither a provisioning bypass nor a silent weakening of the offline guarantee was applied.
M1 remains open at this contract decision. Install-time provider selection has not started.

Reports: `.superpowers/sdd/task-7-cachyos-offline-report.md` and `.superpowers/sdd/task-7-firstboot-status-report.md`.

## Verified Offline Firstboot

The user approved pinned build-time firstboot assets. The implementation bundles Oh My Zsh at immutable commit `6aa16696d508f368586c5dc215b0f73a41d9ff16`.
A full-history Git bundle preserves normal working trees, origin tracking, and updater capability.
Official HTTPS origin and the immutable commit establish initial provenance; no independently authenticated upstream signature is claimed.
Git object verification and staged SHA-256 checks protect the bundle before use.

Required target assets are selected from the workload and checked before disk setup.
An independent Ansible flag prevents a damaged new installation from falling back to online Git acquisition.
Legacy media without the new contract retains its existing online behavior.
Omarchy does not receive Arches shell assets. Runtime updates and custom files survive retries.

The new native CachyOS VM Server ISO has SHA-256 `22048905492ebe2c6a37b6665bd1b4a8743c59934218730681e7e3dcb8551eec`.
Public installation, firstboot, and two disk-only boots ran with no virtual NIC.
Real Ansible loaded the required-assets flag and configured both normal-user and root checkouts from the local bundle.
Both original shell themes and updater files remained intact.

Firstboot completed with 46 tasks ok, 36 changed, zero failed, two skipped, and four ignored.
PostgreSQL initialized through its normal role. SQL, Redis, nginx, SSH, and package-database checks passed with zero failed units.
No pending or failure marker remained.
A manual retry preserved legitimate newer Git commits, custom files, and repository configuration.
The next cold boot did not run firstboot again, and service health remained good.

Independent review accepted the native evidence and the combined scoped M1 gates.
The suite now reports 927 passed and the two accepted baseline failures, with no skipped tests.
The former skipped module test now uses a temporary directory instead of attempting host `/opt` writes.

This acceptance covers the selected Oh My Zsh/VM Server path, not Rust/pip assets, every template, or untested hardware.
ARM remains unit-regression evidence only. Existing optional hardware and upstream boot-policy warnings remain documented.
All task-owned VMs and the builder are stopped; saved baselines and original user changes are unchanged. No commits occurred.

Reports: `.superpowers/sdd/task-7-offline-assets-report.md` and `.superpowers/sdd/task-7-offline-assets-native-report.md`.
