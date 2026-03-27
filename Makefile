# ─────────────────────────────────────────────────────────
# Arches — Custom Install & Recovery ISO
# ─────────────────────────────────────────────────────────

SHELL       := /bin/bash
PROJECT_DIR := $(shell pwd)
ISO_PROFILE := $(PROJECT_DIR)/iso
INSTALLER   := $(PROJECT_DIR)/installer
ANSIBLE_DIR := $(PROJECT_DIR)/ansible
PLATFORMS   := $(PROJECT_DIR)/platforms
SCRIPTS     := $(PROJECT_DIR)/scripts
WORK_DIR    := /tmp/arches-work
OUT_DIR     := $(PROJECT_DIR)/out

# ─── Phony targets ────────────────────────────────────

.PHONY: help \
        iso usb qemu-install \
        host-install host-install-rebuild host-install-dry host-clean \
        fmt test test-template dry-run \
        test-iso test-boot test-iso-bios test-disk ansible-dev \
        clean clean-work clean-all \
        _iso _aur-repo \
        _stage-installer _stage-ansible _stage-platform _stage-bootconfig \
        _assemble-packages _cache-packages \
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

##@ Install

iso: ## Build ISO for auto-detected platform (requires sudo + Podman)
	$(SCRIPTS)/build-iso.sh

usb: ## Build install media and write to USB drive (requires sudo + Podman)
	$(SCRIPTS)/build-usb.sh

qemu-install: ## Build ISO and boot QEMU VM with install disk attached
	$(SCRIPTS)/qemu-install.sh

##@ Host Install (Apple Silicon)

CONFIG ?= examples/host-install.toml

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
		--auto examples/auto-install.toml \
		--platform platforms/x86-64/platform.toml \
		--dry-run

##@ QEMU Testing

test-iso: ## Boot the built ISO in QEMU (UEFI, no install)
	@ISO=$$(ls -t $(OUT_DIR)/arches-*.iso 2>/dev/null | head -1); \
	if [ -z "$$ISO" ]; then echo "No ISO found in $(OUT_DIR)/. Run 'make iso' first."; exit 1; fi; \
	echo "══ Booting $$ISO in QEMU (UEFI) ══"; \
	if [ "$$(uname -m)" = "aarch64" ]; then \
		[ -f /tmp/arches-efi-vars.raw ] || \
			cp /usr/share/edk2/aarch64/vars-template-pflash.raw /tmp/arches-efi-vars.raw; \
		qemu-system-aarch64 -M virt -enable-kvm -cpu host -m 4G -smp 4 \
			-drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/aarch64/QEMU_EFI-pflash.raw \
			-drive if=pflash,format=raw,file=/tmp/arches-efi-vars.raw \
			-device virtio-gpu-pci -device qemu-xhci -device usb-kbd -device usb-tablet \
			-drive file=/tmp/arches-test-disk.qcow2,format=qcow2,if=virtio \
			-device usb-storage,drive=cdrom0,bootindex=2 \
			-drive id=cdrom0,file=$$ISO,format=raw,if=none,media=cdrom,readonly=on \
			-net nic -net user,hostfwd=tcp::2222-:22; \
	else \
		qemu-system-x86_64 -enable-kvm -cpu host -m 4G -smp 4 \
			-bios /usr/share/edk2/x64/OVMF.4m.fd \
			-vga virtio \
			-drive file=$$ISO,format=raw,media=cdrom \
			-drive file=/tmp/arches-test-disk.qcow2,format=qcow2,if=virtio \
			-net nic -net user,hostfwd=tcp::2222-:22; \
	fi

test-boot: ## Boot the installed test disk in QEMU (UEFI, no ISO)
	@echo "══ Booting installed disk in QEMU (UEFI) ══"
	@echo "    SSH: ssh -p 2222 <user>@localhost"
	@if [ "$$(uname -m)" = "aarch64" ]; then \
		[ -f /tmp/arches-efi-vars.raw ] || \
			cp /usr/share/edk2/aarch64/vars-template-pflash.raw /tmp/arches-efi-vars.raw; \
		qemu-system-aarch64 -M virt -enable-kvm -cpu host -m 4G -smp 4 \
			-drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/aarch64/QEMU_EFI-pflash.raw \
			-drive if=pflash,format=raw,file=/tmp/arches-efi-vars.raw \
			-device virtio-gpu-pci -device qemu-xhci -device usb-kbd -device usb-tablet \
			-drive file=/tmp/arches-test-disk.qcow2,format=qcow2,if=virtio \
			-net nic -net user,hostfwd=tcp::2222-:22; \
	else \
		qemu-system-x86_64 -enable-kvm -cpu host -m 4G -smp 4 \
			-bios /usr/share/edk2/x64/OVMF.4m.fd \
			-vga virtio \
			-drive file=/tmp/arches-test-disk.qcow2,format=qcow2,if=virtio \
			-net nic -net user,hostfwd=tcp::2222-:22; \
	fi

