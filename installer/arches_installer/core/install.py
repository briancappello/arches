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

# Ansible playbooks shipped on the ISO
ISO_ANSIBLE_DIR = Path("/opt/arches/ansible")

# Build host SSH public key (optional, embedded at ISO build time)
ISO_BUILD_HOST_PUBKEY = Path("/opt/arches/build-host.pub")


def _make_pacman_conf_with_cache() -> Path:
    """Create a pacman.conf that includes the ISO package cache as a CacheDir.

    Returns the path to a temporary config file. If no ISO cache exists,
    returns the original platform pacman.conf unchanged.
    """
    if not ISO_PKG_CACHE.exists() or not any(ISO_PKG_CACHE.glob("*.pkg.tar.*")):
        return ISO_PACMAN_CONF

    conf_text = ISO_PACMAN_CONF.read_text()
    # Insert our cache dir before the default, so pacman checks it first
    cache_line = f"CacheDir = {ISO_PKG_CACHE}/\nCacheDir = /var/cache/pacman/pkg/\n"
    conf_text = conf_text.replace(
        "[options]\n",
        f"[options]\n{cache_line}",
        1,
    )
    tmp = Path(tempfile.mktemp(prefix="arches-pacman-", suffix=".conf"))
    tmp.write_text(conf_text)
    return tmp


def pacstrap(
    platform: PlatformConfig,
    template: InstallTemplate,
    log: LogCallback | None = None,
) -> None:
    """Install base packages into MOUNT_ROOT via pacstrap."""
    _log("Running pacstrap...", log)

    # Platform-agnostic base
    base_packages = [
        "base",
        platform.kernel.package,
        platform.kernel.headers,
        "linux-firmware",
        "mkinitcpio",
        "sudo",
        "ansible",
    ]

    # Platform-specific base packages (keyrings, mirrorlists, settings)
    base_packages.extend(platform.base_packages)

    # Template-specific packages (pacstrap phase only).
    # Override and firstboot packages are handled in separate pipeline steps.
    all_packages = base_packages + template.install.pacstrap

    pacman_conf = _make_pacman_conf_with_cache()
    if pacman_conf != ISO_PACMAN_CONF:
        _log(f"Using cached packages from {ISO_PKG_CACHE}", log)

    run(
        ["pacstrap", "-C", str(pacman_conf), str(MOUNT_ROOT)] + all_packages,
        log=log,
    )


def generate_fstab(log: LogCallback | None = None) -> None:
    """Generate fstab from current mounts."""
    _log("Generating fstab...", log)
    result = run(
        ["genfstab", "-U", str(MOUNT_ROOT)],
        log=log,
        capture_output=True,
    )
    fstab_path = MOUNT_ROOT / "etc" / "fstab"
    fstab_path.write_text(result.stdout)


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

    # Copy the local AUR repo into the target so packages are available
    # inside the chroot (for ansible tasks that install from arches-local).
    iso_repo = Path("/opt/arches-repo")
    target_repo = MOUNT_ROOT / "opt" / "arches-repo"
    if iso_repo.exists() and not target_repo.exists():
        _log("Copying local AUR repo into target...", log)
        shutil.copytree(iso_repo, target_repo)


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
    chroot_run(
        ["pacman", "-Sy", "--noconfirm", "--overwrite", "*"]
        + template.install.override,
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
    vconsole.write_text("KEYMAP=us\n")


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
    """Create a user with sudo privileges."""
    _log(f"Creating user {username}...", log)
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

    # mkinitcpio's keymap/sd-vconsole hooks need vconsole.conf
    (etc / "vconsole.conf").write_text("KEYMAP=us\n")
    _log("Pre-created /etc/vconsole.conf for pacstrap hooks.", log)

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
    install_override_packages(template, log)
    configure_locale(template.system.locale, log)
    configure_timezone(template.system.timezone, log)
    configure_hostname(hostname, log)
    create_user(username, password, log)
    deploy_ssh_key(username, log)
    run_hardware_detection(platform, log)
    # NOTE: mkinitcpio is NOT run here — it's handled by
    # limine-mkinitcpio in the bootloader phase (Phase 3),
    # which generates both the initramfs and limine.conf entries.
    enable_services(template.services, log)
    stage_ansible(template, log)
