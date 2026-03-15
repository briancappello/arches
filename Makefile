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

.PHONY: help iso-x86-64 iso-aarch64-generic aur-repo clean clean-all \
        lint format test test-unit test-tui test-template check-root check-deps \
        stage-installer stage-ansible stage-platform assemble-packages \
        test-iso test-boot test-iso-bios test-disk dry-run \
        ansible-dev

# ─── Default ──────────────────────────────────────────

help: ## Show this help
	@printf '\n  \033[1mArches Build Targets\033[0m\n\n'
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'
	@printf '\n'

# ─── ISO build ────────────────────────────────────────

iso-x86-64: PLATFORM := x86-64
iso-x86-64: export ARCHES_ARCH := x86_64
iso-x86-64: check-root check-deps aur-repo-x86-64 stage-installer stage-ansible stage-platform assemble-packages cache-packages ## Build ISO for x86-64 (requires sudo)
	@echo "══ Building Arches ISO (x86-64) ══"
	@rm -rf $(WORK_DIR)
	@mkdir -p $(OUT_DIR)
	mkarchiso -v -w $(WORK_DIR) -o $(OUT_DIR) $(ISO_PROFILE)
	@echo ""
	@echo "══ ISO built ══"
	@ls -lh $(OUT_DIR)/arches-*.iso 2>/dev/null

iso-aarch64-generic: PLATFORM := aarch64-generic
iso-aarch64-generic: export ARCHES_ARCH := aarch64
iso-aarch64-generic: check-root check-deps stage-installer stage-ansible stage-platform assemble-packages ## Build ISO for aarch64-generic (requires sudo)
	@echo "══ Building Arches ISO (aarch64-generic) ══"
	@rm -rf $(WORK_DIR)
	@mkdir -p $(OUT_DIR)
	mkarchiso -v -w $(WORK_DIR) -o $(OUT_DIR) $(ISO_PROFILE)
	@echo ""
	@echo "══ ISO built ══"
	@ls -lh $(OUT_DIR)/arches-*.iso 2>/dev/null

aur-repo-x86-64: ## Pre-build AUR packages for x86-64 platform
	@echo "══ Building AUR repo (x86-64) ══"
	$(SCRIPTS)/build-aur-repo.sh $(if $(FORCE),--force) x86-64

stage-installer: ## Copy installer into ISO airootfs
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

stage-ansible: ## Copy Ansible playbooks into ISO airootfs
	@echo "══ Staging Ansible ══"
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/ansible
	@cp -r $(ANSIBLE_DIR)/* $(ISO_PROFILE)/airootfs/opt/arches/ansible/

stage-platform: ## Copy platform config into ISO airootfs
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

assemble-packages: ## Assemble ISO package list from common + platform
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

cache-packages: ## Pre-download template packages into ISO for offline install
	@echo "══ Caching template packages ══"
	@$(SCRIPTS)/cache-template-packages.sh $(PLATFORM)

# ─── Development ──────────────────────────────────────

lint: ## Lint Python code with ruff
	@echo "══ Linting ══"
	ruff check $(INSTALLER)
	ruff format --check $(INSTALLER)

format: ## Auto-format Python code with ruff
	ruff format $(INSTALLER)
	ruff check --fix $(INSTALLER)

test: ## Run all tests (unit + TUI)
	@echo "══ Running all tests ══"
	python -m pytest $(INSTALLER)/tests/ -v

test-unit: ## Run core unit tests only (no Textual dependency)
	@echo "══ Running unit tests ══"
	python -m pytest $(INSTALLER)/tests/core/ -v

test-tui: ## Run Textual TUI tests only
	@echo "══ Running TUI tests ══"
	python -m pytest $(INSTALLER)/tests/tui/ -v

test-template: ## Validate all TOML templates parse correctly
	@echo "══ Validating templates ══"
	PYTHONPATH=$(INSTALLER) python -c "\
from arches_installer.core.template import discover_templates; \
templates = discover_templates(); \
print(f'Loaded {len(templates)} templates:'); \
[print(f'  - {t.name}: {t.description}') for t in templates]"

dry-run: ## Dry-run the example auto-install config
	@echo "══ Dry run ══"
	PYTHONPATH=$(INSTALLER) python -m arches_installer \
		--auto examples/auto-install.toml \
		--platform platforms/x86-64/platform.toml \
		--dry-run

# ─── QEMU testing ─────────────────────────────────────

test-iso: ## Boot the built ISO in QEMU (UEFI, x86-64)
	@ISO=$$(ls -t $(OUT_DIR)/arches-*.iso 2>/dev/null | head -1); \
	if [ -z "$$ISO" ]; then echo "No ISO found in $(OUT_DIR)/. Run 'make iso-x86-64' first."; exit 1; fi; \
	echo "══ Booting $$ISO in QEMU (UEFI) ══"; \
	qemu-system-x86_64 \
		-enable-kvm \
		-m 4G \
		-cpu host \
		-smp 4 \
		-bios /usr/share/edk2/x64/OVMF.4m.fd \
		-drive file=$$ISO,format=raw,media=cdrom \
		-drive file=/tmp/arches-test-disk.qcow2,format=qcow2,if=virtio \
		-net nic -net user,hostfwd=tcp::2222-:22 \
		-vga virtio

test-boot: ## Boot the installed test disk in QEMU (UEFI, no ISO)
	@echo "══ Booting installed disk in QEMU (UEFI) ══"
	@echo "    SSH: ssh -p 2222 <user>@localhost"
	qemu-system-x86_64 \
		-enable-kvm \
		-m 4G \
		-cpu host \
		-smp 4 \
		-bios /usr/share/edk2/x64/OVMF.4m.fd \
		-drive file=/tmp/arches-test-disk.qcow2,format=qcow2,if=virtio \
		-net nic -net user,hostfwd=tcp::2222-:22 \
		-vga virtio

test-iso-bios: ## Boot the built ISO in QEMU (BIOS, x86-64)
	@ISO=$$(ls -t $(OUT_DIR)/arches-*.iso 2>/dev/null | head -1); \
	if [ -z "$$ISO" ]; then echo "No ISO found in $(OUT_DIR)/. Run 'make iso-x86-64' first."; exit 1; fi; \
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

# ─── Ansible dev against VM ───────────────────────────

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

test-disk: ## Create a QEMU test disk image (20G)
	@echo "══ Creating test disk ══"
	qemu-img create -f qcow2 /tmp/arches-test-disk.qcow2 20G
	@echo "Created /tmp/arches-test-disk.qcow2 (20G)"

# ─── Cleanup ─────────────────────────────────────────

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

clean-work: ## Remove mkarchiso work directory
	@echo "══ Cleaning work directory ══"
	rm -rf $(WORK_DIR)

clean-all: clean clean-work ## Remove all build artifacts
	@echo "══ Cleaning output ══"
	rm -rf $(OUT_DIR)
	rm -f /tmp/arches-test-disk.qcow2

# ─── Checks ──────────────────────────────────────────

check-root:
	@if [ "$$(id -u)" -ne 0 ]; then \
		echo "ERROR: ISO build requires root. Run: sudo make iso-x86-64"; \
		exit 1; \
	fi

check-deps:
	@missing=""; \
	for cmd in mkarchiso pacman-key mksquashfs grub-mkstandalone; do \
		if ! command -v $$cmd &>/dev/null; then \
			missing="$$missing $$cmd"; \
		fi; \
	done; \
	if [ -n "$$missing" ]; then \
		echo "ERROR: Missing required commands:$$missing"; \
		echo "Install prerequisites: sudo pacman -S archiso grub"; \
		exit 1; \
	fi
