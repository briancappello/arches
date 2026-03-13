"""Confirmation screen — review selections before install."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, Static


class ConfirmScreen(Screen):
    """Review all selections and confirm before installing."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("Confirm Installation", classes="title")
                yield Static(id="summary")
                yield Label("")
                yield Label(
                    "[bold red]WARNING: This will ERASE the target disk![/bold red]"
                )
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

        text = (
            f"  Platform:    {platform.name}\n"
            f"  Template:    {tmpl.name}\n"
            f"  Device:      {self.app.selected_device}\n"
            f"  Filesystem:  {tmpl.disk.filesystem}\n"
            f"  Kernel:      {platform.kernel.package}\n"
            f"  Bootloader:  {tmpl.bootloader.type}\n"
            f"  Snapshots:   {'Yes' if tmpl.bootloader.snapshot_boot else 'No'}\n"
            f"  Hostname:    {self.app.hostname}\n"
            f"  User:        {self.app.username}\n"
            f"  Packages:    {len(tmpl.system.packages)} packages\n"
            f"  Services:    {len(tmpl.services)} services\n"
        )

        if tmpl.disk.filesystem == "btrfs" and tmpl.disk.subvolumes:
            text += f"  Subvolumes:  {', '.join(tmpl.disk.subvolumes)}\n"

        if tmpl.ansible.chroot_roles:
            text += f"  Ansible (chroot):  {', '.join(tmpl.ansible.chroot_roles)}\n"
        if tmpl.ansible.firstboot_roles:
            text += f"  Ansible (1st boot): {', '.join(tmpl.ansible.firstboot_roles)}\n"

        summary.update(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-install":
            self.app.push_screen("progress")
        elif event.button.id == "btn-back":
            self.app.pop_screen()
