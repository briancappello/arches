"""Confirmation screen -- review selections before install."""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.widgets import Button, Label, Static

from arches_installer.core.existing import resolve_and_plan
from arches_installer.tui import ArrowNavScreen


def ssh_keys_summary(keys: list[str]) -> str:
    """One line per declared SSH key: its type, the end of the key, its comment."""
    if not keys:
        return "  SSH keys:    none (no authorized_keys)\n"
    lines = ""
    for k in keys:
        fields = k.split(None, 2)
        kind, body = fields[0], fields[1] if len(fields) > 1 else ""
        comment = fields[2] if len(fields) > 2 else ""
        lines += f"  SSH key:     {kind} ...{body[-12:]} {comment}".rstrip() + "\n"
    return lines


class ConfirmScreen(ArrowNavScreen):
    """Review all selections and confirm before installing."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("Confirm Installation", classes="title")
                yield Static(id="summary")
                yield Label("")
                yield Static(id="disk-warning")
                yield Label("")
                yield Button(
                    "Install",
                    variant="error",
                    id="btn-install",
                    classes="btn-primary",
                )
                yield Button("Back", variant="default", id="btn-back")

    _plan_error: str = ""

    def _existing_summary(self, layout) -> str:
        """Resolve the existing layout here and now, and run its guard."""
        hw = self.app.hardware_config
        try:
            resolved, plan = resolve_and_plan(
                layout,
                machine_disks=hw.disk_role_overrides() if hw else None,
            )
        except Exception as e:  # the guard's message is the useful part
            self._plan_error = str(e)
            self.app.resolved_disk_roles = None
            return "  Targets:     (guard failed, see below)\n"
        self._plan_error = ""
        self.app.resolved_disk_roles = resolved
        text = f"  Targets:     {', '.join(plan.targets)}  (erased)\n"
        for vol in plan.volumes:
            spec = vol.spec
            stack = []
            if spec.raid_level:
                stack.append(f"mdadm RAID{spec.raid_level}")
            if spec.encryption:
                stack.append("LUKS2 (TPM2 + PIN, PCR 7)")
            stack.append(spec.filesystem)
            text += f"    {spec.partlabel}: {' -> '.join(stack)} -> {spec.mount_point}\n"
            for sv in spec.subvolumes:
                text += f"      subvol: {sv.name} -> {sv.mount_point or '(none)'}\n"
        text += f"  ESP:         {plan.esp}  (reused, not formatted)\n"
        return text

    def on_mount(self) -> None:
        """Build and display the install summary."""
        summary = self.query_one("#summary", Static)
        tmpl = self.app.selected_template
        platform = self.app.platform

        if tmpl is None:
            summary.update("Error: no template selected")
            return

        mode = self.app.partition_mode
        parts = self.app.partition_map
        layout = self.app.selected_layout
        raid = self.app.raid_config

        text = (
            f"  Platform:    {platform.name}\n"
            f"  Template:    {tmpl.name}\n"
            f"  Device:      {self.app.selected_device}\n"
            f"  Partitions:  {'manual' if mode == 'manual' else 'auto'}\n"
            f"  Kernel:      {platform.kernel.package}\n"
            f"  Bootloader:  {platform.bootloader.type}\n"
        )

        # Show RAID config if set
        if raid:
            text += (
                f"  RAID:        {raid.backend.value} "
                f"RAID{raid.level.value} "
                f"({len(raid.devices)} disks)\n"
            )
            text += f"  RAID disks:  {', '.join(raid.devices)}\n"

        if mode == "manual" and parts:
            text += f"  Root:        {parts.root}\n"
            text += f"  ESP:         {parts.esp}\n"
            if parts.boot:
                text += f"  Boot:        {parts.boot}\n"
            if parts.home:
                text += f"  Home:        {parts.home}\n"
        elif layout and layout.is_existing:
            text += f"  Disk layout: {layout.name}\n"
            text += self._existing_summary(layout)
        elif layout:
            text += f"  Disk layout: {layout.name}\n"
            for i, part in enumerate(layout.partitions):
                fs = part.filesystem or "raw"
                mp = part.mount_point or "(none)"
                text += f"    Part {i + 1}: {fs}  {part.size}  -> {mp}\n"
                for sv in part.subvolumes:
                    sv_mp = sv.mount_point or "(none)"
                    text += f"      subvol: {sv.name} -> {sv_mp}\n"

        text += (
            f"  Snapshots:   {'Yes' if platform.bootloader.snapshot_boot else 'No'}\n"
            f"  Hostname:    {self.app.hostname}\n"
            f"  User:        {self.app.username}\n"
            f"  Packages:    {len(tmpl.install.all_packages)} packages\n"
            f"  Services:    {len(tmpl.services)} services\n"
        )

        if tmpl.ansible.firstboot_roles:
            text += f"  Ansible (1st boot): {', '.join(tmpl.ansible.firstboot_roles)}\n"

        text += ssh_keys_summary(self.app.ssh_authorized_keys)

        # Hardware configuration
        hw = self.app.hardware_config
        if hw and (hw.machine or hw.quirks):
            text += "\n  [bold]Hardware:[/bold]\n"
            if hw.machine:
                text += f"  Machine:     {hw.machine.name}\n"
            if hw.quirks:
                text += f"  Quirks:      {', '.join(q.name for q in hw.quirks)}\n"
            if hw.all_packages:
                text += f"  HW packages: {len(hw.all_packages)}\n"
            if hw.all_firstboot_roles:
                text += f"  HW ansible:  {', '.join(hw.all_firstboot_roles)}\n"

        summary.update(text)

        # Set appropriate warning based on partition mode
        warning = self.query_one("#disk-warning", Static)
        if self._plan_error:
            warning.update(f"[bold red]{escape(self._plan_error)}[/bold red]")
            self.query_one("#btn-install", Button).disabled = True
        elif layout and mode != "manual" and layout.is_existing:
            warning.update(
                "[bold red]WARNING: This will ERASE only the target partitions "
                "listed above.[/bold red] The ESP is reused, not formatted; "
                "its limine.conf is backed up and merged."
                + (
                    "\nThe disk recovery key is shown ONCE during the install."
                    " Have somewhere offline to write it down."
                    if layout.is_encrypted
                    else ""
                )
            )
        elif mode == "manual":
            warning.update(
                "[bold yellow]WARNING: The system will be installed onto "
                "your mounted partitions![/bold yellow]"
            )
        else:
            warning.update(
                "[bold red]WARNING: This will ERASE the target disk![/bold red]"
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-install":
            self.app.push_screen("progress")
        elif event.button.id == "btn-back":
            self.app.pop_screen()
