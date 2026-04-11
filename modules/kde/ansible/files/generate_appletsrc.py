#!/usr/bin/env python3
"""
generate_appletsrc.py — Generate plasma-org.kde.plasma.desktop-appletsrc

Reads a declarative panel/widget JSON config (produced by Ansible from
kde_panel in defaults/main.yml) and writes a valid KConfig INI file that
Plasma will load on first boot.

Key concepts:
  - A "Containment" is a top-level container (desktop, panel, systray).
  - An "Applet" lives inside a containment and has a unique numeric ID.
  - IDs must be globally unique positive integers across the whole file.
  - The System Tray widget is special: the visible applet references a
    *separate* containment (SystrayContainmentId) that holds the tray's
    internal applets.

Usage:
  python3 generate_appletsrc.py --config panel.json --output appletsrc
"""

from __future__ import annotations

import argparse
import json
import sys
from configparser import ConfigParser
from pathlib import Path


# ── Location / alignment / hiding maps ─────────────────────────────────────
LOCATION_MAP = {
    "top": 3,
    "bottom": 4,
    "left": 5,
    "right": 6,
}

ALIGNMENT_MAP = {
    "left": 1,
    "center": 4,
    "right": 2,
}

HIDING_MAP = {
    "none": 0,
    "autohide": 1,
    "dodgewindows": 2,
    "windowsgobelow": 3,
}


class IdAllocator:
    """Monotonically increasing ID allocator for containments and applets."""

    def __init__(self, start: int = 1) -> None:
        self._next = start

    def next(self) -> int:
        val = self._next
        self._next += 1
        return val