test-iso-bios: ## Boot the built ISO in QEMU (BIOS, x86-64 only)
	@if [ "$$(uname -m)" = "aarch64" ]; then \
		echo "ERROR: BIOS boot is not supported on aarch64 (UEFI only)."; \
		echo "       Use 'make test-iso' instead."; \
		exit 1; \
	fi
	@ISO=$$(ls -t $(OUT_DIR)/arches-*.iso 2>/dev/null | head -1); \
	if [ -z "$$ISO" ]; then echo "No ISO found in $(OUT_DIR)/. Run 'make iso' first."; exit 1; fi; \
	echo "══ Booting $$ISO in QEMU (BIOS) ══"; \
	qemu-system-x86_64 \
		-enable-kvm \
		-m 4G \
		-cpu host \
		-smp 4 \
		-drive file=$$ISO,format=raw,media=cdrom \
		-drive file=/tmp/arches-test-disk.qcow2,format=qcow2,if=virtio \
		-net nic -net user,hostfwd=tcp::2222-:22 \
		-vga std

test-disk: ## Create a QEMU test disk image (20G)
	@echo "══ Creating test disk ══"
	qemu-img create -f qcow2 /tmp/arches-test-disk.qcow2 20G
	@echo "Created /tmp/arches-test-disk.qcow2 (20G)"

VM_SSH_PORT := 2222
VM_USER     := arches
TAGS        ?= all

ansible-dev: ## Run Ansible roles against the QEMU VM (TAGS=all)
	@echo "══ Running Ansible against VM (tags: $(TAGS)) ══"
	ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook $(ANSIBLE_DIR)/playbook.yml \
		-i $(ANSIBLE_DIR)/inventory/dev-vm.ini \
		--become \
		-e ansible_user=$(VM_USER) \
		-e install_user=$(VM_USER) \
		--tags $(TAGS) \
		-v

##@ Cleanup

clean: ## Remove staged files from ISO airootfs
	@echo "══ Cleaning staged files ══"
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches/installer
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches/ansible
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches/platform
	rm -f  $(ISO_PROFILE)/airootfs/usr/local/bin/arches-install
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches-repo
	rm -f  $(ISO_PROFILE)/airootfs/opt/arches/build-host.pub
	rm -f  $(ISO_PROFILE)/packages.x86_64
	rm -f  $(ISO_PROFILE)/packages.aarch64
	rm -f  $(ISO_PROFILE)/airootfs/etc/pacman.d/hooks/archiso-mkinitcpio-preset.hook
	rm -f  $(ISO_PROFILE)/airootfs/etc/mkinitcpio.conf.d/archiso.conf
	rm -f  $(ISO_PROFILE)/grub/grub.cfg
	rm -f  $(ISO_PROFILE)/grub/loopback.cfg
	rm -f  $(ISO_PROFILE)/syslinux/archiso_sys-linux.cfg
	rm -f  $(ISO_PROFILE)/syslinux/archiso_pxe-linux.cfg

clean-work: ## Remove mkarchiso work directory
	@echo "══ Cleaning work directory ══"
	rm -rf $(WORK_DIR)

clean-all: clean clean-work ## Remove all build artifacts
	@echo "══ Cleaning output ══"
	rm -rf $(OUT_DIR)
	rm -f /tmp/arches-test-disk.qcow2
	rm -f /tmp/arches-efi-vars.raw

# ─────────────────────────────────────────────────────────
# Internal targets (not shown in help)
# ─────────────────────────────────────────────────────────

# Unified ISO build — called by build-iso.sh inside the container.
# PLATFORM and ARCHES_ARCH are passed in by the container.
_iso: export ARCHES_ARCH := $(ARCHES_ARCH)
_iso: export ARCHES_PLATFORM := $(PLATFORM)
_iso: _check-root _check-deps _aur-repo _stage-installer _stage-ansible _stage-platform _stage-bootconfig _assemble-packages _cache-packages
	@echo "══ Building Arches ISO ($(PLATFORM)) ══"
	@rm -rf $(WORK_DIR)
	@mkdir -p $(OUT_DIR)
	mkarchiso -v -w $(WORK_DIR) -o $(OUT_DIR) $(ISO_PROFILE)
	@echo ""
	@echo "══ ISO built ══"
	@ls -lh $(OUT_DIR)/arches-*.iso 2>/dev/null

