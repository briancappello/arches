"""First-boot service injection for post-install Ansible."""

from __future__ import annotations

from typing import TYPE_CHECKING

from arches_installer.core.disk import MOUNT_ROOT
from arches_installer.core.platform import PlatformConfig
from arches_installer.core.run import LogCallback, _log
from arches_installer.core.template import InstallTemplate

if TYPE_CHECKING:
    from arches_installer.core.hardware import HardwareConfig


def _firstboot_service_unit(graphical: bool = True) -> str:
    """Generate the systemd unit for the first-boot service.

    Graphical installs gate on ``graphical.target`` so the display
    manager waits for Ansible to finish.  Headless installs use
    ``multi-user.target`` instead — ``graphical.target`` is never
    reached without a display manager.

    The sentinel removal happens inside the script (not via
    ExecStartPost) so the script can decide success vs failure and
    write a /opt/arches/firstboot-failed marker on failure. The
    marker makes failures visible at login (MOTD reads it) and over
    SSH, instead of being silently lost the way an unconditional
    ExecStartPost rm would lose them.
    """
    target = "graphical.target" if graphical else "multi-user.target"
    before = "Before=display-manager.service\n" if graphical else ""
    return (
        "[Unit]\n"
        "Description=Arches first-boot setup\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        f"{before}"
        "ConditionPathExists=/opt/arches/firstboot-pending\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/opt/arches/firstboot.sh\n"
        "RemainAfterExit=yes\n"
        "StandardOutput=journal+console\n"
        "StandardError=journal+console\n"
        "\n"
        "[Install]\n"
        f"WantedBy={target}\n"
    )


# Keep a constant for backward compat (used by tests that import it)
FIRSTBOOT_SERVICE = _firstboot_service_unit(graphical=True)


