# ─────────────────────────────────────────────────────────
# Arches — Custom Arch/CachyOS Install & Recovery ISO
# ─────────────────────────────────────────────────────────

SHELL       := /bin/bash
PROJECT_DIR := $(shell pwd)
ISO_PROFILE := $(PROJECT_DIR)/iso
INSTALLER   := $(PROJECT_DIR)/installer
ANSIBLE_DIR := $(PROJECT_DIR)/ansible
SCRIPTS     := $(PROJECT_DIR)/scripts
WORK_DIR    := /tmp/arches-work
OUT_DIR     := $(PROJECT_DIR)/out

# ─── Phony targets ────────────────────────────────────

.PHONY: help iso aur-repo clean clean-all lint format test test-unit test-tui \
        test-template check-root check-deps stage-installer stage-ansible

# ─── Default ──────────────────────────────────────────

help: ## Show this help
	@printf '\n  \033[1mArches Build Targets\033[0m\n\n'
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@printf '\n'

# ─── ISO build ────────────────────────────────────────

iso: check-root check-deps aur-repo stage-installer stage-ansible ## Build the full ISO (requires sudo)
	@echo "══ Building Arches ISO ══"
	@mkdir -p $(OUT_DIR)
	mkarchiso -v -w $(WORK_DIR) -o $(OUT_DIR) $(ISO_PROFILE)
	@echo ""
	@echo "══ ISO built ══"
	@ls -lh $(OUT_DIR)/arches-*.iso 2>/dev/null

aur-repo: ## Pre-build AUR packages into local repo
	@echo "══ Building AUR repo ══"
	$(SCRIPTS)/build-aur-repo.sh

stage-installer: ## Copy installer into ISO airootfs
	@echo "══ Staging installer ══"
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/installer
	@cp -r $(INSTALLER)/* $(ISO_PROFILE)/airootfs/opt/arches/installer/
	@mkdir -p $(ISO_PROFILE)/airootfs/usr/local/bin
	@printf '#!/usr/bin/env bash\n\
cd /opt/arches/installer\n\
python -m pip install --quiet --break-system-packages textual 2>/dev/null\n\
exec python -m arches_installer\n' > $(ISO_PROFILE)/airootfs/usr/local/bin/arches-install
	@chmod +x $(ISO_PROFILE)/airootfs/usr/local/bin/arches-install

stage-ansible: ## Copy Ansible playbooks into ISO airootfs
	@echo "══ Staging Ansible ══"
	@mkdir -p $(ISO_PROFILE)/airootfs/opt/arches/ansible
	@cp -r $(ANSIBLE_DIR)/* $(ISO_PROFILE)/airootfs/opt/arches/ansible/

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
		--auto examples/auto-install.toml --dry-run

# ─── QEMU testing ─────────────────────────────────────

test-iso: ## Boot the built ISO in QEMU (UEFI)
	@ISO=$$(ls -t $(OUT_DIR)/arches-*.iso 2>/dev/null | head -1); \
	if [ -z "$$ISO" ]; then echo "No ISO found in $(OUT_DIR)/. Run 'make iso' first."; exit 1; fi; \
	echo "══ Booting $$ISO in QEMU (UEFI) ══"; \
	qemu-system-x86_64 \
		-enable-kvm \
		-m 4G \
		-cpu host \
		-smp 4 \
		-bios /usr/share/ovmf/x64/OVMF.fd \
		-drive file=$$ISO,format=raw,media=cdrom \
		-drive file=/tmp/arches-test-disk.qcow2,format=qcow2,if=virtio \
		-net nic -net user \
		-vga virtio

test-iso-bios: ## Boot the built ISO in QEMU (BIOS)
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
		-net nic -net user \
		-vga std

test-disk: ## Create a QEMU test disk image (20G)
	@echo "══ Creating test disk ══"
	qemu-img create -f qcow2 /tmp/arches-test-disk.qcow2 20G
	@echo "Created /tmp/arches-test-disk.qcow2 (20G)"

# ─── Cleanup ─────────────────────────────────────────

clean: ## Remove staged files from ISO airootfs
	@echo "══ Cleaning staged files ══"
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches/installer
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches/ansible
	rm -f  $(ISO_PROFILE)/airootfs/usr/local/bin/arches-install
	rm -rf $(ISO_PROFILE)/airootfs/opt/arches-repo

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
		echo "ERROR: ISO build requires root. Run: sudo make iso"; \
		exit 1; \
	fi

check-deps:
	@missing=""; \
	for cmd in mkarchiso pacman-key mksquashfs; do \
		if ! command -v $$cmd &>/dev/null; then \
			missing="$$missing $$cmd"; \
		fi; \
	done; \
	if [ -n "$$missing" ]; then \
		echo "ERROR: Missing required commands:$$missing"; \
		echo "Install archiso: sudo pacman -S archiso"; \
		exit 1; \
	fi
