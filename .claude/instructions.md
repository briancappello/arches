## Python tooling

Always use `uv` for all Python operations. The Python project lives in `installer/`.

- Use `uv run python -m pytest` (from `installer/`) instead of `pytest` or `python -m pytest`
- Use `uv run python` instead of `python` or `python3`
- Use `uv pip install` instead of `pip install` or `pip3 install`
- Use `uv sync` to install dependencies from pyproject.toml
- Never use bare `pip`, `pip3`, `python -m pip`, or `python -m pytest`

## ISO Build & Test Workflow

### Persistent Builder

A persistent privileged Podman container is used for ISO builds. It runs under root's podman with passwordless sudo configured for the `brian` user via `/etc/sudoers.d/zz-arches-builder`.

**Starting the builder** (one-time per session, no sudo password needed):
```bash
./scripts/builder.sh start
```

**Building ISOs** (no sudo password needed):
```bash
./scripts/builder.sh build       # Graphical live ISO (default)
./scripts/builder.sh build-fb    # Framebuffer-only ISO (no desktop)
# or: make builder-iso / make builder-iso-fb
```

**Other commands:**
```bash
./scripts/builder.sh status      # Check if builder is running
./scripts/builder.sh log         # Show last 50 lines of build log
./scripts/builder.sh exec <cmd>  # Run arbitrary command in the builder
./scripts/builder.sh stop        # Stop the builder
```

**Build log:** Full build output is saved to `builder.log` at the project root. Use `./scripts/builder.sh log` or read the file directly for error details.

**Built ISOs** appear in `out/` on the host filesystem (bind-mounted from the container).

### Full Test Cycle

To test a complete install from ISO build through first-boot ansible:

```bash
# 1. Build the ISO
./scripts/builder.sh build

# 2. Create a fresh QEMU disk
make qemu-disk

# 3. Auto-install from the ISO (boots, installs, shuts down automatically)
make qemu-install

# 4. Boot the installed system (first-boot ansible runs on first boot)
make qemu-boot

# 5. SSH into the running VM to inspect
#    Username: arches, Password: password
ssh -p 2222 arches@localhost
```

### Debugging the Installed System via SSH

SSH uses port 2222 forwarded to the VM. Since there is no interactive TTY available, use the `SSH_ASKPASS` trick:

```bash
# Set up the askpass helper (one-time per session)
echo '#!/bin/sh
echo password' > /tmp/sshpass.sh && chmod +x /tmp/sshpass.sh

# SSH commands
DISPLAY=:0 SSH_ASKPASS=/tmp/sshpass.sh SSH_ASKPASS_REQUIRE=force \
  ssh -p 2222 -o PubkeyAuthentication=no -o StrictHostKeyChecking=accept-new \
  arches@localhost "<command>"

# If host key changes (after reinstall), clear it first:
ssh-keygen -R "[localhost]:2222"
```

### Key Log Locations on the Installed VM

- `/var/log/arches-install.log` — Full install pipeline log (persisted from live ISO)
- `/var/log/arches-firstboot.log` — Ansible first-boot playbook output
- `systemctl status arches-firstboot` — First-boot service status
- `/var/log/pacman.log` — Package installation history

### Clearing Stale Caches

If the offline package cache has corrupted/outdated packages:
```bash
# Use the builder container (has root access to the bind-mounted dir)
./scripts/builder.sh exec rm -rf /build/.offline-cache
```

If the AUR repo needs rebuilding:
```bash
FORCE=1 ./scripts/builder.sh build
```

### Environment Variables

- `OFFLINE=0` — Skip offline package caching (default: 1, cache everything)
- `FORCE=1` — Force rebuild of AUR/custom packages (ignores build cache)

### Architecture Notes

- The builder container runs with `--privileged` and `--pids-limit=-1` (unlimited PIDs required for mkarchiso)
- The project directory is bind-mounted at `/build` inside the container
- Sibling repos (for custom KDE packages) are discovered from `modules/*/build.mounts` and mounted read-only
- `SUDO_USER=builder` is set in the container so `build-aur-repo.sh` can drop privileges for `makepkg`
- The ISO boot menu entries are conditionally generated based on `ISO_MODE` (graphical/fb) and presence of `auto-install.toml`
- Auto-install is triggered by the `arches.autoinstall=1` kernel cmdline parameter, not by the mere presence of `auto-install.toml`
