"""Tests for the NetworkScreen."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from textual.widgets import Button, Input, OptionList

from arches_installer.core.network import NetworkInterface, WifiNetwork
from arches_installer.tui.app import ArchesApp
from arches_installer.tui.network import (
    STEP_WIFI_SCAN,
    NetworkScreen,
)


FAKE_IFACES = [
    NetworkInterface(name="wlan0", type="wifi", connected=False, ip_address=""),
    NetworkInterface(
        name="eth0", type="ethernet", connected=True, ip_address="10.0.0.5"
    ),
]

SINGLE_WIFI_IFACE = [
    NetworkInterface(name="wlan0", type="wifi", connected=False, ip_address=""),
]

FAKE_WIFI_NETWORKS = [
    WifiNetwork(ssid="HomeNet", signal=85, security="WPA2", in_use=False),
    WifiNetwork(ssid="CoffeeShop", signal=55, security="WPA2 WPA3", in_use=False),
    WifiNetwork(ssid="OpenWifi", signal=30, security="--", in_use=False),
]


async def _wait_for_workers(pilot, iterations=20, delay=0.05):
    """Wait for background workers to complete."""
    for _ in range(iterations):
        await pilot.wait_for_animation()
        await asyncio.sleep(delay)


@patch("arches_installer.tui.welcome.check_connectivity", return_value=True)
@patch("arches_installer.tui.network.get_interfaces", return_value=FAKE_IFACES)
async def test_network_screen_renders(mock_ifaces, mock_conn) -> None:
    """Clicking btn-network should push the NetworkScreen."""
    app = ArchesApp(platform=_make_platform())
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.wait_for_animation()
        await pilot.click("#btn-network")
        await _wait_for_workers(pilot)

        assert isinstance(app.screen, NetworkScreen)


@patch("arches_installer.tui.welcome.check_connectivity", return_value=True)
@patch("arches_installer.tui.network.get_interfaces", return_value=SINGLE_WIFI_IFACE)
@patch("arches_installer.tui.network.scan_wifi", return_value=FAKE_WIFI_NETWORKS)
async def test_network_single_wifi_skips_to_scan(
    mock_scan, mock_ifaces, mock_conn
) -> None:
    """With only one wifi interface, the screen should skip to wifi scan step."""
    app = ArchesApp(platform=_make_platform())
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.wait_for_animation()
        await pilot.click("#btn-network")
        await _wait_for_workers(pilot)

        screen = app.screen
        assert isinstance(screen, NetworkScreen)
        assert screen.step == STEP_WIFI_SCAN


@patch("arches_installer.tui.welcome.check_connectivity", return_value=True)
@patch("arches_installer.tui.network.get_interfaces", return_value=SINGLE_WIFI_IFACE)
@patch("arches_installer.tui.network.scan_wifi", return_value=FAKE_WIFI_NETWORKS)
async def test_network_wifi_scan_populates_list(
    mock_scan, mock_ifaces, mock_conn
) -> None:
    """WiFi scan results should populate the wifi OptionList."""
    app = ArchesApp(platform=_make_platform())
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.wait_for_animation()
        await pilot.click("#btn-network")
        await _wait_for_workers(pilot)

        screen = app.screen
        assert isinstance(screen, NetworkScreen)
        wifi_list = screen.query_one("#wifi-list", OptionList)
        assert wifi_list.option_count == len(FAKE_WIFI_NETWORKS)


@patch("arches_installer.tui.welcome.check_connectivity", return_value=True)
@patch("arches_installer.tui.network.get_interfaces", return_value=SINGLE_WIFI_IFACE)
@patch("arches_installer.tui.network.scan_wifi", return_value=FAKE_WIFI_NETWORKS)
async def test_network_back_returns_to_welcome(
    mock_scan, mock_ifaces, mock_conn
) -> None:
    """Pressing Back from NetworkScreen should return to WelcomeScreen."""
    app = ArchesApp(platform=_make_platform())
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.wait_for_animation()
        await pilot.click("#btn-network")
        await _wait_for_workers(pilot)

        assert isinstance(app.screen, NetworkScreen)

        await pilot.click("#btn-back")
        await pilot.wait_for_animation()

        assert app.screen.__class__.__name__ == "WelcomeScreen"


@patch("arches_installer.tui.welcome.check_connectivity", return_value=True)
@patch("arches_installer.tui.network.get_interfaces", return_value=SINGLE_WIFI_IFACE)
@patch("arches_installer.tui.network.scan_wifi", return_value=FAKE_WIFI_NETWORKS)
@patch("arches_installer.tui.network.connect_wifi", return_value=(True, ""))
async def test_network_connect_success_pops_screen(
    mock_connect, mock_scan, mock_ifaces, mock_conn
) -> None:
    """Selecting a network, entering password, and connecting should pop back to Welcome."""
    app = ArchesApp(platform=_make_platform())
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.wait_for_animation()
        await pilot.click("#btn-network")
        await _wait_for_workers(pilot)

        screen = app.screen
        assert isinstance(screen, NetworkScreen)
        assert screen.step == STEP_WIFI_SCAN

        # Select the first wifi network (HomeNet) via button press
        wifi_list = screen.query_one("#wifi-list", OptionList)
        wifi_list.highlighted = 0
        screen.query_one("#btn-connect", Button).press()
        await _wait_for_workers(pilot)

        assert screen.step == "connect_details"

        # Type a password
        password_input = screen.query_one("#input-password", Input)
        password_input.value = "mysecretpass"

        # Click Connect to initiate the wifi connection
        screen.query_one("#btn-connect", Button).press()
        await _wait_for_workers(pilot)

        # Should have popped back to WelcomeScreen
        assert app.screen.__class__.__name__ == "WelcomeScreen"
        mock_connect.assert_called_once()


# --- Helpers ---


def _make_platform():
    """Build a minimal PlatformConfig for tests."""
    from arches_installer.core.platform import (
        BootloaderPlatformConfig,
        HardwareDetectionConfig,
        KernelConfig,
        KernelVariant,
        PlatformConfig,
    )

    return PlatformConfig(
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