_aur-repo:
	@echo "══ Building AUR repo ($(PLATFORM)) ══"
	$(SCRIPTS)/build-aur-repo.sh $(if $(FORCE),--force) $(PLATFORM)

_stage-installer:
	@echo "══ Staging installer ══"
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/installer
	@cp -r $(INSTALLER)/* $(ISO_PROFILE)/airootfs/opt/arches/installer/
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

_stage-ansible:
	@echo "══ Staging Ansible ══"
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/ansible
	@cp -r $(ANSIBLE_DIR)/* $(ISO_PROFILE)/airootfs/opt/arches/ansible/

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
	@# Read kernel package name from platform.toml
	@KERNEL=$$(grep '^package' $(PLATFORMS)/$(PLATFORM)/platform.toml | head -1 | \
		sed 's/.*= *"\(.*\)"/\1/'); \
	if [ -z "$$KERNEL" ]; then \
		echo "ERROR: Could not read kernel package from platform.toml"; \
		exit 1; \
	fi; \
	echo "  Kernel package: $$KERNEL"; \
	\
	echo "  Generating mkinitcpio preset hook for $$KERNEL"; \
	mkdir -p $(ISO_PROFILE)/airootfs/etc/pacman.d/hooks; \
	sed "s/@KERNEL@/$$KERNEL/g" $(ISO_PROFILE)/archiso-mkinitcpio-preset.hook.in \
		> $(ISO_PROFILE)/airootfs/etc/pacman.d/hooks/archiso-mkinitcpio-preset.hook; \
	\
	echo "  Generating boot configs from templates"; \
	sed "s/@KERNEL@/$$KERNEL/g" $(ISO_PROFILE)/grub/grub.cfg.in \
		> $(ISO_PROFILE)/grub/grub.cfg; \
	sed "s/@KERNEL@/$$KERNEL/g" $(ISO_PROFILE)/grub/loopback.cfg.in \
		> $(ISO_PROFILE)/grub/loopback.cfg; \
	sed "s/@KERNEL@/$$KERNEL/g" $(ISO_PROFILE)/syslinux/archiso_sys-linux.cfg.in \
		> $(ISO_PROFILE)/syslinux/archiso_sys-linux.cfg; \
	sed "s/@KERNEL@/$$KERNEL/g" $(ISO_PROFILE)/syslinux/archiso_pxe-linux.cfg.in \
		> $(ISO_PROFILE)/syslinux/archiso_pxe-linux.cfg; \
	\
	echo "  Copying mkinitcpio archiso.conf from platform"; \
	mkdir -p $(ISO_PROFILE)/airootfs/etc/mkinitcpio.conf.d; \
	cp $(PLATFORMS)/$(PLATFORM)/archiso.conf \
		$(ISO_PROFILE)/airootfs/etc/mkinitcpio.conf.d/archiso.conf

_assemble-packages:
	@echo "══ Assembling package list ($(PLATFORM)) ══"
	@if [ -z "$(PLATFORM)" ]; then echo "ERROR: PLATFORM not set"; exit 1; fi
	@# Read platform arch from platform.toml for the archiso package filename
	@ARCH=$$(grep '^arch' $(PLATFORMS)/$(PLATFORM)/platform.toml | head -1 | \
		sed 's/.*= *"\(.*\)"/\1/'); \
	echo "  Platform arch: $$ARCH"; \
	cat $(ISO_PROFILE)/packages.common $(PLATFORMS)/$(PLATFORM)/packages \
		| grep -v '^#' | grep -v '^$$' | sort -u \
		> $(ISO_PROFILE)/packages.$$ARCH; \
	echo "  Wrote packages.$$ARCH ($$(wc -l < $(ISO_PROFILE)/packages.$$ARCH) packages)"
	@# Install the platform pacman.conf, rewriting the arches-local repo
	@# path to the build-host location (the ISO airootfs copy).
	@# At install time the live ISO uses the original /opt/arches-repo path.
	@sed 's|file:///opt/arches-repo|file://$(ISO_PROFILE)/airootfs/opt/arches-repo|' \
		$(PLATFORMS)/$(PLATFORM)/pacman.conf > $(ISO_PROFILE)/pacman.conf

_cache-packages:
	@echo "══ Caching template packages ══"
	@$(SCRIPTS)/cache-template-packages.sh $(PLATFORM)

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
