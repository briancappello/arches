# Provider Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate platform, provider, and template selection, prove an Omarchy installation, then offer supported providers from one installation ISO.

**Architecture:** Keep hardware constraints in the platform and workload composition in templates. A small provider definition selects repositories, package policy, and installation ownership. Pass the resolved provider explicitly through existing flows instead of adding a plugin framework.

**Tech Stack:** Python 3.11+, dataclasses, TOML, pytest, Textual, shell, Make, Podman, pacman, pacstrap, Ansible, archiso, and QEMU.

## Global Constraints

- This document is a plan, not authorization to implement or commit. Commit only after an explicit later request.
- Preserve unrelated worktree edits, including current builder, KDE, hardware, power, and llama.cpp changes.
- CachyOS remains the DEFAULT optimized provider. Stock Arch and Omarchy are sibling choices, not CachyOS overlays.
- Omarchy owns pacman and the update lifecycle on Omarchy targets. No CachyOS packages, repositories, settings, or hardware tooling enter those targets.
- Preserve existing ARM behavior. ALARM and Asahi are not stock x86-64 Arch and must retain their current repositories and boot constraints.
- **Proposed support boundary:** initial Omarchy support is x86-64, UEFI, and btrfs only, pending installation and runtime proof.
- Host alongside/replace modes, encryption, RAID, unusual subvolume layouts, and additional hardware remain unsupported for Omarchy until their proof gates pass.
- M1 uses a provider selected at build time. M2 offers supported target providers at installation time from one ISO.
- Alternative-provider installs can require network access. Multi-provider offline media is not required.
- Never use an unsafe remote-script pipe, signature bypass, disabled trust checks, or an Omarchy update-guard bypass.
- Add failing tests before each implementation change. Preserve a baseline and report existing failures separately from new failures.
- Do not run a repository-wide formatter. No new runtime dependency or generic lifecycle framework is planned.

## Inputs And Evidence

- Design companion: `docs/superpowers/specs/2026-09-07-provider-selection-design.md`.
- Research: `docs/superpowers/specs/2026-09-07-omarchy-integration-research.md`. Its mutable upstream sources are research, not release pins.
- `core/` paths in this plan refer to `installer/arches_installer/core/`. Test commands always use full paths.
- `core/platform.py` and `platforms/x86-64/platform.toml` combine hardware, CachyOS tier, kernels, package sets, and chwd policy.
- `core/install.py:29-35,249-305` uses ISO-global configuration/cache paths and copies live databases and trust state into the target.
- `_setup_local_repo_mirror` and `_make_pacman_conf_with_cache` introduce file mirrors. Shared names such as `core` and `extra` do not establish provider compatibility.
- `install_system:1261-1373` replaces pacman configuration, creates users, applies hardware/network configuration, enables services, and stages Ansible.
- `create_user:762-809` forces zsh and passwordless wheel sudo. `_pre_pacstrap_setup:1057` writes provisional boot and initramfs configuration.
- `core/pipeline.py` calls disk preparation before system installation, then bootloader, snapshots, and firstboot phases. `core/host_install.py` also calls `install_system` directly.
- `modules/base/ansible/tasks/main.yml` writes mirrors and pacman configuration. The actual network module is `modules/networking/`, not `modules/network/`.
- `scripts/iso-config.py:233` assembles live layers. Its `cmd_build_modules:307` unions modules across installable templates. Keep these concepts separate.
- `.pkg-cache/$PLATFORM` is platform-scoped. `.offline-cache`, `/opt/arches/pkg-cache`, and `/opt/arches-repo` are not provider-scoped.
- `scripts/lib/build-cache.sh` already hashes literal `str:` inputs. Reuse this for provider and build-environment identity.
- `Makefile:121-142` defines `test`, `test-unit`, `test-template`, and `dry-run`. `pyproject.toml` sets test discovery to `installer/tests`.
- `scripts/qemu-install.sh` selects the latest ISO by platform, not provider. Its `--fresh-disk` deletes fixed VM paths under `/tmp`.

## File Responsibilities

| Area | Existing files to change or inspect | Narrow addition |
|---|---|---|
| Provider model | `core/platform.py`, `core/template.py`, `core/module.py`, `platforms/*/platform.toml` | `core/provider.py`, `providers/<id>/provider.toml` |
| Package transactions | `core/install.py`, provider/platform pacman configuration | Provider-owned configuration and bootstrap lock under `providers/omarchy/` |
| Orchestration | `core/pipeline.py`, `core/host_install.py`, `core/firstboot.py`, `core/bootloader.py`, `core/snapper.py` | `core/omarchy.py` only for the proven upstream adapter |
| Build | `Makefile`, `scripts/build-iso.sh`, `scripts/builder.sh`, `scripts/cache-template-packages.sh`, `scripts/build-aur-repo.sh`, `scripts/lib/build-cache.sh`, `scripts/iso-config.py` | Provider-scoped staged metadata and caches |
| Input propagation | `installer/arches_installer/__main__.py`, `core/auto.py`, `core/host_install.py`, `tui/app.py`, `tui/progress.py`, `scripts/host-install.sh` | One provider field, not a second configuration system |
| Interactive selection | `tui/template_select.py`, `tui/module_select.py`, `tui/hardware_confirm.py`, `tui/confirm.py` | `tui/provider_select.py` |
| Workload and ownership | `templates/iso.toml`, existing module definitions and roles | `templates/omarchy-workstation.toml`, minimal `modules/omarchy/module.toml` only if composition needs it |
| Evidence | Existing core/TUI tests and research | `installer/tests/core/test_provider.py`, `test_provider_build.py`, `test_omarchy.py`, `installer/tests/tui/test_provider_select.py` |

All new paths are proposed deliverables, not existing APIs. Task 1 must reconcile them with the companion design before implementation.

## Milestones

**M1: Build-Time Provider Proof.** Complete Tasks 1-7. A build-selected Omarchy target installs, boots, updates, and restores a bootable snapshot. Existing CachyOS and ARM paths retain their behavior.

