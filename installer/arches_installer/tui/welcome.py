"""Welcome screen — disk detection and selection."""

from __future__ import annotations

import subprocess

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from arches_installer.core.disk import BlockDevice, detect_block_devices


def _check_network() -> bool:
    """Return True if we have internet connectivity."""
    try:
        subprocess.run(
            [
                "curl",
                "-s",
                "--max-time",
                "3",
                "-o",
                "/dev/null",
                "https://archlinux.org",
            ],
            capture_output=True,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


class WelcomeScreen(Screen):
    """Welcome screen with disk detection and selection."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("A R C H E S", classes="title")
                yield Label(
                    "Arches Install & Recovery",
                    classes="subtitle",
                )
                yield Static(id="net-status")
                yield Label("Select target disk:")
                yield OptionList(id="disk-list")
                yield Button(
                    "Continue",
                    variant="primary",
                    id="btn-continue",
                    classes="btn-primary",
                )
                yield Button(
                    "Configure WiFi",
                    variant="warning",
                    id="btn-wifi",
                )
                yield Button(
                    "Recovery Shell",
                    variant="default",
                    id="btn-shell",
                )

    def on_mount(self) -> None:
        """Detect disks and check network."""
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

        self._update_net_status()

    def _update_net_status(self) -> None:
        """Check and display network connectivity."""
        status = self.query_one("#net-status", Static)
        if _check_network():
            status.update("[green]Network: connected[/green]")
        else:
            status.update(
                "[red]Network: offline[/red]  —  WiFi required? Use the button below."
            )

    def _select_disk(self) -> None:
        """Accept the currently highlighted disk and advance."""
        disk_list = self.query_one("#disk-list", OptionList)
        if disk_list.highlighted is not None and self._devices:
            device = self._devices[disk_list.highlighted]
            self.app.selected_device = device.path
            self.app.push_screen("partition")

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        """Enter pressed on a disk option — select it and advance."""
        self._select_disk()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-shell":
            self.app.exit(return_code=2)
            return

        if event.button.id == "btn-wifi":
            self.app.suspend()
            subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    'echo ""; '
                    'echo "══ WiFi Setup ══"; '
                    'echo ""; '
                    'echo "Use iwctl to connect to a wireless network:"; '
                    'echo "  station wlan0 scan"; '
                    'echo "  station wlan0 get-networks"; '
                    'echo "  station wlan0 connect <SSID>"; '
                    'echo ""; '
                    'echo "Or use nmtui for a menu-driven interface:"; '
                    'echo "  nmtui"; '
                    'echo ""; '
                    'echo "Type exit when done."; '
                    'echo ""; '
                    "exec /bin/bash",
                ],
            )
            self.app.resume()
            self._update_net_status()
            return

        if event.button.id == "btn-continue":
            self._select_disk()