def generate_firstboot_script(
    template: InstallTemplate,
    username: str,
    platform: PlatformConfig | None = None,
    extra_roles: list[str] | None = None,
    extra_vars: dict[str, str] | None = None,
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
    extra_vars:
        Additional Ansible extra variables to pass via ``-e key=value``.
        Typically from ``[ansible_vars]`` in an auto-install config or
        from a machine profile.
    """
    lines = [
        "#!/usr/bin/env bash",
        "# Track ansible's exit code separately. Don't use set -e — we want",
        "# to ALWAYS remove the pending sentinel (so we don't loop) but write",
        "# a 'failed' marker if ansible failed, so the operator can see it.",
        "set -uo pipefail",
        "",
        "_ANSIBLE_RC=0",
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

        # Build user-supplied extra vars (from [ansible_vars] in auto-install
        # config, or from machine profile ansible_vars).
        user_vars: list[str] = []
        if extra_vars:
            for key, val in extra_vars.items():
                user_vars.append(f"    -e {key}={val} \\")

        lines.extend(
            [
                "# Run first-boot Ansible roles. Capture exit code so we can",
                "# distinguish success from failure and write the right marker.",
                f'echo "Running Ansible (roles: {tags})..."',
                "ansible-playbook /opt/arches/ansible/playbook.yml \\",
                "    --connection=local \\",
                "    -i localhost, \\",
                f"    -e install_user={username} \\",
                f"    -e ansible_user={username} \\",
                *platform_vars,
                *user_vars,
                f"    --tags {tags} \\",
                "    -v 2>&1 | tee -a /var/log/arches-firstboot.log",
                "# bash sets PIPESTATUS for piped commands; the first entry",
                "# is ansible-playbook's exit code (the tee always succeeds).",
                "_ANSIBLE_RC=${PIPESTATUS[0]}",
                "",
            ]
        )

    # ── Mark success/failure for downstream tooling ─────
    lines.extend(
        [
            "",
            'if [ "$_ANSIBLE_RC" -eq 0 ]; then',
            '    echo "=== First-boot setup complete ==="',
            "    # Success: clear any prior failure marker, drop the pending",
            "    # sentinel so this service doesn't re-fire next boot.",
            "    rm -f /opt/arches/firstboot-failed",
            "else",
            '    echo "=== First-boot setup FAILED (ansible rc=$_ANSIBLE_RC) ==="',
            "    # Failure: leave a persistent marker so MOTD and operator",
            "    # tools can surface it. Still remove the pending sentinel —",
            "    # retrying on every boot would loop forever; the operator",
            "    # should fix the issue and run /opt/arches/firstboot.sh by",
            "    # hand (or `systemctl start arches-firstboot.service` after",
            "    # re-creating the pending sentinel).",
            "    {",
            '        echo "First-boot Ansible failed at $(date -u +%FT%TZ)"',
            '        echo "exit code: $_ANSIBLE_RC"',
            '        echo "log:       /var/log/arches-firstboot.log"',
            '        echo "to retry:  sudo touch /opt/arches/firstboot-pending '
            '\\&\\& sudo systemctl start arches-firstboot.service"',
            "    } > /opt/arches/firstboot-failed",
            "fi",
            "rm -f /opt/arches/firstboot-pending",
        ]
    )

    # ── Operator banner ─────────────────────────────────
    # Print a human-friendly summary to /dev/console so a headless
    # operator watching the serial console knows the system is ready
    # for SSH, what its hostname is, and what IPs to connect to.
    # Also written to /var/log/arches-ready.log for later inspection.
    #
    # Hostname is read via `uname -n` rather than the `hostname(1)`
    # binary because the latter is shipped in the OPTIONAL `inetutils`
    # package on modern Arch — not present by default in the `base`
    # group. `uname` ships with `coreutils` (which IS in `base`), so
    # this works on every Arches install regardless of which template
    # the operator picked.
    lines.extend(
        [
            "",
            "# Print operator banner to console + log file.",
            "_banner_to() {",
            "    {",
            '        echo ""',
            '        echo "╔════════════════════════════════════════════════╗"',
            '        echo "║   Arches — System ready                        ║"',
            '        echo "╚════════════════════════════════════════════════╝"',
            '        _hn=$(uname -n)',
            '        echo "  hostname: $_hn"',
            '        echo "  user:     ' + username + '"',
            '        echo "  uptime:   $(uptime -p)"',
            '        echo ""',
            '        echo "  network interfaces:"',
            "        ip -o -4 addr show scope global 2>/dev/null \\",
            "            | awk '{printf \"    %-8s %s\\n\", $2, $4}' || true",
            '        echo ""',
            '        echo "  SSH access:"',
            '        echo "    ssh ' + username + '@${_hn}.local      "'
            '"# via mDNS (same L2)"',
            "        ip -o -4 addr show scope global 2>/dev/null \\",
            "            | awk '{split($4,a,\"/\"); "
            'printf "    ssh ' + username + '@%s   # via IP\\n", a[1]}\' || true',
            '        echo ""',
            '        echo "  logs:"',
            '        echo "    install:   /var/log/arches-install.log"',
            '        echo "    firstboot: /var/log/arches-firstboot.log"',
            '        echo "    journal:   journalctl -b"',
            '        echo ""',
            # Close the brace-group. Plain `}` (no `"$@"`) — bash does
            # NOT permit positional args after a brace group, so
            # `} "$@"` is a parse error. The function caller's
            # redirection (e.g. `_banner_to > /dev/console`) already
            # captures stdout from everything inside the brace group,
            # which is the only behaviour we need here.
            "    }",
            "}",
            "",
            "# Write the banner to /var/log/arches-ready.log (for later",
            "# inspection over SSH) AND to /dev/console (for the operator",
            "# watching serial output during the first boot).",
            "_banner_to >/var/log/arches-ready.log 2>&1 || true",
            "_banner_to > /dev/console 2>&1 || true",
        ]
    )

    return "\n".join(lines) + "\n"


def inject_firstboot_service(
    template: InstallTemplate,
    username: str,
    platform: PlatformConfig | None = None,
    hardware: HardwareConfig | None = None,
    extra_vars: dict[str, str] | None = None,
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

    graphical = template.graphical

    # Write the systemd service unit
    service_dir = MOUNT_ROOT / "etc" / "systemd" / "system"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / "arches-firstboot.service"
    service_file.write_text(_firstboot_service_unit(graphical=graphical))

    # Enable the service — graphical installs use graphical.target,
    # headless installs use multi-user.target.
    target = "graphical.target" if graphical else "multi-user.target"
    wants_dir = service_dir / f"{target}.wants"
    wants_dir.mkdir(parents=True, exist_ok=True)
    symlink = wants_dir / "arches-firstboot.service"
    if not symlink.exists():
        symlink.symlink_to("/etc/systemd/system/arches-firstboot.service")

    # Write the first-boot script
    arches_dir = MOUNT_ROOT / "opt" / "arches"
    arches_dir.mkdir(parents=True, exist_ok=True)

    script = arches_dir / "firstboot.sh"
    extra_roles = hardware.all_firstboot_roles if hardware else None
    # Merge hardware ansible_vars with caller-supplied extra_vars.
    # Caller vars (e.g. from auto-install config) take precedence.
    merged_vars: dict[str, str] = {}
    if hardware and hardware.machine and hardware.machine.ansible_vars:
        merged_vars.update(
            {k: str(v) for k, v in hardware.machine.ansible_vars.items()}
        )
    # Surface the resolved GPU stack list to Ansible roles so they can
    # adapt their install commands per-hardware (e.g. aphrodite picking
    # the pytorch CPU wheel index when no NVIDIA stack is in play). The
    # value is a comma-separated list of stack names (e.g. "amd-vulkan"
    # or "amd-vulkan,nvidia-cuda"). Caller-supplied extra_vars still win.
    if template.gpu_stacks:
        merged_vars.setdefault("arches_gpu_stacks", ",".join(template.gpu_stacks))
    if extra_vars:
        merged_vars.update(extra_vars)
    script.write_text(
        generate_firstboot_script(
            template,
            username,
            platform=platform,
            extra_roles=extra_roles,
            extra_vars=merged_vars or None,
        )
    )
    script.chmod(0o755)

    # Create the sentinel file
    sentinel = arches_dir / "firstboot-pending"
    sentinel.touch()

    _log("First-boot service installed.", log)
