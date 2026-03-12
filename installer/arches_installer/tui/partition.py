"""Partition screen — guided partitioning or drop to shell."""

from __future__ import annotations

import subprocess

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, Static


class PartitionScreen(Screen):
    """Partition confirmation / manual partitioning screen.

    The actual partitioning is handled automatically by disk.prepare_disk()
    based on the selected template. This screen gives the user a chance to
    drop to a shell for manual partitioning if they prefer, or to confirm
    the automatic layout.
    """

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("Disk Partitioning", classes="title")
                yield Static(id="disk-info")
                yield Label("")
                yield Label("The installer will create a GPT partition table with:")
                yield Label("  1. EFI System Partition (FAT32)")
                yield Label("  2. Root partition (btrfs or ext4)")
                yield Label("")
                yield Label("ESP size depends on the template (512M-2G).")
                yield Label("")
                yield Button(
                    "Continue (auto-partition)",
                    variant="primary",
                    id="btn-auto",
                    classes="btn-primary",
                )
                yield Button(
                    "Manual (drop to shell)",
                    variant="warning",
                    id="btn-manual",
                )
                yield Button("Back", variant="default", id="btn-back")

    def on_mount(self) -> None:
        """Show selected disk info."""
        info = self.query_one("#disk-info", Static)
        device = self.app.selected_device

        try:
            result = subprocess.run(
                ["lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT", device],
                capture_output=True,
                text=True,
            )
            info.update(result.stdout)
        except Exception as e:
            info.update(f"Could not read disk info: {e}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-auto":
            self.app.push_screen("template_select")
        elif event.button.id == "btn-manual":
            # Suspend the TUI and drop to shell
            self.app.suspend()
            subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    'echo "Use cfdisk, gdisk, or fdisk to partition your disk."; '
                    'echo "Target device: ' + self.app.selected_device + '"; '
                    'echo "Type exit when done."; '
                    "exec /bin/bash",
                ],
            )
            self.app.resume()
            # After returning from shell, continue to template select
            self.app.push_screen("template_select")
        elif event.button.id == "btn-back":
            self.app.pop_screen()
