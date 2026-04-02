# Arches

A personal Arch-based Linux distribution with a declarative, template-driven installer and configurable platform support.

Arches uses a **platform + template matrix** design. A *platform* defines the hardware foundation — kernel, repos, bootloader, disk layout, hardware detection — while a *template* defines the userspace on top — packages, services, Ansible roles. Templates are platform-independent. You build an ISO with `make iso` (the platform is auto-detected), and at install time you select a template.

Currently supported platforms:

| Platform          | Base                        | Kernel(s)                             | Bootloader               | Default Template   | Status                    |
|-------------------|-----------------------------|---------------------------------------|--------------------------|--------------------| --------------------------|
| `x86-64`          | CachyOS (configurable tier) | `linux-cachyos` + `linux-cachyos-lts` | Limine                   | dev-workstation    | Fully implemented         |
| `aarch64-generic` | Arch Linux ARM              | `linux-aarch64`                       | GRUB                     | dev-workstation    | Fully implemented         |
| `aarch64-apple`   | Asahi Linux                 | `linux-asahi`                         | m1n1→U-Boot→extlinux     | dev-workstation    | USB boot via U-Boot       |

Each platform has a README with platform-specific configuration details (see `platforms/{ISA}/README.md`).

## Quickstart

### Prerequisites

Runs inside a Podman container — works from any Linux host (Fedora, Arch, etc.):

```bash
# Arch:
sudo pacman -S podman

# Fedora/RHEL:
sudo dnf install podman
```

#### QEMU testing (optional)

```bash
# Arch
sudo pacman -S qemu-full edk2-ovmf

# Fedora/RHEL
sudo dnf install qemu-system-aarch64 edk2-aarch64
```

### Templates

All templates live in `templates/`. System templates define the installable workloads:

- `dev-workstation.toml` — KDE Plasma desktop with full development toolchain
- `vm-server.toml` — Headless server (Nginx, PostgresSQL, Redis, RabbitMQ)

Config templates are used for install automation:

- `auto-install.toml` — Unattended install config (copy to `iso/airootfs/root/auto-install.toml` to enable)
- `host-install.toml` — Host-install config (install into btrfs subvolumes on an existing system)

#### Auto Install

The installer checks for `/root/auto-install.toml` in the running ISO. If this file exists, it will auto install the declared template.

### Build

All builds run inside Podman containers regardless of host distro. The platform is auto-detected from the host hardware but can be overridden with `PLATFORM=`. The template defaults to the platform's `default_template` but can be overridden with `TEMPLATE=`.

The ISO is built as a **superset** of the selected template's installed system. For graphical templates (like `dev-workstation`), the live ISO boots into the full KDE Plasma desktop with a `liveuser` autologin. For non-graphical templates (like `vm-server`), the ISO boots to a text console with the TUI installer.

```bash
# Build install media and write to USB drive (auto-detects platform + default template)
sudo make usb

# Or just build the ISO
sudo make iso

# Explicit template override
sudo TEMPLATE=vm-server make iso

# Offline ISO — pre-cache all packages so installs work without internet
sudo make iso OFFLINE=1

# Install into a QEMU VM (builds ISO if needed, creates disk, boots QEMU)
make qemu-install

# Offline QEMU test — builds offline ISO + boots VM without network
make qemu-install OFFLINE=1
```

**USB boot on Apple Silicon:**

On Apple Silicon, `make usb` builds the ISO and converts it to a GPT+FAT32 USB image for U-Boot's native extlinux boot protocol. After writing:

1. Plug USB-C drive into a working port (closest to power cable)
2. Reboot into Asahi (where U-Boot runs)
3. Interrupt U-Boot and type: `bootflow scan -b usb`

Auto-install is disabled on Apple Silicon to prevent accidental disk wipes; use the manual partitioning flow or host-install instead. Requires an existing Asahi boot chain (m1n1 + U-Boot) on the Mac's internal NVMe.

**Host install (into btrfs subvolumes on a running Asahi system):**

```bash
sudo make host-install CONFIG=templates/host-install.toml
```

This installs Arches into btrfs subvolumes alongside the existing Asahi Linux (e.g. Fedora) without touching the partition table. Runs inside a Podman container on the host. See `templates/host-install.toml` for configuration.

The built ISO is written to `out/arches-<date>.iso`. The platform config is baked into the ISO at `/opt/arches/platform/platform.toml` so the installer knows which kernel, repos, and bootloader settings to use.

### Development

```bash
make fmt               # Auto-format Python with ruff
make test              # Run all tests (unit + TUI)
make test-unit         # Run fast unit tests only (no TUI/textual tests)
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
template = "dev-workstation.toml"
hostname = "archdev"
username = "brian"
password = "changeme"        # Change this before building
```

> **Note:** The `password` field in config templates (`auto-install.toml`, `host-install.toml`) ships with placeholder values. Always change these before building an ISO or running a host install. The target disk is auto-detected at install time (must be exactly one non-removable disk).

The platform (kernel, repos, hardware detection) is read from the ISO automatically. During development, you can override it with `--platform`:

```bash
arches-install --auto templates/auto-install.toml --platform platforms/x86-64/platform.toml --dry-run
```

