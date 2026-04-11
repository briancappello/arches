"""Module selection screen -- customize modules before install."""

from __future__ import annotations

import dataclasses

from textual.app import ComposeResult
from textual.containers import Center, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Label, Static

from arches_installer.core.module import (
    CATEGORY_ORDER,
    Module,
    ModuleError,
    discover_modules,
    resolve_modules,
)
from arches_installer.core.template import AnsibleConfig

# Human-readable category labels for the TUI.
_CATEGORY_LABELS: dict[str, str] = {
    "base": "Base",
    "networking": "Networking",
    "desktop": "Desktop Environment",
    "dev-toolchain": "Development Toolchains",
    "topic": "Topics",
    "service": "Services",
}


class ModuleSelectScreen(Screen):
    """Screen for customizing which modules are included in the install."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("Customize Modules", classes="title")
                yield Label(
                    "Toggle modules to include in your installation:",
                    classes="subtitle",
                )
                yield VerticalScroll(id="module-scroll")
                yield Static(id="module-status")
                yield Button(
                    "Continue",
                    variant="primary",
                    id="btn-continue",
                    classes="btn-primary",
                )
                yield Button("Back", variant="default", id="btn-back")

    def on_mount(self) -> None:
        """Load modules and populate checkboxes grouped by category."""
        self._all_modules: list[Module] = []
        self._checkboxes: dict[str, Checkbox] = {}
        scroll = self.query_one("#module-scroll", VerticalScroll)

        try:
            self._all_modules = discover_modules()
        except Exception as e:
            scroll.mount(Static(f"Error loading modules: {e}"))
            return

        # Pre-selected slugs from the template
        pre_selected: set[str] = set()
        if self.app.selected_template:
            pre_selected = set(self.app.selected_template.module_slugs)

        # Group modules by category
        by_category: dict[str, list[Module]] = {}
        for mod in self._all_modules:
            by_category.setdefault(mod.category, []).append(mod)

        # Build UI grouped by category order
        for cat in CATEGORY_ORDER:
            mods = by_category.get(cat, [])
            if not mods:
                continue

            label_text = _CATEGORY_LABELS.get(cat, cat.title())
            scroll.mount(Label(f"  [{label_text}]"))

            for mod in mods:
                checked = mod.slug in pre_selected
                cb = Checkbox(
                    f"{mod.name} -- {mod.description}",
                    value=checked,
                    id=f"mod-{mod.slug}",
                )
                self._checkboxes[mod.slug] = cb
                scroll.mount(cb)

        self._update_status()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Validate on every toggle and update the status display."""
        self._update_status()

    def _selected_slugs(self) -> list[str]:
        """Return the list of currently checked module slugs."""
        return [slug for slug, cb in self._checkboxes.items() if cb.value]

    def _update_status(self) -> None:
        """Validate current selection and update the status line."""
        status = self.query_one("#module-status", Static)
        slugs = self._selected_slugs()

        if not slugs:
            status.update("[yellow]No modules selected.[/yellow]")
            return

        try:
            resolved = resolve_modules(slugs, available=self._all_modules)
            n_pkg = len(resolved.merged_install().all_packages)
            n_svc = len(resolved.merged_services())
            n_roles = len(resolved.ansible_roles)
            status.update(
                f"  {len(slugs)} modules | "
                f"{n_pkg} packages | "
                f"{n_svc} services | "
                f"{n_roles} ansible roles"
            )
        except ModuleError as e:
            status.update(f"[red]{e}[/red]")

    def _apply_selection(self) -> None:
        """Resolve modules, update the template, and advance."""
        slugs = self._selected_slugs()

        if not slugs:
            return

        try:
            resolved = resolve_modules(slugs, available=self._all_modules)
        except ModuleError:
            # Status already shows the error
            return

        # Update the template with the resolved module data
        template = self.app.selected_template
        if template is not None:
            self.app.selected_template = dataclasses.replace(
                template,
                module_slugs=slugs,
                install=resolved.merged_install(),
                services=resolved.merged_services(),
                ansible=AnsibleConfig(firstboot_roles=resolved.ansible_roles),
                graphical=resolved.graphical,
            )

        self.app.push_screen("hardware_confirm")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-continue":
            self._apply_selection()
        elif event.button.id == "btn-back":
            self.app.pop_screen()