class AppletsrcWriter:
    """Build and write a plasma-org.kde.plasma.desktop-appletsrc file."""

    def __init__(self) -> None:
        self._ids = IdAllocator(start=1)
        # We accumulate sections as {section_header: {key: value, ...}}
        # Using a list of tuples to preserve ordering (ConfigParser sorts).
        self._sections: list[tuple[str, dict[str, str]]] = []

    def _add_section(self, header: str, entries: dict[str, str]) -> None:
        self._sections.append((header, entries))

    def build(self, config: dict) -> None:
        """Build the full appletsrc from the panel config dict."""
        # ── Desktop containment (Containment 1 — always present) ───────
        desktop_id = self._ids.next()
        self._add_section(
            f"Containments][{desktop_id}",
            {
                "activityId": "",
                "formfactor": "0",
                "immutability": "1",
                "lastScreen": "0",
                "location": "0",
                "plugin": "org.kde.desktopcontainment",
                "wallpaperplugin": "org.kde.image",
            },
        )

        # ── Panel containment ──────────────────────────────────────────
        panel_id = self._ids.next()
        location = LOCATION_MAP.get(config.get("location", "bottom"), 4)
        alignment = ALIGNMENT_MAP.get(config.get("alignment", "center"), 4)
        hiding = HIDING_MAP.get(config.get("hiding", "none"), 0)
        height = config.get("height", 44)
        floating = "true" if config.get("floating", False) else "false"

        self._add_section(
            f"Containments][{panel_id}",
            {
                "activityId": "",
                "formfactor": "2",
                "immutability": "1",
                "lastScreen": "0",
                "location": str(location),
                "plugin": "org.kde.panel",
                "wallpaperplugin": "",
            },
        )
        self._add_section(
            f"Containments][{panel_id}][General",
            {
                "AppletOrder": "",  # filled below
                "iconSize": "2",
                "floating": floating,
            },
        )

        # ── Applets inside the panel ───────────────────────────────────
        applet_ids: list[int] = []
        systray_containment_id: int | None = None
        widgets = config.get("widgets", [])

        for widget in widgets:
            plugin = widget["plugin"]

            if plugin == "org.kde.plasma.systemtray":
                # System tray needs its own sub-containment.
                systray_containment_id = self._ids.next()
                applet_id = self._ids.next()
                applet_ids.append(applet_id)

                # The applet entry inside the panel
                self._add_section(
                    f"Containments][{panel_id}][Applets][{applet_id}",
                    {
                        "immutability": "1",
                        "plugin": "org.kde.plasma.systemtray",
                    },
                )
                self._add_section(
                    f"Containments][{panel_id}][Applets][{applet_id}][Configuration",
                    {
                        "PreloadWeight": "100",
                        "SystrayContainmentId": str(systray_containment_id),
                    },
                )

                # The system tray containment itself
                self._add_section(
                    f"Containments][{systray_containment_id}",
                    {
                        "activityId": "",
                        "formfactor": "2",
                        "immutability": "1",
                        "lastScreen": "0",
                        "location": str(location),
                        "plugin": "org.kde.plasma.private.systemtray",
                    },
                )

                # Default system tray applets — Plasma auto-discovers most,
                # but we seed the common ones.
                default_tray_plugins = [
                    "org.kde.plasma.battery",
                    "org.kde.plasma.bluetooth",
                    "org.kde.plasma.clipboard",
                    "org.kde.plasma.devicenotifier",
                    "org.kde.plasma.manage-inputmethod",
                    "org.kde.plasma.mediacontroller",
                    "org.kde.plasma.networkmanagement",
                    "org.kde.plasma.notifications",
                    "org.kde.plasma.volume",
                    "org.kde.plasma.cameraindicator",
                    "org.kde.plasma.keyboardindicator",
                    "org.kde.plasma.printmanager",
                ]
                tray_applet_ids: list[int] = []
                for tray_plugin in default_tray_plugins:
                    tid = self._ids.next()
                    tray_applet_ids.append(tid)
                    self._add_section(
                        f"Containments][{systray_containment_id}][Applets][{tid}",
                        {
                            "immutability": "1",
                            "plugin": tray_plugin,
                        },
                    )

                self._add_section(
                    f"Containments][{systray_containment_id}][General",
                    {
                        "extraItems": ";".join(p for p in default_tray_plugins),
                        "knownItems": ";".join(p for p in default_tray_plugins),
                    },
                )
            else:
                # Regular applet
                applet_id = self._ids.next()
                applet_ids.append(applet_id)

                self._add_section(
                    f"Containments][{panel_id}][Applets][{applet_id}",
                    {
                        "immutability": "1",
                        "plugin": plugin,
                    },
                )

                # Write per-applet config groups
                widget_config = widget.get("config", {})
                for group_name, group_entries in widget_config.items():
                    section = (
                        f"Containments][{panel_id}][Applets][{applet_id}]"
                        f"[Configuration][{group_name}"
                    )
                    self._add_section(
                        section, {k: str(v) for k, v in group_entries.items()}
                    )

        # Fill in AppletOrder now that we have all IDs.
        for header, entries in self._sections:
            if header == f"Containments][{panel_id}][General":
                entries["AppletOrder"] = ";".join(str(a) for a in applet_ids)
                break

        # ── Panel geometry (ConfigDialog section) ──────────────────────
        self._add_section(
            f"Containments][{panel_id}][ConfigDialog",
            {
                "DialogHeight": "84",
                "DialogWidth": "1920",
            },
        )

        # Panel-level properties Plasma reads from a special section
        # In Plasma 6, panel properties like thickness and alignment are
        # stored under [Containments][N][General] which we already wrote,
        # but we also need to write minLength/maxLength/panelVisibility.
        for header, entries in self._sections:
            if header == f"Containments][{panel_id}][General":
                entries["panelVisibility"] = str(hiding)
                entries["thickness"] = str(height)
                entries["alignment"] = str(alignment)
                entries["lengthMode"] = "2"  # 2 = fill available space
                break

    def write(self, path: str | Path) -> None:
        """Write the appletsrc file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w") as f:
            for header, entries in self._sections:
                f.write(f"[{header}]\n")
                for key, value in entries.items():
                    f.write(f"{key}={value}\n")
                f.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate plasma-org.kde.plasma.desktop-appletsrc"
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to panel config JSON (from Ansible kde_panel variable)",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output path for the appletsrc file",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        return 1

    config = json.loads(config_path.read_text())

    writer = AppletsrcWriter()
    writer.build(config)
    writer.write(args.output)

    print(f"Generated: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