See `templates/auto-install.toml` for a full example.

## How Packages Get Installed and Configured

Customizing what software lands on the system happens at three layers:

| Layer                            | File                             | What it controls                                                              |
|----------------------------------|----------------------------------|-------------------------------------------------------------------------------|
| **Platform `[base_packages]`**   | `platforms/*/platform.toml`      | Platform-specific packages always installed (repo keyrings, settings, kernel) |
| **Template `[install.pacstrap]`** | `templates/*.toml`               | Workload-specific packages installed via `pacstrap`                           |
| **Template `[services] enable`** | `templates/*.toml`               | Which systemd services get enabled at boot                                    |
| **Ansible role**                 | `ansible/roles/*/tasks/main.yml` | How those packages are configured after install                               |

The platform provides the hardware foundation (kernel, repo keys, GPU detection tools). The template says **what** workload to install on top. The Ansible role says **how** to configure it. All three are declarative and version-controlled.

### Example: Adding PostgreSQL and Redis to the VM Server

**Step 1: Add the packages to the template** (`templates/vm-server.toml`):

```toml
[install.pacstrap]
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

Template [install.pacstrap] packages = ["postgresql"]
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

For packages that work out of the box with no configuration (e.g., `htop`, `git`, `tmux`), you only need the template — just add them to `[install.pacstrap] packages`. No Ansible role needed. Ansible is only for post-install configuration that goes beyond `pacman -S`.

## Install Templates

Templates are declarative TOML files in `templates/`. Each defines a **userspace workload** — packages, services, and Ansible roles. Templates are **platform-independent**: the kernel, repo keyrings, bootloader, disk layout, and hardware detection all come from the platform, not the template.

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

[install.pacstrap]
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
│
├── templates/                            # All template files (single source of truth)
│   ├── dev-workstation.toml              # KDE Plasma desktop + dev tools (graphical)
│   ├── vm-server.toml                    # Headless server (nginx, postgres, redis)
│   ├── auto-install.toml                 # Unattended install config (baked into ISO)
│   └── host-install.toml                 # Host-install config (btrfs subvolumes)
│
├── platforms/                            # Platform definitions (hardware layer)
│   ├── x86-64/
│   │   ├── platform.toml                 # Kernel, repos, bootloader, disk layout
│   │   ├── pacman.conf                   # CachyOS v3 + Arch repos
│   │   └── packages                      # Platform-specific ISO packages
│   ├── aarch64-generic/
│   │   ├── platform.toml                 # GRUB + btrfs + subvolumes
│   │   ├── archiso.conf                  # mkinitcpio config for ISO
│   │   ├── pacman.conf                   # Arch Linux ARM repos
│   │   └── packages                      # Platform-specific ISO packages
│   └── aarch64-apple/
│       ├── platform.toml                 # extlinux + btrfs + Asahi firmware
│       ├── archiso.conf                  # mkinitcpio config (Apple USB-C PHY modules)
│       ├── pacman.conf                   # Asahi + ALARM repos
│       └── packages                      # Platform-specific ISO packages
│
├── iso/                                  # archiso profile
│   ├── profiledef.sh                     # ISO identity, boot modes (parameterized)
│   ├── packages.iso                      # ISO live environment packages
│   ├── packages.graphical_iso            # Graphical desktop packages (conditional)
│   └── airootfs/
│       └── root/
│           └── .bash_profile             # Boot menu: installer or recovery shell
│
├── pyproject.toml                        # Package config, builds `arches-install` CLI
├── Containerfile                         # Multi-arch ISO builder (Podman)
├── Containerfile.install                 # Host-install container (aarch64-apple)
│
├── installer/                            # Python package — the TUI installer
│   ├── arches_installer/
│   │   ├── __main__.py                   # Entry point (--auto or TUI, --platform)
│   │   ├── core/
│   │   │   ├── platform.py               # Platform config loader + dataclasses
│   │   │   ├── auto.py                   # Unattended install config parser
│   │   │   ├── template.py               # TOML template loader + dataclasses
│   │   │   ├── disk.py                   # Partition, format, mount, detect mounts
│   │   │   ├── install.py                # pacstrap, genfstab, chroot config, hw detect
│   │   │   ├── pipeline.py               # Install pipeline orchestration
│   │   │   ├── run.py                    # Subprocess execution helpers + logging
│   │   │   ├── bootloader.py             # Limine + GRUB install (dispatched by platform)
│   │   │   ├── snapper.py                # Snapper + limine-snapper-sync setup
│   │   │   ├── host_install.py           # Host-install runner (btrfs subvolumes)
│   │   │   └── firstboot.py              # systemd oneshot for post-install Ansible
│   │   ├── tui/
│   │   │   ├── app.py                    # Textual app + screen routing + install state
│   │   │   ├── welcome.py                # Disk detection + selection
│   │   │   ├── partition.py              # Shell-first partitioning + mount validation
│   │   │   ├── template_select.py        # Template picker with detail preview
│   │   │   ├── user_setup.py             # Hostname, username, password
│   │   │   ├── confirm.py                # Summary review (manual mounts or auto layout)
│   │   │   └── progress.py               # Threaded install (manual or auto partition)
│   └── tests/
│       ├── conftest.py                   # Shared fixtures (platform, templates, mocks)
│       ├── core/
│       │   ├── test_platform.py          # Platform config loading (x86-64 + aarch64)
│       │   ├── test_template.py          # Template loading + validation tests
│       │   ├── test_auto.py              # Auto-install config tests
│       │   ├── test_bootloader.py        # Bootloader dispatch, GRUB + Limine tests
│       │   ├── test_disk.py              # Partition, mount detection, validation tests
│       │   ├── test_install.py           # Core install logic tests
│       │   ├── test_pipeline.py          # Install pipeline tests
│       │   ├── test_run.py               # Command execution tests
│       │   ├── test_firstboot.py         # First-boot service tests
│       │   ├── test_snapper.py           # Snapshot configuration tests
│       │   ├── test_host_install.py      # Host-install config + GRUB entry tests
│       │   └── test_main.py              # CLI entry point tests
│       └── tui/
│           ├── test_welcome.py           # Disk selection screen tests
│           ├── test_partition.py         # Partition screen (shell-first + auto) tests
│           ├── test_template_select.py   # Template picker tests
│           ├── test_user_setup.py        # Input validation tests
│           ├── test_confirm.py           # Confirmation summary tests
│           └── test_progress.py          # Install progress screen tests
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
    ├── detect-platform.sh                # Auto-detect host platform (x86-64/aarch64-generic/aarch64-apple)
    ├── build-iso.sh                      # Build ISO inside Podman container (any platform)
    ├── build-usb.sh                      # Build ISO + write to USB (platform-aware)
    ├── build-aur-repo.sh                 # Pre-build AUR packages into local repo
    ├── cache-template-packages.sh        # Cache template packages for offline install
    ├── qemu-install.sh                   # Build ISO + boot QEMU VM with install disk
    ├── detect-esp.sh                     # Auto-detect ESP partition and bootloader
    ├── iso-to-usb-image.sh              # Convert ISO to GPT+FAT32 USB image (aarch64)
    ├── write-usb.sh                      # Interactive USB drive writer (device select + confirm)
    ├── host-install.sh                   # Host install into btrfs subvolumes (Apple Silicon)
    └── host-clean.sh                     # Remove Arches subvolumes and GRUB entry
```

