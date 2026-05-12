# ─────────────────────────────────────────────────────────
# Arches — Custom Install & Recovery ISO
# ─────────────────────────────────────────────────────────

SHELL       := /bin/bash
PROJECT_DIR := $(shell pwd)
ISO_PROFILE := $(PROJECT_DIR)/iso
INSTALLER   := $(PROJECT_DIR)/installer
TEMPLATES   := $(PROJECT_DIR)/templates
MODULES_DIR := $(PROJECT_DIR)/modules
ANSIBLE_DIR := $(PROJECT_DIR)/ansible
PLATFORMS   := $(PROJECT_DIR)/platforms
SCRIPTS     := $(PROJECT_DIR)/scripts
WORK_DIR    := /tmp/arches-work
OUT_DIR     := $(PROJECT_DIR)/out
OFFLINE     ?= 1
ISO_MODE    ?= graphical

# Optional template filter. When set, restricts the ISO build to a
# single template from iso.toml's [install].templates. Accepts either
# "llm-inference" or "llm-inference.toml". Exported as ARCHES_TEMPLATE
# so downstream scripts (iso-config.py, cache-template-packages.sh,
# build-aur-repo.sh) all operate on the same filtered list.
TEMPLATE         ?=
ARCHES_TEMPLATE  := $(TEMPLATE)
export ARCHES_TEMPLATE

# ─── Phony targets ────────────────────────────────────

.PHONY: help \
        iso iso-fb usb usb-fb \
        builder-start builder-stop builder-status builder-iso builder-iso-fb \
        sv-install sv-dry-run sv-uninstall \
        fmt test test-unit test-template dry-run \
        qemu-install qemu-install-llm qemu-boot qemu-raid qemu-disk qemu-ansible \
        clean clean-caches clean-all \
        _iso _aur-repo \
        _stage-installer _stage-modules _stage-ansible _stage-hardware _stage-platform _stage-bootconfig \
        _assemble-packages _stage-graphical _cache-packages _stage-offline-cache \
        _check-root _check-deps

# ─── Default ──────────────────────────────────────────

help: ## Show this help
	@printf '\n  \033[1mArches Build Targets\033[0m\n'
	@current_section=""; \
	grep -E '^[a-zA-Z_-]+:.*##|^##@' $(MAKEFILE_LIST) | \
	while IFS= read -r line; do \
		if echo "$$line" | grep -qE '^##@'; then \
			section=$$(echo "$$line" | sed 's/^##@ *//'); \
			printf '\n  \033[1;33m%s\033[0m\n' "$$section"; \
		else \
			target=$$(echo "$$line" | sed 's/:.*//' ); \
			desc=$$(echo "$$line" | sed 's/.*## //' ); \
			printf '    \033[36m%-20s\033[0m %s\n' "$$target" "$$desc"; \
		fi; \
	done
	@printf '\n'

# ─────────────────────────────────────────────────────────
# User-facing workflows
# ─────────────────────────────────────────────────────────

##@ Build

iso: ## Build graphical live ISO (OFFLINE=0 skip cache; TEMPLATE=<name>, ARCHES_GPU=<stack-list>)
	ISO_MODE=graphical OFFLINE=$(OFFLINE) TEMPLATE=$(TEMPLATE) ARCHES_GPU=$(ARCHES_GPU) $(SCRIPTS)/build-iso.sh

iso-fb: ## Build framebuffer-only ISO (TEMPLATE=<name>, ARCHES_GPU=<stack-list>)
	ISO_MODE=fb OFFLINE=$(OFFLINE) TEMPLATE=$(TEMPLATE) ARCHES_GPU=$(ARCHES_GPU) $(SCRIPTS)/build-iso.sh

usb: ## Build install media and write to USB drive (requires sudo + Podman)
	ISO_MODE=graphical OFFLINE=$(OFFLINE) TEMPLATE=$(TEMPLATE) ARCHES_GPU=$(ARCHES_GPU) $(SCRIPTS)/build-usb.sh

usb-fb: ## Build framebuffer-only USB install media
	ISO_MODE=fb OFFLINE=$(OFFLINE) TEMPLATE=$(TEMPLATE) ARCHES_GPU=$(ARCHES_GPU) $(SCRIPTS)/build-usb.sh

##@ Persistent Builder

builder-start: ## Start persistent build container (requires sudo)
	$(SCRIPTS)/builder.sh start

builder-stop: ## Stop persistent build container (requires sudo)
	$(SCRIPTS)/builder.sh stop

builder-status: ## Check if persistent builder is running
	@$(SCRIPTS)/builder.sh status

builder-iso: ## Build graphical ISO via persistent builder (no sudo)
	OFFLINE=$(OFFLINE) TEMPLATE=$(TEMPLATE) ARCHES_GPU=$(ARCHES_GPU) $(SCRIPTS)/builder.sh build

builder-iso-fb: ## Build framebuffer ISO via persistent builder (no sudo)
	OFFLINE=$(OFFLINE) TEMPLATE=$(TEMPLATE) ARCHES_GPU=$(ARCHES_GPU) $(SCRIPTS)/builder.sh build-fb

##@ Host Install (Apple Silicon)

CONFIG ?= templates/host-install.toml

