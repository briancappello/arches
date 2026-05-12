"""Main Textual application for the Arches installer."""

from __future__ import annotations

from typing import Any

from textual.app import App
from textual.binding import Binding
from textual.screen import Screen

from arches_installer.core.disk import PartitionMap
from arches_installer.core.disk_layout import DiskLayout, RaidConfig
from arches_installer.core.hardware import HardwareConfig
from arches_installer.core.platform import PlatformConfig
from arches_installer.core.template import InstallTemplate
from arches_installer.tui.welcome import WelcomeScreen
from arches_installer.tui.network import NetworkScreen
from arches_installer.tui.disk_select import DiskSelectScreen
from arches_installer.tui.raid_config import RaidConfigScreen
from arches_installer.tui.layout_select import LayoutSelectScreen
from arches_installer.tui.partition import PartitionScreen
from arches_installer.tui.template_select import TemplateSelectScreen
from arches_installer.tui.module_select import ModuleSelectScreen
from arches_installer.tui.hardware_confirm import HardwareConfirmScreen
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

    # Install state -- populated as user progresses through screens
    selected_device: str = ""
    selected_template: InstallTemplate | None = None
    selected_layout: DiskLayout | None = None
    raid_config: RaidConfig | None = None
    partition_mode: str = ""  # "auto" or "manual"
    partition_map: PartitionMap | None = None  # set by manual flow or progress
    hardware_config: HardwareConfig | None = None  # set by hardware confirm screen
    # Set by __main__._run_auto for multi-disk role-based installs.
    # When None, the progress screen falls back to the legacy
    # single-device + raid_config flow.
    resolved_disk_roles: Any = None  # ResolvedDiskRoles — opaque to avoid import cycle
    hostname: str = ""
    username: str = ""
    password: str = ""

    # Auto-install mode -- when True, skip to progress screen and
    # handle reboot/shutdown on completion.
    auto_install: bool = False
    auto_shutdown: bool = False
    auto_reboot: bool = False
    install_success: bool = False
    # What to do when an auto-install FAILS. On a headless rack box no
    # one is at the keyboard to press a button, so leaving the system
    # at an idle TUI screen is unhelpful. Options:
    #   "poweroff" — power off (default; safe, preserves install media)
    #   "reboot"   — reboot (may loop into the installer again)
    #   "wait"     — leave the TUI up with reboot/shutdown buttons
    auto_install_failed_action: str = "poweroff"

    # Extra Ansible vars from auto-install config [ansible_vars].
    ansible_vars: dict[str, str] | None = None

    # Debug/demo: push this screen name on mount (e.g., "confirm", "user_setup")
    push_screen_on_mount: str = ""

    SCREENS = {
        "welcome": WelcomeScreen,
        "network": NetworkScreen,
        "disk_select": DiskSelectScreen,
        "raid_config": RaidConfigScreen,
        "layout_select": LayoutSelectScreen,
        "partition": PartitionScreen,
        "template_select": TemplateSelectScreen,
        "module_select": ModuleSelectScreen,
        "hardware_confirm": HardwareConfirmScreen,
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
