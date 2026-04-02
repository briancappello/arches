"""Confirmation screen -- review selections before install."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, Static


class ConfirmScreen(Screen):
    """Review all selections and confirm before installing."""

    BINDINGS = [
        ("up", "prev_button", "Previous"),
        ("down", "next_button", "Next"),
    ]

    def action_next_button(self) -> None:
        self.focus_next(Button)

    def action_prev_button(self) -> None:
        self.focus_previous(Button)

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

        summary.update(text)

        # Set appropriate warning based on partition mode
        warning = self.query_one("#disk-warning", Static)
        if mode == "manual":
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