**M2: One ISO, Install-Time Choice.** Complete Tasks 8-10 after M1 passes. One ISO exposes supported providers, with online alternative-provider installation and pre-destructive failure checks.

---

## M1: Build-Time Provider Proof

### Task 1: Capture Baseline And Resolve Upstream Proof Gates

**Files:** Read the companion design, research, `Makefile`, `pyproject.toml`, and the source map above. Create `docs/superpowers/specs/2026-09-07-omarchy-provider-proof.md` during execution.

**Interface:** Produce an evidence record with source pins, trust roots, upstream phase ordering, support restrictions, baseline counts, and stop conditions. No installer interface is produced yet.

- [ ] Record `git status --short`, `git diff --stat`, and `git rev-parse HEAD` before any implementation.
- [ ] Run the baseline commands from the repository root. Record pass, fail, skip, and environment-error counts separately.

```bash
make test-unit
make test
make test-template
make dry-run
uv run pytest -q installer/tests/core/test_platform.py installer/tests/core/test_install.py installer/tests/core/test_pipeline.py installer/tests/core/test_host_install.py installer/tests/core/test_firstboot.py
```

Expected: existing behavior establishes the baseline. A failure is not automatically caused by provider work. Do not rewrite baseline expectations to hide a regression.

- [ ] Record x86-64 kernel/package/repository output and both ARM platform outputs before moving configuration fields.
- [ ] Inspect the actual upstream ISO repository, not only the Omarchy desktop repository.

The inspected upstream tree is `omacom/omarchy-iso` at `2673c613d9a71e23920e43fbb951238145e0f1e8`. This is a research reference, not an approved release pin.

```bash
gh api 'repos/omacom/omarchy-iso/git/trees/2673c613d9a71e23920e43fbb951238145e0f1e8?recursive=1'
gh api repos/omacom/omarchy-iso/releases
gh api repos/omacom/omarchy/releases
gh api repos/omacom/omarchy-pkgs/commits/master
```

These read-only commands discover evidence. They do not authenticate downloaded code.

- [ ] Read upstream `configs/airootfs/usr/local/bin/omarchy-iso-install` and `configs/airootfs/usr/share/omarchy-iso/orchestrator/{main,phases,phases_impl,archinstall_adapter,context}.py` at that reference.
- [ ] Read upstream `builder/build-iso.sh`, `builder/build-omarchy-packages.sh`, `builder/omarchy.gpg`, and `configs/pacman-online-{stable,rc,edge}.conf`.
- [ ] Trace package installation, signing-key bootstrap, package scripts, user creation, `/etc/skel`, system application, initramfs, bootloader, and finalization.
- [ ] Match this source trace to an authenticated upstream ISO artifact and a disposable upstream installation. Record the ISO digest and release identity.
- [ ] Record exact upstream commands, required environment, chroot/live/session context, and resulting files for each phase.
- [ ] Resolve which upstream packages provide the default package set, configuration, shell, update command, and recovery tooling.
- [ ] Record the authentication chain for the bootstrap keyring and packages, including full trusted signing fingerprints and independently checked provenance.

Required gate output:

| Question | Evidence required before implementation |
|---|---|
| Can Arches invoke system setup directly? | Exact pinned entry point, arguments, dependencies, and comparison with upstream ISO orchestration |
| When does the user exist? | Package/skel/user/application ordering, chosen shell, groups, sudo policy, ownership, and migration state |
| Who writes boot and snapshots? | Exact owners of Limine, UKI, mkinitcpio, Snapper, subvolumes, and final image generation |
| Is bootstrap authenticated? | Signed package or authenticated release chain, trusted fingerprint, digest lock, and failure behavior |
| Is stable coherent? | Matching Arch snapshot, Omarchy channel, package versions, repository configuration, and bootstrap versions |
| Which layouts work? | Proven ESP size/mount, subvolumes, encryption assumptions, and supported install modes |

If signatures or trust provenance cannot be established, stop Omarchy bootstrap work. HTTPS or a checksum beside an unsigned download does not resolve this gate.

**Observed blocker:** upstream `configs/pacman-online-stable.conf` at the inspected tree sets Omarchy `SigLevel = Optional TrustAll` and T2 `SigLevel = Never`. Do not copy those settings. This does not prove that packages lack signatures. Prove that required artifacts support an authenticated path with mandatory package signatures. Also prove that upstream refresh, channel, and update operations preserve acceptable trust policy without an Arches configuration-restoration loop. If upstream cannot meet both ownership and trust requirements, stop and record the conflict. Do not enable unproven T2 repositories for the initial target.

If upstream requires a different user or boot sequence, document that sequence before Task 5. Do not guess an `omarchy-apply-system --install-user` integration from the landscape alone.

**Review gate:** M1 remains blocked until this evidence identifies a reproducible, authenticated sequence. ARM support for Omarchy is not implied by ARM packaging.

### Task 2: Separate Platform, Provider, And Template Without Regression

**Files:** Create `core/provider.py`, `providers/cachyos/provider.toml`, `providers/arch/provider.toml`, and `providers/omarchy/provider.toml`. Modify `core/platform.py`, `core/template.py`, `core/module.py`, and platform TOML files. Extend `installer/tests/core/test_platform.py`, `test_template.py`, and `test_module.py`. Create `installer/tests/core/test_provider.py`.

**Suggested narrow interfaces:**

```python
resolve_provider(platform: PlatformConfig, provider_id: str | None = None) -> ProviderConfig
validate_selection(platform: PlatformConfig, provider: ProviderConfig,
                   template: InstallTemplate, *, firmware: str,
                   filesystem: str, install_mode: str) -> None
```

`ProviderConfig` carries `id`, resolved kernel variants, base packages, repository configuration, and the small ownership/compatibility data proven in Task 1. Reuse `KernelConfig` and `KernelVariant`. Do not add a registry of arbitrary executable hooks.

