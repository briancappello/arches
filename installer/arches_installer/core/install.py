"""Core install logic — pacstrap, genfstab, chroot configuration."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from arches_installer.core.disk import MOUNT_ROOT
from arches_installer.core.platform import ISO_PLATFORM_DIR, PlatformConfig
from arches_installer.core.run import LogCallback, _log, chroot_run, run
from arches_installer.core.template import InstallTemplate

# Path to the pacman.conf that includes platform-specific repos.
# This is the platform pacman.conf staged into the ISO (with correct
# repo URLs for the live environment), NOT /etc/pacman.conf (which is
# the archiso build-time config with host-specific paths).
ISO_PACMAN_CONF = ISO_PLATFORM_DIR / "pacman.conf"

# Pre-downloaded package cache baked into the ISO
ISO_PKG_CACHE = Path("/opt/arches/pkg-cache")

# Mount point inside the target for the ISO package cache (bind-mounted)
_TARGET_ISO_CACHE = Path("/mnt/arches-pkg-cache")
_TARGET_ISO_CACHE_MOUNTED = False

# Ansible playbooks shipped on the ISO
ISO_ANSIBLE_DIR = Path("/opt/arches/ansible")

# Build host SSH public key (optional, embedded at ISO build time)
ISO_BUILD_HOST_PUBKEY = Path("/opt/arches/build-host.pub")


def _setup_local_repo_mirror(log: LogCallback | None = None) -> Path | None:
    """Create a local file:// mirror from the live system's synced databases.

    pacstrap always runs ``pacman -Sy`` which downloads database files from
    mirrors.  When offline, this fails.  By creating a local repo structure
    with the pre-synced database files and adding ``file://`` Server lines
    *before* the remote mirrors in each repo section, pacman's ``-Sy``
    succeeds offline (it finds the database at the file:// URL and stops).

    Returns the path to the local mirror directory, or None if not available.
    """
    # Look for databases in the ISO package cache first (baked in by
    # cache-template-packages.sh), then fall back to the live system's
    # pacman database.
    cache_sync = ISO_PKG_CACHE / "sync"
    host_sync = Path("/var/lib/pacman/sync")
    if cache_sync.exists() and any(cache_sync.glob("*.db")):
        sync_dir = cache_sync
    elif host_sync.exists() and any(host_sync.glob("*.db")):
        sync_dir = host_sync
    else:
        return None

    mirror_dir = Path(tempfile.mkdtemp(prefix="arches-local-mirror-"))

    # For each .db file, create a repo directory structure:
    #   <mirror_dir>/<reponame>/<reponame>.db
    for db_file in sync_dir.glob("*.db"):
        repo_name = db_file.name.replace(".db", "")
        repo_dir = mirror_dir / repo_name
        repo_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_file, repo_dir / db_file.name)

    return mirror_dir


def _make_pacman_conf_with_cache() -> Path:
    """Create a pacman.conf with local cache and local mirror for offline use.

    Returns the path to a temporary config file. If no ISO cache exists,
    returns the original platform pacman.conf unchanged.
    """
    if not ISO_PKG_CACHE.exists() or not any(ISO_PKG_CACHE.glob("*.pkg.tar.*")):
        return ISO_PACMAN_CONF

    conf_text = ISO_PACMAN_CONF.read_text()

    # Add the ISO package cache as a CacheDir
    cache_line = f"CacheDir = {ISO_PKG_CACHE}/\nCacheDir = /var/cache/pacman/pkg/\n"
    conf_text = conf_text.replace(
        "[options]\n",
        f"[options]\n{cache_line}",
        1,
    )

    # Create a local file:// mirror from the live system's databases.
    # Insert a Server = file:// line at the top of each repo section
    # so pacman's -Sy finds the database locally before trying mirrors.
    mirror_dir = _setup_local_repo_mirror()
    if mirror_dir:
        import re

        def _add_local_server(match: re.Match) -> str:
            repo_name = match.group(1)
            # Skip [options] and local repos that already have file:// servers
            if repo_name in ("options", "arches-local"):
                return match.group(0)
            local_repo = mirror_dir / repo_name
            if local_repo.exists():
                return f"[{repo_name}]\nServer = file://{local_repo}\n"
            return match.group(0)

        conf_text = re.sub(
            r"^\[([a-zA-Z0-9_-]+)\]\s*$",
            _add_local_server,
            conf_text,
            flags=re.MULTILINE,
        )

    tmp = Path(tempfile.mktemp(prefix="arches-pacman-", suffix=".conf"))
    tmp.write_text(conf_text)
    return tmp


def _mount_iso_cache_in_target(
    target_pacman_conf: Path,
    log: LogCallback | None = None,
) -> None:
    """Bind-mount the ISO package cache into the target and register it as a CacheDir.

    This allows chroot pacman operations (override packages, hardware
    detection) to find packages locally without downloading.  The mount
    is inside the chroot at ``/mnt/arches-pkg-cache``, and the target's
    pacman.conf gets an extra ``CacheDir`` pointing there.
    """
    global _TARGET_ISO_CACHE_MOUNTED
    if not ISO_PKG_CACHE.exists() or not any(ISO_PKG_CACHE.glob("*.pkg.tar.*")):
        return

    mount_point = MOUNT_ROOT / "mnt" / "arches-pkg-cache"
    mount_point.mkdir(parents=True, exist_ok=True)

    try:
        run(["mount", "--bind", str(ISO_PKG_CACHE), str(mount_point)], log=log)
        _TARGET_ISO_CACHE_MOUNTED = True
    except subprocess.CalledProcessError:
        _log("WARNING: Failed to bind-mount package cache into target.", log)
        return

    # Add the bind-mount as an extra CacheDir in the target's pacman.conf.
    # The chroot sees it at /mnt/arches-pkg-cache.
    conf_text = target_pacman_conf.read_text()
    if "/mnt/arches-pkg-cache" not in conf_text:
        cache_line = "CacheDir = /mnt/arches-pkg-cache/\n"
        conf_text = conf_text.replace(
            "[options]\n",
            f"[options]\n{cache_line}",
            1,
        )
        target_pacman_conf.write_text(conf_text)

    _log("Bind-mounted ISO package cache into target.", log)


def _unmount_iso_cache_from_target(log: LogCallback | None = None) -> None:
    """Unmount the ISO package cache from the target and clean up pacman.conf."""
    global _TARGET_ISO_CACHE_MOUNTED
    if not _TARGET_ISO_CACHE_MOUNTED:
        return

    mount_point = MOUNT_ROOT / "mnt" / "arches-pkg-cache"
    try:
        run(["umount", str(mount_point)], log=log)
    except subprocess.CalledProcessError:
        _log("WARNING: Failed to unmount package cache from target.", log)
    _TARGET_ISO_CACHE_MOUNTED = False

    # Remove the extra CacheDir from pacman.conf so the installed system
    # doesn't reference a non-existent path after reboot.
    target_conf = MOUNT_ROOT / "etc" / "pacman.conf"
    if target_conf.exists():
        conf_text = target_conf.read_text()
        conf_text = conf_text.replace("CacheDir = /mnt/arches-pkg-cache/\n", "")
        target_conf.write_text(conf_text)

    # Clean up mount point
    try:
        mount_point.rmdir()
    except OSError:
        pass


def _query_available_packages(
    pacman_conf: Path,
    log: LogCallback | None = None,
) -> set[str] | None:
    """Query pacman for all available package names.

    Returns a set of package names, or None if the query fails.
    Used to filter out packages that aren't in any configured repo.
    """
    try:
        result = subprocess.run(
            ["pacman", "--config", str(pacman_conf), "-Ssq"],
            capture_output=True,
            text=True,
            check=True,
        )
        return set(result.stdout.strip().splitlines())
    except (subprocess.CalledProcessError, FileNotFoundError):
        _log("WARNING: Could not query available packages.", log)
        return None


def _preseed_pacman_databases(log: LogCallback | None = None) -> None:
    """Copy the live system's pacman databases into the target.

    pacstrap runs ``pacman -Sy`` which fails offline because it can't
    sync remote databases.  By pre-seeding the target with the live
    system's databases (which were synced during the ISO build), pacstrap
    finds them already present and can install from the local cache.
    """
    host_db = Path("/var/lib/pacman/sync")
    target_db = MOUNT_ROOT / "var" / "lib" / "pacman" / "sync"
    if host_db.exists() and any(host_db.glob("*.db")):
        target_db.mkdir(parents=True, exist_ok=True)
        for db_file in host_db.iterdir():
            shutil.copy2(db_file, target_db / db_file.name)
        _log("Pre-seeded pacman databases from live system.", log)


def pacstrap(
    platform: PlatformConfig,
    template: InstallTemplate,
    log: LogCallback | None = None,
) -> None:
    """Install base packages into MOUNT_ROOT via pacstrap."""
    _log("Running pacstrap...", log)

    # All base packages come from the platform config (single source of truth).
    # This includes core system packages, keyrings, bootloader, etc.
    base_packages = list(platform.base_packages)

    # Kernel variants (each gets a bootloader entry)
    for variant in platform.kernel.variants:
        base_packages.append(variant.package)
        base_packages.append(variant.headers)

    # Template-specific packages (pacstrap phase only).
    # Override and firstboot packages are handled in separate pipeline steps.
    all_packages = base_packages + template.install.pacstrap

    _log(f"Total packages: {len(all_packages)}", log)

    pacman_conf = _make_pacman_conf_with_cache()
    if pacman_conf != ISO_PACMAN_CONF:
        _log(f"Using cached packages from {ISO_PKG_CACHE}", log)
        # Log local mirror status
        cache_sync = ISO_PKG_CACHE / "sync"
        if cache_sync.exists():
            dbs = list(cache_sync.glob("*.db"))
            _log(f"  Cached databases: {len(dbs)} in {cache_sync}", log)
        else:
            _log(f"  WARNING: No cached databases at {cache_sync}", log)
        # Log file:// servers in generated config
        for line in pacman_conf.read_text().splitlines():
            if "Server = file://" in line:
                _log(f"  {line.strip()}", log)
    else:
        _log("No package cache found — packages will be downloaded", log)

    # Check if the arches-local repo is empty. If so, filter out packages
    # that aren't available in upstream repos to avoid pacstrap failing on
    # custom packages that haven't been pre-built (e.g. host-install without
    # a prior ISO build). Filtered packages are deferred to post-install.
    deferred: list[str] = []
    local_repo = Path("/opt/arches-repo")
    local_repo_empty = not local_repo.exists() or not any(
        local_repo.glob("*.pkg.tar.*")
    )
    if local_repo_empty and template.install.pacstrap:
        available = _query_available_packages(pacman_conf, log)
        if available is not None:
            filtered = []
            for pkg in all_packages:
                if pkg in available:
                    filtered.append(pkg)
                else:
                    deferred.append(pkg)
                    _log(f"  Deferring unavailable package: {pkg}", log)
            if deferred:
                _log(
                    f"Deferred {len(deferred)} package(s) not in upstream repos.",
                    log,
                )
            all_packages = filtered

    # pacstrap uses `pacman -Sy` which syncs databases + installs.  Our
    # custom pacman.conf has file:// Server entries pointing at the live
    # system's pre-synced databases, so -Sy succeeds offline.  Packages
    # are found in the ISO cache (also configured as a CacheDir).
    #
    # -c: Use the host's package cache rather than downloading
    # -M: Don't copy mirrorlist (we install our own later)
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        try:
            run(
                ["pacstrap", "-c", "-M", "-C", str(pacman_conf), str(MOUNT_ROOT)]
                + all_packages,
                log=log,
            )
            break
        except subprocess.CalledProcessError:
            if attempt < max_attempts:
                _log(
                    f"pacstrap failed (attempt {attempt}/{max_attempts}), retrying...",
                    log,
                )
            else:
                raise

    if deferred:
        _log(
            f"NOTE: {len(deferred)} package(s) were deferred because they are "
            "only available in the arches-local repo, which is empty. "
            "Pre-build with: make iso",
            log,
        )
        _log(f"  Deferred: {', '.join(deferred)}", log)


def generate_fstab(log: LogCallback | None = None) -> None:
    """Generate fstab from current mounts."""
    _log("Generating fstab...", log)

    # Unmount stale mounts that pacstrap hooks may have created
    # (e.g., asahi-fwextract mounts the ESP at /run/.system-efi)
    stale_run = MOUNT_ROOT / "run" / ".system-efi"
    if stale_run.is_mount():
        try:
            run(["umount", str(stale_run)], log=log)
        except subprocess.CalledProcessError:
            pass

    result = run(
        ["genfstab", "-U", str(MOUNT_ROOT)],
        log=log,
        capture_output=True,
    )

    # Filter out stale entries that genfstab may have picked up:
    # - /run/.system-efi (asahi-fwextract hook artifact)
    # - nonexistent swap files
    stale_patterns = ["/run/.system-efi", "/var/swap/swapfile"]
    lines = result.stdout.splitlines(keepends=True)
    filtered: list[str] = []
    skip_block = False
    for line in lines:
        if any(p in line for p in stale_patterns):
            skip_block = True
            _log(f"Filtered stale fstab entry: {line.strip()}", log)
            continue
        if skip_block and line.strip() == "":
            skip_block = False
            continue
        if skip_block:
            continue
        filtered.append(line)

    fstab_path = MOUNT_ROOT / "etc" / "fstab"
    fstab_path.write_text("".join(filtered))


def install_pacman_conf(log: LogCallback | None = None) -> None:
    """Copy the platform-specific pacman.conf and local repo into the target."""
    _log("Installing pacman.conf with platform repos...", log)
    # Prefer the platform-specific pacman.conf baked into the ISO
    platform_conf = ISO_PLATFORM_DIR / "pacman.conf"
    target = MOUNT_ROOT / "etc" / "pacman.conf"
    if platform_conf.exists():
        shutil.copy2(platform_conf, target)
    else:
        # Fallback to the ISO's own pacman.conf
        shutil.copy2(ISO_PACMAN_CONF, target)

    # Make the ISO package cache available inside the chroot so that
    # post-pacstrap operations (override packages, hardware detection)
    # can install without downloading.  We bind-mount the cache into
    # the target and add it as an extra CacheDir in pacman.conf.
    _mount_iso_cache_in_target(target, log)

    # Copy the local AUR repo into the target so packages are available
    # inside the chroot (for ansible tasks that install from arches-local).
    iso_repo = Path("/opt/arches-repo")
    target_repo = MOUNT_ROOT / "opt" / "arches-repo"
    if iso_repo.exists() and not target_repo.exists():
        _log("Copying local AUR repo into target...", log)
        shutil.copytree(iso_repo, target_repo)


def sync_chroot_databases(log: LogCallback | None = None) -> None:
    """Best-effort sync of pacman databases inside the chroot.

    Tries to refresh package databases so chroot operations (override
    packages, hardware detection, bootloader hooks) see the latest
    versions.  If network is unavailable this silently continues —
    the databases from pacstrap are still valid and all cached packages
    are usable.
    """
    _log("Syncing pacman databases (best-effort)...", log)
    try:
        chroot_run(["pacman", "-Sy", "--noconfirm"], log=log)
    except subprocess.CalledProcessError:
        _log(
            "Database sync failed (no network?), continuing with cached databases.", log
        )


def install_override_packages(
    template: InstallTemplate,
    log: LogCallback | None = None,
) -> None:
    """Install packages that conflict with stock packages (need --overwrite).

    Must run after install_pacman_conf so the arches-local repo is available.
    Reads from template.install.override.
    """
    if not template.install.override:
        return

    _log(f"Installing override packages: {', '.join(template.install.override)}", log)
    # Databases are already synced from pacstrap.  Use -S (not -Sy) so
    # this works offline.  The local arches-repo is already available.
    chroot_run(
        ["pacman", "-S", "--noconfirm", "--overwrite", "*"] + template.install.override,
        log=log,
    )


def configure_locale(
    locale: str = "en_US.UTF-8",
    log: LogCallback | None = None,
) -> None:
    """Set locale in the target system."""
    _log(f"Setting locale to {locale}...", log)
    locale_gen = MOUNT_ROOT / "etc" / "locale.gen"
    content = locale_gen.read_text()
    content = content.replace(f"#{locale}", locale)
    locale_gen.write_text(content)
    chroot_run(["locale-gen"], log=log)

    locale_conf = MOUNT_ROOT / "etc" / "locale.conf"
    locale_conf.write_text(f"LANG={locale}\n")

    vconsole = MOUNT_ROOT / "etc" / "vconsole.conf"
    vconsole.write_text("KEYMAP=us\nFONT=ter-116n\n")


def configure_timezone(
    timezone: str = "America/Denver",
    log: LogCallback | None = None,
) -> None:
    """Set timezone in the target system."""
    _log(f"Setting timezone to {timezone}...", log)
    chroot_run(
        [
            "ln",
            "-sf",
            f"/usr/share/zoneinfo/{timezone}",
            "/etc/localtime",
        ],
        log=log,
    )
    chroot_run(["hwclock", "--systohc"], log=log)


def configure_hostname(
    hostname: str,
    log: LogCallback | None = None,
) -> None:
    """Set hostname."""
    _log(f"Setting hostname to {hostname}...", log)
    hostname_file = MOUNT_ROOT / "etc" / "hostname"
    hostname_file.write_text(f"{hostname}\n")

    hosts_file = MOUNT_ROOT / "etc" / "hosts"
    hosts_file.write_text(
        f"127.0.0.1  localhost\n::1        localhost\n127.0.1.1  {hostname}\n"
    )

    # Override os-release so that limine-entry-tool and other tools
    # identify the OS as "Arches Linux" instead of "Arch Linux".
    os_release = MOUNT_ROOT / "etc" / "os-release"
    os_release.write_text(
        'NAME="Arches Linux"\n'
        'PRETTY_NAME="Arches Linux"\n'
        "ID=arches\n"
        "ID_LIKE=arch\n"
        "BUILD_ID=rolling\n"
    )
    _log("Set os-release to Arches Linux.", log)


def create_user(
    username: str,
    password: str,
    log: LogCallback | None = None,
) -> None:
    """Create a user with sudo privileges.

    Idempotent: if the user already exists (e.g., from a prior failed
    install on the same subvolume), update their groups and shell instead
    of failing.
    """
    _log(f"Creating user {username}...", log)
    try:
        chroot_run(
            [
                "useradd",
                "-m",
                "-G",
                "wheel",
                "-s",
                "/bin/zsh",
                username,
            ],
            log=log,
        )
    except subprocess.CalledProcessError:
        # User already exists — update groups and shell
        _log(f"User {username} already exists, updating...", log)
        chroot_run(
            ["usermod", "-a", "-G", "wheel", "-s", "/bin/zsh", username],
            log=log,
        )

    # Set password via chpasswd
    _log("Setting password...", log)
    subprocess.run(
        ["arch-chroot", str(MOUNT_ROOT), "chpasswd"],
        input=f"{username}:{password}\n",
        capture_output=True,
        text=True,
        check=True,
    )

    # Enable wheel group in sudoers
    sudoers = MOUNT_ROOT / "etc" / "sudoers.d" / "wheel"
    sudoers.parent.mkdir(parents=True, exist_ok=True)
    sudoers.write_text("%wheel ALL=(ALL:ALL) NOPASSWD: ALL\n")
    sudoers.chmod(0o440)


def deploy_ssh_key(
    username: str,
    log: LogCallback | None = None,
) -> None:
    """Deploy build host's SSH public key to the created user's authorized_keys.

    The key is embedded into the ISO at build time as /opt/arches/build-host.pub.
    If the file doesn't exist (no key was available at build time), this is a
    silent no-op.
    """
    if not ISO_BUILD_HOST_PUBKEY.exists():
        return

    _log("Deploying build host SSH key...", log)
    pubkey = ISO_BUILD_HOST_PUBKEY.read_text().strip()

    ssh_dir = MOUNT_ROOT / "home" / username / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    auth_keys = ssh_dir / "authorized_keys"
    auth_keys.write_text(pubkey + "\n")

    # Set ownership to the user (look up UID/GID from the target's /etc/passwd)
    passwd = (MOUNT_ROOT / "etc" / "passwd").read_text()
    for line in passwd.splitlines():
        fields = line.split(":")
        if fields[0] == username:
            uid, gid = int(fields[2]), int(fields[3])
            break
    else:
        _log("WARNING: Could not find user in /etc/passwd, skipping SSH key.", log)
        return

    os.chown(ssh_dir, uid, gid)
    ssh_dir.chmod(0o700)
    os.chown(auth_keys, uid, gid)
    auth_keys.chmod(0o600)
    _log(f"Installed authorized_keys for {username}.", log)


def enable_services(
    services: list[str],
    log: LogCallback | None = None,
) -> None:
    """Enable systemd services in the target."""
    for service in services:
        _log(f"Enabling {service}...", log)
        chroot_run(["systemctl", "enable", service], log=log)


def stage_ansible(
    template: InstallTemplate,
    log: LogCallback | None = None,
) -> None:
    """Copy ansible playbooks into the target for firstboot use."""
    if not template.ansible.firstboot_roles:
        return

    target_ansible = MOUNT_ROOT / "opt" / "arches" / "ansible"
    if ISO_ANSIBLE_DIR.exists():
        _log("Copying ansible playbooks into target...", log)
        if target_ansible.exists():
            shutil.rmtree(target_ansible)
        shutil.copytree(ISO_ANSIBLE_DIR, target_ansible)


def run_mkinitcpio(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Regenerate initramfs in the target."""
    _log("Regenerating initramfs...", log)
    chroot_run(["mkinitcpio", "-P"], log=log)


def run_hardware_detection(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Run platform-specific hardware detection (e.g., GPU drivers)."""
    hw = platform.hardware_detection
    if not hw.enabled:
        _log("Hardware detection not enabled for this platform, skipping.", log)
        return

    _log(f"Running hardware detection ({hw.tool})...", log)
    try:
        chroot_run([hw.tool] + hw.args, log=log)
    except subprocess.CalledProcessError:
        if hw.optional:
            _log(
                f"{hw.tool} failed (may be expected in a VM), continuing...",
                log,
            )
        else:
            raise


def _pre_pacstrap_setup(log: LogCallback | None = None) -> None:
    """Create config files that pacstrap's post-install hooks need."""
    etc = MOUNT_ROOT / "etc"
    etc.mkdir(parents=True, exist_ok=True)

    # Pre-seed pacman databases so pacstrap works offline
    _preseed_pacman_databases(log)

    # mkinitcpio's keymap/sd-vconsole hooks need vconsole.conf
    (etc / "vconsole.conf").write_text("KEYMAP=us\nFONT=ter-116n\n")
    _log("Pre-created /etc/vconsole.conf for pacstrap hooks.", log)

    # Pre-create limine config files so that limine-mkinitcpio-hook's
    # post-install scripts work during pacstrap (before our bootloader
    # phase writes the full config).  The hook needs:
    #   - /etc/default/limine with ESP_PATH
    #   - /etc/kernel/cmdline (even if placeholder)
    default_dir = etc / "default"
    default_dir.mkdir(parents=True, exist_ok=True)
    (default_dir / "limine").write_text('ESP_PATH="/boot"\n')
    kernel_dir = etc / "kernel"
    kernel_dir.mkdir(parents=True, exist_ok=True)
    (kernel_dir / "cmdline").write_text("root=/dev/vda2 rw\n")
    _log(
        "Pre-created /etc/default/limine and /etc/kernel/cmdline for pacstrap hooks.",
        log,
    )

    # Provide a mkinitcpio.conf with reliable hooks.
    # The default includes 'autodetect' which can fail in chroot/VM.
    # We use a broad set that works everywhere; autodetect can be
    # re-enabled by the user post-install for a smaller initramfs.
    (etc / "mkinitcpio.conf").write_text(
        "# Arches — generated by installer\n"
        "MODULES=()\n"
        "BINARIES=()\n"
        "FILES=()\n"
        "HOOKS=(base udev microcode modconf kms keyboard keymap "
        "block filesystems fsck)\n"
    )
    _log("Pre-created /etc/mkinitcpio.conf (no autodetect).", log)


def copy_apple_firmware(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Copy Apple Silicon firmware from the host into the target system.

    On Apple Silicon, the firmware is extracted from the macOS APFS partition
    by asahi-fwextract and stored at /lib/firmware/vendor/. When installing
    from a running Asahi Linux host, we copy this firmware into the target
    so the new system has working WiFi, Bluetooth, GPU, etc.

    This is a no-op on non-Apple platforms or if the firmware directory
    doesn't exist on the host.
    """
    if platform.name != "aarch64-apple":
        return

    # Check common firmware locations on the host.
    # In the container, host firmware is bind-mounted at /host-firmware.
    # On a native host, it's at /lib/firmware/vendor or /usr/lib/firmware/vendor.
    host_fw_paths = [
        Path("/host-firmware"),
        Path("/lib/firmware/vendor"),
        Path("/usr/lib/firmware/vendor"),
    ]
    host_fw = None
    for p in host_fw_paths:
        if p.exists() and any(p.iterdir()):
            host_fw = p
            break

    if host_fw is None:
        _log(
            "WARNING: No Apple firmware found on host. "
            "WiFi/BT/GPU may not work in the installed system. "
            "Run asahi-fwextract after booting into the new system.",
            log,
        )
        return

    target_fw = MOUNT_ROOT / "lib" / "firmware" / "vendor"
    if target_fw.exists() and any(target_fw.iterdir()):
        _log("Firmware already present in target, skipping.", log)
        return

    _log(f"Copying Apple firmware from {host_fw}...", log)
    target_fw.parent.mkdir(parents=True, exist_ok=True)
    # Use dirs_exist_ok=True because asahi-fwextract creates the empty
    # vendor/ directory during pacstrap.
    shutil.copytree(host_fw, target_fw, dirs_exist_ok=True)
    _log("Apple firmware copied.", log)


def preseed_network_deps(
    username: str,
    log: LogCallback | None = None,
) -> None:
    """Pre-seed resources that would require network at first boot.

    Runs during install (inside the container, where we have network) so
    that firstboot ansible can run fully offline. This includes:
      - oh-my-zsh: git clone for the user and root
      - rustup: initialize the stable toolchain
    """
    _log("Pre-seeding network-dependent resources...", log)

    # oh-my-zsh for user
    omz_user = MOUNT_ROOT / "home" / username / ".oh-my-zsh"
    if not omz_user.exists():
        _log(f"Cloning oh-my-zsh for {username}...", log)
        try:
            chroot_run(
                [
                    "su",
                    "-",
                    username,
                    "-c",
                    "git clone --depth 1 https://github.com/ohmyzsh/ohmyzsh.git"
                    " ~/.oh-my-zsh",
                ],
                log=log,
            )
        except subprocess.CalledProcessError:
            _log("WARNING: oh-my-zsh clone failed for user (no network?)", log)

    # oh-my-zsh for root
    omz_root = MOUNT_ROOT / "root" / ".oh-my-zsh"
    if not omz_root.exists():
        _log("Cloning oh-my-zsh for root...", log)
        try:
            chroot_run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "https://github.com/ohmyzsh/ohmyzsh.git",
                    "/root/.oh-my-zsh",
                ],
                log=log,
            )
        except subprocess.CalledProcessError:
            _log("WARNING: oh-my-zsh clone failed for root (no network?)", log)

    # rustup stable toolchain
    _log(f"Initializing rustup stable toolchain for {username}...", log)
    try:
        chroot_run(
            ["su", "-", username, "-c", "rustup default stable"],
            log=log,
        )
    except subprocess.CalledProcessError:
        _log("WARNING: rustup toolchain init failed (no network?)", log)

    _log("Pre-seeding complete.", log)


def configure_apple_input(
    platform: PlatformConfig,
    log: LogCallback | None = None,
) -> None:
    """Configure Apple Silicon keyboard and touchpad for a usable layout.

    Apple keyboards have a non-standard layout (Fn where Ctrl is, Cmd where
    Alt is, etc.). This sets hid_apple module options to swap keys into a
    standard PC layout:
      - swap_fn_leftctrl: Fn ↔ Left Ctrl
      - swap_opt_cmd: Option ↔ Command (so Cmd acts as Alt)
      - fnmode=2: Function keys are F1-F12 by default (media keys with Fn)

    Also ensures libinput quirks for the Apple touchpad are active.
    """
    if platform.name != "aarch64-apple":
        return

    _log("Configuring Apple keyboard and touchpad...", log)

    # hid_apple module options
    modprobe_dir = MOUNT_ROOT / "etc" / "modprobe.d"
    modprobe_dir.mkdir(parents=True, exist_ok=True)
    (modprobe_dir / "hid_apple.conf").write_text(
        "# Arches — Apple keyboard layout remapping\n"
        "# swap_fn_leftctrl: Fn ↔ Left Ctrl\n"
        "# swap_opt_cmd: Option ↔ Command (Cmd acts as Alt)\n"
        "# fnmode=2: F1-F12 by default, media keys with Fn held\n"
        "options hid_apple swap_fn_leftctrl=1\n"
        "options hid_apple swap_opt_cmd=1\n"
        "options hid_apple fnmode=2\n"
    )

    _log("Apple input configured (hid_apple key remapping).", log)


def install_system(
    platform: PlatformConfig,
    template: InstallTemplate,
    hostname: str,
    username: str,
    password: str,
    log: LogCallback | None = None,
) -> None:
    """Full install pipeline after disk is prepared and mounted."""
    _pre_pacstrap_setup(log)
    pacstrap(platform, template, log)
    generate_fstab(log)
    install_pacman_conf(log)
    sync_chroot_databases(log)
    install_override_packages(template, log)
    configure_locale(template.system.locale, log)
    configure_timezone(template.system.timezone, log)
    configure_hostname(hostname, log)
    create_user(username, password, log)
    deploy_ssh_key(username, log)
    copy_apple_firmware(platform, log)
    configure_apple_input(platform, log)
    preseed_network_deps(username, log)
    run_hardware_detection(platform, log)
    _unmount_iso_cache_from_target(log)
    # NOTE: mkinitcpio is NOT run here — it's handled by
    # limine-mkinitcpio in the bootloader phase (Phase 3),
    # which generates both the initramfs and limine.conf entries.
    enable_services(template.services, log)
    stage_ansible(template, log)
