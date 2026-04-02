"""Network utilities — WiFi scanning, connection, and connectivity checks.

Thin wrappers around ``nmcli`` that return structured data. All functions
are designed to be called from both the TUI (via Textual workers) and the
auto-install path.

Diagnostic output is written to ``/var/log/arches-network.log`` for
debugging on the live ISO (``cat /var/log/arches-network.log``).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from arches_installer.core.disk import MOUNT_ROOT

# ─── Diagnostic log ──────────────────────────────────
# Append-only log for debugging network issues on the live ISO.
# Not the main install log — this is specifically for nmcli interactions.

_NET_LOG = Path("/var/log/arches-network.log")


def _net_log(msg: str) -> None:
    """Append a timestamped message to the network diagnostic log."""
    try:
        _NET_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_NET_LOG, "a") as f:
            ts = datetime.now().strftime("%H:%M:%S")
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


def _log_cmd(cmd: list[str], result: subprocess.CompletedProcess | None = None) -> None:
    """Log a command and its result."""
    _net_log(f"CMD: {' '.join(cmd)}")
    if result is not None:
        _net_log(f"  rc={result.returncode}")
        if result.stdout:
            for line in result.stdout.strip().splitlines()[:20]:
                _net_log(f"  stdout: {line}")
        if result.stderr:
            for line in result.stderr.strip().splitlines()[:10]:
                _net_log(f"  stderr: {line}")


# ─── Data classes ─────────────────────────────────────


@dataclass
class WifiNetwork:
    """A WiFi network discovered by scanning."""

    ssid: str
    signal: int  # 0–100
    security: str  # "WPA2", "WPA3", "WPA2 WPA3", "--" (open)
    in_use: bool


@dataclass
class NetworkInterface:
    """A network interface on the system."""

    name: str  # e.g. "wlan0", "eth0", "enp3s0"
    type: str  # "wifi" or "ethernet"
    connected: bool
    ip_address: str  # current IP or ""


@dataclass
class StaticIPConfig:
    """Static IP configuration for a network connection."""

    ip_cidr: str  # e.g. "192.168.1.50/24"
    gateway: str  # e.g. "192.168.1.1"
    dns: list[str] = field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"])


# ─── Scanning ─────────────────────────────────────────


def scan_wifi() -> list[WifiNetwork]:
    """Scan for available WiFi networks using nmcli.

    Returns a list of ``WifiNetwork`` sorted by signal strength (strongest
    first). Hidden networks (empty SSID) are excluded.
    """
    _net_log("scan_wifi() called")
    try:
        # Trigger a fresh scan first (best-effort, may fail without root)
        rescan_cmd = ["nmcli", "dev", "wifi", "rescan"]
        rescan_result = subprocess.run(
            rescan_cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        _log_cmd(rescan_cmd, rescan_result)
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        _net_log(f"  rescan failed: {e}")

    try:
        cmd = [
            "nmcli",
            "-t",
            "-f",
            "SSID,SIGNAL,SECURITY,IN-USE",
            "dev",
            "wifi",
            "list",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        _log_cmd(cmd, result)
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        _net_log(f"  wifi list failed: {e}")
        return []

    networks: list[WifiNetwork] = []
    seen_ssids: set[str] = set()

    for line in result.stdout.strip().splitlines():
        # nmcli -t uses : as delimiter.  SSID can contain colons (rare),
        # but the last 3 fields are always SIGNAL:SECURITY:IN-USE.
        # Split from the right to handle SSIDs with colons.
        parts = line.rsplit(":", 3)
        if len(parts) < 4:
            continue

        ssid, signal_str, security, in_use = parts
        ssid = ssid.strip()
        if not ssid:
            continue  # skip hidden networks
        if ssid in seen_ssids:
            continue  # deduplicate
        seen_ssids.add(ssid)

        try:
            signal = int(signal_str.strip())
        except ValueError:
            signal = 0

        networks.append(
            WifiNetwork(
                ssid=ssid,
                signal=signal,
                security=security.strip() or "--",
                in_use=in_use.strip() == "*",
            )
        )

    networks.sort(key=lambda n: (-n.in_use, -n.signal))
    _net_log(f"  found {len(networks)} networks: {[n.ssid for n in networks]}")
    return networks


# ─── Interface detection ──────────────────────────────


def get_interfaces() -> list[NetworkInterface]:
    """Return a list of non-loopback network interfaces."""
    _net_log("get_interfaces() called")
    cmd = ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "dev"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        _log_cmd(cmd, result)
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        _net_log(f"  nmcli dev failed: {e}")
        return []

    if result.returncode != 0:
        _net_log(f"  non-zero exit: {result.returncode}")
        return []

    interfaces: list[NetworkInterface] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue

        name = parts[0].strip()
        iface_type = parts[1].strip()
        state = parts[2].strip()

        # Skip loopback, p2p, and non-physical
        if iface_type in ("loopback", "bridge", "dummy", "tun", "wifi-p2p"):
            _net_log(f"  skipping {name} (type={iface_type})")
            continue

        # Get IP address for connected interfaces
        ip_addr = ""
        if state == "connected":
            ip_addr = _get_interface_ip(name)

        interfaces.append(
            NetworkInterface(
                name=name,
                type=iface_type,
                connected=state == "connected",
                ip_address=ip_addr,
            )
        )

    _net_log(
        f"  returning {len(interfaces)} interfaces: {[i.name for i in interfaces]}"
    )
    return interfaces


def _get_interface_ip(device: str) -> str:
    """Get the IPv4 address of a connected interface."""
    cmd = ["nmcli", "-t", "-f", "IP4.ADDRESS", "dev", "show", device]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        _log_cmd(cmd, result)
        for line in result.stdout.strip().splitlines():
            # Format: IP4.ADDRESS[1]:192.168.1.50/24
            if ":" in line:
                ip = line.split(":", 1)[1].strip()
                _net_log(f"  IP for {device}: {ip}")
                return ip
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        _net_log(f"  IP lookup failed for {device}: {e}")
    return ""


# ─── Connection ───────────────────────────────────────


def connect_wifi(
    ssid: str,
    psk: str | None = None,
    static_ip: StaticIPConfig | None = None,
) -> tuple[bool, str]:
    """Connect to a WiFi network via nmcli.

    Returns ``(success, error_message)``.
    """
    _net_log(
        f"connect_wifi(ssid={ssid!r}, has_psk={psk is not None}, static={static_ip})"
    )
    cmd = ["nmcli", "dev", "wifi", "connect", ssid]
    if psk:
        cmd += ["password", psk]

    if static_ip:
        cmd += [
            "ip4",
            static_ip.ip_cidr,
            "gw4",
            static_ip.gateway,
        ]

    # Log command without the actual password
    safe_cmd = [c if c != psk else "***" for c in cmd]
    _net_log(f"  CMD: {' '.join(safe_cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        _log_cmd(safe_cmd, result)
        if result.returncode == 0:
            # If static IP with custom DNS, configure it separately
            if static_ip and static_ip.dns:
                _set_connection_dns(ssid, static_ip.dns)
            _net_log("  connect SUCCESS")
            return True, ""
        err = result.stderr.strip() or result.stdout.strip()
        _net_log(f"  connect FAILED: {err}")
        return False, err
    except subprocess.TimeoutExpired:
        _net_log("  connect TIMEOUT")
        return False, "Connection timed out."
    except FileNotFoundError:
        _net_log("  nmcli not found")
        return False, "nmcli not found."


def connect_ethernet_static(
    iface: str,
    static_ip: StaticIPConfig,
) -> tuple[bool, str]:
    """Configure a wired interface with a static IP via nmcli.

    Returns ``(success, error_message)``.
    """
    _net_log(f"connect_ethernet_static(iface={iface!r}, static={static_ip})")
    con_name = f"arches-{iface}"

    # Remove existing connection with the same name (if any)
    del_cmd = ["nmcli", "con", "delete", con_name]
    try:
        del_result = subprocess.run(del_cmd, capture_output=True, text=True, timeout=10)
        _log_cmd(del_cmd, del_result)
    except subprocess.SubprocessError:
        pass

    dns_str = " ".join(static_ip.dns) if static_ip.dns else ""
    cmd = [
        "nmcli",
        "con",
        "add",
        "type",
        "ethernet",
        "ifname",
        iface,
        "con-name",
        con_name,
        "ip4",
        static_ip.ip_cidr,
        "gw4",
        static_ip.gateway,
    ]
    if dns_str:
        cmd += ["ipv4.dns", dns_str]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        _log_cmd(cmd, result)
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            _net_log(f"  add FAILED: {err}")
            return False, err

        # Bring the connection up
        up_cmd = ["nmcli", "con", "up", con_name]
        up_result = subprocess.run(
            up_cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        _log_cmd(up_cmd, up_result)
        if up_result.returncode == 0:
            _net_log("  ethernet connect SUCCESS")
            return True, ""
        err = up_result.stderr.strip() or up_result.stdout.strip()
        _net_log(f"  ethernet up FAILED: {err}")
        return False, err
    except subprocess.TimeoutExpired:
        _net_log("  ethernet connect TIMEOUT")
        return False, "Connection timed out."
    except FileNotFoundError:
        _net_log("  nmcli not found")
        return False, "nmcli not found."


def _set_connection_dns(con_name: str, dns: list[str]) -> None:
    """Set DNS servers on an existing NM connection."""
    try:
        subprocess.run(
            [
                "nmcli",
                "con",
                "modify",
                con_name,
                "ipv4.dns",
                " ".join(dns),
            ],
            capture_output=True,
            timeout=10,
        )
        # Re-up the connection to apply
        subprocess.run(
            ["nmcli", "con", "up", con_name],
            capture_output=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        pass


# ─── Connectivity check ──────────────────────────────


def check_connectivity() -> bool:
    """Return True if we have internet connectivity."""
    cmd = ["curl", "-s", "--max-time", "3", "-o", "/dev/null", "https://archlinux.org"]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        _net_log("check_connectivity: connected")
        return True
    except subprocess.CalledProcessError:
        _net_log("check_connectivity: offline (curl failed)")
        return False
    except FileNotFoundError:
        _net_log("check_connectivity: curl not found")
        return False


# ─── Persist to installed system ──────────────────────


def copy_network_profiles(log=None) -> None:
    """Copy NetworkManager connection profiles from live env to target.

    This ensures WiFi (or static IP) configured during install carries
    over to the installed system on first boot.
    """
    from arches_installer.core.run import _log

    nm_live = Path("/etc/NetworkManager/system-connections")
    nm_target = MOUNT_ROOT / "etc/NetworkManager/system-connections"

    if not nm_live.exists():
        _net_log("copy_network_profiles: no NM connections dir, skipping")
        return

    profiles = list(nm_live.glob("*.nmconnection"))
    if not profiles:
        _net_log("copy_network_profiles: no .nmconnection files found")
        return

    nm_target.mkdir(parents=True, exist_ok=True)
    for profile in profiles:
        dest = nm_target / profile.name
        shutil.copy2(profile, dest)
        dest.chmod(0o600)

    _log(f"Copied {len(profiles)} NetworkManager profile(s) to target.", log)
