"""Main Textual application for the Arches installer."""

from __future__ import annotations

from textual.app import App
from textual.binding import Binding
from textual.screen import Screen

from arches_installer.core.disk import PartitionMap
from arches_installer.core.platform import PlatformConfig
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
        width: 100%;
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
    partition_mode: str = ""  # "auto" or "manual"
    partition_map: PartitionMap | None = None  # set by manual flow or progress
    hostname: str = ""
    username: str = ""
    password: str = ""

    # Auto-install mode — when True, skip to progress screen and
    # handle reboot/shutdown on completion.
    auto_install: bool = False
    auto_shutdown: bool = False
    auto_reboot: bool = False
    install_success: bool = False

    # Debug/demo: push this screen name on mount (e.g., "confirm", "user_setup")
    push_screen_on_mount: str = ""

    SCREENS = {
        "welcome": WelcomeScreen,
        "partition": PartitionScreen,
        "template_select": TemplateSelectScreen,
        "user_setup": UserSetupScreen,
        "confirm": ConfirmScreen,
        "progress": InstallProgressScreen,
    }

    def __init__(self, *, platform: PlatformConfig, **kwargs) -> None:
        super().__init__(**kwargs)
        self.platform = platform

    def get_default_screen(self) -> Screen:
        """Start at progress screen for auto-install, welcome for interactive."""
        if self.auto_install:
            return InstallProgressScreen()
        return WelcomeScreen()

    def on_mount(self) -> None:
        """Optionally push a specific screen for demo/debug."""
        if self.push_screen_on_mount and self.push_screen_on_mount in self.SCREENS:
            self.push_screen(self.push_screen_on_mount)
