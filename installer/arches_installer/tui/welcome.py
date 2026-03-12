"""Welcome screen — disk detection and selection."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, OptionList
from textual.widgets.option_list import Option

from arches_installer.core.disk import BlockDevice, detect_block_devices


class WelcomeScreen(Screen):
    """Welcome screen with disk detection and selection."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("A R C H E S", classes="title")
                yield Label(
                    "Arch/CachyOS Install & Recovery",
                    classes="subtitle",
                )
                yield Label("Select target disk:")
                yield OptionList(id="disk-list")
                yield Button(
                    "Continue",
                    variant="primary",
                    id="btn-continue",
                    classes="btn-primary",
                )
                yield Button(
                    "Recovery Shell",
                    variant="default",
                    id="btn-shell",
                )

    def on_mount(self) -> None:
        """Detect and populate available disks."""
        disk_list = self.query_one("#disk-list", OptionList)
        self._devices: list[BlockDevice] = []

        try:
            devices = detect_block_devices()
            self._devices = [d for d in devices if not d.removable]

            if not self._devices:
                disk_list.add_option(Option("No disks found", id="none"))
            else:
                for dev in self._devices:
                    disk_list.add_option(Option(dev.display, id=dev.path))
        except Exception as e:
            disk_list.add_option(Option(f"Error: {e}", id="error"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-shell":
            self.app.exit(return_code=2)
            return

        if event.button.id == "btn-continue":
            disk_list = self.query_one("#disk-list", OptionList)
            if disk_list.highlighted is not None and self._devices:
                device = self._devices[disk_list.highlighted]
                self.app.selected_device = device.path
                self.app.push_screen("partition")
