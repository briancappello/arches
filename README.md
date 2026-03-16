# Arches

A personal Arch-based Linux distribution with a declarative, template-driven installer and configurable platform support.

Arches uses a **platform + template matrix** design. A *platform* defines the hardware foundation — kernel, repos, bootloader, disk layout, hardware detection — while a *template* defines the userspace on top — packages, services, Ansible roles. Templates are platform-independent. You build an ISO for a specific platform (`make iso-x86-64`), and at install time you select a template.

Currently supported platforms:

| Platform          | Base                     | Kernel          | Bootloader               | Filesystem         | Status                    |
|-------------------|--------------------------|-----------------|--------------------------|--------------------|---------------------------|
| `x86-64`          | CachyOS v3 (AVX2/SSE4.2) | `linux-cachyos` | Limine                   | btrfs + subvolumes | Fully implemented         |
| `aarch64-generic` | Arch Linux ARM           | `linux-aarch64` | GRUB                     | btrfs + subvolumes | Fully implemented         |
| `aarch64-apple`   | Asahi Linux              | `linux-asahi`   | GRUB (m1n1→U-Boot chain) | TBD                | Not implemented or tested |

## Quickstart

### Prerequisites

**ISO build (x86-64, native on Arch/CachyOS):**

```bash
sudo pacman -S archiso squashfs-tools base-devel git
```

CachyOS signing key must be trusted in your build environment:

```bash
sudo pacman-key --recv-keys F3B607488DB35A47 --keyserver keyserver.ubuntu.com
sudo pacman-key --lsign-key F3B607488DB35A47
```

**ISO build (aarch64, containerized):**

Runs inside a Podman container — works from any Linux host (Fedora, Arch, etc.):

```bash
# Fedora/RHEL:
sudo dnf install podman
# Arch:
sudo pacman -S podman
```

**QEMU testing (optional):**

```bash
# x86-64:
sudo pacman -S qemu-full edk2-ovmf
# aarch64 (Fedora):
sudo dnf install qemu-system-aarch64 edk2-aarch64
```

**Development:**

```bash
# Install uv (Python package manager) — https://docs.astral.sh/uv/
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Templates

See `installer/arches_installer/templates`. Two are included:

Dev Workstation
VM Server

#### Auto Install

`iso/airootfs/root/auto-install.toml`

The installer checks for `/root/auto-install.toml` in the running ISO. If this file exists, it will auto install the declared template. 

### Build

```bash
# 1. Build the ISO (requires root for mkarchiso)
sudo make iso-x86-64
# or
sudo make container-iso-aarch64

# 2. Create a QEMU test disk and boot the ISO
make test-disk
make test-iso          # UEFI mode
```

The built ISO is written to `out/arches-<date>.iso`. Each platform has its own make target (`iso-x86-64`, `iso-aarch64-generic`). The platform config is baked into the ISO at `/opt/arches/platform/platform.toml` so the installer knows which kernel, repos, and bootloader settings to use.

### Development

```bash
make fmt               # Auto-format Python with ruff
make test              # Run all tests (unit + TUI)
make test-template     # Validate all TOML templates parse
make dry-run           # Dry-run the example auto-install config (x86-64)
make clean             # Remove staged files from ISO airootfs
make clean-all         # Remove all build artifacts + output
```

Install dev dependencies:

```bash
uv sync --dev
```

Run `make` with no arguments to see all targets.

### Testing

Tests are split into two suites:

| Suite    | Path          | What it tests                                                                                           | Dependencies                |
|----------|---------------|---------------------------------------------------------------------------------------------------------|-----------------------------|
| **Core** | `tests/core/` | Template/platform loading, bootloader dispatch, disk partitioning, mount detection, auto-install config | Standard library only       |
| **TUI**  | `tests/tui/`  | Screen rendering, navigation, input validation, partition flow (manual + auto)                          | `textual`, `pytest-asyncio` |

TUI tests use Textual's `run_test()` framework to run the full app headlessly — no terminal or VM required. System calls (`lsblk`, `pacstrap`, etc.) are mocked so tests run anywhere.

```bash
# Run everything
make test