host-install: _check-root ## Install Arches into btrfs subvolumes from running host (CONFIG=path)
	@echo "══ Host Install (Apple Silicon) ══"
	$(SCRIPTS)/host-install.sh $(CONFIG)

host-install-rebuild: _check-root ## Rebuild container image and install (CONFIG=path)
	@echo "══ Host Install — Rebuild ══"
	$(SCRIPTS)/host-install.sh --rebuild $(CONFIG)

host-install-dry: _check-root ## Dry-run host install — validate config, print plan (CONFIG=path)
	@echo "══ Host Install — Dry Run ══"
	$(SCRIPTS)/host-install.sh --dry-run $(CONFIG)

host-clean: _check-root ## Remove Arches subvolumes and GRUB entry (CONFIG=path)
	$(SCRIPTS)/host-clean.sh $(CONFIG)

##@ Development

fmt: ## Auto-format and lint Python code with ruff
	@echo "══ Linting ══"
	uv run ruff format installer/
	uv run ruff check --fix installer/

test: ## Run all tests (unit + TUI)
	@echo "══ Running all tests ══"
	uv run pytest -v

test-unit: ## Run fast unit tests only (no TUI/textual tests)
	@echo "══ Running unit tests ══"
	uv run pytest -v installer/tests/core/

test-template: ## Validate all TOML templates parse correctly
	@echo "══ Validating templates ══"
	uv run python -c "\
from arches_installer.core.template import discover_templates; \
templates = discover_templates(); \
print(f'Loaded {len(templates)} templates:'); \
[print(f'  - {t.name}: {t.description}') for t in templates]"

dry-run: ## Dry-run the example auto-install config
	@echo "══ Dry run ══"
	uv run python -m arches_installer \
		--auto templates/auto-install.toml \
		--platform platforms/x86-64/platform.toml \
		--dry-run

##@ QEMU

qemu-install: ## Build ISO + boot QEMU VM (OFFLINE=1 for offline install, no network)
	OFFLINE=$(OFFLINE) $(SCRIPTS)/qemu-install.sh

qemu-install-llm: ## Two-disk QEMU install (20G root + 60G bulk, simulates llm-workstation)
	OFFLINE=$(OFFLINE) $(SCRIPTS)/qemu-install.sh --disk 20G --disk 60G

qemu-boot: ## Boot the installed test disk in QEMU (UEFI, no ISO)
	@echo "══ Booting installed disk in QEMU (UEFI) ══"
	@echo "  SSH: ssh -p 2222 <user>@localhost"
	@echo "  Serial: Ctrl-A X to quit"
	@if [ "$$(uname -m)" = "aarch64" ]; then \
		[ -f /tmp/arches-efi-vars.raw ] || \
			{ echo "ERROR: No EFI vars — run make qemu-install first"; exit 1; }; \
		qemu-system-aarch64 -M virt -enable-kvm -cpu host -m 4G -smp 4 \
			-drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/aarch64/QEMU_EFI-pflash.raw \
			-drive if=pflash,format=raw,file=/tmp/arches-efi-vars.raw \
			-device virtio-gpu-pci -device qemu-xhci -device usb-kbd -device usb-tablet \
			-serial mon:stdio \
			-drive file=/tmp/arches-test-disk.qcow2,format=qcow2,if=virtio \
			-net nic -net user,hostfwd=tcp::2222-:22; \
	else \
		[ -f /tmp/arches-efi-vars.raw ] || \
			{ echo "ERROR: No EFI vars — run make qemu-install first"; exit 1; }; \
		qemu-system-x86_64 -enable-kvm -cpu host -m 4G -smp 4 \
			-drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/x64/OVMF_CODE.4m.fd \
			-drive if=pflash,format=raw,file=/tmp/arches-efi-vars.raw \
			-vga virtio \
			-serial mon:stdio \
			-drive file=/tmp/arches-test-disk.qcow2,format=qcow2,if=virtio \
			-net nic -net user,hostfwd=tcp::2222-:22; \
	fi

qemu-raid: ## Boot QEMU VM with multiple disks for RAID testing (RAID_DISKS=2 RAID_DISK_SIZE=120G)
	$(SCRIPTS)/qemu-raid.sh

qemu-disk: ## Create a fresh QEMU test disk image (60G)
	@echo "══ Creating test disk ══"
	qemu-img create -f qcow2 /tmp/arches-test-disk.qcow2 60G
	@echo "Created /tmp/arches-test-disk.qcow2 (60G)"

VM_SSH_PORT := 2222
VM_USER     := arches
TAGS        ?= all

qemu-ansible: ## Run Ansible roles against the QEMU VM (TAGS=all)
	@echo "══ Running Ansible against VM (tags: $(TAGS)) ══"
	ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook $(ANSIBLE_DIR)/playbook.yml \
		-i $(ANSIBLE_DIR)/inventory/dev-vm.ini \
		--become \
		-e ansible_user=$(VM_USER) \
		-e install_user=$(VM_USER) \
		-e platform_arch=x86_64 \
		-e cachyos_optimization_tier=x86-64-v3 \
		-e pacman_architectures=x86_64,x86_64_v2,x86_64_v3 \
		-e cachyos_tier_mirrorlist=cachyos-v3-mirrorlist \
		--tags $(TAGS) \
		-v

##@ Cleanup

