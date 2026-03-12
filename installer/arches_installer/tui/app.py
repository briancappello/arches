"""Main Textual application for the Arches installer."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding

from arches_installer.core.template import InstallTemplate
from arches_installer.tui.welcome import WelcomeScreen
from arches_installer.tui.partition import PartitionScreen
from arches_installer.tui.template_select import TemplateSelectScreen
from arches_installer.tui.user_setup import UserSetupScreen
from arches_installer.tui.confirm import ConfirmScreen
from arches_installer.tui.progress import InstallProgressScreen


class ArchesApp(App):
    """Arches installer TUI application."""

    TITLE = "Arches Installer"
    CSS = """
    Screen {
        align: center middle;
    }

    .title {
        text-style: bold;
        color: $accent;
        text-align: center;
        width: 100%;
        margin-bottom: 1;
    }

    .subtitle {
        color: $text-muted;
        text-align: center;
        width: 100%;
        margin-bottom: 2;
    }

    .panel {
        width: 80;
        max-width: 100%;
        padding: 1 2;
        border: solid $accent;
        margin: 1;
    }

    .btn-primary {
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
    ]

    # Install state — populated as user progresses through screens
    selected_device: str = ""
    selected_template: InstallTemplate | None = None
    hostname: str = ""
    username: str = ""
    password: str = ""

    SCREENS = {
        "welcome": WelcomeScreen,
        "partition": PartitionScreen,
        "template_select": TemplateSelectScreen,
        "user_setup": UserSetupScreen,
        "confirm": ConfirmScreen,
        "progress": InstallProgressScreen,
    }

    def on_mount(self) -> None:
        """Start at the welcome screen."""
        self.push_screen("welcome")