# Just the fast unit tests (no textual needed)
make test-unit
```

## Install Flow

The installer is a Python/Textual TUI that walks through:

1. **Disk selection** — detects available block devices
2. **Disk setup** — drop to a shell to partition, format, and mount disks onto `/mnt`. The installer detects ESP, root, boot, and home partitions from the mounts. An auto-partition option is available for VMs
3. **Template selection** — pick from pre-defined install profiles (TOML files)
4. **User setup** — hostname, username, password
5. **Confirmation** — review summary (including detected mounts for manual setup), then install

The install pipeline runs: `pacstrap` (platform base packages + kernel + template packages) → `genfstab` → `chroot config` → `hardware detection` (if platform enables it) → `mkinitcpio` → `bootloader` → `Snapper` (if platform uses btrfs + snapshots) → `Ansible (chroot phase)` → `first-boot service`.

For auto-partition mode (VMs only), the disk setup phase also wipes and partitions the target disk using the platform's `disk_layout` config before the install pipeline.

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

| Layer                            | File                             | What it controls                                                              |
|----------------------------------|----------------------------------|-------------------------------------------------------------------------------|
| **Platform `[base_packages]`**   | `platforms/*/platform.toml`      | Platform-specific packages always installed (repo keyrings, settings, kernel) |
| **Template `[system] packages`** | `templates/*.toml`               | Workload-specific packages installed via `pacstrap`                           |
| **Template `[services] enable`** | `templates/*.toml`               | Which systemd services get enabled at boot                                    |
| **Ansible role**                 | `ansible/roles/*/tasks/main.yml` | How those packages are configured after install                               |

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

The template's `[ansible] firstboot_roles` includes `"vm-server"`, which triggers this role on first boot. The installer runs `ansible-playbook --tags vm-server` so these tasks execute against the running system.

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

Template [ansible] firstboot_roles includes "vm-server"
  └─ ansible-playbook --tags vm-server runs on first boot
     └─ vm-server role: initdb, pg_hba.conf, listen_addresses, etc.

First boot
  └─ postgresql.service starts with the config Ansible applied
```

### When you don't need Ansible

For packages that work out of the box with no configuration (e.g., `htop`, `git`, `tmux`), you only need the template — just add them to `[system] packages`. No Ansible role needed. Ansible is only for post-install configuration that goes beyond `pacman -S`.

## Install Templates

Templates are declarative TOML files in `installer/arches_installer/templates/`. Each defines a **userspace workload** — packages, services, and Ansible roles. Templates are **platform-independent**: the kernel, repo keyrings, bootloader, disk layout, and hardware detection all come from the platform, not the template.

| Template            | Desktop         | Use Case                                  |
|---------------------|-----------------|-------------------------------------------|
| **Dev Workstation** | KDE Plasma      | Full development environment with desktop |
| **VM Server**       | None (headless) | Server workloads (nginx, postgres, redis) |

To add a new template, create a `.toml` file in the templates directory. The installer discovers all `.toml` files automatically:

```toml
[meta]
name = "My Template"
description = "Description shown in the installer"

[system]
timezone = "America/New_York"
locale = "en_US.UTF-8"
packages = ["git", "neovim"]     # installed via pacstrap

[services]
enable = ["NetworkManager"]      # enabled via systemctl enable

[ansible]
firstboot_roles = ["base", "zsh"]  # run on first boot
```

Disk layout (filesystem, partition scheme, subvolumes, ESP size) and bootloader configuration (Limine vs GRUB, snapshot boot) are defined by the **platform**, not the template. This means the same template works on x86-64 (btrfs + Limine) and aarch64 (btrfs + GRUB) without modification.

## Post-Install Automation

Configuration is applied in two phases:

| Phase          | When        | Mechanism                                    | Roles                                                          |
|----------------|-------------|----------------------------------------------|----------------------------------------------------------------|
| **First boot** | First login | systemd oneshot service → `ansible-playbook` | `base`, `zsh`, `kde`, `dev-tools`, `vm-server` (as applicable) |

The first-boot service runs once and removes its sentinel file (`/opt/arches/firstboot-pending`), so it won't re-run on subsequent boots.

