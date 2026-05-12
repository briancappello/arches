"""Install progress screen — runs the full install pipeline with live log."""

from __future__ import annotations

import re
import threading
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import HorizontalGroup, Vertical
from textual.widgets import Button, Label, RichLog

from arches_installer.core.pipeline import InstallParams, run_install_pipeline
from arches_installer.tui import ArrowNavScreen

INSTALL_LOG = Path("/var/log/arches-install.log")


class InstallProgressScreen(ArrowNavScreen):
    """Screen that runs the install pipeline and streams log output."""

    CSS = """
    InstallProgressScreen #outer {
        width: 100%;
        height: 100%;
    }
    InstallProgressScreen #title {
        height: auto;
        width: 100%;
        text-align: right;
        padding-right: 2;
    }
    InstallProgressScreen #install-log {
        height: 1fr;
        border: solid $accent;
        margin: 0 1;
        padding: 0 1;
    }
    InstallProgressScreen .button-row {
        height: auto;
        padding: 1 1 0 1;
    }
    InstallProgressScreen .button-row Button {
        margin: 0 1 0 0;
    }
    InstallProgressScreen .btn-primary {
        margin-top: 0;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="outer", classes="panel"):
            yield Label("Installing...", classes="title", id="title")
            yield RichLog(
                highlight=True,
                markup=True,
                wrap=True,
                id="install-log",
            )
            yield Label("")
            with HorizontalGroup(classes="button-row"):
                yield Button(
                    "Reboot",
                    variant="primary",
                    id="btn-reboot",
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
            params = InstallParams(
                platform=platform,
                template=template,
                device=device,
                hostname=self.app.hostname,
                username=self.app.username,
                password=self.app.password,
                partition_map=self.app.partition_map
                if partition_mode == "manual"
                else None,
                disk_layout=self.app.selected_layout
                if partition_mode != "manual"
                else None,
                raid_config=self.app.raid_config,
                # Multi-disk role-based assignment (auto-install path).
                # When None, pipeline falls back to legacy single-device
                # + raid_config behavior.
                resolved_disk_roles=getattr(
                    self.app, "resolved_disk_roles", None
                ),
                hardware=self.app.hardware_config,
                ansible_vars=self.app.ansible_vars,
            )

            parts = run_install_pipeline(params, log=self.log_msg)
            self.app.partition_map = parts
            self.app.install_success = True

            # Done
            self.log_msg("")
            self.log_msg("Remove the installation media and reboot.")

            self.app.call_from_thread(self._on_install_complete)

        except Exception as e:
            self.log_msg(f"\n[bold red]INSTALL FAILED: {e}[/bold red]")
            self.log_msg("Check the log above for details.")
            # In auto-install mode, headless operators won't be at the
            # console to press a button. Honor app.auto_install_failed_action
            # so the box doesn't sit idle forever.
            if getattr(self.app, "auto_install", False):
                self.app.call_from_thread(self._on_install_failed_auto)
            else:
                self.app.call_from_thread(self._enable_reboot)

    def _on_install_failed_auto(self) -> None:
        """Handle install failure in auto-install mode.

        Reads ``app.auto_install_failed_action`` and acts accordingly.
        Defaults to "poweroff" (set in TUI app constructor).
        """
        import subprocess

        action = getattr(self.app, "auto_install_failed_action", "poweroff")
        self._write_log(f"Auto-install failure action: {action}")
        if action == "poweroff":
            self._write_log("Powering off in 10 seconds...")
            subprocess.run(["sleep", "10"], check=False)
            subprocess.run(["systemctl", "poweroff"], check=False)
        elif action == "reboot":
            self._write_log("Rebooting in 10 seconds...")
            subprocess.run(["sleep", "10"], check=False)
            subprocess.run(["systemctl", "reboot"], check=False)
        else:  # "wait" or unknown
            self._enable_reboot()

    def _on_install_complete(self) -> None:
        """Handle install completion — auto shutdown/reboot or enable buttons.

        This runs on the main app thread (dispatched via call_from_thread),
        so use _write_log directly instead of log_msg (which would fail
        with "must run in a different thread").
        """
        import subprocess

        if getattr(self.app, "auto_install", False):
            if self.app.auto_shutdown:
                self._write_log("Shutting down...")
                subprocess.run(["systemctl", "poweroff"], check=False)
            elif self.app.auto_reboot:
                self._write_log("Rebooting into installed system...")
                subprocess.run(["systemctl", "reboot"], check=False)
            else:
                self._enable_reboot()
        else:
            self._enable_reboot()

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
