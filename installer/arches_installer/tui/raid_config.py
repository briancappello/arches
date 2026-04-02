"""RAID configuration screen -- select backend, level, and disks."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from arches_installer.core.disk import BlockDevice, detect_block_devices
from arches_installer.core.disk_layout import RaidBackend, RaidConfig, RaidLevel


# RAID level descriptions shown in the UI
_RAID_LEVEL_INFO = {
    RaidLevel.RAID0: "Striped, no redundancy. Maximum capacity/speed. Any disk failure loses all data.",
    RaidLevel.RAID1: "Mirrored. Data duplicated on all disks. Can survive N-1 disk failures.",
    RaidLevel.RAID10: "Striped mirrors. Requires 4+ disks. Good balance of speed and redundancy.",
}

# Backend descriptions
_BACKEND_INFO = {
    RaidBackend.MDADM: "Linux software RAID (mdadm). Creates a virtual block device. Supports RAID 0/1/10.",
    RaidBackend.BTRFS: "btrfs native multi-device. Supports RAID 0/1/10 on data and metadata.",
}


class RaidConfigScreen(Screen):
    """Multi-step RAID configuration screen."""

    BINDINGS = [
        ("up", "prev_button", "Previous"),
        ("down", "next_button", "Next"),
    ]

    def action_next_button(self) -> None:
        self.focus_next(Button)

    def action_prev_button(self) -> None:
        self.focus_previous(Button)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._step = 1  # 1=backend, 2=level, 3=disks
        self._selected_backend: RaidBackend | None = None
        self._selected_level: RaidLevel | None = None
        self._selected_devices: list[str] = []
        self._devices: list[BlockDevice] = []

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("RAID Configuration", classes="title")
                yield Static(id="step-label")
                yield OptionList(id="raid-list")
                yield Static(id="raid-info")
                yield Static(id="raid-error")
                yield Button(
                    "Continue",
                    variant="primary",
                    id="btn-continue",
                    classes="btn-primary",
                )
                yield Button("Back", variant="default", id="btn-back")

    def on_mount(self) -> None:
        """Show step 1: backend selection."""
        self._show_step_1()

    def _show_step_1(self) -> None:
        """Step 1: Select RAID backend."""
        self._step = 1
        step_label = self.query_one("#step-label", Static)
        step_label.update("[bold]Step 1/3:[/bold] Select RAID backend")

        raid_list = self.query_one("#raid-list", OptionList)
        raid_list.clear_options()
        raid_list.add_option(Option("mdadm (Linux software RAID)", id="mdadm"))
        raid_list.add_option(Option("btrfs (native multi-device)", id="btrfs"))
        raid_list.highlighted = 0
        raid_list.focus()

        self.query_one("#raid-info", Static).update(_BACKEND_INFO[RaidBackend.MDADM])
        self.query_one("#raid-error", Static).update("")

    def _show_step_2(self) -> None:
        """Step 2: Select RAID level."""
        self._step = 2
        step_label = self.query_one("#step-label", Static)
        step_label.update(
            f"[bold]Step 2/3:[/bold] Select RAID level "
            f"(backend: {self._selected_backend.value})"
        )

        raid_list = self.query_one("#raid-list", OptionList)
        raid_list.clear_options()

        # All levels available for both backends
        available_levels = [RaidLevel.RAID0, RaidLevel.RAID1, RaidLevel.RAID10]

        for level in available_levels:
            raid_list.add_option(Option(f"RAID {level.value}", id=f"raid{level.value}"))
        raid_list.highlighted = 0
        raid_list.focus()

        self.query_one("#raid-info", Static).update(
            _RAID_LEVEL_INFO[available_levels[0]]
        )
        self.query_one("#raid-error", Static).update("")

    def _show_step_3(self) -> None:
        """Step 3: Select physical disks."""
        self._step = 3
        step_label = self.query_one("#step-label", Static)
        step_label.update(
            f"[bold]Step 3/3:[/bold] Select disks for "
            f"{self._selected_backend.value} "
            f"RAID{self._selected_level.value}"
        )

        raid_list = self.query_one("#raid-list", OptionList)
        raid_list.clear_options()
        self._selected_devices = []

        try:
            devices = detect_block_devices()
            self._devices = [d for d in devices if not d.removable]

            if len(self._devices) < 2:
                self.query_one("#raid-error", Static).update(
                    "[red]RAID requires at least 2 non-removable disks.[/red]"
                )
                return

            for dev in self._devices:
                # Prefix with [ ] for toggle-style selection
                raid_list.add_option(Option(f"[ ] {dev.display}", id=dev.path))
            raid_list.highlighted = 0
            raid_list.focus()
        except Exception as e:
            self.query_one("#raid-error", Static).update(
                f"[red]Error detecting disks: {e}[/red]"
            )

        # Show size warning and minimum disk count
        min_disks = 4 if self._selected_level == RaidLevel.RAID10 else 2
        info = f"Select at least {min_disks} disks. "
        info += "Press Enter to toggle selection."
        self.query_one("#raid-info", Static).update(info)
        self.query_one("#raid-error", Static).update("")

    def on_option_list_highlighted(
        self,
        event: OptionList.OptionHighlighted,
    ) -> None:
        """Update info panel when an option is highlighted."""
        if event.option_index is None:
            return

        info = self.query_one("#raid-info", Static)

        if self._step == 1:
            backends = [RaidBackend.MDADM, RaidBackend.BTRFS]
            if event.option_index < len(backends):
                info.update(_BACKEND_INFO[backends[event.option_index]])

        elif self._step == 2:
            available = [RaidLevel.RAID0, RaidLevel.RAID1, RaidLevel.RAID10]
            if event.option_index < len(available):
                info.update(_RAID_LEVEL_INFO[available[event.option_index]])

        elif self._step == 3 and self._devices:
            if event.option_index < len(self._devices):
                dev = self._devices[event.option_index]
                details = (
                    f"  {dev.path}  {dev.size}  {dev.model}\n"
                    f"  Selected: {len(self._selected_devices)} disk(s)"
                )
                info.update(details)

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        """Handle Enter press -- advance step or toggle disk selection."""
        if self._step == 1:
            self._handle_step_1_select(event.option_index)
        elif self._step == 2:
            self._handle_step_2_select(event.option_index)
        elif self._step == 3:
            self._toggle_disk(event.option_index)

    def _handle_step_1_select(self, index: int) -> None:
        """Select backend and advance to step 2."""
        backends = [RaidBackend.MDADM, RaidBackend.BTRFS]
        if index < len(backends):
            self._selected_backend = backends[index]
            self._show_step_2()

    def _handle_step_2_select(self, index: int) -> None:
        """Select RAID level and advance to step 3."""
        available = [RaidLevel.RAID0, RaidLevel.RAID1, RaidLevel.RAID10]
        if index < len(available):
            self._selected_level = available[index]
            self._show_step_3()

    def _toggle_disk(self, index: int) -> None:
        """Toggle a disk's selection state in step 3."""
        if index >= len(self._devices):
            return

        dev = self._devices[index]
        raid_list = self.query_one("#raid-list", OptionList)

        if dev.path in self._selected_devices:
            self._selected_devices.remove(dev.path)
            marker = "[ ]"
        else:
            self._selected_devices.append(dev.path)
            marker = "[x]"

        # Update the option text to show selection state
        raid_list.replace_option_prompt_at_index(index, f"{marker} {dev.display}")

        # Update info with disk sizes for mismatch warning
        self._update_disk_selection_info()

    def _update_disk_selection_info(self) -> None:
        """Update info panel with selection count and size warnings."""
        info = self.query_one("#raid-info", Static)
        count = len(self._selected_devices)

        # Check for size mismatches
        selected_devs = [d for d in self._devices if d.path in self._selected_devices]
        sizes = [d.size for d in selected_devs]
        unique_sizes = set(sizes)

        msg = f"Selected: {count} disk(s)"
        if len(unique_sizes) > 1:
            msg += (
                f"\n[yellow]Warning: disk sizes differ "
                f"({', '.join(f'{d.path}: {d.size}' for d in selected_devs)}). "
                f"Usable capacity will be limited to the smallest disk.[/yellow]"
            )
        info.update(msg)

    def _validate_and_build(self) -> bool:
        """Validate RAID config and store it on the app."""
        error = self.query_one("#raid-error", Static)

        if not self._selected_backend:
            error.update("[red]Select a RAID backend first.[/red]")
            return False
        if not self._selected_level:
            error.update("[red]Select a RAID level first.[/red]")
            return False

        min_disks = 4 if self._selected_level == RaidLevel.RAID10 else 2
        if len(self._selected_devices) < min_disks:
            error.update(
                f"[red]RAID {self._selected_level.value} requires at least "
                f"{min_disks} disks. Selected: {len(self._selected_devices)}.[/red]"
            )
            return False

        # Build and store the RAID config
        config = RaidConfig(
            level=self._selected_level,
            backend=self._selected_backend,
            devices=list(self._selected_devices),
        )
        self.app.raid_config = config
        # Set the primary device for the rest of the install flow
        self.app.selected_device = self._selected_devices[0]
        error.update("")
        return True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-continue":
            if self._step == 1:
                # Need to select via the option list
                raid_list = self.query_one("#raid-list", OptionList)
                if raid_list.highlighted is not None:
                    self._handle_step_1_select(raid_list.highlighted)
            elif self._step == 2:
                raid_list = self.query_one("#raid-list", OptionList)
                if raid_list.highlighted is not None:
                    self._handle_step_2_select(raid_list.highlighted)
            elif self._step == 3:
                if self._validate_and_build():
                    # Pop back to disk_select, which should then advance
                    # to layout_select since raid_config is set
                    self.app.pop_screen()
                    self.app.push_screen("layout_select")

        elif event.button.id == "btn-back":
            if self._step == 1:
                self.app.pop_screen()
            elif self._step == 2:
                self._show_step_1()
            elif self._step == 3:
                self._show_step_2()
