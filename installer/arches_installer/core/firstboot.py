"""First-boot service injection for post-install Ansible."""

from __future__ import annotations

from typing import TYPE_CHECKING

from arches_installer.core.disk import MOUNT_ROOT
from arches_installer.core.platform import PlatformConfig
from arches_installer.core.run import LogCallback, _log
from arches_installer.core.template import InstallTemplate

if TYPE_CHECKING:
    from arches_installer.core.hardware import HardwareConfig


FIRSTBOOT_SERVICE = """\
[Unit]
Description=Arches first-boot setup
After=network-online.target
Wants=network-online.target
Before=display-manager.service
ConditionPathExists=/opt/arches/firstboot-pending

[Service]
Type=oneshot
ExecStart=/opt/arches/firstboot.sh
ExecStartPost=/usr/bin/rm -f /opt/arches/firstboot-pending
RemainAfterExit=yes
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=graphical.target
"""


def generate_firstboot_script(
    template: InstallTemplate,
    username: str,
    platform: PlatformConfig | None = None,
    extra_roles: list[str] | None = None,
) -> str:
    """Generate the first-boot shell script.

    Parameters
    ----------
    platform:
        Platform configuration.  When provided, platform-level variables
        (architecture, CachyOS tier, etc.) are passed to Ansible so roles
        can adapt to the target hardware.
    extra_roles:
        Additional Ansible roles to run (e.g. from a machine profile).
        Merged with the template's roles, deduplicated.
    """
    lines = [
        "#!/usr/bin/env bash",
        "# Don't use set -e — if ansible fails, we still want to remove the",
        "# firstboot-pending marker so the service doesn't retry every boot.",
        "set -uo pipefail",
        "",
        'echo ""',
        'echo "╔═══════════════════════════════════════════╗"',
        'echo "║     Arches — First-Boot Configuration     ║"',
        'echo "╚═══════════════════════════════════════════╝"',
        'echo ""',
        'echo "Configuring your system. The display manager will start when complete."',
        'echo ""',
        "",
    ]

    # Merge template roles with extra roles (from hardware profile)
    all_roles = list(template.ansible.firstboot_roles)
    for role in extra_roles or []:
        if role not in all_roles:
            all_roles.append(role)

    # Run first-boot ansible roles if configured
    if all_roles:
        tags = ",".join(all_roles)

        # Build platform-level extra vars for Ansible roles
        platform_vars: list[str] = []
        if platform:
            arches = ",".join(platform.pacman_architectures)
            platform_vars.extend(
                [
                    f"    -e platform_arch={platform.arch} \\",
                    f"    -e cachyos_optimization_tier={platform.cachyos_optimization_tier} \\",
                    f"    -e pacman_architectures={arches} \\",
                ]
            )
            if platform.cachyos_mirrorlist_name:
                platform_vars.append(
                    f"    -e cachyos_tier_mirrorlist={platform.cachyos_mirrorlist_name} \\",
                )

        lines.extend(
            [
                "# Run first-boot Ansible roles",
                f'echo "Running Ansible (roles: {tags})..."',
                "ansible-playbook /opt/arches/ansible/playbook.yml \\",
                "    --connection=local \\",
                "    -i localhost, \\",
                f"    -e install_user={username} \\",
                f"    -e ansible_user={username} \\",
                *platform_vars,
                f"    --tags {tags} \\",
                "    -v 2>&1 | tee -a /var/log/arches-firstboot.log",
                "",
            ]
        )

    lines.append('echo "=== First-boot setup complete ==="')

    return "\n".join(lines) + "\n"


def inject_firstboot_service(
    template: InstallTemplate,
    username: str,
    platform: PlatformConfig | None = None,
    hardware: HardwareConfig | None = None,
    log: LogCallback | None = None,
) -> None:
    """Write the first-boot systemd service and script into the target."""
    # Merge hardware ansible roles with template roles
    all_roles = list(template.ansible.firstboot_roles)
    if hardware:
        for role in hardware.all_firstboot_roles:
            if role not in all_roles:
                all_roles.append(role)

    if not all_roles:
        _log("No first-boot roles configured, skipping.", log)
        return

    _log("Injecting first-boot service...", log)

    # Write the systemd service unit
    service_dir = MOUNT_ROOT / "etc" / "systemd" / "system"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / "arches-firstboot.service"
    service_file.write_text(FIRSTBOOT_SERVICE)

    # Enable the service (create symlink in graphical.target)
    # Use the absolute path as it will appear on the booted system,
    # NOT the path under MOUNT_ROOT.
    wants_dir = service_dir / "graphical.target.wants"
    wants_dir.mkdir(parents=True, exist_ok=True)
    symlink = wants_dir / "arches-firstboot.service"
    if not symlink.exists():
        symlink.symlink_to("/etc/systemd/system/arches-firstboot.service")

    # Write the first-boot script
    arches_dir = MOUNT_ROOT / "opt" / "arches"
    arches_dir.mkdir(parents=True, exist_ok=True)

    script = arches_dir / "firstboot.sh"
    extra_roles = hardware.all_firstboot_roles if hardware else None
    script.write_text(
        generate_firstboot_script(
            template,
            username,
            platform=platform,
            extra_roles=extra_roles,
        )
    )
    script.chmod(0o755)

    # Create the sentinel file
    sentinel = arches_dir / "firstboot-pending"
    sentinel.touch()

    _log("First-boot service installed.", log)
