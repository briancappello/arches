"""Hardware confirmation screen -- show detected machine and quirks."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, Static


class HardwareConfirmScreen(Screen):
    """Show auto-detected hardware config and let the user confirm or skip."""

    BINDINGS = [
        ("up", "prev_button", "Previous"),
        ("down", "next_button", "Next"),
    ]

    def action_next_button(self) -> None:
        self.focus_next(Button)

    def action_prev_button(self) -> None:
        self.focus_previous(Button)

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("Hardware Configuration", classes="title")
                yield Static(id="hw-summary")
                yield Label("")
                yield Button(
                    "Continue",
                    variant="primary",
                    id="btn-continue",
                    classes="btn-primary",
                )
                yield Button(
                    "Skip Hardware Config",
                    variant="default",
                    id="btn-skip",
                )
                yield Button("Back", variant="default", id="btn-back")

    def on_mount(self) -> None:
        """Run hardware detection and display results."""
        from arches_installer.core.hardware import (
            detect_pci_ids,
            discover_machines,
            discover_quirks,
            get_dmi_info,
            match_quirks,
            resolve_hardware,
            suggest_machine,
        )

        # Detect hardware
        try:
            pci_ids = detect_pci_ids()
            dmi = get_dmi_info()
        except Exception:
            pci_ids = set()
            from arches_installer.core.hardware import DMIInfo

            dmi = DMIInfo()

        # Load and match
        try:
            all_quirks = discover_quirks()
            all_machines = discover_machines()
        except FileNotFoundError:
            all_quirks = []
            all_machines = []

        matched_quirks = match_quirks(all_quirks, pci_ids, dmi.chassis_type)
        suggested = suggest_machine(all_machines, dmi)

        # Store on app for pipeline
        hw = resolve_hardware(suggested, matched_quirks, all_quirks)
        self.app.hardware_config = hw

        # Build display text
        summary = self.query_one("#hw-summary", Static)
        text = ""

        if suggested:
            text += f"  Machine:  {suggested.name}  (auto-detected)\n"
            text += f"            {suggested.description}\n"
        else:
            text += "  Machine:  None detected\n"

        if matched_quirks or (suggested and suggested.quirk_includes):
            text += "\n  Hardware quirks:\n"
            seen = set()
            for q in hw.quirks:
                if q.slug not in seen:
                    text += f"    + {q.name}\n"
                    seen.add(q.slug)

        if hw.all_packages:
            text += f"\n  Packages:  {len(hw.all_packages)} additional\n"
        if hw.all_services:
            text += f"  Services:  {len(hw.all_services)} additional\n"
        if hw.all_firstboot_roles:
            text += f"  Ansible:   {', '.join(hw.all_firstboot_roles)} (first boot)\n"

        if not text.strip():
            text = "  No hardware-specific configuration detected.\n"

        summary.update(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-continue":
            self.app.push_screen("user_setup")
        elif event.button.id == "btn-skip":
            self.app.hardware_config = None
            self.app.push_screen("user_setup")
        elif event.button.id == "btn-back":
            self.app.pop_screen()