clean: ## Remove staged files from ISO airootfs
	@echo "══ Cleaning staged files ══"
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches/installer
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches/templates
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches/modules
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches/disk-layouts
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches/ansible
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches/platform
	rm -f  $(ISO_PROFILE)/airootfs/usr/local/bin/arches-install
	rm -f  $(ISO_PROFILE)/airootfs/root/auto-install.toml
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches-repo
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches/pkg-cache
	rm -f  $(ISO_PROFILE)/airootfs/opt/arches/build-host.pub
	rm -f  $(ISO_PROFILE)/packages.x86_64
	rm -f  $(ISO_PROFILE)/packages.aarch64
	rm -f  $(ISO_PROFILE)/airootfs/etc/pacman.d/hooks/archiso-mkinitcpio-preset.hook
	rm -f  $(ISO_PROFILE)/airootfs/etc/mkinitcpio.conf.d/archiso.conf
	rm -f  $(ISO_PROFILE)/grub/grub.cfg
	rm -f  $(ISO_PROFILE)/grub/loopback.cfg
	rm -f  $(ISO_PROFILE)/syslinux/archiso_sys-linux.cfg
	rm -f  $(ISO_PROFILE)/syslinux/archiso_pxe-linux.cfg
	rm -f  $(ISO_PROFILE)/pacman.conf
	# Graphical staging artifacts
	rm -f  $(ISO_PROFILE)/airootfs/etc/systemd/system/arches-liveuser.service
	rm -f  $(ISO_PROFILE)/airootfs/etc/systemd/system/arches-live-sshd.service
	rm -f  $(ISO_PROFILE)/airootfs/etc/systemd/system/display-manager.service
	rm -f  $(ISO_PROFILE)/airootfs/etc/systemd/system/multi-user.target.wants/arches-liveuser.service
	rm -f  $(ISO_PROFILE)/airootfs/etc/systemd/system/multi-user.target.wants/arches-live-sshd.service
	rm -f  $(ISO_PROFILE)/airootfs/usr/local/bin/arches-live-sshd-setup
	rm -rf $(ISO_PROFILE)/airootfs/etc/systemd/system/sddm.service.d
	rm -rf $(ISO_PROFILE)/airootfs/etc/sddm.conf.d
	rm -rf $(ISO_PROFILE)/airootfs/etc/xdg
	rm -rf $(ISO_PROFILE)/airootfs/usr/share/applications
	rm -f  $(ISO_PROFILE)/airootfs/usr/local/bin/arches-liveuser-setup
	rm -rf $(ISO_PROFILE)/airootfs/home


clean-caches: ## Remove AUR repo, pacman cache, and offline package cache
	@echo "══ Cleaning caches ══"
	@# The persistent builder bind-mounts .pkg-cache/ and .offline-cache/.
	@# Deleting those directories while the container is running makes the
	@# mounts stale — the next build inside the container will fail with
	@# "No such file or directory" when pacman tries to write to the cache.
	@# Stop the builder first so the mounts are released cleanly.
	@if sudo -n podman inspect --format '{{.State.Running}}' arches-builder 2>/dev/null | grep -q true; then \
		echo "  Persistent builder is running — stopping it first..."; \
		sudo -n podman rm -f arches-builder >/dev/null 2>&1 || { \
			echo "ERROR: The persistent builder (arches-builder) is running and"; \
			echo "       bind-mounts the cache directories. Deleting them while"; \
			echo "       the container is running will break the next build."; \
			echo ""; \
			echo "       Stop it first:  make builder-stop"; \
			echo "       Then retry:     make clean-caches"; \
			exit 1; \
		}; \
		echo "  Builder stopped."; \
	fi
	rm -rf .aur-repo
	rm -rf .pkg-cache
	rm -rf .offline-cache

clean-all: clean clean-caches ## Remove all build artifacts (incl. builder container + image)
	@echo "══ Cleaning output ══"
	rm -rf $(OUT_DIR)
	rm -f /tmp/arches-test-disk.qcow2
	rm -f /tmp/arches-test-disk-*.qcow2
	rm -f /tmp/arches-efi-vars.raw
	rm -f /tmp/arches-raid-disk-*.qcow2
	rm -f /tmp/arches-raid-efi-vars.raw
	@echo "══ Cleaning builder container + image ══"
	@# clean-caches already stopped/removed the arches-builder container
	@# (it bind-mounts the caches). Also remove the builder image so the
	@# next build starts from a clean slate. Both amd64 and arm64 variants
	@# are removed if present.
	@for img in arches-builder-amd64 arches-builder-arm64; do \
		if sudo -n podman image exists "$$img" 2>/dev/null; then \
			echo "  Removing image: $$img"; \
			sudo -n podman rmi -f "$$img" >/dev/null 2>&1 || \
				echo "  WARNING: failed to remove $$img (in use?)"; \
		fi; \
	done

# ─────────────────────────────────────────────────────────
# Internal targets (not shown in help)
# ─────────────────────────────────────────────────────────

