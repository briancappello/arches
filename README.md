# Arches

A personal Arch-based Linux distribution with a declarative, template-driven installer and configurable platform support.

Arches uses a **platform + template matrix** design. A *platform* defines the hardware foundation — kernel, repos, bootloader, hardware detection — while a *template* defines the workload on top — filesystem, packages, services, Ansible roles. You build an ISO for a specific platform (`make iso-x86-64`), and at install time you select a template. Templates are platform-independent.

Currently supported platforms:

| Platform | Base | Kernel | Status |
|----------|------|--------|--------|
| `x86-64` | CachyOS v3 (AVX2/SSE4.2) | `linux-cachyos` | Fully implemented |
| `aarch64-generic` | Arch Linux ARM | `linux-aarch64` | Stub |
| `aarch64-apple` | Asahi Linux | `linux-asahi` | Stub |

## Quickstart

### Prerequisites

You need an Arch Linux (or Arch-based) system with:

```bash
sudo pacman -S archiso base-devel git qemu-full edk2-ovmf
```

CachyOS signing key must be trusted in your build environment:

```bash
sudo pacman-key --recv-keys F3B607488DB35A47 --keyserver keyserver.ubuntu.com
sudo pacman-key --lsign-key F3B607488DB35A47
```

### Build

```bash
# 1. Build the ISO for x86-64 (requires root — mkarchiso needs it)
#    This also pre-builds AUR packages, stages the platform config,
#    and assembles the package list from common + platform-specific lists.
sudo make iso-x86-64

# 2. Create a QEMU test disk and boot the ISO
make test-disk
make test-iso          # UEFI mode
make test-iso-bios     # BIOS mode
```

The built ISO is written to `out/arches-<date>.iso`. Each platform has its own make target (`iso-x86-64`, `iso-aarch64-generic`). The platform config is baked into the ISO at `/opt/arches/platform/platform.toml` so the installer knows which kernel, repos, and bootloader settings to use.

### Development

```bash
make lint              # Lint Python with ruff
make format            # Auto-format Python with ruff
make test              # Run all tests (unit + TUI)
make test-unit         # Run core unit tests only (no Textual needed)
make test-tui          # Run Textual TUI tests only
make test-template     # Validate all TOML templates parse
make dry-run           # Dry-run the example auto-install config (x86-64)
make clean             # Remove staged files from ISO airootfs
make clean-all         # Remove all build artifacts + output
```

Install dev dependencies:

```bash
cd installer && pip install -e '.[dev]'
```

Run `make` with no arguments to see all targets.

### Testing

Tests are split into two suites:

| Suite | Path | What it tests | Dependencies |
|-------|------|---------------|-------------|
| **Core** | `tests/core/` | Template loading, platform config, auto-install config, validation | Standard library only |
| **TUI** | `tests/tui/` | Screen rendering, navigation, input validation | `textual`, `pytest-asyncio` |

TUI tests use Textual's `run_test()` framework to run the full app headlessly — no terminal or VM required. System calls (`lsblk`, `pacstrap`, etc.) are mocked so tests run anywhere.

```bash
# Run everything
make test

# Just the fast unit tests (no textual needed)
make test-unit
```

## Install Flow

When the ISO boots, you're presented with a choice:

```
  [1] Launch Installer
  [2] Recovery Shell
```

The installer is a Python/Textual TUI that walks through:

1. **Disk selection** — detects available block devices
2. **Partitioning** — auto-partition (GPT: ESP + root) or drop to a shell for manual setup
3. **Template selection** — pick from pre-defined install profiles (TOML files)
4. **User setup** — hostname, username, password
5. **Confirmation** — review summary, then install

The install pipeline runs: `partition` → `pacstrap` (platform base packages + kernel + template packages) → `genfstab` → `chroot config` → `hardware detection` (if platform enables it) → `mkinitcpio` → `bootloader` → `Snapper` (if template uses btrfs snapshots) → `Ansible (chroot phase)` → `first-boot service`.

### Unattended Install (`--auto`)

For scripted/automated installs (e.g., in a VM), skip the TUI entirely:

