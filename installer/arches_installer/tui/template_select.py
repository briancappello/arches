"""Template selection screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from arches_installer.core.template import InstallTemplate, discover_templates


class TemplateSelectScreen(Screen):
    """Screen for selecting an install template."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("Install Template", classes="title")
                yield Label(
                    "Select an install profile:",
                    classes="subtitle",
                )
                yield OptionList(id="template-list")
                yield Static(id="template-desc")
                yield Button(
                    "Continue",
                    variant="primary",
                    id="btn-continue",
                    classes="btn-primary",
                )
                yield Button("Back", variant="default", id="btn-back")

    def on_mount(self) -> None:
        """Load and display available templates."""
        template_list = self.query_one("#template-list", OptionList)
        self._templates: list[InstallTemplate] = []

        try:
            self._templates = discover_templates()
            for tmpl in self._templates:
                template_list.add_option(Option(tmpl.name, id=tmpl.name))
        except Exception as e:
            template_list.add_option(
                Option(f"Error loading templates: {e}", id="error")
            )

    def on_option_list_highlighted(
        self,
        event: OptionList.OptionHighlighted,
    ) -> None:
        """Show template description when highlighted."""
        desc = self.query_one("#template-desc", Static)
        if event.option_index is not None and self._templates:
            tmpl = self._templates[event.option_index]
            info = (
                f"{tmpl.description}\n\n"
                f"  Filesystem: {tmpl.disk.filesystem}\n"
                f"  Packages:   {len(tmpl.system.packages)} packages\n"
                f"  Snapshots:  {'Yes' if tmpl.bootloader.snapshot_boot else 'No'}"
            )
            desc.update(info)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-continue":
            template_list = self.query_one("#template-list", OptionList)
            if template_list.highlighted is not None and self._templates:
                self.app.selected_template = self._templates[template_list.highlighted]
                self.app.push_screen("user_setup")
        elif event.button.id == "btn-back":
            self.app.pop_screen()
