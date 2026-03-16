"""Install progress screen — runs the full install pipeline with live log."""

from __future__ import annotations

import re
import threading
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Label, RichLog

from arches_installer.core.bootloader import install_bootloader
from arches_installer.core.disk import prepare_disk
from arches_installer.core.firstboot import inject_firstboot_service
from arches_installer.core.install import install_system
from arches_installer.core.snapper import setup_snapshots

INSTALL_LOG = Path("/var/log/arches-install.log")


class InstallProgressScreen(Screen):
    """Screen that runs the install pipeline and streams log output."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("Installing...", classes="title", id="title")
                with VerticalScroll():
                    yield RichLog(
                        highlight=True,
                        markup=True,
                        wrap=True,
                        id="install-log",
                    )
                with Horizontal(classes="button-row"):
                    yield Button(
                        "Reboot",
                        variant="primary",
                        id="btn-reboot",
                        classes="btn-primary",
                        disabled=True,
                    )
                    yield Button(
                        "Shutdown",
                        variant="default",
                        id="btn-shutdown",
                        disabled=True,
                    )

    def log_msg(self, msg: str) -> None:
        """Thread-safe log message to the RichLog widget and log file."""
        self.app.call_from_thread(self._write_log, msg)
        # Also append to the log file (strip Rich markup for plain text)
        try:
            plain = re.sub(r"\[/?[^\]]*\]", "", msg)
            with INSTALL_LOG.open("a") as f:
                f.write(plain + "\n")
        except OSError:
            pass

    def _write_log(self, msg: str) -> None:
        log = self.query_one("#install-log", RichLog)
        log.write(msg)

    def on_mount(self) -> None:
        """Start the install in a background thread."""
        # Initialize the log file
        try:
            INSTALL_LOG.parent.mkdir(parents=True, exist_ok=True)
            INSTALL_LOG.write_text("=== Arches Install Log ===\n")
        except OSError:
            pass
        thread = threading.Thread(target=self._run_install, daemon=True)
        thread.start()

    def _run_install(self) -> None:
        """Run the full install pipeline."""
        template = self.app.selected_template
        device = self.app.selected_device
        platform = self.app.platform
        partition_mode = self.app.partition_mode

        if template is None:
            self.log_msg("[red]ERROR: No template selected![/red]")
            return

        try:
            # Phase 1: Disk preparation
            self.log_msg("[bold cyan]── Phase 1: Disk Setup ──[/bold cyan]")
            if partition_mode == "manual":
                parts = self.app.partition_map
                if parts is None:
                    self.log_msg(
                        "[red]ERROR: No partition map from manual setup![/red]"
                    )
                    return
                self.log_msg("Using manually prepared mounts:")
                self.log_msg(f"  Root: {parts.root}")
                self.log_msg(f"  ESP:  {parts.esp}")
                if parts.boot:
                    self.log_msg(f"  Boot: {parts.boot}")
                if parts.home:
                    self.log_msg(f"  Home: {parts.home}")
                self.log_msg("[green]Manual mounts verified.[/green]")
            else:
                parts = prepare_disk(device, platform)
                self.log_msg("[green]Disk prepared successfully.[/green]")
            self.app.partition_map = parts

            # Phase 2: System installation
            self.log_msg("[bold cyan]── Phase 2: System Install ──[/bold cyan]")
            install_system(
                platform,
                template,
                self.app.hostname,
                self.app.username,
                self.app.password,
                log=self.log_msg,
            )
            self.log_msg("[green]System installed successfully.[/green]")

            # Phase 3: Bootloader
            self.log_msg("[bold cyan]── Phase 3: Bootloader ──[/bold cyan]")
            install_bootloader(
                platform,
                device,
                parts.esp,
                parts.root,
                log=self.log_msg,
            )
            self.log_msg("[green]Bootloader installed.[/green]")

            # Phase 4: Snapshots (if btrfs platform)
            if platform.disk_layout.filesystem == "btrfs":
                self.log_msg("[bold cyan]── Phase 4: Snapshots ──[/bold cyan]")
                setup_snapshots(platform, log=self.log_msg)
                self.log_msg("[green]Snapshot support configured.[/green]")

            # Phase 5: First-boot service
            self.log_msg("[bold cyan]── Phase 5: First-Boot ──[/bold cyan]")
            inject_firstboot_service(
                template,
                self.app.username,
                log=self.log_msg,
            )

            # Done
            self.log_msg("")
            self.log_msg("[bold green]Installation complete![/bold green]")
            self.log_msg("Remove the installation media and reboot.")

            self.app.call_from_thread(self._enable_reboot)

        except Exception as e:
            self.log_msg(f"\n[bold red]INSTALL FAILED: {e}[/bold red]")
            self.log_msg("Check the log above for details.")
            self.app.call_from_thread(self._enable_reboot)

    def _enable_reboot(self) -> None:
        """Enable the reboot/shutdown buttons and focus reboot."""
        title = self.query_one("#title", Label)
        title.update("Complete")
        btn_reboot = self.query_one("#btn-reboot", Button)
        btn_reboot.disabled = False
        btn_reboot.focus()
        btn_shutdown = self.query_one("#btn-shutdown", Button)
        btn_shutdown.disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        import subprocess

        if event.button.id == "btn-reboot":
            subprocess.run(["systemctl", "reboot"])
        elif event.button.id == "btn-shutdown":
            subprocess.run(["systemctl", "poweroff"])