- [ ] Add tests that preserve the current default x86-64 kernel variants, CachyOS tier, chwd, and ordered package list.
- [ ] Add tests that resolve stock Arch and Omarchy without any CachyOS packages or chwd.
- [ ] Add rejection tests for unknown providers and incompatible provider/template/module combinations.
- [ ] Add tests for both ARM defaults: `linux-aarch64` with ALARM and `linux-asahi` with Asahi repositories.
- [ ] Preserve Apple `allow_auto_install = false`, GRUB behavior, firmware copying, and disabled chwd.

Example test for the proposed interface:

```python
import pytest
from arches_installer.core.provider import resolve_provider

@pytest.mark.parametrize("provider_id", ["arch", "omarchy"])
def test_alternative_provider_has_no_cachyos_packages(x86_64_platform, provider_id):
    provider = resolve_provider(x86_64_platform, provider_id)
    packages = provider.base_packages + [v.package for v in provider.kernel.variants]
    assert not any("cachyos" in package for package in packages)
    assert "chwd" not in packages

def test_x86_default_remains_cachyos(x86_64_platform):
    provider = resolve_provider(x86_64_platform)
    assert provider.id == "cachyos"
    assert provider.kernel.package == "linux-cachyos"
```

- [ ] Run the focused tests before implementation and record the expected missing-interface or wrong-selection failure.

```bash
uv run pytest -q installer/tests/core/test_provider.py installer/tests/core/test_platform.py installer/tests/core/test_template.py installer/tests/core/test_module.py
```

- [ ] Move x86 provider package, tier, mirror, and chwd decisions out of the hardware definition.
- [ ] Keep firmware, CPU architecture, disk safety constraints, boot capability, and platform-required kernel constraints in the platform.
- [ ] Preserve existing ARM profiles through explicit ALARM/Asahi resolution. Do not silently select CachyOS on ARM.
- [ ] Define platform-dependent omission defaults for existing configs. Existing configs and staged media are concrete compatibility consumers.
- [ ] Restrict Omarchy to the proven workload and platform combination. Reject conflicting modules instead of silently dropping requested behavior.
- [ ] Run the focused command again, then `make test-unit` and `make test-template`.

**Review gate:** Compare resolved defaults with Task 1 snapshots. Require unchanged CachyOS/ARM behavior and explicit alternative-provider package provenance.

### Task 3: Isolate Target Package Transactions From The Live System

**Files:** Modify `core/install.py` and `core/provider.py`. Add provider pacman configuration under `providers/cachyos/`, `providers/arch/`, and `providers/omarchy/`. Extend `installer/tests/core/test_install.py` and `test_provider.py`.

**Interface:** Pass the resolved `ProviderConfig` explicitly into `pacstrap` and `install_system` as a keyword argument. Keep transaction paths local to the installation, not mutable module globals.

- [ ] Add tests with live CachyOS `core.db` and target Omarchy `core.db` containing different package versions.
- [ ] Assert that target queries, synchronization, and pacstrap use only the selected provider database and repository configuration.
- [ ] Add keyring fixtures with a live-only trusted key. Assert that the target trust store does not inherit it.
- [ ] Add tests for missing cache identity, wrong provider/channel/architecture, invalid signatures, and absent required packages.
- [ ] Add failure-cleanup tests for cache mounts and temporary configuration after a failed pacstrap or finalization.

Required mock assertions in the package tests:

```python
# Capture every pacman command issued through the existing subprocess mock.
for argv in pacman_commands:
    assert "--config" in argv
    assert "--dbpath" in argv
    assert "--gpgdir" in argv
    assert argv[argv.index("--dbpath") + 1] != "/var/lib/pacman"
    assert argv[argv.index("--gpgdir") + 1] != "/etc/pacman.d/gnupg"
assert target_core_db.read_bytes() == omarchy_core_db.read_bytes()
assert not target_live_only_key.exists()
```

Here `pacman_commands` is the list captured from the existing command mocks. The three paths are distinct `tmp_path` fixtures, not real host paths.

- [ ] Run `uv run pytest -q installer/tests/core/test_install.py installer/tests/core/test_provider.py` and record the new failures.
- [ ] Replace ISO-global target configuration lookup in `_make_pacman_conf_with_cache` with the resolved provider source.
- [ ] Restrict `_setup_local_repo_mirror` to an authenticated, identity-matched cache. Never fall back to arbitrary live sync databases.
- [ ] Change `_preseed_pacman_databases` and `_preseed_pacman_keyring` to use selected, isolated sources rather than copying live state.
- [ ] Give `_query_available_packages` explicit database and keyring paths. A custom `--config` alone does not isolate pacman state.
- [ ] Review pacstrap `-c`, `-M`, and keyring behavior against the installed `arch-install-scripts` version before changing flags.
- [ ] Preserve writable download storage outside the small live overlay. Keep preflight metadata small and separate from post-mount package downloads.
- [ ] Keep target and live `core`/`extra` identities distinct even when repository names match.
- [ ] Make unavailable provider-required packages fatal. Do not reuse the existing package-deferral path for an incomplete Omarchy installation.
- [ ] Preserve coherent repository state across pacstrap, override installation, and target synchronization. Do not mix a delayed snapshot with current Arch packages.
- [ ] Remove temporary cache paths and file mirrors from final target configuration without restoring Arches policy over Omarchy policy.
- [ ] Run the focused tests again and `make test-unit`.

**Review gate:** Read the captured argv and file writes for every target transaction. No live database, mirror, keyring, or unsigned local artifact enters an alternative-provider target.

### Task 4: Add Build-Time Selection And Provider-Scoped Artifacts