# Unified ISO build — called by build-iso.sh inside the container.
# PLATFORM, ARCHES_ARCH, and TEMPLATE are passed in by the container.
_iso: export ARCHES_ARCH := $(ARCHES_ARCH)
_iso: export ARCHES_PLATFORM := $(PLATFORM)
_iso: _check-root _check-deps _aur-repo _stage-installer _stage-modules _stage-ansible _stage-hardware _stage-platform _stage-bootconfig _assemble-packages _stage-graphical $(if $(filter 1,$(OFFLINE)),_cache-packages _stage-offline-cache)
	@# Ensure pkg-cache is NOT in airootfs for online builds (may be left over
	@# from a prior OFFLINE=1 build). The host-side .offline-cache/ is untouched.
	@if [ "$(OFFLINE)" != "1" ]; then \
		rm -rf $(ISO_PROFILE)/airootfs/opt/arches/pkg-cache; \
	fi
	@echo "══ Building Arches ISO ($(PLATFORM), mode: $(ISO_MODE)$(if $(filter 1,$(OFFLINE)), — OFFLINE)$(if $(ARCHES_TEMPLATE), — TEMPLATE=$(ARCHES_TEMPLATE))) ══"
	@rm -rf $(WORK_DIR)
	@mkdir -p $(OUT_DIR)
	mkarchiso -v -w $(WORK_DIR) -o $(OUT_DIR) $(ISO_PROFILE)
	@echo ""
	@echo "══ ISO built ══"
	@for f in $(OUT_DIR)/arches-*.iso; do \
		size=$$(ls -lh "$$f" | awk '{print $$5}'); \
		echo "  out/$$(basename $$f) ($$size)"; \
	done

_aur-repo:
	@echo "══ Building AUR repo ($(PLATFORM)) ══"
	$(SCRIPTS)/build-aur-repo.sh $(if $(FORCE),--force) $(PLATFORM)

