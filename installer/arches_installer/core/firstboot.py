"""First-boot service injection for post-install Ansible."""

from __future__ import annotations


from arches_installer.core.disk import MOUNT_ROOT
from arches_installer.core.run import LogCallback, _log
from arches_installer.core.template import InstallTemplate


FIRSTBOOT_SERVICE = """\
[Unit]
Description=Arches first-boot setup
After=network-online.target
Wants=network-online.target
Before=display-manager.service sddm.service
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
) -> str:
    """Generate the first-boot shell script."""
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        'echo ""',
        'echo "╔═══════════════════════════════════════════╗"',
        'echo "║     Arches — First-Boot Configuration     ║"',
        'echo "╚═══════════════════════════════════════════╝"',
        'echo ""',
        'echo "Configuring your system. SDDM will start when complete."',
        'echo ""',
        "",
    ]

    # Run first-boot ansible roles if configured
    if template.ansible.firstboot_roles:
        tags = ",".join(template.ansible.firstboot_roles)
        lines.extend(
            [
                "# Run first-boot Ansible roles",
                f'echo "Running Ansible (roles: {tags})..."',
                "ansible-playbook /opt/arches/ansible/playbook.yml \\",
                "    --connection=local \\",
                "    -i localhost, \\",
                f"    -e install_user={username} \\",
                f"    -e ansible_user={username} \\",
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
    log: LogCallback | None = None,
) -> None:
    """Write the first-boot systemd service and script into the target."""
    if not template.ansible.firstboot_roles:
        _log("No first-boot roles configured, skipping.", log)
        return

    _log("Injecting first-boot service...", log)

    # Write the systemd service unit
    service_dir = MOUNT_ROOT / "etc" / "systemd" / "system"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / "arches-firstboot.service"
    service_file.write_text(FIRSTBOOT_SERVICE)

    # Enable the service (create symlink in graphical.target)
    wants_dir = service_dir / "graphical.target.wants"
    wants_dir.mkdir(parents=True, exist_ok=True)
    symlink = wants_dir / "arches-firstboot.service"
    if not symlink.exists():
        symlink.symlink_to(service_file)

    # Write the first-boot script
    arches_dir = MOUNT_ROOT / "opt" / "arches"
    arches_dir.mkdir(parents=True, exist_ok=True)

    script = arches_dir / "firstboot.sh"
    script.write_text(generate_firstboot_script(template, username))
    script.chmod(0o755)

    # Create the sentinel file
    sentinel = arches_dir / "firstboot-pending"
    sentinel.touch()

    _log("First-boot service installed.", log)