Shell configuration uses [Oh My Zsh](https://ohmyz.sh/). The regular user gets the `bullet-train` theme (Powerline prompt); root gets the `fino` theme (visually distinct so it's obvious you're operating as root). Neovim base config is deployed to both users by the `base` role.

## Project Structure

```
arches/
├── Makefile                              # Build targets (see `make help`)
├── examples/
│   └── auto-install.toml                 # Example config for --auto mode
│
├── platforms/                            # Platform definitions (hardware layer)
│   ├── x86-64/
│   │   ├── platform.toml                 # Kernel, repos, bootloader, disk layout
│   │   ├── pacman.conf                   # CachyOS v3 + Arch repos
│   │   └── packages                      # Platform-specific ISO packages
│   ├── aarch64-generic/
│   │   ├── platform.toml                 # GRUB + ext4 + 4-partition layout
│   │   ├── pacman.conf                   # Arch Linux ARM repos
│   │   └── packages                      # Platform-specific ISO packages
│   └── aarch64-apple/
│       ├── platform.toml                 # GRUB + ext4 + Asahi firmware
│       ├── pacman.conf                   # Asahi + ALARM repos
│       └── packages                      # Platform-specific ISO packages
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
│   │   │   ├── disk.py                   # Partition, format, mount, detect mounts
│   │   │   ├── install.py                # pacstrap, genfstab, chroot config, hw detect
│   │   │   ├── bootloader.py             # Limine + GRUB install (dispatched by platform)
│   │   │   ├── snapper.py                # Snapper + limine-snapper-sync setup
│   │   │   └── firstboot.py              # systemd oneshot for post-install Ansible
│   │   ├── tui/
│   │   │   ├── app.py                    # Textual app + screen routing + install state
│   │   │   ├── welcome.py                # Disk detection + selection
│   │   │   ├── partition.py              # Shell-first partitioning + mount validation
│   │   │   ├── template_select.py        # Template picker with detail preview
│   │   │   ├── user_setup.py             # Hostname, username, password
│   │   │   ├── confirm.py                # Summary review (manual mounts or auto layout)
│   │   │   └── progress.py               # Threaded install (manual or auto partition)
│   │   └── templates/
│   │       ├── dev-workstation.toml      # KDE + btrfs + snapshots + dev tools
│   │       └── vm-server.toml            # Headless + ext4 + nginx/postgres/redis
│   └── tests/
│       ├── conftest.py                   # Shared fixtures (platform, templates, mocks)
│       ├── core/
│       │   ├── test_platform.py          # Platform config loading (x86-64 + aarch64)
│       │   ├── test_template.py          # Template loading + validation tests
│       │   ├── test_auto.py              # Auto-install config tests
│       │   ├── test_bootloader.py        # Bootloader dispatch, GRUB + Limine tests
│       │   └── test_disk.py              # Partition, mount detection, validation tests
│       └── tui/
│           ├── test_welcome.py           # Disk selection screen tests
│           ├── test_partition.py         # Partition screen (shell-first + auto) tests
│           ├── test_template_select.py   # Template picker tests
│           ├── test_user_setup.py        # Input validation tests
│           └── test_confirm.py           # Confirmation summary tests
│
├── ansible/                              # Post-install configuration
│   ├── playbook.yml                      # Tag-driven role dispatch
│   └── roles/
│       ├── base/tasks/main.yml           # pacman, journal, NTP, neovim config
│       ├── zsh/tasks/main.yml            # Oh-My-Zsh for user (bullet-train) + root (fino)
│       ├── kde/tasks/main.yml            # SDDM, Plasma config
│       ├── dev-tools/tasks/main.yml      # Rustup, Docker, user groups
│       └── vm-server/tasks/main.yml      # SSH hardening, Postgres, Redis
│
└── scripts/
    ├── build-aur-repo.sh                 # Pre-build AUR packages into local repo
    └── build-iso.sh                      # Full ISO build (called by Makefile)
```

## Key Technical Decisions

- **Platform/template matrix** — Hardware concerns (kernel, repos, bootloader, disk layout, GPU detection) are separated from workload concerns (packages, services, Ansible roles). Platforms are selected at ISO build time; templates are selected at install time. Templates work on any platform without modification.
- **Shell-first partitioning** — The default install flow drops the user to a shell to partition, format, and mount disks. The installer detects the mount layout on return. Auto-partition is available for VMs.
- **CachyOS v3 repos** (x86-64 platform) — Full Arch package set recompiled with AVX2/SSE4.2 optimizations. Covers all x86-64 hardware from 2011 onward. The CachyOS custom pacman fork is intentionally excluded to maintain standard Arch pacman semantics.
- **Bootloader dispatch** — The platform config determines the bootloader. x86-64 uses Limine (BIOS + UEFI, snapshot boot entries via `limine-snapper-sync`). aarch64-generic uses GRUB (UEFI-only, snapshot boot entries via `grub-btrfs`). Firmware type is auto-detected at install time.
- **Disk layout per platform** — x86-64: ESP (2G, doubles as /boot) + btrfs root with subvolumes (`@`, `@home`, `@var`). aarch64-generic: ESP (512M, at /boot/efi) + btrfs root with subvolumes (`@`, `@home`, `@var`). GRUB reads kernels from btrfs natively — no separate /boot partition needed.
- **ESP sizing** — 2 GiB on x86-64 for snapshot booting (each bootable snapshot copies its kernel/initramfs into the ESP via `limine-snapper-sync`). 512 MiB on aarch64 (`grub-btrfs` reads snapshots directly from btrfs, no kernel copies needed).
- **Hardware detection** — Controlled by the platform config. The x86-64 platform uses CachyOS `chwd` (Rust-based, replaces Manjaro's `mhwd`) to auto-install GPU drivers. ARM platforms disable it. Failures are always non-fatal.
- **Recovery mode** — The ISO doubles as a recovery environment with `btrfs-progs`, `testdisk`, `ddrescue`, `nvme-cli`, `smartmontools`, `nmap`, and more.

## Licensing

The build scripts and installer code in this repository are your own. CachyOS first-party packages used by the x86-64 platform (`cachyos-settings`, `chwd`, `linux-cachyos`) are GPL-3.0. CachyOS binary repositories are used under their terms for personal/Arch user use; pre-built ISOs with their repos embedded should not be publicly redistributed.