**Files:** Modify `Makefile`, `scripts/build-iso.sh`, `scripts/builder.sh`, `scripts/cache-template-packages.sh`, `scripts/build-aur-repo.sh`, `scripts/lib/build-cache.sh`, and `scripts/iso-config.py`. Inspect `Containerfile`, `scripts/detect-platform.sh`, `scripts/build-usb.sh`, and `iso/profiledef.sh` before changing their inputs. Create `installer/tests/core/test_provider_build.py`.

**Interface:** Proposed build input `PROVIDER=omarchy`, exported downstream as `ARCHES_PROVIDER`. M1 stages one selected target provider and its identity. Omission retains existing per-platform behavior.

- [ ] Add subprocess tests for Make/shell propagation using temporary script stubs, not privileged container builds.
- [ ] Add package-list tests for CachyOS, stock Arch, Omarchy, generic ARM, and Apple Silicon defaults.
- [ ] Add cache tests where identical source/package names have different provider, channel, architecture, or repository snapshot identities.
- [ ] Add a staged-output test that builds CachyOS metadata, then Omarchy metadata, without deleting unrelated host caches.

Runnable cache regression example for the existing helper:

```python
import subprocess

def test_build_cache_hash_includes_provider():
    def digest(provider):
        return subprocess.check_output(
            ["bash", "-c",
             'source scripts/lib/build-cache.sh; '
             'compute_cache_hash "str:provider=$1" "str:source=fixed"',
             "cache-test", provider], text=True,
        ).strip()
    assert digest("cachyos") != digest("omarchy")
```

This example proves the existing hash primitive, not caller propagation. Add a failing caller test that captures the actual cache identity passed by `build-aur-repo.sh`.

- [ ] Run `uv run pytest -q installer/tests/core/test_provider_build.py` before implementation.
- [ ] Thread the provider input through both one-shot and persistent builders, including the privilege-drop environment in `build-aur-repo.sh`.
- [ ] Distinguish live package selection from target package selection in Make staging and cache assembly.
- [ ] Replace platform-only kernel/base-package reads with resolved platform/provider reads where they describe the target.
- [ ] Scope caches by provider, architecture/platform, channel/snapshot identity, and relevant build inputs. Reject unlabelled alternative-provider caches.
- [ ] Scope repository databases, package directories, build output directories, and `.cachehash` sidecars together.
- [ ] Include toolchain/base-image identity and dependency snapshot in custom binary cache keys. Source hashes alone do not establish ABI compatibility.
- [ ] Rebuild approved custom packages against the selected provider environment. Otherwise reject the module for that provider.
- [ ] Keep CachyOS-built Qt/KDE, llama.cpp, GPU, and AUR binaries out of Omarchy by default, regardless of matching package names.
- [ ] Preserve existing CachyOS local-repository behavior. Require an authenticated, reviewed provenance path before admitting any local package to Omarchy.
- [ ] Remove stale provider artifacts from ISO staging between builds. Do not delete or reset unrelated source changes or caches.
- [ ] Extend ISO metadata and output selection so a build proof cannot boot the wrong provider ISO by timestamp.
- [ ] Run the focused tests, `make test-unit`, and shell syntax checks for edited scripts.

```bash
bash -n scripts/build-iso.sh scripts/builder.sh scripts/cache-template-packages.sh scripts/build-aur-repo.sh scripts/lib/build-cache.sh
make -n iso PROVIDER=omarchy OFFLINE=0 TEMPLATE=omarchy-workstation
make -n builder-iso PROVIDER=omarchy OFFLINE=0 TEMPLATE=omarchy-workstation
```

Expected after implementation: both dry runs propagate the provider. `make -n` does not prove container environment propagation. The subprocess tests must prove that separately.

**Review gate:** Compare a CachyOS build followed by an Omarchy build against isolated clean builds. Require the same target package/repository manifests and no cross-provider cache hits.

### Task 5: Implement Only The Proven Omarchy Bootstrap And Ownership Sequence

**Files:** Create `core/omarchy.py`, `providers/omarchy/bootstrap.lock.toml`, `installer/tests/core/test_omarchy.py`, and `templates/omarchy-workstation.toml`. Modify `core/install.py`, `core/pipeline.py`, `core/firstboot.py`, and `core/provider.py`. Inspect `core/bootloader.py`, `core/snapper.py`, `core/hardware_rescan.py`, and `modules/hardware-rescan/ansible/files/arches-hardware-rescan` before enabling runtime services.

**Interface:** `core/omarchy.py` contains one explicit sequence from Task 1, not guessed upstream leaves. It consumes the selected provider, target mount, username, and log callback. It reports failure through the existing exception/log path. Keep it unreachable from destructive installation until Task 6 passes.

- [ ] Add tests that reject a bootstrap artifact with a wrong digest, missing signature, unexpected fingerprint, or unsupported upstream revision.
- [ ] Add command-order tests from the reviewed ISO trace. Use one event list across mocks so ordering is actually asserted.
- [ ] Add a user/skel test where Omarchy packages create `/etc/skel/.bashrc` before the approved user-copy phase.
- [ ] Add tests that preserve Omarchy shell, groups, sudo policy, home ownership, and migration state after all Arches phases.
- [ ] Add tests that an upstream setup failure prevents boot success, firstboot success, and automatic reboot-as-success.

Example negative ownership test using existing pipeline fixtures and Task 2 resolution:

```python
from pathlib import Path
from arches_installer.core.pipeline import run_install_pipeline
from arches_installer.core.provider import resolve_provider
from arches_installer.core.template import load_template, resolve_and_merge_modules

def test_omarchy_skips_arches_policy_phases(auto_params, mock_pipeline):
    auto_params.provider = resolve_provider(auto_params.platform, "omarchy")
    auto_params.template = resolve_and_merge_modules(
        load_template(Path("templates/omarchy-workstation.toml"))
    )
    run_install_pipeline(auto_params, log=lambda message: None)
    mock_pipeline["setup_snapshots"].assert_not_called()
    mock_pipeline["inject_firstboot_service"].assert_not_called()
```

