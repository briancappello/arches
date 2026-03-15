"""Partition screen — shell-first partitioning with mount validation.

The primary flow is: user drops to a shell, partitions/formats/mounts their
disks onto /mnt, then returns to the installer. We validate the mounts and
detect ESP, root, boot, and home partitions automatically.

An auto-partition option is available for VMs and unattended installs — it
uses the platform's disk_layout config to wipe and partition the selected disk.
"""

from __future__ import annotations

import subprocess

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, Static


class PartitionScreen(Screen):
    """Partition screen — drop to shell or auto-partition."""

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
                yield Label("Disk Setup", classes="title")
                yield Static(id="disk-info")
                yield Label("")
                yield Label(
                    "Partition, format, and mount your disks onto /mnt.\n"
                    "The installer will detect your layout when you return."
                )
                yield Label("")
                yield Label(
                    "Required: root mounted at /mnt, ESP mounted at\n"
                    "/mnt/boot (Limine) or /mnt/boot/efi (GRUB)."
                )
                yield Label("")
                yield Static(id="mount-status")
                yield Label("")
                yield Button(
                    "Open Shell",
                    variant="primary",
                    id="btn-shell",
                    classes="btn-primary",
                )
                yield Button(
                    "Validate Mounts & Continue",
                    variant="success",
                    id="btn-validate",
                )
                yield Button(
                    "Auto-partition (VM only)",
                    variant="warning",
                    id="btn-auto",
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

        self._update_mount_status()

    def _update_mount_status(self) -> None:
        """Check and display current mount status."""
        from arches_installer.core.disk import detect_mounts, validate_mounts

        status = self.query_one("#mount-status", Static)
        parts = detect_mounts()

        if parts is None:
            status.update("[dim]No filesystems mounted at /mnt[/dim]")
            return

        errors = validate_mounts(parts)
        if errors:
            msg = "[yellow]Mount issues:[/yellow]\n"
            for err in errors:
                msg += f"  • {err}\n"
            status.update(msg)
        else:
            msg = (
                "[green]Mounts detected:[/green]\n"
                f"  Root: {parts.root}\n"
                f"  ESP:  {parts.esp}\n"
            )
            if parts.boot:
                msg += f"  Boot: {parts.boot}\n"
            if parts.home:
                msg += f"  Home: {parts.home}\n"
            status.update(msg)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-shell":
            self._drop_to_shell()
        elif event.button.id == "btn-validate":
            self._validate_and_continue()
        elif event.button.id == "btn-auto":
            self._auto_partition()
        elif event.button.id == "btn-back":
            self.app.pop_screen()

    def _drop_to_shell(self) -> None:
        """Suspend the TUI and drop to an interactive shell."""
        device = self.app.selected_device
        self.app.suspend()
        subprocess.run(
            [
                "/bin/bash",
                "-c",
                'echo ""; '
                'echo "══ Arches Disk Setup Shell ══"; '
                'echo ""; '
                'echo "Target device: ' + device + '"; '
                'echo ""; '
                'echo "Partition, format, and mount your disks onto /mnt."; '
                'echo "Example (GPT + btrfs):"; '
                'echo "  sgdisk --zap-all ' + device + '"; '
                'echo "  sgdisk -n 1:0:+2G -t 1:EF00 ' + device + '"; '
                'echo "  sgdisk -n 2:0:0 -t 2:8300 ' + device + '"; '
                'echo "  mkfs.fat -F32 ' + device + '1"; '
                'echo "  mkfs.btrfs -f ' + device + '2"; '
                'echo "  mount ' + device + '2 /mnt"; '
                'echo "  mount --mkdir ' + device + '1 /mnt/boot"; '
                'echo ""; '
                'echo "Type exit when done."; '
                'echo ""; '
                "exec /bin/bash",
            ],
        )
        self.app.resume()
        self._update_mount_status()

    def _validate_and_continue(self) -> None:
        """Validate mounts at /mnt and continue if valid."""
        from arches_installer.core.disk import detect_mounts, validate_mounts

        parts = detect_mounts()
        if parts is None:
            status = self.query_one("#mount-status", Static)
            status.update(
                "[red]Nothing is mounted at /mnt.[/red]\n"
                "Open a shell and set up your disks first."
            )
            return

        errors = validate_mounts(parts)
        if errors:
            status = self.query_one("#mount-status", Static)
            msg = "[red]Cannot continue — mount issues:[/red]\n"
            for err in errors:
                msg += f"  • {err}\n"
            status.update(msg)
            return

        # Mounts are valid — store them and advance
        self.app.partition_mode = "manual"
        self.app.partition_map = parts
        self.app.push_screen("template_select")

    def _auto_partition(self) -> None:
        """Use auto-partition (wipes disk, uses platform disk_layout)."""
        self.app.partition_mode = "auto"
        self.app.partition_map = None
        self.app.push_screen("template_select")