## Key Technical Decisions

- **Platform/template matrix** — Hardware concerns (kernel, repos, bootloader, disk layout, GPU detection) are separated from workload concerns (packages, services, Ansible roles). Platforms are selected at ISO build time; templates are selected at install time. Templates work on any platform without modification.
- **Shell-first partitioning** — The default install flow drops the user to a shell to partition, format, and mount disks. The installer detects the mount layout on return. Auto-partition is available for VMs.
- **CachyOS repos** (x86-64 platform) — Full Arch package set recompiled with configurable optimization tiers (`x86-64` baseline, `x86-64-v3` AVX2/SSE4.2, `x86-64-v4` AVX-512, `znver4` AMD Zen 4/5). See `platforms/x86-64/README.md` for tier details. The CachyOS custom pacman fork is intentionally excluded to maintain standard Arch pacman semantics.
- **Bootloader dispatch** — The platform config determines the bootloader. x86-64 uses Limine (BIOS + UEFI, snapshot boot entries via `limine-snapper-sync`). aarch64-generic uses GRUB (UEFI-only, snapshot boot entries via `grub-btrfs`). aarch64-apple uses the m1n1 → U-Boot → extlinux chain; U-Boot's `bootflow scan` finds `/extlinux/extlinux.conf` on the USB drive and boots the kernel directly (no GRUB in the USB boot path). Firmware type is auto-detected at install time.
- **Disk layout per platform** — x86-64: ESP (2G, doubles as /boot) + btrfs root with subvolumes (`@`, `@home`, `@var`). aarch64-generic: ESP (512M, at /boot/efi) + btrfs root with subvolumes (`@`, `@home`, `@var`). GRUB reads kernels from btrfs natively — no separate /boot partition needed.
- **ESP sizing** — 2 GiB on x86-64 for snapshot booting (each bootable snapshot copies its kernel/initramfs into the ESP via `limine-snapper-sync`). 512 MiB on aarch64 (`grub-btrfs` reads snapshots directly from btrfs, no kernel copies needed).
- **Hardware detection** — Controlled by the platform config. The x86-64 platform uses CachyOS `chwd` (Rust-based, replaces Manjaro's `mhwd`) to auto-install GPU drivers. ARM platforms disable it. Failures are always non-fatal.
- **Recovery mode** — The ISO doubles as a recovery environment with `btrfs-progs`, `testdisk`, `ddrescue`, `nvme-cli`, `smartmontools`, `nmap`, and more.

## Licensing

The build scripts and installer code in this repository are your own. CachyOS first-party packages used by the x86-64 platform (`cachyos-settings`, `chwd`, `linux-cachyos`) are GPL-3.0. CachyOS binary repositories are used under their terms for personal/Arch user use; pre-built ISOs with their repos embedded should not be publicly redistributed.