Add a separate `install_system` test that asserts the approved adapter ran exactly once. The exact bootloader assertion follows Task 1 ownership evidence. In Task 6, extend the existing `mock_pipeline` fixture to stub preflight for phase-only tests. Preflight failure tests must supply their own explicit failure mock.

- [ ] Run `uv run pytest -q installer/tests/core/test_omarchy.py installer/tests/core/test_install.py installer/tests/core/test_pipeline.py installer/tests/core/test_firstboot.py` before implementation.
- [ ] Lock reviewed source revisions, artifact URLs, package versions, channel, digests, signature references, and trusted fingerprints.
- [ ] Download artifacts as files, authenticate them, then run only the reviewed local entry point. Never execute network output directly.
- [ ] Install a coherent Omarchy base from the start. Never install CachyOS first and remove it later.
- [ ] Replace forced zsh/passwordless-sudo user creation only on the Omarchy path with the proven upstream-compatible sequence.
- [ ] Install skel/configuration packages before home population when the upstream sequence requires that order.
- [ ] Create the user exactly once, with the approved shell and groups. Preserve secrets outside argv, logs, metadata, and evidence.
- [ ] Apply user/system configuration in the required execution context. Do not assume a root chroot equals a user login session.
- [ ] Run final boot-image generation only after final root identifiers, fstab, initramfs hooks, and Omarchy configuration are present.
- [ ] Replace unconditional Arches snapshot/firstboot phases with explicit ownership decisions. Do not install two snapshot owners.
- [ ] Exclude `base`, `zsh`, `networking`, and `snapshots` policy from Omarchy composition unless individually proven compatible.
- [ ] Audit hardware-injected packages, roles, files, services, and boot parameters. A clean template alone does not prevent policy injection.
- [ ] Prevent chwd and hardware-rescan from later installing CachyOS packages or replacing Omarchy hardware policy.
- [ ] Avoid `install_pacman_conf`, mirror refresh, `sync_chroot_databases`, broad override installs, and `preseed_network_deps` after Omarchy assumes ownership.
- [ ] Preserve the installed Omarchy pacman configuration after cache cleanup. Do not rewrite it with the staged Arches configuration.
- [ ] Prove rerun behavior or reject a partially provisioned target before mutation. Do not claim idempotence for unreviewed upstream commands.
- [ ] Run focused tests again. Compare package/configuration output against the authenticated upstream reference installation.

**Review gate:** Installation, firstboot, hardware handling, and subsequent updates have one owner per policy file. No later Arches setup undoes Omarchy configuration.

### Task 6: Propagate Identity And Fail Before Destructive Disk Operations

**Files:** Modify `installer/arches_installer/__main__.py`, `core/auto.py`, `core/host_install.py`, `core/pipeline.py`, `core/provider.py`, `tui/app.py`, `tui/progress.py`, and `scripts/host-install.sh`. Extend `installer/tests/core/test_auto.py`, `test_host_install.py`, `test_main.py`, `test_pipeline.py`, and `test_multi_disk_e2e.py`.

**Interfaces:** Add `provider: ProviderConfig` to resolved `InstallParams` and app state. Parse optional `[install].provider` and proposed CLI `--provider ID`. Add `preflight_provider(params: InstallParams) -> None` in `core/pipeline.py`, backed by Task 2 selection checks and Task 3 transaction isolation.

Precedence is explicit CLI selection, configuration selection, then staged build default. Reject an explicit selection that is unavailable in M1 media. Never silently fall back from a requested Omarchy install to CachyOS.

- [ ] Add table-driven tests for omitted provider, explicit CLI/config provider, conflicting selections, unknown values, and staged defaults.
- [ ] Assert that dry-run output includes platform, provider, template, kernel, channel, support restriction, and network/cache requirement without secrets.
- [ ] Add preflight failures for unsupported firmware/filesystem/mode, incompatible modules/hardware, unreachable repositories, invalid trust, and missing required packages.
- [ ] Assert zero disk calls on each failure, for both single-disk and resolved multi-disk flows.
- [ ] Add separate host-path tests: `run_host_install` must reject unsupported Omarchy modes before mounts, formatting, or subvolume changes.

Runnable preflight-order test after the proposed function exists:

```python
from unittest.mock import patch
import pytest
from arches_installer.core.pipeline import run_install_pipeline

def test_provider_preflight_precedes_disk_changes(auto_params, mock_pipeline):
    with patch("arches_installer.core.pipeline.preflight_provider",
               side_effect=RuntimeError("provider unavailable")):
        with pytest.raises(RuntimeError, match="provider unavailable"):
            run_install_pipeline(auto_params, log=lambda message: None)
    mock_pipeline["apply_disk_layout"].assert_not_called()
    mock_pipeline["install_system"].assert_not_called()
```

- [ ] Run the focused tests before implementation.

```bash
uv run pytest -q installer/tests/core/test_auto.py installer/tests/core/test_host_install.py installer/tests/core/test_main.py installer/tests/core/test_pipeline.py installer/tests/core/test_multi_disk_e2e.py
```

- [ ] Carry selection through `_run_auto`, `_run_host`, `_run_tui`, `ArchesApp`, and `InstallProgressScreen._run_install` without re-deriving it from live pacman.
- [ ] Preserve `scripts/host-install.sh` container propagation and existing Apple install restrictions. Unsupported providers must fail in the host path too.
- [ ] Run preflight before `apply_disk_layout`, `apply_disk_layout_resolved`, any host replacement, and any Arches-managed manual-disk mutation.
- [ ] For externally prepared mounts, reject incompatible targets before further writes. Arches cannot undo destructive commands already run outside the installer.
- [ ] Check package and bootstrap reachability, signatures, trust, coherent database state, and required dependency availability in an isolated preflight workspace.
- [ ] Distinguish a failed network check from invalid authentication. Neither can fall back to another provider or untrusted local state.
- [ ] Explain that preflight cannot guarantee later network availability. On later failure, preserve diagnostic state and do not report installation success.
- [ ] Persist non-secret target identity atomically at `<target>/var/lib/arches/provider.json` before provider-specific setup.
- [ ] Record provider, platform, template, installation channel, bootstrap revision, and install status. Mark completion only after successful finalization. Treat channel/revision as installation provenance, not an override of Omarchy's current runtime channel.
- [ ] Follow `core/disk_layout.py:write_disk_roles_state` for atomic replacement rather than adding a persistence framework.
- [ ] Reject a rerun with conflicting persisted identity. Runtime Arches tools must use target identity, never the currently booted ISO provider.
- [ ] Add round-trip, truncated-state, wrong-provider, and failed-install-status tests for persisted identity.
- [ ] Run focused tests again, `make test-unit`, and `make dry-run`.

