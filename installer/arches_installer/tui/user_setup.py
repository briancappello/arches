"""User setup screen — hostname, username, password."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Label


class UserSetupScreen(Screen):
    """Screen for configuring hostname, username, and password."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("System Setup", classes="title")

                yield Label("Hostname:")
                yield Input(
                    placeholder="arches",
                    value="arches",
                    id="input-hostname",
                )

                yield Label("Username:")
                yield Input(
                    placeholder="user",
                    id="input-username",
                )

                yield Label("Password:")
                yield Input(
                    placeholder="password",
                    password=True,
                    id="input-password",
                )

                yield Label("Confirm password:")
                yield Input(
                    placeholder="password",
                    password=True,
                    id="input-password-confirm",
                )

                yield Label("", id="error-label")

                yield Button(
                    "Continue",
                    variant="primary",
                    id="btn-continue",
                    classes="btn-primary",
                )
                yield Button("Back", variant="default", id="btn-back")

    def _validate(self) -> str | None:
        """Validate inputs. Returns error message or None."""
        hostname = self.query_one("#input-hostname", Input).value.strip()
        username = self.query_one("#input-username", Input).value.strip()
        password = self.query_one("#input-password", Input).value
        confirm = self.query_one("#input-password-confirm", Input).value

        if not hostname:
            return "Hostname is required."
        if not username:
            return "Username is required."
        if not password:
            return "Password is required."
        if password != confirm:
            return "Passwords do not match."
        if len(password) < 4:
            return "Password must be at least 4 characters."
        if not username.isalnum() or username[0].isdigit():
            return "Username must be alphanumeric, starting with a letter."
        return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-continue":
            error = self._validate()
            error_label = self.query_one("#error-label", Label)

            if error:
                error_label.update(f"[red]{error}[/red]")
                return

            self.app.hostname = self.query_one(
                "#input-hostname",
                Input,
            ).value.strip()
            self.app.username = self.query_one(
                "#input-username",
                Input,
            ).value.strip()
            self.app.password = self.query_one(
                "#input-password",
                Input,
            ).value

            self.app.push_screen("confirm")

        elif event.button.id == "btn-back":
            self.app.pop_screen()
