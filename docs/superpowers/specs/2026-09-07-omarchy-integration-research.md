# Arches and Omarchy Integration Landscape

Date: 2026-09-07
Status: Research, not an approved implementation design.

This document preserves the full substance of the landscape response, including alternatives, trade-offs, unknowns, and the original recommendation.
It is not a verbatim transcript. Qualifications correct overstatements in that response.
Research used the local Arches checkout, Omarchy's `quattro` branch, and `omarchy-pkgs` on `master`.
Those upstream branches are mutable. No installation or runtime compatibility tests were performed.

## Purpose

Explore possible integrations before selecting an implementation.
The initial interest combined an Omarchy desktop module with adoption of useful operational ideas.
The subsequent direction is strategy A without CachyOS, with Omarchy owning package management.
That direction changes the trade-offs described here. The original recommendation assumed retention of CachyOS.

## 1. Shared System Mechanisms

Arches and Omarchy share important mechanisms on x86-64.
Shared mechanisms reduce integration work, but do not establish configuration compatibility.

| Layer | Arches | Omarchy |
|---|---|---|
| Bootloader | Limine and `limine-mkinitcpio-hook` | Limine and `limine-mkinitcpio-hook` |
| Snapshots | Snapper config `root`, `SNAPPER_CONFIGS="root"`, `limine-snapper-sync` | Same config names and tools |
| Initramfs | mkinitcpio | mkinitcpio |
| Kernel handling | Platform-defined kernel variants | Mostly generic boot tooling, with hardware-specific kernel assumptions |
| Networking | Configurable through Arches modules | NetworkManager-oriented system setup |

Relevant local source: `installer/arches_installer/core/snapper.py` and `installer/arches_installer/core/bootloader.py`.
Arches enables timeline snapshots with a number limit of 50.
Omarchy disables timeline creation and uses a number limit of 5.
Omarchy ships `ENABLE_UKI=yes` through a Limine drop-in.
It also ships a full mkinitcpio `HOOKS` override that includes Plymouth, encryption, and `btrfs-overlayfs`.

The original response called this integration a userspace layer rather than a port.
That description was too strong: Omarchy's desktop packages also configure the system.
The original claim of no kernel hardcoding referred to boot-image paths, not all scripts.
Hardware scripts do contain package-name assumptions.

## 2. Integration Conflicts

1. **Pacman configuration replacement.** `omarchy-apply-system`, `omarchy-refresh-pacman`, and `omarchy-channel-set` replace the pacman configuration and mirror list. This removes CachyOS repository entries. The inspected normal update path does not perform that replacement directly. Future migrations still require review.
2. **Repository compatibility.** Omarchy stable uses a delayed Arch mirror. Combining its binaries with current CachyOS libraries introduces compatibility risk. Quickshell's use of Qt private APIs is a concrete concern. Edge reduces snapshot mismatch, but does not guarantee CachyOS compatibility.
3. **System configuration replacement.** `omarchy-settings.install` deliberately copies files on installation and upgrade. These include `os-release`, `nsswitch.conf`, `faillock.conf`, `plymouthd.conf`, and the skeleton `.bashrc`. Apple Silicon has special handling.
4. **Update guard.** An ALPM hook blocks direct system upgrades and directs users to Omarchy's update command. The inspected bypass is `OMARCHY_ALLOW_DIRECT_PACMAN=1`. Bypassing the guard also bypasses its safety purpose.
5. **NVIDIA kernel detection.** The inspected kernel regex excludes `linux-cachyos`. This can prevent automatic installation of the correct headers. Actual DKMS failure depends on whether another installer already supplies those headers.
6. **Installation entry points.** The inspected tree has no legacy `boot.sh` or `install.sh`. Current documentation describes ISO installation. Sourceable setup leaves exist, but they are internal interfaces rather than a guaranteed third-party integration API.

Other overlaps include Snapper retention, SDDM, NetworkManager, firewall rules, Plymouth, hardware configuration, and system branding.
Removing CachyOS resolves the competing repository policy. It does not resolve all configuration ownership questions automatically.

## 3. Strategy Space

### A. Full Omarchy Installation Through Arches

The initial proposal adds an Omarchy module and template.
Arches installs the keyring, repository, Omarchy packages, and upstream default package set.
It calls `omarchy-apply-system --install-user` in the target chroot.
Omarchy then owns the desktop, migrations, and updates.

The original CachyOS variant restored the Arches pacman configuration after setup.
That creates competing owners: later channel changes can replace it again.
This variant is suitable only as a disposable experiment, not the recommended maintenance model.

**Benefit:** Most of the upstream desktop experience with little duplicated setup logic.

**Cost with CachyOS:** Repeated conflicts over repositories, system configuration, boot configuration, and update policy.

**Revised direction without CachyOS:** Arches provisions the machine using a coherent Omarchy repository set from the start.
Omarchy owns pacman and its update lifecycle without a configuration-restoration step.
The integration must also define which boot, snapshot, hardware, and user configuration belongs to Omarchy.

### B. Curated Omarchy Desktop With Arches System Ownership

Arches installs the desktop but does not run the complete `omarchy-apply-system` command.
An integration role runs selected user and login setup leaves.
It excludes repository, snapshot, and hardware setup that conflicts with Arches.
Arches supplies the update entry point and explicitly runs required Omarchy migrations.

**Benefit:** Preserves Arches' platform and module ownership while adopting the Omarchy desktop.

**Cost:** Maintains an adapter against changing upstream internals, including package installation scripts and migrations.
Skipping setup leaves alone does not prevent `omarchy-settings` package scripts from replacing files.
This requires more work than the original response implied.

