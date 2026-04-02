"""Tests for network scanning, connection, and profile copying."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from arches_installer.core.network import (
    StaticIPConfig,
    check_connectivity,
    connect_ethernet_static,
    connect_wifi,
    copy_network_profiles,
    get_interfaces,
    scan_wifi,
)

MODULE = "arches_installer.core.network"


# ---------------------------------------------------------------------------
# scan_wifi()
# ---------------------------------------------------------------------------

NMCLI_WIFI_OUTPUT = "HomeNetwork:85:WPA2: \nCoffeeShop:62:WPA2:*\nOpenNet:40:--: \n"


@patch(f"{MODULE}.subprocess.run")
def test_scan_wifi_parses_nmcli(mock_run: MagicMock) -> None:
    """nmcli output is parsed into WifiNetwork objects."""
    # First call is rescan (ignore), second is the actual list
    rescan = MagicMock(returncode=0)
    listing = MagicMock(returncode=0, stdout=NMCLI_WIFI_OUTPUT)
    mock_run.side_effect = [rescan, listing]

    networks = scan_wifi()

    assert len(networks) == 3
    # CoffeeShop is in_use so it sorts first, then HomeNetwork (85), then OpenNet (40)
    assert networks[0].ssid == "CoffeeShop"
    assert networks[0].in_use is True
    assert networks[0].signal == 62
    assert networks[0].security == "WPA2"

    assert networks[1].ssid == "HomeNetwork"
    assert networks[1].signal == 85
    assert networks[1].in_use is False

    assert networks[2].ssid == "OpenNet"
    assert networks[2].signal == 40
    assert networks[2].security == "--"


@patch(f"{MODULE}.subprocess.run")
def test_scan_wifi_deduplicates_ssids(mock_run: MagicMock) -> None:
    """Duplicate SSIDs keep only the first occurrence."""
    listing = MagicMock(
        returncode=0,
        stdout="DupeNet:90:WPA2: \nDupeNet:70:WPA2: \nOther:50:--: \n",
    )
    mock_run.side_effect = [MagicMock(), listing]

    networks = scan_wifi()

    ssids = [n.ssid for n in networks]
    assert ssids.count("DupeNet") == 1
    assert len(networks) == 2


@patch(f"{MODULE}.subprocess.run")
def test_scan_wifi_skips_hidden_networks(mock_run: MagicMock) -> None:
    """Lines with empty SSID are excluded."""
    listing = MagicMock(
        returncode=0,
        stdout=":55:WPA2: \nVisible:80:WPA2: \n",
    )
    mock_run.side_effect = [MagicMock(), listing]

    networks = scan_wifi()

    assert len(networks) == 1
    assert networks[0].ssid == "Visible"


@patch(f"{MODULE}.subprocess.run")
def test_scan_wifi_sorted_by_signal(mock_run: MagicMock) -> None:
    """Networks sort with in_use first, then by descending signal."""
    listing = MagicMock(
        returncode=0,
        stdout="Weak:20:WPA2: \nStrong:90:WPA2: \nActive:50:WPA2:*\n",
    )
    mock_run.side_effect = [MagicMock(), listing]

    networks = scan_wifi()

    assert networks[0].ssid == "Active"
    assert networks[1].ssid == "Strong"
    assert networks[2].ssid == "Weak"


@patch(f"{MODULE}.subprocess.run", side_effect=FileNotFoundError("nmcli"))
def test_scan_wifi_empty_on_error(mock_run: MagicMock) -> None:
    """Returns empty list when subprocess raises."""
    assert scan_wifi() == []


# ---------------------------------------------------------------------------
# get_interfaces()
# ---------------------------------------------------------------------------

NMCLI_DEV_OUTPUT = (
    "wlan0:wifi:connected\n"
    "enp3s0:ethernet:unavailable\n"
    "lo:loopback:connected (externally)\n"
    "p2p-dev-wlan0:wifi-p2p:disconnected\n"
)


@patch(f"{MODULE}.subprocess.run")
def test_get_interfaces_parses_output(mock_run: MagicMock) -> None:
    """nmcli device output is parsed into NetworkInterface objects."""

    def _side_effect(cmd, **kwargs):
        if "dev" in cmd and "show" not in cmd:
            # nmcli -t -f DEVICE,TYPE,STATE dev
            return MagicMock(returncode=0, stdout=NMCLI_DEV_OUTPUT)
        if "show" in cmd and "wlan0" in cmd:
            # nmcli -t -f IP4.ADDRESS dev show wlan0
            return MagicMock(returncode=0, stdout="IP4.ADDRESS[1]:192.168.1.50/24\n")
        return MagicMock(returncode=0, stdout="")

    mock_run.side_effect = _side_effect

    ifaces = get_interfaces()

    # loopback, wifi-p2p filtered out
    assert len(ifaces) == 2

    wifi = next(i for i in ifaces if i.name == "wlan0")
    assert wifi.type == "wifi"
    assert wifi.connected is True
    assert wifi.ip_address == "192.168.1.50/24"

    eth = next(i for i in ifaces if i.name == "enp3s0")
    assert eth.type == "ethernet"
    assert eth.connected is False
    assert eth.ip_address == ""


@patch(f"{MODULE}.subprocess.run")
def test_get_interfaces_filters_loopback(mock_run: MagicMock) -> None:
    """Loopback, bridge, dummy, tun, and wifi-p2p interfaces are excluded."""

    def _side_effect(cmd, **kwargs):
        if "dev" in cmd and "show" not in cmd:
            return MagicMock(
                returncode=0,
                stdout=(
                    "lo:loopback:connected (externally)\n"
                    "br0:bridge:connected\n"
                    "dummy0:dummy:disconnected\n"
                    "tun0:tun:connected\n"
                    "p2p-dev-wlan0:wifi-p2p:disconnected\n"
                    "eth0:ethernet:connected\n"
                ),
            )
        if "show" in cmd and "eth0" in cmd:
            return MagicMock(returncode=0, stdout="IP4.ADDRESS[1]:192.168.1.10/24\n")
        return MagicMock(returncode=0, stdout="")

    mock_run.side_effect = _side_effect

    ifaces = get_interfaces()

    assert len(ifaces) == 1
    assert ifaces[0].name == "eth0"


# ---------------------------------------------------------------------------
# connect_wifi()
# ---------------------------------------------------------------------------


@patch(f"{MODULE}.subprocess.run")
def test_connect_wifi_dhcp_success(mock_run: MagicMock) -> None:
    """Successful DHCP wifi connection returns (True, '')."""
    mock_run.return_value = MagicMock(returncode=0)

    ok, err = connect_wifi("TestSSID")

    assert ok is True
    assert err == ""
    # Verify the command
    call_args = mock_run.call_args_list[0][0][0]
    assert call_args == ["nmcli", "dev", "wifi", "connect", "TestSSID"]


@patch(f"{MODULE}.subprocess.run")
def test_connect_wifi_with_password(mock_run: MagicMock) -> None:
    """Password is passed in the nmcli command args."""
    mock_run.return_value = MagicMock(returncode=0)

    connect_wifi("SecureNet", psk="s3cretPass!")

    call_args = mock_run.call_args_list[0][0][0]
    assert "password" in call_args
    assert "s3cretPass!" in call_args


@patch(f"{MODULE}._set_connection_dns")
@patch(f"{MODULE}.subprocess.run")
def test_connect_wifi_static_ip(mock_run: MagicMock, mock_dns: MagicMock) -> None:
    """Static IP config adds ip4/gw4 arguments to the command."""
    mock_run.return_value = MagicMock(returncode=0)
    static = StaticIPConfig(
        ip_cidr="192.168.1.100/24",
        gateway="192.168.1.1",
        dns=["1.1.1.1"],
    )

    ok, err = connect_wifi("StaticNet", psk="pass", static_ip=static)

    assert ok is True
    call_args = mock_run.call_args_list[0][0][0]
    assert "ip4" in call_args
    assert "192.168.1.100/24" in call_args
    assert "gw4" in call_args
    assert "192.168.1.1" in call_args
    # DNS is set separately
    mock_dns.assert_called_once_with("StaticNet", ["1.1.1.1"])


@patch(f"{MODULE}.subprocess.run")
def test_connect_wifi_failure(mock_run: MagicMock) -> None:
    """Non-zero exit returns (False, error_message)."""
    mock_run.return_value = MagicMock(
        returncode=1,
        stderr="Error: No network with SSID 'Bad' found.",
        stdout="",
    )

    ok, err = connect_wifi("Bad")

    assert ok is False
    assert "No network with SSID" in err


@patch(
    f"{MODULE}.subprocess.run",
    side_effect=subprocess.TimeoutExpired(cmd="nmcli", timeout=30),
)
def test_connect_wifi_timeout(mock_run: MagicMock) -> None:
    """TimeoutExpired returns (False, 'Connection timed out.')."""
    ok, err = connect_wifi("SlowNet")

    assert ok is False
    assert "timed out" in err.lower()


# ---------------------------------------------------------------------------
# connect_ethernet_static()
# ---------------------------------------------------------------------------


@patch(f"{MODULE}.subprocess.run")
def test_connect_ethernet_static_success(mock_run: MagicMock) -> None:
    """Successful static ethernet connection runs add then up."""
    # delete (ignore), add (success), up (success)
    mock_run.side_effect = [
        MagicMock(returncode=0),  # delete
        MagicMock(returncode=0, stderr="", stdout=""),  # add
        MagicMock(returncode=0, stderr="", stdout=""),  # up
    ]
    static = StaticIPConfig(
        ip_cidr="10.0.0.50/24",
        gateway="10.0.0.1",
        dns=["1.1.1.1", "8.8.8.8"],
    )

    ok, err = connect_ethernet_static("eth0", static)

    assert ok is True
    assert err == ""

    # Verify the add command
    add_args = mock_run.call_args_list[1][0][0]
    assert "con" in add_args and "add" in add_args
    assert "10.0.0.50/24" in add_args
    assert "10.0.0.1" in add_args
    assert "ipv4.dns" in add_args

    # Verify the up command
    up_args = mock_run.call_args_list[2][0][0]
    assert up_args == ["nmcli", "con", "up", "arches-eth0"]


@patch(f"{MODULE}.subprocess.run")
def test_connect_ethernet_static_failure(mock_run: MagicMock) -> None:
    """Failure on 'up' command returns (False, error)."""
    mock_run.side_effect = [
        MagicMock(returncode=0),  # delete
        MagicMock(returncode=0, stderr="", stdout=""),  # add
        MagicMock(returncode=1, stderr="Error: activation failed.", stdout=""),  # up
    ]
    static = StaticIPConfig(ip_cidr="10.0.0.50/24", gateway="10.0.0.1")

    ok, err = connect_ethernet_static("eth0", static)

    assert ok is False
    assert "activation failed" in err


# ---------------------------------------------------------------------------
# check_connectivity()
# ---------------------------------------------------------------------------


@patch(f"{MODULE}.subprocess.run")
def test_check_connectivity_success(mock_run: MagicMock) -> None:
    """curl returning 0 means connectivity."""
    mock_run.return_value = MagicMock(returncode=0)

    assert check_connectivity() is True


@patch(
    f"{MODULE}.subprocess.run",
    side_effect=subprocess.CalledProcessError(returncode=7, cmd="curl"),
)
def test_check_connectivity_failure(mock_run: MagicMock) -> None:
    """curl non-zero (CalledProcessError) means no connectivity."""
    assert check_connectivity() is False


# ---------------------------------------------------------------------------
# copy_network_profiles()
# ---------------------------------------------------------------------------


@patch("arches_installer.core.run._log")
def test_copy_network_profiles(mock_log: MagicMock, tmp_path: Path) -> None:
    """NM profiles are copied to target with 0o600 permissions."""
    # Set up source directory with .nmconnection files
    src = tmp_path / "live" / "etc" / "NetworkManager" / "system-connections"
    src.mkdir(parents=True)
    (src / "WiFi-Home.nmconnection").write_text("[connection]\nid=Home\n")
    (src / "Wired.nmconnection").write_text("[connection]\nid=Wired\n")

    # Set up target directory
    target_root = tmp_path / "target"
    target_root.mkdir()
    nm_target_dir = target_root / "etc" / "NetworkManager" / "system-connections"

    # Patch Path() construction for nm_live and MOUNT_ROOT for nm_target
    with (
        patch(f"{MODULE}.Path", return_value=src),
        patch(f"{MODULE}.MOUNT_ROOT", target_root),
    ):
        copy_network_profiles(log=None)

    assert nm_target_dir.exists()
    copied = sorted(f.name for f in nm_target_dir.iterdir())
    assert copied == ["WiFi-Home.nmconnection", "Wired.nmconnection"]

    for f in nm_target_dir.iterdir():
        assert f.stat().st_mode & 0o777 == 0o600


@patch("arches_installer.core.run._log")
def test_copy_network_profiles_no_source(mock_log: MagicMock, tmp_path: Path) -> None:
    """If source NM directory doesn't exist, function is a no-op."""
    with patch(f"{MODULE}.Path") as mock_path:
        fake_src = MagicMock()
        fake_src.exists.return_value = False
        mock_path.return_value = fake_src

        # Should return without error
        copy_network_profiles(log=None)

    mock_log.assert_not_called()