Proposed CLI acceptance command, available only after this task:

```bash
uv run python -m arches_installer --auto templates/auto-install-omarchy.toml --platform platforms/x86-64/platform.toml --provider omarchy --dry-run
```

Create `templates/auto-install-omarchy.toml` in this task from the proven layout and Omarchy template. Use a non-production test credential policy, not a distributable real password. Dry-run performs no target mutation and cannot claim a successful privileged package transaction.

**Review gate:** Every entry point selects the same provider, records the same identity, and fails before its first destructive operation.

### Task 7: Prove M1 Installation, Update, Boot, And Rollback

**Files:** Modify `scripts/qemu-install.sh` only for explicit ISO selection. Extend `installer/tests/core/test_qemu_disk_descriptors.py`. Record results in `docs/superpowers/specs/2026-09-07-omarchy-provider-proof.md`.

**Interface:** Proposed `scripts/qemu-install.sh --iso PATH` selects an existing artifact without an implicit rebuild. Reuse existing VM/log behavior. Do not build another VM framework.

- [ ] Add a failing argument test that passes an explicit ISO and asserts that no newest-by-platform lookup or builder call replaces it.
- [ ] Add an error test for a missing explicit ISO before disk creation.
- [ ] Run `uv run pytest -q installer/tests/core/test_qemu_disk_descriptors.py` before implementing that option.
- [ ] Implement explicit artifact selection, then rerun the focused tests and `bash -n scripts/qemu-install.sh`.
- [ ] Complete Tasks 1-6 before any Omarchy installation on a disposable disk.
- [ ] Record the exact built ISO pathname, digest, source revision, provider identity, and upstream bootstrap lock.

Build commands after Tasks 2-6, not commands to run during planning:

```bash
sudo make iso PROVIDER=omarchy OFFLINE=0 TEMPLATE=omarchy-workstation
make builder-iso PROVIDER=omarchy OFFLINE=0 TEMPLATE=omarchy-workstation
```

Use one build command, according to the available builder. The second requires the existing persistent builder. Prove both input paths with unit tests.

CAUTION: The VM runner uses `/tmp/arches-test-disk.qcow2` and `/tmp/arches-efi-vars.raw`. `--fresh-disk` deletes those files. Use a dedicated execution environment with no valuable VM artifacts at those paths.

```bash
sha256sum "${OMARCHY_ISO:?Set the exact ISO path recorded in the proof report}"
PLATFORM=x86-64 OFFLINE=0 scripts/qemu-install.sh --iso "$OMARCHY_ISO" --fresh-disk --log /tmp/opencode/omarchy-install.log
make qemu-boot
```

- [ ] Prove a clean installation and a second boot without the ISO or any ISO cache mount.
- [ ] Prove first login, desktop startup, terminal launch, networking, graphics acceleration appropriate to the VM, and absence of failed provisioning.
- [ ] Record package and configuration evidence inside the installed guest.

```bash
pacman -Q
pacman-conf --repo-list
findmnt /
findmnt /boot
systemctl --failed
sudo snapper -c root list
sudo journalctl -b -p err
```

- [ ] Check package provenance, not only package names. Require no CachyOS repository, package, chwd action, or inherited live key trust.
- [ ] Compare final pacman, mirror, skel, user, initramfs, Limine, and Snapper policy with the approved upstream reference.
- [ ] Run the exact authenticated upstream update command recorded in Task 1 as the installed user, with its required privilege flow.
- [ ] Include an actual package update, an Omarchy migration, and a kernel/initramfs update. A no-op update does not prove the gate.
- [ ] Boot the updated kernel without installation media. Record kernel version, boot entry, and successful desktop login.
- [ ] Create a snapshot using the proven upstream mechanism, then make a harmless marker/configuration change in the guest.
- [ ] Select that snapshot through the upstream boot recovery path and restore it with the documented upstream rollback operation.
- [ ] Boot the restored default system twice. Check marker state, package database consistency, boot images, networking, desktop, and another update.
- [ ] Prove the actual btrfs `/var` and pacman database layout participates correctly in rollback. Arches var bind mounts cannot be assumed compatible.
- [ ] Exercise mirror refresh and any channel operation allowed by the supported configuration. Reject trust-policy weakening or restored bypass settings.
- [ ] Inject network and bootstrap failures on fresh disposable targets. Require failure diagnostics and no success marker.
- [ ] Repeat an existing CachyOS offline installation with `OFFLINE=1`, then boot it without the ISO.
- [ ] Run generic ARM regression tests and an ARM VM where available. Require Apple hardware evidence for Apple boot/firmware claims.

```bash
make test
make test-template
make dry-run
uv run pytest -q installer/tests/core/test_platform.py installer/tests/core/test_host_install.py installer/tests/core/test_bootloader.py installer/tests/core/test_snapper.py installer/tests/core/test_shipped_invariants.py
```

**M1 acceptance:** All four runtime gates pass: installation/login, update/migration, updated-kernel boot, and bootable snapshot restoration. Report unavailable ARM hardware checks as unverified, not passing. M2 cannot promote Omarchy as supported if any required M1 gate fails.

## M2: One ISO, Install-Time Choice

