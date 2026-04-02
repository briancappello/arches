"""Welcome screen -- network check and entry point."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, Static

from arches_installer.core.network import check_connectivity


class WelcomeScreen(Screen):
    """Welcome screen with network status and navigation."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("A R C H E S", classes="title")
                yield Label(
                    "Arches Install & Recovery",
                    classes="subtitle",
                )
                yield Static(id="net-status")
                yield Label("")
                yield Button(
                    "Continue",
                    variant="primary",
                    id="btn-continue",
                    classes="btn-primary",
                )
                yield Button(
                    "Configure Network",
                    variant="warning",
                    id="btn-network",
                )
                yield Button(
                    "Exit Installer",
                    variant="default",
                    id="btn-shell",
                )

    def on_mount(self) -> None:
        """Check network on mount."""
        self._update_net_status()

    def _update_net_status(self) -> None:
        """Check and display network connectivity."""
        status = self.query_one("#net-status", Static)
        if check_connectivity():
            status.update("[green]Network: connected[/green]")
        else:
            status.update(
                "[red]Network: offline[/red]  -- Use the button below to configure."
            )

    def on_screen_resume(self) -> None:
        """Re-check network when returning from the network screen."""
        self._update_net_status()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-shell":
            self.app.exit(return_code=2)
            return

        if event.button.id == "btn-network":
            self.app.push_screen("network")
            return

        if event.button.id == "btn-continue":
            self.app.push_screen("disk_select")