_stage-installer:
	@echo "══ Staging installer ══"
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/installer
	@cp -r $(INSTALLER)/* $(ISO_PROFILE)/airootfs/opt/arches/installer/
	@# Stage scripts/gpu-stacks.toml (the GPU stack -> modules mapping
	@# read by arches_installer.core.gpu_stacks at install time). The
	@# search path in that module looks for /opt/arches/scripts/gpu-stacks.toml.
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/scripts
	@cp $(SCRIPTS)/gpu-stacks.toml $(ISO_PROFILE)/airootfs/opt/arches/scripts/
	@# Stage templates (the installer discovers them at /opt/arches/templates/).
	@# When ARCHES_TEMPLATE is set, only stage that one workload template;
	@# always stage iso.toml itself. The auto-install.toml is staged only
	@# if its [install].template matches the workload — otherwise the
	@# autoinstall would point at a template that isn't in the ISO.
	@# Per-template autoinstall files (auto-install-<name>.toml) are
	@# preferred when ARCHES_TEMPLATE is set.
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/templates
	@rm -f $(ISO_PROFILE)/airootfs/opt/arches/templates/*.toml
	@cp $(TEMPLATES)/iso.toml $(ISO_PROFILE)/airootfs/opt/arches/templates/
	@# Resolve the filtered template list from iso-config.py. Capture into
	@# a temp file first so an iso-config.py failure (e.g. invalid
	@# ARCHES_TEMPLATE) propagates as a Make error instead of being
	@# swallowed by command substitution inside the for-loop.
	@tmpl_list=$$(mktemp); trap "rm -f $$tmpl_list" EXIT; \
	if ! python3 $(SCRIPTS)/iso-config.py templates > "$$tmpl_list"; then \
		echo "ERROR: iso-config.py failed to resolve template list"; \
		exit 1; \
	fi; \
	while read -r tmpl; do \
		[ -z "$$tmpl" ] && continue; \
		src="$(TEMPLATES)/$$tmpl"; \
		if [ ! -f "$$src" ]; then \
			echo "ERROR: Template '$$tmpl' (from iso.toml) not found at $$src"; \
			exit 1; \
		fi; \
		cp "$$src" $(ISO_PROFILE)/airootfs/opt/arches/templates/; \
	done < "$$tmpl_list"
	@# Pick the right auto-install file for the staged workload.
	@# An auto-install candidate is suitable iff its [install].template
	@# field references a template we just staged. We prefer
	@# auto-install-<filter>.toml when ARCHES_TEMPLATE is set, falling
	@# back to any auto-install*.toml whose target template is staged,
	@# then plain auto-install.toml. The match is by .template field, NOT
	@# filename, so the existing auto-install-inference.toml convention
	@# (which references llm-inference.toml) works without name mapping.
	@# Place the auto-install file at BOTH locations:
	@#   1. /opt/arches/templates/auto-install.toml — discoverable by the
	@#      installer's TUI for inspection/debugging.
	@#   2. /root/auto-install.toml — the path that
	@#      installer/arches_installer/__main__.py:AUTO_INSTALL_PATH
	@#      reads at boot. Without this, the kernel cmdline flag
	@#      arches.autoinstall=1 fires but the installer can't find the
	@#      config and silently falls back to interactive TUI — fatal
	@#      on a headless install.
	@# pick-auto-install.py emits the chosen path on stdout, validation
	@# errors on stderr (and exit code 2). Capture both so a placeholder
	@# password aborts the build with a visible message rather than
	@# silently producing a no-autoinstall ISO.
	@AUTOINSTALL_SRC=$$($(SCRIPTS)/pick-auto-install.py \
		"$(TEMPLATES)" \
		"$(ISO_PROFILE)/airootfs/opt/arches/templates") || exit 2; \
	if [ -n "$$AUTOINSTALL_SRC" ]; then \
		ai_template=$$(python3 -c "import tomllib; \
print(tomllib.load(open('$$AUTOINSTALL_SRC','rb')).get('install',{}).get('template',''))"); \
		cp "$$AUTOINSTALL_SRC" $(ISO_PROFILE)/airootfs/opt/arches/templates/auto-install.toml; \
		mkdir -p $(ISO_PROFILE)/airootfs/root; \
		cp "$$AUTOINSTALL_SRC" $(ISO_PROFILE)/airootfs/root/auto-install.toml; \
		chmod 600 $(ISO_PROFILE)/airootfs/root/auto-install.toml; \
		echo "  Auto-install: $$(basename $$AUTOINSTALL_SRC) -> $$ai_template (staged to /root/ + /opt/arches/templates/)"; \
	else \
		rm -f $(ISO_PROFILE)/airootfs/root/auto-install.toml 2>/dev/null || true; \
		echo "  Auto-install: none (no auto-install*.toml references a staged template — manual install only)"; \
	fi
	@if [ -n "$(ARCHES_TEMPLATE)" ]; then \
		echo "  Template filter: ARCHES_TEMPLATE=$(ARCHES_TEMPLATE)"; \
	fi
	@echo "  Staged templates: $$(ls $(ISO_PROFILE)/airootfs/opt/arches/templates/*.toml | xargs -n1 basename | tr '\n' ' ')"
	@# Stage disk layouts (the installer discovers them at /opt/arches/disk-layouts/)
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/disk-layouts
	@cp $(PROJECT_DIR)/disk-layouts/*.toml $(ISO_PROFILE)/airootfs/opt/arches/disk-layouts/
	@echo "  Staged disk layouts: $$(ls $(PROJECT_DIR)/disk-layouts/*.toml | xargs -n1 basename | tr '\n' ' ')"
	@# Stage auto-install config into /root/ for auto-detect on boot
	@mkdir -p $(ISO_PROFILE)/airootfs/usr/local/bin
	@printf '#!/usr/bin/env bash\n\
cd /opt/arches/installer\n\
exec python -m arches_installer "$$@"\n' > $(ISO_PROFILE)/airootfs/usr/local/bin/arches-install
	@chmod +x $(ISO_PROFILE)/airootfs/usr/local/bin/arches-install
	@# Embed build host's SSH public key for passwordless access to installed systems
	@REAL_HOME=$$(eval echo ~$${SUDO_USER:-$$USER}); \
	if [ -f "$$REAL_HOME/.ssh/id_ed25519.pub" ]; then \
		cp "$$REAL_HOME/.ssh/id_ed25519.pub" $(ISO_PROFILE)/airootfs/opt/arches/build-host.pub; \
		echo "  Embedded SSH key: $$REAL_HOME/.ssh/id_ed25519.pub"; \
	elif [ -f "$$REAL_HOME/.ssh/id_rsa.pub" ]; then \
		cp "$$REAL_HOME/.ssh/id_rsa.pub" $(ISO_PROFILE)/airootfs/opt/arches/build-host.pub; \
		echo "  Embedded SSH key: $$REAL_HOME/.ssh/id_rsa.pub"; \
	else \
		echo "  WARNING: No SSH public key found ($$REAL_HOME/.ssh/id_ed25519.pub or id_rsa.pub)"; \
		echo "           Installed systems will not have build-host SSH access."; \
	fi
	@# Live-ISO sshd: seed root's authorized_keys from the build-host
	@# public key and start sshd. Runs in BOTH graphical and fb ISO
	@# modes (this stanza is in _stage-installer, not _stage-graphical)
	@# so a headless operator can SSH into a stuck install for debug.
	@# To disable: `touch iso/airootfs/etc/arches-no-live-ssh` before build.
	@mkdir -p $(ISO_PROFILE)/airootfs/usr/local/bin
	@mkdir -p $(ISO_PROFILE)/airootfs/etc/systemd/system/multi-user.target.wants
	@cp $(ISO_PROFILE)/services/arches-live-sshd-setup \
		$(ISO_PROFILE)/airootfs/usr/local/bin/arches-live-sshd-setup
	@chmod 755 $(ISO_PROFILE)/airootfs/usr/local/bin/arches-live-sshd-setup
	@cp $(ISO_PROFILE)/services/arches-live-sshd.service \
		$(ISO_PROFILE)/airootfs/etc/systemd/system/arches-live-sshd.service
	@ln -sf ../arches-live-sshd.service \
		$(ISO_PROFILE)/airootfs/etc/systemd/system/multi-user.target.wants/arches-live-sshd.service
	@echo "  Live-ISO sshd: setup script + service staged"
	@# Test logging: the installer writes to /dev/virtio-ports/arches-log
	@# if it exists (QEMU test harness). No staging needed — handled in run.py.

_stage-modules:
	@echo "══ Staging modules ══"
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/modules
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/ansible/roles
	@for mod in $(MODULES_DIR)/*/module.toml; do \
		slug=$$(basename $$(dirname "$$mod")); \
		mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/modules/$$slug; \
		cp "$$mod" $(ISO_PROFILE)/airootfs/opt/arches/modules/$$slug/; \
		ansible_dir="$$(dirname $$mod)/ansible"; \
		if [ -d "$$ansible_dir" ]; then \
			mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/ansible/roles/$$slug; \
			cp -r "$$ansible_dir"/* $(ISO_PROFILE)/airootfs/opt/arches/ansible/roles/$$slug/; \
		fi; \
	done
	@echo "  Staged modules: $$(ls -1d $(MODULES_DIR)/*/module.toml | xargs -n1 dirname | xargs -n1 basename | tr '\n' ' ')"