**Original verdict:** Better ownership boundaries when CachyOS remains mandatory.
This recommendation does not apply automatically after removal of CachyOS.

### C. Arches Platforms Beyond the Standard Omarchy ISO

Arches has generic ARM and Apple Silicon platform definitions.
Omarchy has ARM-aware packaging and points users to community Apple Silicon projects.
An integration can combine these capabilities without replacing the Apple boot chain.

**Benefit:** Reuses Arches hardware and installation support for a different desktop experience.

**Cost:** Package availability, graphical operation, migrations, and boot recovery require separate ARM checks.
ARM build support does not prove that all required packages are published or usable.

The original response overstated novelty: community Omarchy-on-Mac integrations already exist.
The opportunity is an Arches-managed installation path, not the first Omarchy installation on Apple Silicon.

### D. Adopt Operational Ideas Without Omarchy Packages

| Idea | Arches context | Trade-off |
|---|---|---|
| One update command | No equivalent was established in this research | Coordinates snapshots, upgrades, migrations, and failure reporting |
| Timestamped migrations | Omarchy records completion per user | Simple mechanism, but retries and system-versus-user ownership need explicit rules |
| Stable, RC, and edge channels | Current x86-64 configuration uses CachyOS | Requires coherent repository snapshots, not channel names alone |
| `cidata` unattended configuration | Arches supports ISO-provided `auto-install.toml` | Separates machine configuration from ISO builds |
| Signed repository promotion | Arches includes a local repository in installation media | Useful for distributing maintained packages, with hosting and signing costs |
| Configuration refresh and theme templates | Arches already uses Ansible and Jinja templates | Reuse existing tools unless a desktop-specific need remains |

The original response estimated very small line counts for updates and migrations.
Those estimates were not implementation estimates and excluded recovery and integration work.
Likewise, Omarchy's `cidata` label uses a familiar transport convention, not a general cloud-init implementation.

### E. Lower-Priority Directions

**Use the Omarchy ISO, then apply Arches customization.**
The original response rejected this as a dead end.
That was not established by the research.
Documented unattended installs make this a possible route when preserving the Arches installer is not essential.
It offers less control over installation than direct integration through Arches.

**Contribute the Arches platform layer upstream.**
The original response assumed that Omarchy maintainers would reject a platform matrix.
No maintainer decision was obtained.
This remains an upstream collaboration question, not a demonstrated technical impossibility.

## 4. Unresolved Questions and Experiments

The original CachyOS experiment proposed one disposable QEMU installation and an `/etc` comparison before and after Omarchy setup.
It aimed to answer these questions:

1. Does Omarchy edge install and run against CachyOS libraries, especially Qt and Quickshell?
2. Do Omarchy UKI drop-ins coexist with Arches boot-entry generation?
3. Do `cachyos-settings` and `omarchy-settings` conflict at package or configuration level?
4. Does the required desktop package set run on the target ARM hardware?

The original one-day estimate was speculative.
Graphical checks, upgrades, and snapshot restoration were not performed.

For strategy A without CachyOS, questions 1 and 3 no longer define the first experiment.
The replacement checks cover installation from Omarchy's coherent repository set, first login, updates, kernel upgrades, and bootable snapshot restoration.
An ARM experiment remains separate.

## 5. Original Assessment and Revised Direction

Arches exposes platform, kernel, disk-layout, template, and module choices.
Omarchy supplies an integrated desktop experience and an opinionated update lifecycle.
These are different strengths, not proof that one project has universally better abstractions.

The original recommendation was B plus selected ideas from D, with C as a later extension.
It rejected A because the proposal retained CachyOS alongside Omarchy's repository ownership.

The user subsequently requested A without CachyOS.
That removes the central objection to A.
The new design can preserve Arches installation abstractions while handing package management and system policy to Omarchy.
The user clarified the scope: CachyOS remains the standard optimized package provider.
Omarchy, CachyOS, and stock upstream Arch are sibling provider choices.
Only Omarchy installations exclude CachyOS. This is not a project-wide replacement.
The implementation must separate provider selection from hardware platform and desktop template selection.

## 6. Documentation Finding

The Arches README describes workload roles under `ansible/roles/`.
Current templates instead compose `modules/<name>/module.toml`, with optional module-local Ansible roles and build scripts.
The earlier report classified the broader README refresh as Tier 4 and did not change it.
The README is not a reliable map for implementing the integration without source inspection.

## Sources

- [Omarchy website](https://omarchy.org/)
- [Omarchy source](https://github.com/omacom/omarchy/tree/quattro)
- [Package build repository](https://github.com/omacom/omarchy-pkgs)
- [Update policy](https://omarchy.org/manual/updates/)
- [Snapshot behavior](https://omarchy.org/manual/system-snapshots/)
- [Unattended installs](https://omarchy.org/manual/unattended-installs/)
- [Other platforms and community ports](https://omarchy.org/manual/omarchy-on/)
- [Installation entry points](https://github.com/omacom/omarchy/blob/quattro/agents/skills/install-scripts.md)
- [Omarchy package definition](https://github.com/omacom/omarchy-pkgs/blob/master/pkgbuilds/omarchy/PKGBUILD)
- [System configuration package](https://github.com/omacom/omarchy-pkgs/tree/master/pkgbuilds/omarchy-settings)
- Local `platforms/x86-64/platform.toml` and `platforms/x86-64/pacman.conf`
- Local `templates/kde-workstation.toml`, `modules/kde/module.toml`, and `modules/cosmic/module.toml`
- Local `installer/arches_installer/core/snapper.py` and `installer/arches_installer/core/bootloader.py`