```bash
arches-install --auto config.toml
```

The config file specifies everything the TUI would ask for:

```toml
[install]
device = "/dev/vda"
template = "installer/arches_installer/templates/dev-workstation.toml"
hostname = "archdev"
username = "brian"
password = "changeme"
```

The platform (kernel, repos, hardware detection) is read from the ISO automatically. During development, you can override it with `--platform`:

```bash
arches-install --auto examples/auto-install.toml --platform platforms/x86-64/platform.toml --dry-run
```

See `examples/auto-install.toml` for a full example.

## How Packages Get Installed and Configured

Customizing what software lands on the system happens at three layers:

| Layer | File | What it controls |
|-------|------|-----------------|
| **Platform `[base_packages]`** | `platforms/*/platform.toml` | Platform-specific packages always installed (repo keyrings, settings, kernel) |
| **Template `[system] packages`** | `templates/*.toml` | Workload-specific packages installed via `pacstrap` |
| **Template `[services] enable`** | `templates/*.toml` | Which systemd services get enabled at boot |
| **Ansible role** | `ansible/roles/*/tasks/main.yml` | How those packages are configured after install |

The platform provides the hardware foundation (kernel, repo keys, GPU detection tools). The template says **what** workload to install on top. The Ansible role says **how** to configure it. All three are declarative and version-controlled.

### Example: Adding PostgreSQL and Redis to the VM Server

**Step 1: Add the packages to the template** (`installer/arches_installer/templates/vm-server.toml`):

```toml
[system]
packages = [
    # ...existing packages...
    "postgresql",
    "redis",
]

[services]
enable = [
    # ...existing services...
    "postgresql",
    "redis",
]
```

This ensures `pacstrap` installs both packages and `systemctl enable` starts them on boot. At this point the services would run with their default upstream configs — that's often enough for a dev environment.

**Step 2: Configure them via Ansible** (`ansible/roles/vm-server/tasks/main.yml`):

```yaml
- name: Initialize PostgreSQL data directory
  command: su - postgres -c "initdb -D /var/lib/postgres/data"
  args:
    creates: /var/lib/postgres/data/PG_VERSION
  ignore_errors: yes

- name: Configure Redis to bind localhost only
  lineinfile:
    path: /etc/redis/redis.conf
    regexp: '^bind'
    line: 'bind 127.0.0.1 -::1'
    create: yes
```

The template's `[ansible] chroot_roles = ["base", "vm-server"]` triggers this role during install. The installer runs `ansible-playbook --tags vm-server` inside `arch-chroot`, so these tasks execute against the new filesystem before the first boot.

**Step 3: That's it.** On first boot, PostgreSQL and Redis start with the config Ansible applied.

### The full sequence for a single package

Taking `postgresql` as a concrete example, here's exactly what happens at each stage of the install:

```
Platform [base_packages] install = ["cachyos-keyring", ...]
  └─ pacstrap installs platform base packages + kernel

Template [system] packages = ["postgresql"]
  └─ pacstrap installs the postgresql package from platform repos

Template [services] enable = ["postgresql"]
  └─ systemctl enable postgresql inside the chroot

Template [ansible] chroot_roles = ["vm-server"]
  └─ ansible-playbook --tags vm-server runs in chroot
     └─ vm-server role: initdb, pg_hba.conf, listen_addresses, etc.

First boot
  └─ postgresql.service starts with the config Ansible applied
```

### When you don't need Ansible

For packages that work out of the box with no configuration (e.g., `htop`, `git`, `tmux`), you only need the template — just add them to `[system] packages`. No Ansible role needed. Ansible is only for post-install configuration that goes beyond `pacman -S`.

## Install Templates

Templates are declarative TOML files in `installer/arches_installer/templates/`. Each defines a workload — filesystem layout, packages, services, and Ansible roles. Templates are **platform-independent**: the kernel, repo keyrings, and hardware detection come from the platform, not the template.

