# Provider Selection Design

Date: 2026-09-07
Status: Approved direction; detailed support boundaries and upstream proof gates remain subject to review.

## Goal

Separate hardware platforms, package providers, and workload templates.
CachyOS remains the default optimized provider on x86-64.
Stock Arch and Omarchy are sibling choices, not overlays on CachyOS.

Deliver two milestones:

1. Build-time provider selection and a proven Omarchy installation.
2. Install-time selection from one ISO, with network access required for alternatives without complete provider-specific caches.

Multi-provider offline media is not required.
No existing machine conversion or project-wide CachyOS removal is planned.

## Ownership

| Concern | Owner |
|---|---|
| Architecture, firmware, hardware constraints, disk safety | Arches platform |
| Repository set, trust roots, channel, provider package defaults | Selected provider |
| Compatible desktop and workload composition | Arches template and modules |
| Installation orchestration, input validation, diagnostics | Arches installer |
| Omarchy system configuration, desktop, migrations, updates | Omarchy |
| Existing CachyOS and ARM behavior | Existing Arches paths, preserved during separation |

Provider defaults resolve against platform constraints. They cannot replace an Apple boot chain with an x86 boot policy.
ALARM and Asahi remain architecture-specific foundations. Stock x86-64 Arch is not an ARM repository replacement.

Use a small TOML provider definition and explicit resolved state through existing functions.
Do not create a generic plugin framework or arbitrary lifecycle hooks.
Omarchy needs one narrow adapter based on its actual ISO sequence, not a collection of guessed setup leaves.

## Compatibility

Not every platform/provider/template combination is valid.
Validate the complete selection, including transitive modules and hardware-injected packages or roles.
Reject incompatible combinations with a reason. Never silently substitute providers or discard requested modules.

The proposed first Omarchy proof is x86-64, UEFI, btrfs, and the Omarchy workstation template in a disposable VM.
Encryption, RAID, host-side replacement, ARM Omarchy, and additional hardware need separate proof before support claims.
These are initial verification boundaries, not architectural exclusions.

## Package Isolation

The live ISO provider and target provider are separate identities.
A CachyOS live desktop can install an Omarchy target without changing its own provider.

Target transactions use the selected provider's configuration, databases, and authenticated trust roots.
Matching repository names do not establish compatibility: `core.db` can describe different snapshots for different providers.
Alternative-provider transactions must not inherit the live pacman databases or trust store.

Cached databases, packages, and custom binaries carry provider, architecture, channel, and build-environment identity.
An unmatched cache is unavailable, not a fallback source.
Existing CachyOS offline operation remains supported for its bundled workloads.
Alternative providers initially download packages unless their own complete cache is available.

Use Omarchy's coherent package set from the first target transaction.
Do not install CachyOS and then attempt to convert that target.
Keep mandatory signature checks for the new integration. Do not bypass Omarchy's update guard.

## Installation Flow

1. Resolve platform, provider, template, modules, and hardware requirements.
2. Validate support and authenticated repository/bootstrap availability before installer-managed destructive operations.
3. Prepare the selected disk layout through the existing Arches flow.
4. Install the selected provider's kernel and packages into the target with isolated transaction state.
5. Apply the proven provider-specific user, system, boot, and finalization sequence.
6. Remove temporary installation paths without replacing provider-owned runtime policy.
7. Record successful completion only after finalization succeeds.

Persist non-secret installation identity under `/var/lib/arches/` using the existing atomic-write pattern.
Include provider, platform, template, installation channel/revision, and completion status.
This is installation provenance, not a second authority for Omarchy's current runtime channel.

Omarchy's package scripts, skeleton files, user creation, and user finalization require explicit ordering.
Current Arches code forces zsh and applies additional configuration after package installation.
The Omarchy path must not inherit those policies accidentally.
Likewise, later snapshot, network, hardware-rescan, or Ansible phases must not overwrite Omarchy policy.

Exact boot generation and user-finalization commands require a pinned upstream ISO trace and reference installation before implementation.
The adapter must preserve one owner per policy, including Snapper and boot configuration.

## Failure Behavior

Unknown providers, incompatible selections, missing required packages, and authentication failures stop installation.
Online-only providers fail preflight without usable repository access.
Preflight does not guarantee that the network remains available during installation.
Later failures preserve logs and incomplete status instead of reporting success or selecting another provider.

Arches cannot undo disk commands already performed by the user outside the installer.
Its preflight guarantee covers its own destructive operations.
Dry runs report intended selection and restrictions without mutating the target or claiming runtime proof.

## Milestones

### M1: Provider Separation

Move provider-specific repositories, optimization defaults, package choices, and hardware-tool policy out of the hardware-only definition.
Preserve existing platform defaults and shipped configuration behavior.
Add build-time selection, scoped artifacts, isolated target transactions, and an Omarchy adapter.
Carry provider identity through CLI, auto-install, host-install validation, and installer state.

M1 acceptance requires installation and desktop login, update with migration, updated-kernel boot, and bootable snapshot restoration.
The btrfs layout must restore a coherent package database and root filesystem.
A successful build or mocked test does not satisfy these runtime gates.

### M2: Installation-Time Selection

Stage supported provider definitions in one ISO without installing their packages into the live desktop.
Add a provider screen before installer-managed destructive disk operations.
Default to CachyOS on x86-64 and show the network and compatibility requirements for alternatives.

Selection precedence is explicit CLI input, unattended configuration, then the staged default.
Changing providers invalidates incompatible dependent selections.
Back navigation preserves compatible choices. All input paths use the same validation.

Prove CachyOS offline and stock Arch/Omarchy online from the same ISO digest.
Prove safe offline rejection for alternatives without caches.
The installed system must boot and update without installation media.

## Upstream Proof Gate

The planning trace found permissive signature settings in the inspected upstream ISO configuration.
This is a Tier 4 integration risk, not evidence that every upstream package is unsigned.
The plan requires an authenticated bootstrap and acceptable runtime trust policy without a recurring Arches configuration-restoration step.

If upstream refresh operations cannot preserve that policy, stop and request an ownership/security decision.
Do not silently weaken signature checks or claim that HTTPS alone authenticates the signing key.

The implementation plan records the inspected upstream revision and the exact evidence required.
Release pins, key fingerprints, user ordering, and boot ordering are proof outputs, not invented values in this design.

## Verification and Scope

Capture the test baseline before implementation and report existing failures separately.
Add focused regression tests for provider resolution, database isolation, cache identity, ownership, and failure-before-disk behavior.
Use existing pytest, Textual, Podman, and QEMU tooling.
Retain ARM regression coverage and report unavailable hardware checks as unverified.

No changes to running installations, dependency additions, commits, or broad cleanup are authorized by this document.

## Related Documents

- [Integration landscape](2026-09-07-omarchy-integration-research.md)
- [Implementation plan](../plans/2026-09-07-provider-selection.md)
