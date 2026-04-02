"""Disk selection screen -- choose a target disk or configure RAID."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from arches_installer.core.disk import BlockDevice, detect_block_devices


class DiskSelectScreen(Screen):
    """Screen for selecting a target block device or configuring RAID."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("Select Target Disk", classes="title")
                yield Label(
                    "Choose the disk to install to, or configure RAID first.",
                    classes="subtitle",
                )
                yield OptionList(id="disk-list")
                yield Static(id="disk-info")
                yield Button(
                    "Continue",
                    variant="primary",
                    id="btn-continue",
                    classes="btn-primary",
                )
                yield Button(
                    "Configure RAID",
                    variant="warning",
                    id="btn-raid",
                )
                yield Button("Back", variant="default", id="btn-back")

    def on_mount(self) -> None:
        """Detect disks and populate the list."""
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
                disk_list.highlighted = 0
                disk_list.focus()
        except Exception as e:
            disk_list.add_option(Option(f"Error: {e}", id="error"))

    def on_option_list_highlighted(
        self,
        event: OptionList.OptionHighlighted,
    ) -> None:
        """Show disk details when highlighted."""
        info = self.query_one("#disk-info", Static)
        if event.option_index is not None and self._devices:
            dev = self._devices[event.option_index]
            details = (
                f"  Device:     {dev.path}\n"
                f"  Size:       {dev.size}\n"
                f"  Model:      {dev.model}\n"
                f"  Partitions: {len(dev.partitions)}"
            )
            if dev.partitions:
                details += f" ({', '.join(dev.partitions)})"
            info.update(details)

    def _select_disk(self) -> None:
        """Accept the currently highlighted disk and advance."""
        disk_list = self.query_one("#disk-list", OptionList)
        if disk_list.highlighted is not None and self._devices:
            device = self._devices[disk_list.highlighted]
            self.app.selected_device = device.path
            self.app.push_screen("layout_select")

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        """Enter pressed on a disk option -- select it and advance."""
        self._select_disk()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-continue":
            self._select_disk()
        elif event.button.id == "btn-raid":
            self.app.push_screen("raid_config")
        elif event.button.id == "btn-back":
            self.app.pop_screen()
