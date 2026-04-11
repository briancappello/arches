"""Tests for the ModuleSelectScreen."""

from __future__ import annotations

from unittest.mock import patch

from textual.widgets import Checkbox

from arches_installer.core.module import Module
from arches_installer.core.platform import (
    BootloaderPlatformConfig,
    HardwareDetectionConfig,
    KernelConfig,
    KernelVariant,
    PlatformConfig,
)
from arches_installer.core.template import (
    AnsibleConfig,
    InstallPhases,
    InstallTemplate,
    SystemConfig,
)
from arches_installer.tui.app import ArchesApp

FAKE_MODULES = [
    Module(
        slug="base",
        name="Base System",
        description="Core system",
        category="base",
        install=InstallPhases(pacstrap=["base-devel", "git"]),
        has_ansible_role=True,
    ),
    Module(
        slug="zsh",
        name="Zsh",
        description="Zsh shell",
        category="base",
        install=InstallPhases(pacstrap=["zsh"]),
        has_ansible_role=True,
        requires=["base"],
    ),
    Module(
        slug="networking",
        name="Networking",
        description="NetworkManager",
        category="networking",
        install=InstallPhases(pacstrap=["networkmanager"]),
        services=["NetworkManager"],
    ),
    Module(
        slug="kde",
        name="KDE Plasma",
        description="KDE desktop",
        category="desktop",
        install=InstallPhases(pacstrap=["plasma-meta", "sddm"]),
        services=["sddm"],
        has_ansible_role=True,
        conflicts=["cosmic"],
    ),
    Module(
        slug="rust",
        name="Rust",
        description="Rust toolchain",
        category="dev-toolchain",
        install=InstallPhases(pacstrap=["rustup"]),
    ),
]

FAKE_TEMPLATE = InstallTemplate(
    name="Dev Workstation",
    description="Test template",
    system=SystemConfig(),
    install=InstallPhases(pacstrap=["git", "plasma-meta", "sddm", "networkmanager"]),
    services=["NetworkManager", "sddm"],
    ansible=AnsibleConfig(firstboot_roles=["base", "kde"]),
    graphical=True,
    module_slugs=["base", "zsh", "networking", "kde"],
)

TEST_PLATFORM = PlatformConfig(
    name="x86-64",
    description="test",
    arch="x86_64",
    kernel=KernelConfig(
        variants=[
            KernelVariant(package="linux-cachyos", headers="linux-cachyos-headers")
        ]
    ),
    bootloader=BootloaderPlatformConfig(),
    hardware_detection=HardwareDetectionConfig(),
)


def _setup_app() -> ArchesApp:
    """Create app with template pre-selected and push to module screen."""
    app = ArchesApp(platform=TEST_PLATFORM)
    app.selected_template = FAKE_TEMPLATE
    app.push_screen_on_mount = "module_select"
    return app


@patch(
    "arches_installer.tui.module_select.discover_modules",
    return_value=FAKE_MODULES,
)
async def test_module_screen_shows_checkboxes(mock_discover) -> None:
    """Module screen should show a checkbox for each available module."""
    app = _setup_app()
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.wait_for_animation()

        assert app.screen.__class__.__name__ == "ModuleSelectScreen"

        # Should have checkboxes for all modules
        checkboxes = app.screen.query(Checkbox)
        assert len(checkboxes) == len(FAKE_MODULES)


@patch(
    "arches_installer.tui.module_select.discover_modules",
    return_value=FAKE_MODULES,
)
async def test_module_screen_pre_selects_template_modules(mock_discover) -> None:
    """Modules from the template should be pre-checked."""
    app = _setup_app()
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.wait_for_animation()

        # base, zsh, networking, kde should be checked (from template)
        for slug in ["base", "zsh", "networking", "kde"]:
            cb = app.screen.query_one(f"#mod-{slug}", Checkbox)
            assert cb.value is True, f"Expected {slug} to be checked"

        # rust should NOT be checked (not in template)
        rust_cb = app.screen.query_one("#mod-rust", Checkbox)
        assert rust_cb.value is False


@patch(
    "arches_installer.tui.module_select.discover_modules",
    return_value=FAKE_MODULES,
)
async def test_module_screen_continue_updates_template(mock_discover) -> None:
    """Clicking Continue should update the template with resolved modules."""
    app = _setup_app()
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.wait_for_animation()

        await pilot.click("#btn-continue")

        # Should have advanced to hardware_confirm
        assert app.screen.__class__.__name__ == "HardwareConfirmScreen"

        # Template should still have the modules
        assert app.selected_template is not None
        assert "base" in app.selected_template.module_slugs
        assert "kde" in app.selected_template.module_slugs


@patch(
    "arches_installer.tui.module_select.discover_modules",
    return_value=FAKE_MODULES,
)
async def test_module_screen_back_pops(mock_discover) -> None:
    """Back button should pop the module select screen."""
    app = _setup_app()
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.wait_for_animation()
        assert app.screen.__class__.__name__ == "ModuleSelectScreen"

        await pilot.click("#btn-back")
        assert app.screen.__class__.__name__ != "ModuleSelectScreen"


@patch(
    "arches_installer.tui.module_select.discover_modules",
    return_value=FAKE_MODULES,
)
async def test_module_screen_toggle_adds_module(mock_discover) -> None:
    """Toggling a module checkbox should include it in the selection."""
    app = _setup_app()
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.wait_for_animation()

        # Rust is not checked initially
        rust_cb = app.screen.query_one("#mod-rust", Checkbox)
        assert rust_cb.value is False

        # Toggle it on
        rust_cb.value = True
        await pilot.wait_for_animation()

        # Click continue
        await pilot.click("#btn-continue")

        assert app.selected_template is not None
        assert "rust" in app.selected_template.module_slugs
