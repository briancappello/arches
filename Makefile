# ─────────────────────────────────────────────────────────
# Arches — Custom Install & Recovery ISO
# ─────────────────────────────────────────────────────────

SHELL       := /bin/bash
PROJECT_DIR := $(shell pwd)
ISO_PROFILE := $(PROJECT_DIR)/iso
INSTALLER   := $(PROJECT_DIR)/installer
TEMPLATES   := $(PROJECT_DIR)/templates
ANSIBLE_DIR := $(PROJECT_DIR)/ansible
PLATFORMS   := $(PROJECT_DIR)/platforms
SCRIPTS     := $(PROJECT_DIR)/scripts
WORK_DIR    := /tmp/arches-work
OUT_DIR     := $(PROJECT_DIR)/out
OFFLINE     ?= 0

# ─── Phony targets ────────────────────────────────────

.PHONY: help \
        iso usb \
        sv-install sv-dry-run sv-uninstall \
        fmt test test-unit test-template dry-run \
        qemu-install qemu-boot qemu-raid qemu-disk qemu-ansible \
        clean clean-caches clean-all \
        _iso _aur-repo \
        _stage-installer _stage-ansible _stage-platform _stage-bootconfig \
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

iso: ## Build ISO (OFFLINE=1 to pre-cache all packages for offline install)
	OFFLINE=$(OFFLINE) $(SCRIPTS)/build-iso.sh

usb: ## Build install media and write to USB drive (requires sudo + Podman)
	$(SCRIPTS)/build-usb.sh

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
		--tags $(TAGS) \
		-v

##@ Cleanup

clean: ## Remove staged files from ISO airootfs
	@echo "══ Cleaning staged files ══"
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches/installer
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches/templates
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
	rm -f  $(ISO_PROFILE)/airootfs/etc/systemd/system/display-manager.service
	rm -f  $(ISO_PROFILE)/airootfs/etc/systemd/system/multi-user.target.wants/arches-liveuser.service
	rm -rf $(ISO_PROFILE)/airootfs/etc/systemd/system/sddm.service.d
	rm -rf $(ISO_PROFILE)/airootfs/etc/sddm.conf.d
	rm -rf $(ISO_PROFILE)/airootfs/etc/xdg
	rm -rf $(ISO_PROFILE)/airootfs/usr/share/applications
	rm -f  $(ISO_PROFILE)/airootfs/usr/local/bin/arches-liveuser-setup
	rm -rf $(ISO_PROFILE)/airootfs/home


clean-caches: ## Remove AUR repo, pacman cache, and offline package cache
	@echo "══ Cleaning caches ══"
	rm -rf .aur-repo
	rm -rf .pkg-cache
	rm -rf .offline-cache

clean-all: clean clean-caches ## Remove all build artifacts
	@echo "══ Cleaning output ══"
	rm -rf $(OUT_DIR)
	rm -f /tmp/arches-test-disk.qcow2
	rm -f /tmp/arches-efi-vars.raw
	rm -f /tmp/arches-raid-disk-*.qcow2
	rm -f /tmp/arches-raid-efi-vars.raw

# ─────────────────────────────────────────────────────────
# Internal targets (not shown in help)
# ─────────────────────────────────────────────────────────