### Task 8: Stage Supported Providers Without Mixing Their Environments

**Files:** Modify `Makefile`, `templates/iso.toml`, `scripts/iso-config.py`, `scripts/cache-template-packages.sh`, `scripts/build-aur-repo.sh`, `core/provider.py`, and build wrappers from Task 4. Extend `installer/tests/core/test_provider_build.py` and `test_provider.py`.

**Interface:** Extend staged metadata under `/opt/arches/providers/` with available provider definitions, the default provider, and per-provider online/offline capability. The selected live/build provider remains separate from the selected target provider.

- [ ] Add tests that stage CachyOS, stock Arch, and proven Omarchy definitions in one x86-64 ISO.
- [ ] Assert that adding an Omarchy choice does not add Omarchy packages or repositories to the live CachyOS root.
- [ ] Assert that only supported platform/provider/template tuples appear in install metadata.
- [ ] Add tests for omitted alternative caches, matching default cache, wrong-provider cache, and missing mandatory provider artifacts.

Example manifest assertions in the temporary staging test:

```python
assert metadata["default_provider"] == "cachyos"
assert set(metadata["providers"]) == {"cachyos", "arch", "omarchy"}
assert metadata["providers"]["omarchy"]["requires_network"] is True
assert "omarchy" not in live_packages
assert not any("omarchy" in repo for repo in live_repositories)
```

The test obtains these values from staged files and generated lists, not constants supplied by its own fixture. The metadata keys are the proposed minimal contract for this task.

- [ ] Run `uv run pytest -q installer/tests/core/test_provider_build.py installer/tests/core/test_provider.py` before implementation.
- [ ] Stage provider definitions and authenticated bootstrap material without installing each provider into the live root.
- [ ] Keep `[framebuffer]` and `[graphical]` package assembly separate from `[install]` target-provider metadata.
- [ ] Filter target package/build-module resolution by provider compatibility instead of globally unioning incompatible modules.
- [ ] Preserve the default CachyOS offline cache when requested. Mark alternatives online unless their own complete authenticated cache is present.
- [ ] Keep `OFFLINE` as a build/cache control. Do not treat it as permission to use a different provider cache during installation.
- [ ] Extend the Task 6 resolver to accept any staged, supported target provider while retaining explicit selection precedence.
- [ ] Reject missing provider definitions, bootstrap locks, or authentication material before disk mutation, even if the network works.
- [ ] Run focused tests, `make test-unit`, and `make test-template`.

**Review gate:** A single ISO contains the supported choices and their requirements. It does not contain a mixed provider root or promise multi-provider offline operation.

### Task 9: Add Provider Choice To The Existing TUI And Input Flows

**Files:** Create `installer/arches_installer/tui/provider_select.py` and `installer/tests/tui/test_provider_select.py`. Modify `tui/app.py`, `tui/welcome.py`, `tui/network.py`, `tui/template_select.py`, `tui/module_select.py`, `tui/hardware_confirm.py`, `tui/confirm.py`, and `tui/progress.py`. Inspect `tui/partition.py`, `tui/disk_select.py`, and `tui/layout_select.py` for destructive actions. Extend existing template/module/confirmation/progress tests and `installer/tests/core/test_main.py`.

**Interface:** `ProviderSelectScreen` updates the existing app instance's selected provider. `ArchesApp.SCREENS` registers `provider_select`. All compatibility decisions call Task 2 validation, and installation still calls Task 6 preflight.

- [ ] Add Textual pilot tests for the default CachyOS choice, sibling labels, selection, back navigation, and keyboard-only operation.
- [ ] Add a test that unsupported providers display a reason and cannot advance.
- [ ] Add a test that an online-only provider cannot advance without successful provider-specific network/trust preflight.
- [ ] Add tests that provider changes invalidate incompatible templates, modules, hardware choices, and kernel choices.
- [ ] Add a test that the confirmation screen shows the selected provider and that progress passes that exact object to `InstallParams`.

Example UI test body inside the existing app fixture pattern:

```python
async with app.run_test() as pilot:
    app.push_screen("provider_select")
    await pilot.pause()
    assert app.selected_provider.id == "cachyos"
    await pilot.press("down", "enter")
    await pilot.pause()
    assert app.selected_provider.id == "arch"
```

Make the fixture supply supported provider metadata in stable order: CachyOS, stock Arch, Omarchy. Mock network probes in UI tests. Do not make the suite depend on external mirrors.

- [ ] Run `uv run pytest -q installer/tests/tui/test_provider_select.py installer/tests/tui/test_template_select.py installer/tests/tui/test_module_select.py installer/tests/tui/test_confirm.py installer/tests/tui/test_progress.py` before implementation.
- [ ] Insert the provider screen after initial welcome/network preparation and before any installer-managed destructive disk tool.
- [ ] Keep provider selection in app instance state, not a shared mutable class default.
- [ ] At `_select_template`, gate the transition to `module_select` using the selected provider instead of advancing unconditionally.
- [ ] Repeat compatibility checks after optional-module and hardware-profile resolution. Transitive dependencies can introduce prohibited packages and roles.
- [ ] Show `CachyOS (Default)`, `Stock Arch`, and `Omarchy` with architecture/layout/network requirements in the existing visual style.
- [ ] Preserve selection on back navigation. Clear only dependent values that are incompatible with the new provider.
- [ ] For an unavailable online provider, offer network repair or return to selection. Never silently substitute CachyOS.
- [ ] Carry the same choice through interactive, explicit auto, ISO-detected auto, host, and CLI dry-run paths.
- [ ] Keep unsupported Omarchy host/ARM modes visible as unsupported where relevant, rather than pretending that propagation adds support.
- [ ] Run the focused TUI command again, `make test`, and `make dry-run`.

**Review gate:** Provider choice changes target behavior, not just labels. No alternate input path or backward navigation bypasses compatibility and pre-destructive checks.

### Task 10: Prove One-ISO Behavior And Publish The Supported Matrix

