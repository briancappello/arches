"""Arches installer TUI screens."""

from __future__ import annotations

from textual.screen import Screen
from textual.widgets import Button


class ArrowNavScreen(Screen):
    """Screen subclass that adds up/down arrow navigation between buttons."""

    BINDINGS = [
        ("up", "prev_button", "Previous"),
        ("down", "next_button", "Next"),
    ]

    def action_next_button(self) -> None:
        self.focus_next(Button)

    def action_prev_button(self) -> None:
        self.focus_previous(Button)