# Unified ISO build — called by build-iso.sh inside the container.
# PLATFORM, ARCHES_ARCH, and TEMPLATE are passed in by the container.
_iso: export ARCHES_ARCH := $(ARCHES_ARCH)
_iso: export ARCHES_PLATFORM := $(PLATFORM)
_iso: _check-root _check-deps _aur-repo _stage-installer _stage-ansible _stage-platform _stage-bootconfig _assemble-packages _stage-graphical $(if $(filter 1,$(OFFLINE)),_cache-packages _stage-offline-cache)
	@# Ensure pkg-cache is NOT in airootfs for online builds (may be left over
	@# from a prior OFFLINE=1 build). The host-side .offline-cache/ is untouched.
	@if [ "$(OFFLINE)" != "1" ]; then \
		rm -rf $(ISO_PROFILE)/airootfs/opt/arches/pkg-cache; \
	fi
	@echo "══ Building Arches ISO ($(PLATFORM), template: $(TEMPLATE)$(if $(filter 1,$(OFFLINE)), — OFFLINE)) ══"
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
	@# Stage templates (the installer discovers them at /opt/arches/templates/)
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/templates
	@cp $(TEMPLATES)/*.toml $(ISO_PROFILE)/airootfs/opt/arches/templates/
	@echo "  Staged templates: $$(ls $(TEMPLATES)/*.toml | xargs -n1 basename | tr '\n' ' ')"
	@# Stage disk layouts (the installer discovers them at /opt/arches/disk-layouts/)
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/disk-layouts
	@cp $(PROJECT_DIR)/disk-layouts/*.toml $(ISO_PROFILE)/airootfs/opt/arches/disk-layouts/
	@echo "  Staged disk layouts: $$(ls $(PROJECT_DIR)/disk-layouts/*.toml | xargs -n1 basename | tr '\n' ' ')"
	@# Stage auto-install config into /root/ for auto-detect on boot
	@if [ -f "$(TEMPLATES)/auto-install.toml" ]; then \
		cp "$(TEMPLATES)/auto-install.toml" $(ISO_PROFILE)/airootfs/root/auto-install.toml; \
		echo "  Staged auto-install.toml to /root/"; \
	fi
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
	@# Test logging: the installer writes to /dev/virtio-ports/arches-log
	@# if it exists (QEMU test harness). No staging needed — handled in run.py.

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
	sed -e "s/@KERNEL@/$$KERNEL/g" -e "s|@KERNEL_FLAGS@|$$KERNEL_FLAGS|g" $(ISO_PROFILE)/grub/grub.cfg.in \
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
	@echo "══ Assembling package list ($(PLATFORM)) ══"
	@if [ -z "$(PLATFORM)" ]; then echo "ERROR: PLATFORM not set"; exit 1; fi
	@# Resolve template to check graphical flag
	@TMPL="$(TEMPLATE)"; \
	if [ -z "$$TMPL" ]; then \
		TMPL=$$(python3 -c "import tomllib; \
			d=tomllib.load(open('$(PLATFORMS)/$(PLATFORM)/platform.toml','rb')); \
			print(d.get('platform',{}).get('default_template',''))"); \
	fi; \
	GRAPHICAL=false; \
	if [ -n "$$TMPL" ]; then \
		TMPL_FILE="$(TEMPLATES)/$$TMPL.toml"; \
		if [ -f "$$TMPL_FILE" ]; then \
			GRAPHICAL=$$(python3 -c "import tomllib; \
				d=tomllib.load(open('$$TMPL_FILE','rb')); \
				print('true' if d.get('meta',{}).get('graphical',False) else 'false')"); \
		fi; \
	fi; \
	ARCH=$$(python3 -c "import tomllib; \
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
	echo "  Graphical ISO: $$GRAPHICAL"; \
	echo "  Kernel packages: $$(echo $$KERNEL_PKGS | tr '\n' ' ')"; \
	GRAPHICAL_FILE=""; \
	if [ "$$GRAPHICAL" = "true" ] && [ -f "$(ISO_PROFILE)/packages.graphical_iso" ]; then \
		GRAPHICAL_FILE="$(ISO_PROFILE)/packages.graphical_iso"; \
		echo "  Including graphical ISO packages"; \
	fi; \
	{ cat $(ISO_PROFILE)/packages.iso $(PLATFORMS)/$(PLATFORM)/packages; \
	  echo "$$KERNEL_PKGS"; \
	  if [ -n "$$GRAPHICAL_FILE" ]; then cat "$$GRAPHICAL_FILE"; fi; \
	} | grep -v '^#' | grep -v '^$$' | sort -u \
		> $(ISO_PROFILE)/packages.$$ARCH; \
	echo "  Wrote packages.$$ARCH ($$(wc -l < $(ISO_PROFILE)/packages.$$ARCH) packages)"
	@# Install the platform pacman.conf, rewriting the arches-local repo
	@# path to the build-host location (the ISO airootfs copy).
	@# At install time the live ISO uses the original /opt/arches-repo path.
	@sed 's|file:///opt/arches-repo|file://$(ISO_PROFILE)/airootfs/opt/arches-repo|' \
		$(PLATFORMS)/$(PLATFORM)/pacman.conf > $(ISO_PROFILE)/pacman.conf

_stage-graphical:
	@# Conditionally set up graphical live boot based on template's graphical flag.
	@TMPL="$(TEMPLATE)"; \
	if [ -z "$$TMPL" ]; then \
		TMPL=$$(python3 -c "import tomllib; \
			d=tomllib.load(open('$(PLATFORMS)/$(PLATFORM)/platform.toml','rb')); \
			print(d.get('platform',{}).get('default_template',''))"); \
	fi; \
	TMPL_FILE="$(TEMPLATES)/$$TMPL.toml"; \
	GRAPHICAL=$$(python3 -c "import tomllib; \
		d=tomllib.load(open('$$TMPL_FILE','rb')); \
		print('true' if d.get('meta',{}).get('graphical',False) else 'false')"); \
	echo "  Cleaning previous graphical staging artifacts"; \
	rm -rf $(ISO_PROFILE)/airootfs/etc/systemd/system/arches-liveuser.service \
		$(ISO_PROFILE)/airootfs/etc/systemd/system/display-manager.service \
		$(ISO_PROFILE)/airootfs/etc/systemd/system/multi-user.target.wants/arches-liveuser.service \
		$(ISO_PROFILE)/airootfs/etc/systemd/system/sddm.service.d \
		$(ISO_PROFILE)/airootfs/etc/sddm.conf.d \
		$(ISO_PROFILE)/airootfs/etc/xdg \
		$(ISO_PROFILE)/airootfs/usr/share/applications \
		$(ISO_PROFILE)/airootfs/usr/local/bin/arches-liveuser-setup \
		$(ISO_PROFILE)/airootfs/home 2>/dev/null || true; \
	if [ "$$GRAPHICAL" = "true" ]; then \
		echo "══ Staging graphical live boot (template: $$TMPL) ══"; \
		echo "  Installing liveuser setup service"; \
		mkdir -p $(ISO_PROFILE)/airootfs/usr/local/bin; \
		cp $(ISO_PROFILE)/services/arches-liveuser-setup $(ISO_PROFILE)/airootfs/usr/local/bin/; \
		chmod 755 $(ISO_PROFILE)/airootfs/usr/local/bin/arches-liveuser-setup; \
		mkdir -p $(ISO_PROFILE)/airootfs/etc/systemd/system/multi-user.target.wants; \
		cp $(ISO_PROFILE)/services/arches-liveuser.service $(ISO_PROFILE)/airootfs/etc/systemd/system/; \
		ln -sf ../arches-liveuser.service \
			$(ISO_PROFILE)/airootfs/etc/systemd/system/multi-user.target.wants/arches-liveuser.service; \
		echo "  Configuring SDDM autologin"; \
		mkdir -p $(ISO_PROFILE)/airootfs/etc/sddm.conf.d; \
		printf '[Autologin]\nUser=liveuser\nSession=plasma\n' \
			> $(ISO_PROFILE)/airootfs/etc/sddm.conf.d/autologin.conf; \
		echo "  Enabling SDDM service (with liveuser dependency)"; \
		ln -sf /usr/lib/systemd/system/sddm.service \
			$(ISO_PROFILE)/airootfs/etc/systemd/system/display-manager.service; \
		mkdir -p $(ISO_PROFILE)/airootfs/etc/systemd/system/sddm.service.d; \
		printf '[Unit]\nRequires=arches-liveuser.service\nAfter=arches-liveuser.service\n' \
			> $(ISO_PROFILE)/airootfs/etc/systemd/system/sddm.service.d/liveuser.conf; \
		echo "  Creating installer desktop entry"; \
		mkdir -p $(ISO_PROFILE)/airootfs/usr/share/applications; \
		printf '[Desktop Entry]\n\
Name=Arches Installer\n\
Comment=Install Arches Linux\n\
Exec=konsole --noclose -e sudo /usr/local/bin/arches-install\n\
Icon=system-software-install\n\
Terminal=false\n\
Type=Application\n\
Categories=System;\n' > $(ISO_PROFILE)/airootfs/usr/share/applications/arches-install.desktop; \
		mkdir -p $(ISO_PROFILE)/airootfs/etc/xdg/autostart; \
		cp $(ISO_PROFILE)/airootfs/usr/share/applications/arches-install.desktop \
			$(ISO_PROFILE)/airootfs/etc/xdg/autostart/arches-install.desktop; \
	else \
		echo "══ Skipping graphical live boot (template $$TMPL is not graphical) ══"; \
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
