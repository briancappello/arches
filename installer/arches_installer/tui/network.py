"""Network configuration screen — WiFi and wired static IP setup."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import (
    Button,
    Input,
    Label,
    LoadingIndicator,
    OptionList,
    RadioButton,
    RadioSet,
    Static,
)
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

from arches_installer.core.network import (
    NetworkInterface,
    StaticIPConfig,
    WifiNetwork,
    connect_ethernet_static,
    connect_wifi,
    get_interfaces,
    scan_wifi,
)

# ─── Signal strength bars ─────────────────────────────

_SIGNAL_BARS = [
    (75, "\u2582\u2584\u2586\u2588"),  # ▂▄▆█
    (50, "\u2582\u2584\u2586_"),  # ▂▄▆_
    (25, "\u2582\u2584__"),  # ▂▄__
    (0, "\u2582___"),  # ▂___
]


def _signal_icon(signal: int) -> str:
    for threshold, bars in _SIGNAL_BARS:
        if signal >= threshold:
            return bars
    return "____"


def _lock_icon(security: str) -> str:
    return "\U0001f512" if security != "--" else ""


# ─── Steps ────────────────────────────────────────────

STEP_INTERFACES = "interfaces"
STEP_WIFI_SCAN = "wifi_scan"
STEP_CONNECT_DETAILS = "connect_details"
STEP_WIRED_STATIC = "wired_static"
STEP_CONNECTING = "connecting"


class NetworkScreen(Screen):
    """Network configuration screen for WiFi and wired connections."""

    step: reactive[str] = reactive(STEP_INTERFACES, layout=True)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._interfaces: list[NetworkInterface] = []
        self._wifi_networks: list[WifiNetwork] = []
        self._selected_iface: NetworkInterface | None = None
        self._selected_ssid: str = ""
        self._selected_security: str = ""
        self._error_message: str = ""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(classes="panel"):
                yield Label("Configure Network", classes="title")
                yield Static("", id="error-msg")

                # Step: Interface selection
                yield Label("Select a network interface:", id="lbl-iface")
                yield OptionList(id="iface-list")

                # Step: WiFi scan
                yield Label("Available networks:", id="lbl-wifi")
                yield OptionList(id="wifi-list")
                yield Label("Hidden SSID:")
                yield Input(
                    placeholder="Enter hidden network name", id="input-hidden-ssid"
                )

                # Step: Connection details (WiFi)
                yield Static("", id="lbl-network-name")
                yield Label("Password:", id="lbl-password")
                yield Input(
                    placeholder="WiFi password", password=True, id="input-password"
                )

                # Step: IP configuration (shared by WiFi connect + wired)
                yield Static("", id="lbl-ip-iface")
                yield RadioSet(
                    RadioButton("DHCP (automatic)", id="radio-dhcp", value=True),
                    RadioButton("Static IP", id="radio-static"),
                    id="ip-mode",
                )
                yield Label("IP Address:", id="lbl-ip")
                yield Input(placeholder="192.168.1.50/24", id="input-ip")
                yield Label("Gateway:", id="lbl-gw")
                yield Input(placeholder="192.168.1.1", id="input-gateway")
                yield Label("DNS:", id="lbl-dns")
                yield Input(placeholder="1.1.1.1, 8.8.8.8", id="input-dns")

                # Step: Connecting
                yield LoadingIndicator(id="connecting-spinner")
                yield Static("", id="connecting-msg")

                # Buttons
                yield Button("Rescan", variant="default", id="btn-rescan")
                yield Button(
                    "Connect",
                    variant="primary",
                    id="btn-connect",
                    classes="btn-primary",
                )
                yield Button(
                    "Apply", variant="primary", id="btn-apply", classes="btn-primary"
                )
                yield Button("Back", variant="default", id="btn-back")

    def on_mount(self) -> None:
        self._load_interfaces()

    # ─── Step transitions ─────────────────────────────

    def watch_step(self, new_step: str) -> None:  # noqa: C901
        """Show/hide widgets based on the current step."""
        # All widget IDs and which steps they're visible in
        visibility: dict[str, set[str]] = {
            "#lbl-iface": {STEP_INTERFACES},
            "#iface-list": {STEP_INTERFACES},
            "#lbl-wifi": {STEP_WIFI_SCAN},
            "#wifi-list": {STEP_WIFI_SCAN},
            "Input#input-hidden-ssid": {STEP_WIFI_SCAN},
            "#lbl-network-name": {STEP_CONNECT_DETAILS},
            "#lbl-password": {STEP_CONNECT_DETAILS},
            "Input#input-password": {STEP_CONNECT_DETAILS},
            "#lbl-ip-iface": {STEP_WIRED_STATIC},
            "#ip-mode": {STEP_CONNECT_DETAILS, STEP_WIRED_STATIC},
            "#lbl-ip": {STEP_CONNECT_DETAILS, STEP_WIRED_STATIC},
            "Input#input-ip": {STEP_CONNECT_DETAILS, STEP_WIRED_STATIC},
            "#lbl-gw": {STEP_CONNECT_DETAILS, STEP_WIRED_STATIC},
            "Input#input-gateway": {STEP_CONNECT_DETAILS, STEP_WIRED_STATIC},
            "#lbl-dns": {STEP_CONNECT_DETAILS, STEP_WIRED_STATIC},
            "Input#input-dns": {STEP_CONNECT_DETAILS, STEP_WIRED_STATIC},
            "#connecting-spinner": {STEP_CONNECTING},
            "#connecting-msg": {STEP_CONNECTING},
            "#btn-rescan": {STEP_WIFI_SCAN},
            "#btn-connect": {STEP_WIFI_SCAN, STEP_CONNECT_DETAILS},
            "#btn-apply": {STEP_WIRED_STATIC},
            "#btn-back": {
                STEP_INTERFACES,
                STEP_WIFI_SCAN,
                STEP_CONNECT_DETAILS,
                STEP_WIRED_STATIC,
            },
        }

        for selector, visible_steps in visibility.items():
            try:
                widget = self.query_one(selector)
                widget.display = new_step in visible_steps
            except Exception:
                pass

        # Static IP fields start hidden (DHCP is default)
        if new_step in (STEP_CONNECT_DETAILS, STEP_WIRED_STATIC):
            self._update_static_ip_visibility()

        # Error message
        error_widget = self.query_one("#error-msg", Static)
        if self._error_message:
            error_widget.update(f"[red]{self._error_message}[/red]")
            self._error_message = ""
        else:
            error_widget.update("")

    # ─── Step 0: Interface selection ──────────────────

    def _load_interfaces(self) -> None:
        self.run_worker(self._fetch_interfaces, thread=True)

    async def _fetch_interfaces(self) -> list[NetworkInterface]:
        return get_interfaces()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Handle worker completion for interface/wifi scan/connect."""
        if event.state != WorkerState.SUCCESS:
            return

        worker_name = event.worker.name or ""

        if worker_name == "_fetch_interfaces":
            self._on_interfaces_loaded(event.worker.result)
        elif worker_name == "_fetch_wifi":
            self._on_wifi_scanned(event.worker.result)
        elif worker_name == "_do_connect_wifi":
            self._on_connect_result(event.worker.result)
        elif worker_name == "_do_connect_ethernet":
            self._on_connect_result(event.worker.result)

    def _on_interfaces_loaded(self, interfaces: list[NetworkInterface]) -> None:
        self._interfaces = interfaces

        # If only one interface, skip selection
        if len(interfaces) == 1:
            self._selected_iface = interfaces[0]
            if interfaces[0].type == "wifi":
                self.step = STEP_WIFI_SCAN
                self._start_wifi_scan()
            else:
                self.step = STEP_WIRED_STATIC
                self._show_wired_static()
            return

        iface_list = self.query_one("#iface-list", OptionList)
        iface_list.clear_options()

        if not interfaces:
            iface_list.add_option(Option("No network interfaces found", id="none"))
        else:
            for iface in interfaces:
                status = "connected" if iface.connected else "disconnected"
                if iface.ip_address:
                    status += f" \u2014 {iface.ip_address}"
                label = f"{iface.name}  ({iface.type}, {status})"
                iface_list.add_option(Option(label, id=iface.name))
            iface_list.highlighted = 0
            iface_list.focus()

        self.step = STEP_INTERFACES

    # ─── Step 1: WiFi scan ────────────────────────────

    def _start_wifi_scan(self) -> None:
        self.step = STEP_WIFI_SCAN
        wifi_list = self.query_one("#wifi-list", OptionList)
        wifi_list.clear_options()
        wifi_list.add_option(Option("Scanning...", id="scanning"))
        self.run_worker(self._fetch_wifi, thread=True)

    async def _fetch_wifi(self) -> list[WifiNetwork]:
        return scan_wifi()

    def _on_wifi_scanned(self, networks: list[WifiNetwork]) -> None:
        self._wifi_networks = networks
        wifi_list = self.query_one("#wifi-list", OptionList)
        wifi_list.clear_options()

        if not networks:
            wifi_list.add_option(Option("No networks found", id="none"))
        else:
            for net in networks:
                bars = _signal_icon(net.signal)
                lock = _lock_icon(net.security)
                active = " \u2713" if net.in_use else ""
                sec = net.security if net.security != "--" else "open"
                label = f"{bars}  {net.ssid:<30} {sec:<10} {lock}{active}"
                wifi_list.add_option(Option(label, id=net.ssid))
            wifi_list.highlighted = 0
            wifi_list.focus()

        self.step = STEP_WIFI_SCAN

    # ─── Step 2: Connection details ───────────────────

    def _show_connect_details(self, ssid: str, security: str) -> None:
        self._selected_ssid = ssid
        self._selected_security = security

        name_label = self.query_one("#lbl-network-name", Static)
        name_label.update(f"Network: [bold]{ssid}[/bold] ({security})")

        # Hide password for open networks
        is_open = security in ("--", "open", "")
        self.query_one("#lbl-password", Label).display = not is_open
        self.query_one("#input-password", Input).display = not is_open

        # Reset fields
        self.query_one("#input-password", Input).value = ""
        self._reset_ip_fields()

        self.step = STEP_CONNECT_DETAILS

    # ─── Step 3: Wired static IP ──────────────────────

    def _show_wired_static(self) -> None:
        iface = self._selected_iface
        if iface:
            label = self.query_one("#lbl-ip-iface", Static)
            label.update(f"Interface: [bold]{iface.name}[/bold] (Ethernet)")
        self._reset_ip_fields()
        self.step = STEP_WIRED_STATIC

    # ─── Step 4: Connecting ───────────────────────────

    def _start_connect_wifi(self) -> None:
        ssid = self._selected_ssid
        is_open = self._selected_security in ("--", "open", "")
        psk = None if is_open else self.query_one("#input-password", Input).value
        static_ip = self._get_static_ip_config()

        msg = self.query_one("#connecting-msg", Static)
        msg.update(f"Connecting to [bold]{ssid}[/bold]...")
        self.step = STEP_CONNECTING

        self.run_worker(
            lambda: connect_wifi(ssid, psk, static_ip),
            name="_do_connect_wifi",
            thread=True,
        )

    def _start_connect_ethernet(self) -> None:
        iface = self._selected_iface
        if not iface:
            return
        static_ip = self._get_static_ip_config()
        if not static_ip:
            # DHCP — nothing to do, should already be connected
            self.app.pop_screen()
            return

        msg = self.query_one("#connecting-msg", Static)
        msg.update(f"Configuring [bold]{iface.name}[/bold]...")
        self.step = STEP_CONNECTING

        self.run_worker(
            lambda: connect_ethernet_static(iface.name, static_ip),
            name="_do_connect_ethernet",
            thread=True,
        )

    def _on_connect_result(self, result: tuple[bool, str]) -> None:
        success, error = result
        if success:
            self.app.pop_screen()
        else:
            self._error_message = error or "Connection failed."
            # Return to the appropriate selection step
            if self._selected_iface and self._selected_iface.type == "wifi":
                self._start_wifi_scan()
            else:
                self._show_wired_static()

    # ─── IP mode helpers ──────────────────────────────

    def _reset_ip_fields(self) -> None:
        """Reset IP fields and select DHCP by default."""
        try:
            # Select the first button (DHCP)
            dhcp_btn = self.query_one("#radio-dhcp", RadioButton)
            dhcp_btn.value = True
        except Exception:
            pass
        for field_id in ("#input-ip", "#input-gateway", "#input-dns"):
            try:
                self.query_one(field_id, Input).value = ""
            except Exception:
                pass
        self._update_static_ip_visibility()

    def _update_static_ip_visibility(self) -> None:
        """Show/hide static IP fields based on radio selection."""
        try:
            static_btn = self.query_one("#radio-static", RadioButton)
            show = static_btn.value
        except Exception:
            show = False

        for widget_id in (
            "#lbl-ip",
            "#input-ip",
            "#lbl-gw",
            "#input-gateway",
            "#lbl-dns",
            "#input-dns",
        ):
            try:
                self.query_one(widget_id).display = show
            except Exception:
                pass

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        """Toggle static IP fields when radio selection changes."""
        self._update_static_ip_visibility()

    def _get_static_ip_config(self) -> StaticIPConfig | None:
        """Read static IP config from the form, or None if DHCP."""
        try:
            static_btn = self.query_one("#radio-static", RadioButton)
            if not static_btn.value:
                return None
        except Exception:
            return None

        ip_cidr = self.query_one("#input-ip", Input).value.strip()
        gateway = self.query_one("#input-gateway", Input).value.strip()
        dns_raw = self.query_one("#input-dns", Input).value.strip()

        if not ip_cidr or not gateway:
            return None

        dns = [d.strip() for d in dns_raw.split(",") if d.strip()] if dns_raw else []

        return StaticIPConfig(ip_cidr=ip_cidr, gateway=gateway, dns=dns)

    # ─── Event handlers ───────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:  # noqa: C901
        btn = event.button.id

        if btn == "btn-back":
            if self.step == STEP_INTERFACES:
                self.app.pop_screen()
            elif self.step == STEP_WIFI_SCAN:
                if len(self._interfaces) == 1:
                    self.app.pop_screen()
                else:
                    self.step = STEP_INTERFACES
            elif self.step == STEP_CONNECT_DETAILS:
                self.step = STEP_WIFI_SCAN
            elif self.step == STEP_WIRED_STATIC:
                if len(self._interfaces) == 1:
                    self.app.pop_screen()
                else:
                    self.step = STEP_INTERFACES

        elif btn == "btn-rescan":
            self._start_wifi_scan()

        elif btn == "btn-connect":
            if self.step == STEP_WIFI_SCAN:
                self._on_wifi_select()
            elif self.step == STEP_CONNECT_DETAILS:
                self._start_connect_wifi()

        elif btn == "btn-apply":
            if self.step == STEP_WIRED_STATIC:
                self._start_connect_ethernet()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Double-click or Enter on a list item."""
        if event.option_list.id == "iface-list":
            self._on_iface_select()
        elif event.option_list.id == "wifi-list":
            self._on_wifi_select()

    def _on_iface_select(self) -> None:
        """Handle interface selection."""
        iface_list = self.query_one("#iface-list", OptionList)
        if iface_list.highlighted is None or not self._interfaces:
            return
        self._selected_iface = self._interfaces[iface_list.highlighted]
        if self._selected_iface.type == "wifi":
            self._start_wifi_scan()
        else:
            self._show_wired_static()

    def _on_wifi_select(self) -> None:
        """Handle WiFi network selection — from list or hidden SSID input."""
        hidden_input = self.query_one("#input-hidden-ssid", Input)
        hidden_ssid = hidden_input.value.strip()

        if hidden_ssid:
            self._show_connect_details(hidden_ssid, "WPA2")
            return

        wifi_list = self.query_one("#wifi-list", OptionList)
        if wifi_list.highlighted is None or not self._wifi_networks:
            return

        net = self._wifi_networks[wifi_list.highlighted]
        self._show_connect_details(net.ssid, net.security)