| Template | Filesystem | Desktop | Snapshots |
|----------|-----------|---------|-----------|
| **Dev Workstation** | btrfs (`@`, `@home`, `@var`, `@snapshots`) | KDE Plasma | Yes (Snapper + limine-snapper-sync) |
| **VM Server** | ext4 | None (headless) | No |

To add a new template, create a `.toml` file in the templates directory. The installer discovers all `.toml` files automatically:

```toml
[meta]
name = "My Template"
description = "Description shown in the installer"

[disk]
filesystem = "btrfs"             # "btrfs" or "ext4"
subvolumes = ["@", "@home"]      # btrfs only
mount_options = "compress=zstd:1,noatime"
esp_size_mib = 2048
swap = "zram"

[bootloader]
type = "limine"
snapshot_boot = true

[system]
timezone = "America/New_York"
locale = "en_US.UTF-8"
packages = ["git", "neovim"]     # installed via pacstrap

[services]
enable = ["NetworkManager"]      # enabled via systemctl enable

[ansible]
chroot_roles = ["base"]          # run during install (in chroot)
firstboot_roles = ["dotfiles"]   # run on first boot
```

## Post-Install Automation

Configuration is applied in two phases:

| Phase | When | Mechanism | Roles |
|-------|------|-----------|-------|
| **Chroot** | During install | `ansible-playbook` inside `arch-chroot` | `base`, `kde`, `dev-tools`, `vm-server` |
| **First boot** | First login | systemd oneshot service → `ansible-playbook` | `dotfiles` |

The split exists because some things must happen against the offline filesystem (locale, bootloader, service enablement, database init), while others need a running system (network-dependent dotfiles, user-session config).

The first-boot service runs once and removes its sentinel file (`/opt/arches/firstboot-pending`), so it won't re-run on subsequent boots.

