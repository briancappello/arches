"""Core install logic — pacstrap, genfstab, chroot configuration."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

from arches_installer.core.disk import MOUNT_ROOT
from arches_installer.core.template import InstallTemplate

# Path to the pacman.conf that includes CachyOS v3 repos
ISO_PACMAN_CONF = Path("/etc/pacman.conf")

# Ansible playbooks shipped on the ISO
ISO_ANSIBLE_DIR = Path("/opt/arches/ansible")

LogCallback = Callable[[str], None]


def _log(msg: str, callback: LogCallback | None = None) -> None:
    if callback:
        callback(msg)


def run(
    cmd: list[str],
    log: LogCallback | None = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a command, logging output."""
    _log(f"$ {' '.join(cmd)}", log)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        **kwargs,
    )
    if result.stdout.strip():
        _log(result.stdout.strip(), log)
    if result.returncode != 0:
        _log(f"ERROR: {result.stderr.strip()}", log)
        result.check_returncode()
    return result


def chroot_run(
    cmd: list[str],
    log: LogCallback | None = None,
) -> subprocess.CompletedProcess:
    """Run a command inside the target chroot."""
    return run(["arch-chroot", str(MOUNT_ROOT)] + cmd, log=log)


def pacstrap(template: InstallTemplate, log: LogCallback | None = None) -> None:
    """Install base packages into MOUNT_ROOT via pacstrap."""
    _log("Running pacstrap...", log)
    base_packages = [
        "base",
        template.system.kernel,
        f"{template.system.kernel}-headers",
        "linux-firmware",
        "mkinitcpio",
        "sudo",
        "cachyos-keyring",
        "cachyos-mirrorlist",
        "cachyos-v3-mirrorlist",
    ]
    all_packages = base_packages + template.system.packages
    run(
        ["pacstrap", "-C", str(ISO_PACMAN_CONF), str(MOUNT_ROOT)] + all_packages,
        log=log,
    )


def generate_fstab(log: LogCallback | None = None) -> None:
    """Generate fstab from current mounts."""
    _log("Generating fstab...", log)
    result = run(["genfstab", "-U", str(MOUNT_ROOT)], log=log)
    fstab_path = MOUNT_ROOT / "etc" / "fstab"
    fstab_path.write_text(result.stdout)


def install_pacman_conf(log: LogCallback | None = None) -> None:
    """Copy the CachyOS v3 pacman.conf into the target system."""
    _log("Installing pacman.conf with CachyOS v3 repos...", log)
    target = MOUNT_ROOT / "etc" / "pacman.conf"
    shutil.copy2(ISO_PACMAN_CONF, target)


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


def configure_timezone(
    timezone: str = "America/New_York",
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
    chroot_run(
        ["chpasswd"],
        log=log,
    )
    # Use subprocess directly for piping password
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
    sudoers.write_text("%wheel ALL=(ALL:ALL) ALL\n")
    sudoers.chmod(0o440)


def enable_services(
    services: list[str],
    log: LogCallback | None = None,
) -> None:
    """Enable systemd services in the target."""
    for service in services:
        _log(f"Enabling {service}...", log)
        chroot_run(["systemctl", "enable", service], log=log)


def run_chroot_ansible(
    template: InstallTemplate,
    log: LogCallback | None = None,
) -> None:
    """Run ansible playbook inside chroot for base system config."""
    if not template.ansible.chroot_roles:
        _log("No chroot ansible roles configured, skipping.", log)
        return

    # Copy ansible dir into chroot
    target_ansible = MOUNT_ROOT / "opt" / "arches" / "ansible"
    if ISO_ANSIBLE_DIR.exists():
        _log("Copying ansible playbooks into target...", log)
        if target_ansible.exists():
            shutil.rmtree(target_ansible)
        shutil.copytree(ISO_ANSIBLE_DIR, target_ansible)

        # Build role tags from template
        tags = ",".join(template.ansible.chroot_roles)
        _log(f"Running ansible (roles: {tags})...", log)
        chroot_run(
            [
                "ansible-playbook",
                "/opt/arches/ansible/playbook.yml",
                "--connection=local",
                "-i",
                "localhost,",
                "--tags",
                tags,
            ],
            log=log,
        )


def run_mkinitcpio(
    kernel: str,
    log: LogCallback | None = None,
) -> None:
    """Regenerate initramfs in the target."""
    _log("Regenerating initramfs...", log)
    chroot_run(["mkinitcpio", "-P"], log=log)


def run_chwd(log: LogCallback | None = None) -> None:
    """Run CachyOS hardware detection for GPU drivers."""
    _log("Running hardware detection (chwd)...", log)
    try:
        chroot_run(["chwd", "-a"], log=log)
    except subprocess.CalledProcessError:
        _log("chwd failed (may be expected in a VM), continuing...", log)


def install_system(
    template: InstallTemplate,
    hostname: str,
    username: str,
    password: str,
    log: LogCallback | None = None,
) -> None:
    """Full install pipeline after disk is prepared and mounted."""
    pacstrap(template, log)
    generate_fstab(log)
    install_pacman_conf(log)
    configure_locale(template.system.locale, log)
    configure_timezone(template.system.timezone, log)
    configure_hostname(hostname, log)
    create_user(username, password, log)
    run_chwd(log)
    run_mkinitcpio(template.system.kernel, log)
    enable_services(template.services, log)
    run_chroot_ansible(template, log)
