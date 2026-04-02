"""Disk layout selection screen -- choose a partition layout or go manual."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from arches_installer.core.disk_layout import DiskLayout, discover_disk_layouts

# Sentinel ID for the manual shell option at the bottom of the list
_MANUAL_OPTION_ID = "__manual_shell__"


class LayoutSelectScreen(Screen):
    """Screen for selecting a disk layout or dropping to manual shell."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("Disk Layout", classes="title")
                yield Label(
                    "Select a partition layout for your disk:",
                    classes="subtitle",
                )
                yield OptionList(id="layout-list")
                yield Static(id="layout-desc")
                yield Button(
                    "Continue",
                    variant="primary",
                    id="btn-continue",
                    classes="btn-primary",
                )
                yield Button("Back", variant="default", id="btn-back")

    def on_mount(self) -> None:
        """Load and display available disk layouts."""
        layout_list = self.query_one("#layout-list", OptionList)
        self._layouts: list[DiskLayout] = []

        try:
            self._layouts = discover_disk_layouts()
            for layout in self._layouts:
                layout_list.add_option(Option(layout.name, id=layout.name))
        except Exception as e:
            layout_list.add_option(Option(f"Error loading layouts: {e}", id="error"))

        # Add manual shell option at the bottom
        layout_list.add_option(Option("Open Shell (Manual)", id=_MANUAL_OPTION_ID))

        if self._layouts:
            layout_list.highlighted = 0
            layout_list.focus()
            # Show first layout description
            self._show_layout_info(0)

    def _show_layout_info(self, index: int) -> None:
        """Display partition table preview for a layout."""
        desc = self.query_one("#layout-desc", Static)

        if index >= len(self._layouts):
            # Manual option selected
            desc.update(
                "Drop to a shell and partition, format, and mount\n"
                "your disks manually onto /mnt. The installer will\n"
                "detect your layout when you return."
            )
            return

        layout = self._layouts[index]
        info = f"{layout.description}\n\n"
        info += "  Bootloaders: " + ", ".join(layout.bootloaders) + "\n\n"
        info += "  # | Filesystem | Size    | Mount       | Label\n"
        info += "  --+------------+---------+-------------+-----------\n"

        for i, part in enumerate(layout.partitions):
            fs = part.filesystem or "raw"
            size = part.size
            mp = part.mount_point or "(none)"
            label = part.label or ""
            info += f"  {i + 1} | {fs:<10} | {size:<7} | {mp:<11} | {label}\n"

            for sv in part.subvolumes:
                sv_mp = sv.mount_point or "(none)"
                info += f"    |   subvol: {sv.name:<8} -> {sv_mp}\n"

        desc.update(info)

    def on_option_list_highlighted(
        self,
        event: OptionList.OptionHighlighted,
    ) -> None:
        """Show layout description when highlighted."""
        if event.option_index is not None:
            self._show_layout_info(event.option_index)

    def _select_layout(self) -> None:
        """Accept the currently highlighted layout and advance."""
        layout_list = self.query_one("#layout-list", OptionList)
        if layout_list.highlighted is None:
            return

        index = layout_list.highlighted

        # Check if manual option was selected
        option = layout_list.get_option_at_index(index)
        if option.id == _MANUAL_OPTION_ID:
            self.app.push_screen("partition")
            return

        # Layout selected
        if index < len(self._layouts):
            self.app.selected_layout = self._layouts[index]
            self.app.partition_mode = "auto"
            self.app.push_screen("template_select")

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        """Enter pressed on an option -- select it and advance."""
        self._select_layout()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-continue":
            self._select_layout()
        elif event.button.id == "btn-back":
            self.app.pop_screen()