**Files:** Extend `installer/tests/core/test_provider_build.py`, `test_pipeline.py`, `test_main.py`, and `installer/tests/tui/test_provider_select.py`. Update the proof report and focused provider documentation. Do not use this task for a general README rewrite.

**Interface:** A release evidence table ties one ISO digest to each supported target provider and its install/runtime results.

- [ ] Add a regression test that uses one staged manifest with a CachyOS live database and different provider target databases.
- [ ] Assert that all entry points resolve the same supported choices and that no target query uses live package state.
- [ ] Add failure tests for network loss, cache mismatch, bootstrap authentication failure, and provider change after preflight.
- [ ] Run focused tests before correcting any defects exposed by the new one-ISO cases.

```bash
uv run pytest -q installer/tests/core/test_provider_build.py installer/tests/core/test_provider.py installer/tests/core/test_install.py installer/tests/core/test_pipeline.py installer/tests/core/test_main.py installer/tests/tui/test_provider_select.py
```

- [ ] Build one ISO with CachyOS as the default live/build provider and all supported target definitions.
- [ ] Record its digest once. Use the same exact artifact for every target-provider row.
- [ ] Use fresh, disposable VM disks for CachyOS, stock Arch, and Omarchy installations. Do not rebuild between rows.
- [ ] Boot an interactive live entry for provider-choice tests, not the baked autoinstall entry. Run unattended cases separately.

```bash
sudo make iso PROVIDER=cachyos OFFLINE=1
sha256sum "${SHARED_ISO:?Set the exact shared ISO path recorded in the proof report}"
OFFLINE=1 scripts/qemu-install.sh --iso "$SHARED_ISO" --fresh-disk --log /tmp/opencode/provider-cachyos-offline.log
OFFLINE=0 scripts/qemu-install.sh --iso "$SHARED_ISO" --fresh-disk --log /tmp/opencode/provider-arch-online.log
OFFLINE=0 scripts/qemu-install.sh --iso "$SHARED_ISO" --fresh-disk --log /tmp/opencode/provider-omarchy-online.log
```

Run one VM command at a time under the Task 7 disposable-path caution. Select the matching provider interactively, or use the matching auto configuration from within the guest. The current QEMU `OFFLINE=1` removes the virtual NIC. With explicit `--iso`, it must not rebuild the artifact.

- [ ] Prove offline CachyOS installation retains the existing advertised behavior for the selected cached workload.
- [ ] Prove stock Arch and Omarchy install online from that ISO without CachyOS target packages, keys, or database inheritance.
- [ ] Disconnect the network and request an online-only alternative. Require failure before partitioning, formatting, or subvolume deletion.
- [ ] Run Task 7 installation/update/boot/rollback gates for Omarchy from the shared ISO.
- [ ] Boot and update stock Arch using its native lifecycle. Prove CachyOS still uses its existing kernel/repository/hardware path.
- [ ] Run CLI, auto, host rejection, and TUI acceptance paths. Compare installed `provider.json` with the requested provider.
- [ ] Check that no `/opt/arches/pkg-cache`, installation file mirror, or temporary keyring path remains in runtime pacman configuration.
- [ ] Run the final suite and compare failure counts with Task 1.

```bash
make test-unit
make test
make test-template
make dry-run
git diff --check
git status --short
```

**M2 acceptance:** One ISO digest proves supported provider selection, online alternative installs, safe offline rejection, persisted identity, and preserved default behavior. Multi-provider offline payloads are explicitly excluded.

## Stop Conditions And Open Design Gates

| Gate | Required resolution | Blocks |
|---|---|---|
| Upstream signatures | Authenticated bootstrap and runtime trust policy without `TrustAll`, `Never`, or an Arches rewrite loop | Omarchy M1 |
| Upstream lifecycle sequencing | Pinned ISO trace and reference install establish user/skel/system/boot/finalization order | Omarchy M1 |
| Snapshot layout | Bootable restoration includes coherent package database and boot artifacts with the chosen subvolumes | Omarchy support |
| Custom binary compatibility | Provider-specific build environment, signatures, and dependency/ABI evidence | Each affected module |
| Additional hardware/modes | Runtime evidence for NVIDIA, T2, encryption, RAID, host alongside/replace, or ARM Omarchy | Only those combinations |
| ARM test hardware unavailable | Preserve existing tests/configuration and report hardware results as unverified | Claims of new ARM runtime proof |

The upstream signature-policy conflict is **Tier 4**: resolution needs upstream evidence and an ownership decision. This plan records the blocker without changing application code.

## Execution Rules

- Each task ends with its focused tests and a diff review. No commit occurs without a later explicit request.
- Stop at an unresolved proof gate rather than inventing an integration contract.
- Re-read files with concurrent edits before changing them. Preserve the initial worktree changes throughout execution.
- Do not run `make fmt`, `make clean-all`, USB writes, host installs, or VM deletion commands as documentation checks.
- Distinguish unit evidence, build evidence, installation evidence, and runtime evidence in the report.
- A successful ISO build or mocked phase test does not prove an Omarchy installation.

## Planning Verification

This planning pass inspected local source through CodeGraph, then read relevant configuration and shell files. It checked the upstream ISO tree and stable pacman configuration at a fixed research reference.

Executed during planning: `git status --short`, `git rev-parse --show-toplevel`, and `make -n test test-unit test-template dry-run`. The dry run matched the commands documented here. No implementation, installation, runtime test, build, or commit was performed. Task 1 captures the executable test baseline before implementation.

The document passed `git diff --no-index --check /dev/null docs/superpowers/plans/2026-09-07-provider-selection.md`. The task/placeholder scan found ten tasks and no unfinished placeholder markers. The completed companion design was reviewed against both milestones, ownership rules, compatibility boundaries, package isolation, and runtime proof gates. Detailed upstream commands remain explicitly blocked on Task 1 evidence, not delegated to guessed implementation.