Dotfiles are managed via [chezmoi](https://www.chezmoi.io/). Stubs are provided in the `dotfiles/` directory — point them at your own repo when ready.

## Project Structure

```
arches/
├── Makefile                              # Build targets (see `make help`)
├── examples/
│   └── auto-install.toml                 # Example config for --auto mode
│
├── platforms/                            # Platform definitions (hardware layer)
│   ├── x86-64/
│   │   ├── platform.toml                 # Kernel, repos, bootloader, base packages
│   │   ├── pacman.conf                   # CachyOS v3 + Arch repos
│   │   └── packages                      # Platform-specific ISO packages
│   ├── aarch64-generic/
│   │   └── platform.toml                 # Stub — Arch Linux ARM
│   └── aarch64-apple/
│       └── platform.toml                 # Stub — Asahi Linux
│
├── iso/                                  # archiso profile
│   ├── profiledef.sh                     # ISO identity, boot modes (parameterized)
│   ├── packages.common                   # Platform-agnostic ISO packages
│   └── airootfs/
│       ├── etc/
│       │   └── mkinitcpio.conf           # Hardware-agnostic (kms, no autodetect)
│       └── root/
│           └── .bash_profile             # Boot menu: installer or recovery shell
│
├── installer/                            # Python package — the TUI installer
│   ├── pyproject.toml                    # Package config, builds `arches-install` CLI
│   ├── arches_installer/
│   │   ├── __main__.py                   # Entry point (--auto or TUI, --platform)
│   │   ├── core/
│   │   │   ├── platform.py               # Platform config loader + dataclasses
│   │   │   ├── auto.py                   # Unattended install runner
│   │   │   ├── template.py               # TOML template loader + dataclasses
│   │   │   ├── disk.py                   # Partition, format, mount (btrfs + ext4)
│   │   │   ├── install.py                # pacstrap, genfstab, chroot config, hw detect
│   │   │   ├── bootloader.py             # Limine install + config (UEFI/BIOS auto)
│   │   │   ├── snapper.py                # Snapper + limine-snapper-sync setup
│   │   │   └── firstboot.py              # systemd oneshot for post-install Ansible
│   │   ├── tui/
│   │   │   ├── app.py                    # Textual app + screen routing
│   │   │   ├── welcome.py                # Disk detection + selection
│   │   │   ├── partition.py              # Auto-partition or drop to shell
│   │   │   ├── template_select.py        # Template picker with detail preview
│   │   │   ├── user_setup.py             # Hostname, username, password
│   │   │   ├── confirm.py                # Summary review (platform + template)
│   │   │   └── progress.py               # Threaded install with live log output
│   │   └── templates/
│   │       ├── dev-workstation.toml      # KDE + btrfs + snapshots + dev tools
│   │       └── vm-server.toml            # Headless + ext4 + nginx/postgres/redis
│   └── tests/
│       ├── conftest.py                   # Shared fixtures (platform, templates, mocks)
│       ├── core/
│       │   ├── test_platform.py          # Platform config loading + validation
│       │   ├── test_template.py          # Template loading + validation tests
│       │   └── test_auto.py              # Auto-install config tests
│       └── tui/
│           ├── test_welcome.py           # Disk selection screen tests
│           ├── test_partition.py         # Partition screen tests
│           ├── test_template_select.py   # Template picker tests
│           ├── test_user_setup.py        # Input validation tests
│           └── test_confirm.py           # Confirmation summary tests
│
├── ansible/                              # Post-install configuration
│   ├── playbook.yml                      # Tag-driven role dispatch
│   └── roles/
│       ├── base/tasks/main.yml           # pacman, journal, NTP, shell defaults
│       ├── kde/tasks/main.yml            # SDDM, Plasma config
│       ├── dev-tools/tasks/main.yml      # Rustup, Docker, user groups
│       ├── vm-server/tasks/main.yml      # SSH hardening, Postgres, Redis
│       └── dotfiles/tasks/main.yml       # chezmoi bootstrap
│
├── dotfiles/                             # chezmoi-managed stubs
│   ├── .chezmoiroot
│   └── home/
│       ├── dot_zshrc                     # Zsh config (starship, zoxide, aliases)
│       └── dot_config/nvim/init.lua      # Neovim base config
│
└── scripts/
    ├── build-aur-repo.sh                 # Pre-build AUR packages into local repo
    └── build-iso.sh                      # Full ISO build (called by Makefile)
```

## Key Technical Decisions

- **Platform/template matrix** — Hardware concerns (kernel, repos, bootloader EFI paths, GPU detection) are separated from workload concerns (filesystem, packages, services, Ansible roles). Platforms are selected at ISO build time; templates are selected at install time. Templates work on any platform without modification.
- **CachyOS v3 repos** (x86-64 platform) — Full Arch package set recompiled with AVX2/SSE4.2 optimizations. Covers all x86-64 hardware from 2011 onward. The CachyOS custom pacman fork is intentionally excluded to maintain standard Arch pacman semantics.
- **Limine bootloader** — Supports both BIOS and UEFI. Firmware type is auto-detected at install time. EFI binary paths are platform-specific (`BOOTX64.EFI` vs `BOOTAA64.EFI`). Snapshot boot entries are managed by `limine-snapper-sync`.
- **Btrfs layout** — `@ / @home / @var / @snapshots` with `compress=zstd:1,noatime,ssd,discard=async`. The `@var` subvolume is separated to exclude logs/cache from snapshots.
- **ESP sizing** — 2 GiB for templates with snapshot booting (each bootable snapshot copies its kernel/initramfs into the ESP), 512 MiB otherwise.
- **Hardware detection** — Controlled by the platform config. The x86-64 platform uses CachyOS `chwd` (Rust-based, replaces Manjaro's `mhwd`) to auto-install GPU drivers. ARM platforms disable it. Failures are always non-fatal.
- **Recovery mode** — The ISO doubles as a recovery environment with `btrfs-progs`, `testdisk`, `ddrescue`, `nvme-cli`, `smartmontools`, `nmap`, and more.

## Licensing

The build scripts and installer code in this repository are your own. CachyOS first-party packages used by the x86-64 platform (`cachyos-settings`, `chwd`, `linux-cachyos`) are GPL-3.0. CachyOS binary repositories are used under their terms for personal/Arch user use; pre-built ISOs with their repos embedded should not be publicly redistributed.