_stage-ansible:
	@echo "══ Staging Ansible ══"
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/ansible
	@# Stage playbook and non-module roles (hardware-injected roles like power)
	@cp $(ANSIBLE_DIR)/playbook.yml $(ISO_PROFILE)/airootfs/opt/arches/ansible/
	@if [ -d "$(ANSIBLE_DIR)/roles" ]; then \
		cp -r $(ANSIBLE_DIR)/roles $(ISO_PROFILE)/airootfs/opt/arches/ansible/; \
	fi
	@if [ -d "$(ANSIBLE_DIR)/inventory" ]; then \
		cp -r $(ANSIBLE_DIR)/inventory $(ISO_PROFILE)/airootfs/opt/arches/ansible/; \
	fi

_stage-hardware:
	@echo "══ Staging hardware profiles ══"
	@if [ -d "$(PROJECT_DIR)/hardware" ]; then \
		mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/hardware; \
		cp -r $(PROJECT_DIR)/hardware/* $(ISO_PROFILE)/airootfs/opt/arches/hardware/; \
		echo "  Staged quirks: $$(ls $(PROJECT_DIR)/hardware/quirks/*.toml 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')"; \
		echo "  Staged machines: $$(ls $(PROJECT_DIR)/hardware/machines/*.toml 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')"; \
	else \
		echo "  No hardware/ directory found, skipping"; \
	fi

_stage-platform:
	@echo "══ Staging platform config ($(PLATFORM)) ══"
	@if [ -z "$(PLATFORM)" ]; then echo "ERROR: PLATFORM not set"; exit 1; fi
	@if [ ! -d "$(PLATFORMS)/$(PLATFORM)" ]; then \
		echo "ERROR: Platform directory not found: $(PLATFORMS)/$(PLATFORM)"; \
		exit 1; \
	fi
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/platform
	@cp $(PLATFORMS)/$(PLATFORM)/platform.toml $(ISO_PROFILE)/airootfs/opt/arches/platform/
	@cp $(PLATFORMS)/$(PLATFORM)/pacman.conf $(ISO_PROFILE)/airootfs/opt/arches/platform/
	@echo "  Copied platform.toml and pacman.conf for $(PLATFORM)"

_stage-bootconfig:
	@echo "══ Staging boot config ($(PLATFORM)) ══"
	@if [ -z "$(PLATFORM)" ]; then echo "ERROR: PLATFORM not set"; exit 1; fi
	@# Read default kernel and kernel flags from platform.toml
	@KERNEL=$$(python3 -c "import tomllib; \
		d=tomllib.load(open('$(PLATFORMS)/$(PLATFORM)/platform.toml','rb')); \
		vs=d['kernel']['variants']; \
		print(next((v['package'] for v in vs if v.get('default')), vs[0]['package']))"); \
	KERNEL_FLAGS=$$(python3 -c "import tomllib; \
		d=tomllib.load(open('$(PLATFORMS)/$(PLATFORM)/platform.toml','rb')); \
		flags=d.get('kernel',{}).get('flags',[]); \
		print(' '.join(flags))"); \
	if [ -z "$$KERNEL" ]; then \
		echo "ERROR: Could not read kernel package from platform.toml"; \
		exit 1; \
	fi; \
	echo "  Default kernel: $$KERNEL"; \
	echo "  Kernel flags: $$KERNEL_FLAGS"; \
	\
	echo "  Generating mkinitcpio preset hook for $$KERNEL"; \
	mkdir -p $(ISO_PROFILE)/airootfs/etc/pacman.d/hooks; \
	sed "s/@KERNEL@/$$KERNEL/g" $(ISO_PROFILE)/archiso-mkinitcpio-preset.hook.in \
		> $(ISO_PROFILE)/airootfs/etc/pacman.d/hooks/archiso-mkinitcpio-preset.hook; \
	\
	echo "  Generating boot configs from templates"; \
	echo "  ISO mode: $(ISO_MODE)"; \
	HAS_AUTOINSTALL=false; \
	if [ -f "$(ISO_PROFILE)/airootfs/opt/arches/templates/auto-install.toml" ]; then \
		HAS_AUTOINSTALL=true; \
	fi; \
	echo "  Auto-install: $$HAS_AUTOINSTALL"; \
	\
	DEFAULT_ENTRY=framebuffer; \
	if [ "$(ISO_MODE)" = "graphical" ] && [ "$$HAS_AUTOINSTALL" = "true" ]; then \
		DEFAULT_ENTRY=autoinstall; \
	elif [ "$(ISO_MODE)" = "graphical" ]; then \
		DEFAULT_ENTRY=graphical; \
	elif [ "$$HAS_AUTOINSTALL" = "true" ]; then \
		DEFAULT_ENTRY=autoinstall; \
	fi; \
	echo "  Default boot entry: $$DEFAULT_ENTRY"; \
	\
	sed -e "s/@KERNEL@/$$KERNEL/g" \
		-e "s|@KERNEL_FLAGS@|$$KERNEL_FLAGS|g" \
		-e "s|@DEFAULT_ENTRY@|$$DEFAULT_ENTRY|g" \
		$(ISO_PROFILE)/grub/grub.cfg.in \
		| if [ "$$HAS_AUTOINSTALL" = "true" ]; then sed 's/^@IF_AUTOINSTALL@//'; else grep -v '^@IF_AUTOINSTALL@'; fi \
		| if [ "$(ISO_MODE)" = "graphical" ] && [ "$$HAS_AUTOINSTALL" = "true" ]; then sed 's/^@IF_GRAPHICAL_AUTOINSTALL@//'; else grep -v '^@IF_GRAPHICAL_AUTOINSTALL@'; fi \
		| if [ "$(ISO_MODE)" = "graphical" ]; then sed 's/^@IF_GRAPHICAL@//'; else grep -v '^@IF_GRAPHICAL@'; fi \
		> $(ISO_PROFILE)/grub/grub.cfg; \
	sed -e "s/@KERNEL@/$$KERNEL/g" -e "s|@KERNEL_FLAGS@|$$KERNEL_FLAGS|g" $(ISO_PROFILE)/grub/loopback.cfg.in \
		> $(ISO_PROFILE)/grub/loopback.cfg; \
	sed -e "s/@KERNEL@/$$KERNEL/g" -e "s|@KERNEL_FLAGS@|$$KERNEL_FLAGS|g" $(ISO_PROFILE)/syslinux/archiso_sys-linux.cfg.in \
		> $(ISO_PROFILE)/syslinux/archiso_sys-linux.cfg; \
	sed -e "s/@KERNEL@/$$KERNEL/g" -e "s|@KERNEL_FLAGS@|$$KERNEL_FLAGS|g" $(ISO_PROFILE)/syslinux/archiso_pxe-linux.cfg.in \
		> $(ISO_PROFILE)/syslinux/archiso_pxe-linux.cfg; \
	\
	echo "  Copying mkinitcpio archiso.conf from platform"; \
	mkdir -p $(ISO_PROFILE)/airootfs/etc/mkinitcpio.conf.d; \
	cp $(PLATFORMS)/$(PLATFORM)/archiso.conf \
		$(ISO_PROFILE)/airootfs/etc/mkinitcpio.conf.d/archiso.conf

_assemble-packages:
	@echo "══ Assembling package list ($(PLATFORM), mode: $(ISO_MODE)) ══"
	@if [ -z "$(PLATFORM)" ]; then echo "ERROR: PLATFORM not set"; exit 1; fi
	@ARCH=$$(python3 -c "import tomllib; \
		d=tomllib.load(open('$(PLATFORMS)/$(PLATFORM)/platform.toml','rb')); \
		print(d['platform']['arch'])"); \
	KERNEL_PKGS=$$(python3 -c "import tomllib; \
		d=tomllib.load(open('$(PLATFORMS)/$(PLATFORM)/platform.toml','rb')); \
		vs=d['kernel']['variants']; \
		pkgs=[]; \
		[pkgs.extend([v['package'], v['headers']]) for v in vs]; \
		pkgs.append('linux-firmware'); \
		print('\n'.join(pkgs))"); \
	echo "  Platform arch: $$ARCH"; \
	echo "  ISO mode: $(ISO_MODE)"; \
	echo "  Kernel packages: $$(echo $$KERNEL_PKGS | tr '\n' ' ')"; \
	ISO_PKGS=$$(python3 $(SCRIPTS)/iso-config.py packages $(MODULES_DIR) $(ISO_MODE)); \
	echo "  ISO packages: $$(echo "$$ISO_PKGS" | wc -l) from iso.toml"; \
	{ cat $(PLATFORMS)/$(PLATFORM)/packages; \
	  echo "$$KERNEL_PKGS"; \
	  echo "$$ISO_PKGS"; \
	} | grep -v '^#' | grep -v '^$$' | sort -u \
		> $(ISO_PROFILE)/packages.$$ARCH; \
	echo "  Wrote packages.$$ARCH ($$(wc -l < $(ISO_PROFILE)/packages.$$ARCH) packages)"
	@# Install the platform pacman.conf, rewriting the arches-local repo
	@# path to the build-host location (the ISO airootfs copy).
	@# At install time the live ISO uses the original /opt/arches-repo path.
	@sed 's|file:///opt/arches-repo|file://$(ISO_PROFILE)/airootfs/opt/arches-repo|' \
		$(PLATFORMS)/$(PLATFORM)/pacman.conf > $(ISO_PROFILE)/pacman.conf

_stage-graphical:
	@# Set up graphical live boot if ISO_MODE=graphical.
	@# Reads the desktop module's [iso] section for DM/session/terminal config.
	@echo "  Cleaning previous graphical staging artifacts"; \
	rm -rf $(ISO_PROFILE)/airootfs/etc/systemd/system/arches-liveuser.service \
		$(ISO_PROFILE)/airootfs/etc/systemd/system/display-manager.service \
		$(ISO_PROFILE)/airootfs/etc/systemd/system/multi-user.target.wants/arches-liveuser.service \
		$(ISO_PROFILE)/airootfs/etc/systemd/system/sddm.service.d \
		$(ISO_PROFILE)/airootfs/etc/sddm.conf.d \
		$(ISO_PROFILE)/airootfs/etc/xdg \
		$(ISO_PROFILE)/airootfs/usr/share/applications \
		$(ISO_PROFILE)/airootfs/usr/local/bin/arches-liveuser-setup \
		$(ISO_PROFILE)/airootfs/home 2>/dev/null || true; \
	if [ "$(ISO_MODE)" = "graphical" ]; then \
		echo "══ Staging graphical live boot ══"; \
		eval $$(python3 $(SCRIPTS)/iso-config.py graphical-config $(MODULES_DIR)); \
		echo "  Desktop: $$SESSION (DM: $$DISPLAY_MANAGER, terminal: $$TERMINAL)"; \
		echo "  Installing liveuser setup service"; \
		mkdir -p $(ISO_PROFILE)/airootfs/usr/local/bin; \
		cp $(ISO_PROFILE)/services/arches-liveuser-setup $(ISO_PROFILE)/airootfs/usr/local/bin/; \
		chmod 755 $(ISO_PROFILE)/airootfs/usr/local/bin/arches-liveuser-setup; \
		mkdir -p $(ISO_PROFILE)/airootfs/etc/systemd/system/multi-user.target.wants; \
		cp $(ISO_PROFILE)/services/arches-liveuser.service $(ISO_PROFILE)/airootfs/etc/systemd/system/; \
		ln -sf ../arches-liveuser.service \
			$(ISO_PROFILE)/airootfs/etc/systemd/system/multi-user.target.wants/arches-liveuser.service; \
		echo "  Configuring $$DISPLAY_MANAGER autologin (session: $$SESSION)"; \
		mkdir -p $(ISO_PROFILE)/airootfs/etc/sddm.conf.d; \
		printf '[Autologin]\nUser=liveuser\nSession=%s\n' "$$SESSION" \
			> $(ISO_PROFILE)/airootfs/etc/sddm.conf.d/autologin.conf; \
		echo "  Enabling $$DISPLAY_MANAGER service (with liveuser dependency)"; \
		ln -sf /usr/lib/systemd/system/$${DISPLAY_MANAGER}.service \
			$(ISO_PROFILE)/airootfs/etc/systemd/system/display-manager.service; \
		mkdir -p $(ISO_PROFILE)/airootfs/etc/systemd/system/$${DISPLAY_MANAGER}.service.d; \
		printf '[Unit]\nRequires=arches-liveuser.service\nAfter=arches-liveuser.service\n' \
			> $(ISO_PROFILE)/airootfs/etc/systemd/system/$${DISPLAY_MANAGER}.service.d/liveuser.conf; \
		echo "  Creating installer desktop entry (terminal: $$TERMINAL)"; \
		mkdir -p $(ISO_PROFILE)/airootfs/usr/share/applications; \
		printf '[Desktop Entry]\nName=Arches Installer\nComment=Install Arches Linux\nExec=%s sudo /usr/local/bin/arches-install\nIcon=system-software-install\nTerminal=false\nType=Application\nCategories=System;\n' \
			"$$TERMINAL" \
			> $(ISO_PROFILE)/airootfs/usr/share/applications/arches-install.desktop; \
		mkdir -p $(ISO_PROFILE)/airootfs/etc/xdg/autostart; \
		cp $(ISO_PROFILE)/airootfs/usr/share/applications/arches-install.desktop \
			$(ISO_PROFILE)/airootfs/etc/xdg/autostart/arches-install.desktop; \
	else \
		echo "══ Skipping graphical live boot (ISO_MODE=$(ISO_MODE)) ══"; \
	fi

_cache-packages:
	@echo "══ Caching template packages (into .offline-cache/) ══"
	@$(SCRIPTS)/cache-template-packages.sh $(PLATFORM)

_stage-offline-cache:
	@echo "══ Staging offline package cache into ISO ══"
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/pkg-cache
	@cp -a .offline-cache/* $(ISO_PROFILE)/airootfs/opt/arches/pkg-cache/ 2>/dev/null || true
	@echo "  Staged $$(ls .offline-cache/*.pkg.tar.* 2>/dev/null | wc -l) packages"

# ─── Checks ──────────────────────────────────────────

_check-root:
	@if [ "$$(id -u)" -ne 0 ]; then \
		echo "ERROR: This target requires root. Run with sudo."; \
		exit 1; \
	fi

_check-deps:
	@missing=""; \
	for cmd in mkarchiso pacman-key mksquashfs; do \
		if ! command -v $$cmd &>/dev/null; then \
			missing="$$missing $$cmd"; \
		fi; \
	done; \
	if echo "$(PLATFORM)" | grep -q aarch64 || [ -z "$(PLATFORM)" ]; then \
		if ! command -v grub-mkstandalone &>/dev/null; then \
			missing="$$missing grub-mkstandalone"; \
		fi; \
	fi; \
	if [ -n "$$missing" ]; then \
		echo "ERROR: Missing required commands:$$missing"; \
		echo "Install prerequisites: sudo pacman -S archiso squashfs-tools grub"; \
		exit 1; \
	fi
