"""
Switching Circuit V2 - Main TUI Application.

Two-column layout with the circuit diagram on the left and control
panels on the right. Connects to the Raspberry Pi server over TCP
and updates in real-time via a state subscription stream.
"""

import logging
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Static, Input, Label
from textual.worker import Worker

from tui.client import PiClient, ConnectionState
from tui.data_logger import DataLogger
from tui.discovery import discover_async, save_host, FLEET_HOSTNAMES, AP_GATEWAY
from tui.latency_probe import LatencyProbe
from tui import wifi_scan
from tui.widgets.circuit_diagram import CircuitDiagram, STATE_DEFS, STATE_PATHS
from tui.widgets.fleet_list import FleetList, FleetEntry
from tui.widgets.left_panel import LeftPanel
from tui.widgets.calibration_screen import CalibrationScreen
from tui.widgets.pi_picker import PiPicker
from tui.widgets.eth_picker import EthPicker
from tui.widgets.right_panel import RightPanel
from tui.widgets.sensor_plot import SensorPlot
from tui.widgets.auto_panel import AutoPanel
from tui.widgets.connection_bar import ConnectionBar
from tui.widgets.mascot import Mascot
from tui.widgets.schedule_screen import SchedulePickerScreen, SchedulePreviewScreen

log = logging.getLogger(__name__)

MIN_FREQ = 0.1
MAX_FREQ = 1000.0

CIRCUIT_MODES = ["idle", "charge", "discharge", "pulse_charge", "debug"]


# ---------------------------------------------------------------------------
# Connection dialog
# ---------------------------------------------------------------------------
class ConnectDialog(ModalScreen[str]):
    """Modal dialog that auto-discovers the Pi, shows the AP fleet, and
    accepts a manual IP."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("r", "rescan", "Rescan APs", show=False),
        Binding("j", "join_selected", "Join AP", show=False),
        *[Binding(str(n), f"select_{n}", show=False) for n in range(1, 9)],
    ]

    DEFAULT_CSS = """
    ConnectDialog {
        align: center middle;
    }
    #connect-box {
        width: 68;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #connect-title {
        text-align: center;
        width: 100%;
        margin-bottom: 1;
    }
    #connect-status {
        text-align: center;
        width: 100%;
        margin-top: 1;
        margin-bottom: 1;
        color: $text-muted;
    }
    #connect-hint {
        text-align: center;
        width: 100%;
        margin-top: 1;
        color: $text-muted;
    }
    #fleet-title {
        width: 100%;
        color: $text-muted;
    }
    """

    def __init__(self, auto_discover: bool = True, prescan: Optional[object] = None):
        super().__init__()
        self._auto_discover = auto_discover
        # Optional wifi_scan.ScanResult captured at app launch so the dialog
        # can show nearby pi_SW# APs the moment it opens instead of waiting
        # another ~10s for system_profiler.
        self._prescan = prescan

    def compose(self) -> ComposeResult:
        with Vertical(id="connect-box"):
            yield Label("Connect to Raspberry Pi", id="connect-title")
            yield Label("Visible Pi APs:", id="fleet-title")
            yield FleetList(id="fleet-list")
            yield Label("", id="connect-status")
            yield Input(placeholder="IP address or Enter to auto-discover", id="ip-input")
            yield Label(
                "↑↓ select  1-8 jump  J join AP  r rescan  Enter connect  Esc cancel",
                id="connect-hint",
            )

    def on_mount(self) -> None:
        if self._auto_discover:
            self._start_discovery()
        self._start_wifi_scan()

    # -- auto discovery ------------------------------------------------------

    def _start_discovery(self) -> None:
        status = self.query_one("#connect-status", Label)
        status.update("[bold cyan]Scanning for Pi server...[/]")
        discover_async(
            callback=self._on_discovery_result,
            on_status=self._on_discovery_status,
        )

    def _on_discovery_status(self, msg: str) -> None:
        try:
            self.app.call_from_thread(self._update_status, msg)
        except Exception:
            pass

    def _update_status(self, msg: str) -> None:
        try:
            status = self.query_one("#connect-status", Label)
            status.update(f"[dim]{msg}[/]")
        except Exception:
            pass

    def _on_discovery_result(self, ip: Optional[str]) -> None:
        try:
            self.app.call_from_thread(self._handle_discovery_result, ip)
        except Exception:
            pass

    def _handle_discovery_result(self, ip: Optional[str]) -> None:
        if ip:
            status = self.query_one("#connect-status", Label)
            status.update(f"[bold green]Found Pi at {ip}[/]")
            self.dismiss(ip)
        else:
            status = self.query_one("#connect-status", Label)
            status.update("[bold red]Auto-discovery failed[/] — enter IP or pick an AP")

    # -- WiFi AP scan --------------------------------------------------------

    def _start_wifi_scan(self) -> None:
        # If the app already ran a scan at launch (overlapped with discovery),
        # surface those results immediately — the user pressed `r` or the
        # scan just finished. Fall through to a fresh scan if no prescan.
        if self._prescan is not None:
            entries = self._build_entries(self._prescan.aps)
            self._apply_fleet_entries(entries, self._prescan.warning)
            self._prescan = None  # single-shot; rescan-via-r still works
            return
        import threading
        threading.Thread(
            target=self._wifi_scan_worker, daemon=True, name="wifi-scan"
        ).start()

    def _wifi_scan_worker(self) -> None:
        warning: Optional[str] = None
        try:
            scan = wifi_scan.scan_pi_aps()
            aps = scan.aps
            warning = scan.warning
        except Exception:
            log.exception("WiFi scan failed")
            aps = []
        entries = self._build_entries(aps)
        try:
            self.app.call_from_thread(self._apply_fleet_entries, entries, warning)
        except Exception:
            pass

    def _build_entries(self, aps) -> list[FleetEntry]:
        seen = {ap.ssid: ap for ap in aps}
        entries: list[FleetEntry] = []
        # Online first (already sorted by signal in wifi_scan)
        for ap in aps:
            entries.append(FleetEntry(
                ssid=ap.ssid, signal_dbm=ap.signal_dbm,
                is_current=ap.is_current, online=True,
            ))
        # Known fleet hostnames that didn't appear in the scan — dimmed as offline
        known_ssids = [h.replace(".local", "").replace("-", "_") for h in FLEET_HOSTNAMES]
        for ssid in known_ssids:
            if ssid not in seen:
                entries.append(FleetEntry(
                    ssid=ssid, signal_dbm=None, is_current=False, online=False,
                ))
        return entries

    def _apply_fleet_entries(
        self, entries: list[FleetEntry], warning: Optional[str] = None
    ) -> None:
        try:
            self.query_one("#fleet-list", FleetList).set_entries(entries)
            title = self.query_one("#fleet-title", Label)
            if warning:
                title.update(f"Visible Pi APs — [yellow]{warning}[/]")
            else:
                title.update("Visible Pi APs:")
        except Exception:
            pass

    # -- actions -------------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss("")

    def action_rescan(self) -> None:
        self._update_status("Rescanning APs...")
        self._start_wifi_scan()

    def action_join_selected(self) -> None:
        fleet = self.query_one("#fleet-list", FleetList)
        entry = fleet.selected_entry()
        if not entry:
            self._update_status("No AP selected")
            return
        # macOS system_profiler serves a stale cache, so a fleet SSID may be
        # broadcasting but not appear in the scan. Let the user attempt to
        # join any known fleet entry — networksetup will error in a few
        # seconds if the AP really isn't in range.
        if entry.online:
            self._update_status(f"[bold cyan]Joining {entry.ssid}...[/]")
        else:
            self._update_status(
                f"[bold cyan]Trying {entry.ssid}[/] [dim](not in recent scan — may not be in range)[/]"
            )
        import threading
        threading.Thread(
            target=self._join_worker, args=(entry.ssid,),
            daemon=True, name="wifi-join",
        ).start()

    def _join_worker(self, ssid: str) -> None:
        def status(msg: str) -> None:
            try:
                self.app.call_from_thread(
                    self._update_status, f"[bold cyan]{msg}[/]"
                )
            except Exception:
                pass

        result = wifi_scan.join_ap(ssid, status_cb=status)
        try:
            self.app.call_from_thread(self._handle_join_result, ssid, result)
        except Exception:
            pass

    def _handle_join_result(self, ssid: str, result) -> None:
        if result.ok:
            self._update_status(
                f"[bold green]Joined {ssid} and reached Pi at {AP_GATEWAY}[/]"
            )
            self.dismiss(AP_GATEWAY)
        else:
            self._update_status(f"[bold red]Join failed:[/] {result.error or 'unknown'}")

    def _select(self, idx: int) -> None:
        self.query_one("#fleet-list", FleetList).set_selected(idx)

    # Number-key jump bindings (1-8) — installed dynamically in BINDINGS above.
    def action_select_1(self) -> None: self._select(0)
    def action_select_2(self) -> None: self._select(1)
    def action_select_3(self) -> None: self._select(2)
    def action_select_4(self) -> None: self._select(3)
    def action_select_5(self) -> None: self._select(4)
    def action_select_6(self) -> None: self._select(5)
    def action_select_7(self) -> None: self._select(6)
    def action_select_8(self) -> None: self._select(7)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value:
            self.dismiss(value)
        else:
            # Re-run discovery
            self._start_discovery()


# ---------------------------------------------------------------------------
# Help overlay
# ---------------------------------------------------------------------------
class HelpScreen(ModalScreen[None]):
    """Simple help overlay showing all key bindings."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("question_mark", "close", "Close"),
    ]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-box {
        width: 58;
        height: auto;
        max-height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
        overflow-y: auto;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(
                "[bold cyan]Switching Circuit V2 - Key Bindings[/]\n\n"
                "[bold white]CONTROL[/]\n"
                "  [bold]Space[/]     Start/Stop (idle <-> charge)\n"
                "  [bold]m[/]         Cycle mode\n\n"
                "[bold white]MODE[/]\n"
                "  [bold]c[/]  Charge    [bold]i[/]  Idle      [bold]x[/]  Discharge\n"
                "  [bold]p[/]  Pulse     [bold]g[/]  Debug     [bold]a[/]  Auto\n\n"
                "[bold white]AUTO MODE[/]  [dim](press [bold]a[/] to load schedule)[/]\n"
                "  [bold]n[/]         Skip to next step\n"
                "  [bold]Space[/]     Pause / resume auto\n"
                "  [bold]i[/]         Stop auto, return to idle\n\n"
                "[bold white]FREQUENCY[/]\n"
                "  [bold]= / -[/]     +/- 0.1 Hz    [bold]w / s[/]  +/- 0.1 Hz\n"
                "  [bold]e / d[/]     +/- 1.0 Hz    [bold]W / S[/]  +/- 10 Hz\n\n"
                "[bold white]SEQUENCE[/]\n"
                "  [bold]1-8[/]       Select switching sequence\n"
                "  [bold]1-4[/]       [dim](Debug mode: toggle P1/P2/N1/N2)[/]\n\n"
                "[bold white]SENSORS & PLOT[/]\n"
                "  [bold]* / /[/]     Sensor rate +/- (0.5-1000 Hz)\n"
                "  [bold]j[/]         Cycle INA226 averaging (1/4/16/64)\n"
                "  [bold]k[/]         Cycle bus-voltage decimation (1/2/3/5/10/off)\n"
                "  [bold]v[/]         Cycle plot mode (line/dot/bar)\n\n"
                "[bold white]RECORDING[/]\n"
                "  [bold]l[/]         Start / stop recording\n"
                "  [bold][ / ][/]     Recording duration -/+\n\n"
                "[bold white]NETWORK[/]\n"
                "  [bold]A[/]  Pi to AP mode (pi_SW#)     [bold]C[/]  Pi back to client WiFi\n\n"
                "[bold white]OTHER[/]\n"
                "  [bold]r[/]  Reconnect    [bold]?[/]  This help    [bold]q[/]  Quit\n\n"
                "[dim]Press Escape or ? to close[/]"
            )

    def action_close(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Network-mode confirmation modal
# ---------------------------------------------------------------------------
class NetworkModeConfirm(ModalScreen[Optional[str]]):
    """Confirmation dialog for AP <-> Client flips. Dismisses with the
    chosen mode string on confirm, or None on cancel."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("y", "confirm", "Confirm"),
        Binding("enter", "confirm", "Confirm"),
        Binding("n", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    NetworkModeConfirm { align: center middle; }
    #nmc-box {
        width: 64;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    #nmc-title { text-align: center; width: 100%; margin-bottom: 1; }
    #nmc-hint { text-align: center; width: 100%; margin-top: 1; color: $text-muted; }
    """

    def __init__(self, mode: str, message: str):
        super().__init__()
        self._mode = mode
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="nmc-box"):
            title = "Flip to AP Mode" if self._mode == "ap" else "Return to Client Mode"
            yield Label(f"[bold yellow]{title}[/]", id="nmc-title")
            yield Static(self._message)
            yield Label("[y / Enter] Confirm   [n / Esc] Cancel", id="nmc-hint")

    def action_confirm(self) -> None:
        self.dismiss(self._mode)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------
class SwitchingCircuitApp(App):
    """TUI client for the Switching Circuit V2 Pi server."""

    TITLE = "Switching Circuit V2"
    SUB_TITLE = "H-Bridge Controller"

    # Width threshold: if the center column is wider than this, show
    # the sensor plot to the right of the circuit; otherwise below it.
    WIDE_THRESHOLD = 130

    CSS = """
    Screen {
        background: $background;
    }
    #main-layout {
        height: 1fr;
    }
    #left-col {
        width: 32;
        border-right: solid $accent;
        overflow-y: auto;
        align-horizontal: left;
    }
    #center-col {
        width: 1fr;
        min-width: 68;
        overflow-y: auto;
        align-horizontal: center;
    }
    #right-col {
        width: 36;
        border-left: solid $accent;
        overflow-y: auto;
        align-horizontal: left;
    }
    ConnectionBar {
        dock: top;
        height: 1;
    }
    CircuitDiagram {
        width: auto;
    }

    /* Default: sensor plot below circuit (narrow) */
    #center-inner {
        width: 100%;
        height: auto;
    }
    SensorPlot {
        width: 100%;
        height: auto;
        border-top: solid $accent;
        margin-top: 1;
    }

    /* Wide layout: sensor plot beside circuit */
    #center-inner.wide-layout {
        layout: horizontal;
    }
    #center-inner.wide-layout SensorPlot {
        width: 1fr;
        border-top: none;
        border-left: solid $accent;
        margin-top: 0;
        margin-left: 1;
    }
    #center-inner.wide-layout CircuitDiagram {
        width: auto;
    }

    Mascot {
        height: auto;
    }
    .hidden {
        display: none;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("question_mark", "help", "Help"),
        Binding("space", "toggle_run", "Start/Stop", priority=True),
        Binding("1", "seq_1", "Seq 1", show=False),
        Binding("2", "seq_2", "Seq 2", show=False),
        Binding("3", "seq_3", "Seq 3", show=False),
        Binding("4", "seq_4", "Seq 4", show=False),
        Binding("5", "seq_5", "Seq 5", show=False),
        Binding("6", "seq_6", "Seq 6", show=False),
        Binding("7", "seq_7", "Seq 7", show=False),
        Binding("8", "seq_8", "Seq 8", show=False),
        Binding("w", "freq_up_fine", "+0.1Hz", show=False),
        Binding("s", "freq_down_fine", "-0.1Hz", show=False),
        Binding("e", "freq_up_med", "+1Hz", show=False),
        Binding("d", "freq_down_med", "-1Hz", show=False),
        Binding("W", "freq_up_coarse", "+10Hz", show=False),
        Binding("S", "freq_down_coarse", "-10Hz", show=False),
        Binding("m", "cycle_mode", "Mode", show=False),
        Binding("c", "mode_charge", "Charge", show=False),
        Binding("i", "mode_idle", "Idle", show=False),
        Binding("x", "mode_discharge", "Discharge", show=False),
        Binding("p", "mode_pulse", "Pulse", show=False),
        Binding("r", "reconnect", "Reconnect", show=False),
        Binding("g", "mode_debug", "Debug", show=False),
        Binding("=", "freq_up_fine", "+0.1Hz", show=False),
        Binding("-", "freq_down_fine", "-0.1Hz", show=False),
        Binding("*", "sensor_rate_up", "Sensor+", show=False),
        Binding("/", "sensor_rate_down", "Sensor-", show=False),
        Binding("j", "cycle_ina_avg", "AVG", show=False),
        Binding("k", "cycle_bus_every", "V-decim", show=False),
        Binding("v", "cycle_viz", "Viz Mode", show=False),
        Binding("y", "cycle_cycle_window", "Cycles", show=False),
        Binding("l", "toggle_log", "Log", show=False),
        Binding("[", "log_duration_down", "Dur-", show=False),
        Binding("]", "log_duration_up", "Dur+", show=False),
        Binding("a", "load_schedule", "Load schedule", show=False),
        Binding("tab", "toggle_right_panel", "Toggle Panel", show=False, priority=True),
        Binding("A", "ap_mode", "AP Mode", show=False),
        Binding("F", "auto_follow_toggle", "Auto-follow toggle", show=False),
        Binding("T", "auto_follow_cycle_target", "Auto-follow target", show=False),
        Binding("comma", "auto_follow_enter_down", "I_enter -1mA", show=False),
        Binding("full_stop", "auto_follow_enter_up", "I_enter +1mA", show=False),
        Binding("semicolon", "auto_follow_exit_down", "I_exit -0.5mA", show=False),
        Binding("apostrophe", "auto_follow_exit_up", "I_exit +0.5mA", show=False),
        Binding("less_than_sign", "auto_follow_cc_down", "CC setpoint -", show=False),
        Binding("greater_than_sign", "auto_follow_cc_up", "CC setpoint +", show=False),
        Binding("M", "restart_monitor", "Restart monitor clock", show=False),
        Binding("C", "client_mode", "Client Mode", show=False),
        Binding("D", "toggle_probe", "Latency", show=False),
        Binding("P", "switch_pi", "Switch Pi", show=True),
        Binding("E", "switch_pi_eth", "Switch Pi (Eth)", show=True),
        Binding("O", "offload", "Offload", show=False),
        Binding("B", "calibrate", "Calibrate", show=False),
        Binding("R", "toggle_plot_range", "Range", show=False),
    ]

    # During startup, limit state updates to let the layout stabilize.
    # 0.8s was overcautious — it drops ~12 frames/sec for most of a second
    # before anything useful can appear. 0.2s is enough for Textual to
    # settle layout without a visible freeze; raise if flicker returns.
    _WARMUP_S = 0.2       # seconds of reduced update rate after first data
    _WARMUP_FPS = 3       # max frames/sec during warmup

    def __init__(self, host: str = "", port: int = 5555):
        super().__init__()
        self._initial_host = host
        self._initial_port = port
        self._client: Optional[PiClient] = None
        self._circuit_mode = "idle"
        self._current_freq = 1.0
        self._current_seq = 0
        self._data_logger = DataLogger()
        self._first_state_time = 0.0     # monotonic time of first state update
        self._last_apply_time = 0.0      # last time _apply_state ran
        self._showing_auto_panel = False  # right column: status vs auto panel
        self._prev_mode = "idle"         # track mode changes for panel auto-switch
        self._latest_auto_follow: dict = {}  # mirror of last auto_follow status
        self._probe = LatencyProbe()
        self._offset_worker_started = False
        self._last_probe_display_ns = 0
        self._state_pending = False  # backpressure: skip if prior frame not consumed
        self._current_pi_name = ""   # e.g. "SW4" — derived from SSID
        self._fleet_cache: list = []
        # Launch-time WiFi prescan: overlaps with auto-discovery so the
        # ConnectDialog can show nearby pi_SW# APs the moment it opens.
        import threading as _threading
        self._prescan_lock = _threading.Lock()
        self._prescan_result = None
        self._prescan_at = 0.0

    # -- Compose -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield ConnectionBar(id="conn-bar")
        with Horizontal(id="main-layout"):
            with Vertical(id="left-col"):
                yield LeftPanel(id="left-panel")
                yield Mascot(id="mascot")
            with Vertical(id="center-col"):
                with Container(id="center-inner"):
                    yield CircuitDiagram(id="circuit")
                    yield SensorPlot(id="sensor-plot")
            with Vertical(id="right-col"):
                yield RightPanel(id="right-panel")
                yield AutoPanel(id="auto-panel", classes="hidden")
        yield Footer()

    # -- Lifecycle -----------------------------------------------------------

    def on_resize(self, event: Resize) -> None:
        """Toggle wide/narrow layout and expanded sensor plots based on terminal size."""
        try:
            inner = self.query_one("#center-inner", Container)
            plot = self.query_one("#sensor-plot", SensorPlot)
            # Total width minus left (32) and right (36) columns and borders
            center_width = event.size.width - 32 - 36
            wide = center_width >= self.WIDE_THRESHOLD
            if wide:
                inner.add_class("wide-layout")
            else:
                inner.remove_class("wide-layout")
            # Expand sensor plots to 8 individual plots when there's room
            plot.expanded = wide
            if wide:
                # In wide layout the circuit takes ~60 cols; rest goes to plots
                plot_width = center_width - 62
                plot.available_width = max(60, plot_width)
                # Full terminal height minus header(1), conn bar(1), footer(1)
                plot.available_height = max(20, event.size.height - 3)
            else:
                plot.available_width = max(60, center_width - 4)
                plot.available_height = max(20, event.size.height // 2)
        except Exception:
            pass

    def on_mount(self) -> None:
        """Initialize client and connect."""
        # Disable terminal mouse tracking. This TUI is entirely
        # keyboard-driven; mouse motion over the sensor plots triggers a
        # flood of MouseMove events which compete with state updates in the
        # event loop and produce visible stutter. Writing the ANSI disable
        # sequence at the driver level prevents the terminal from sending
        # them at all — cleaner than intercepting and dropping.
        try:
            if self._driver is not None and hasattr(
                self._driver, "_disable_mouse_support"
            ):
                self._driver._disable_mouse_support()
        except Exception:
            log.exception("failed to disable mouse tracking")
        # retry_delay=2.0, max_retries=30 -> ~60s window to survive a network
        # flip (AP activation, WiFi reassociation, etc.) before giving up.
        self._client = PiClient(
            on_state=self._on_state_update,
            on_connection_change=self._on_connection_change,
            retry_delay=2.0,
            max_retries=30,
        )

        if self._initial_host:
            self._do_connect(self._initial_host, self._initial_port)
        else:
            # Try auto-discovery first, fall back to manual dialog
            self._update_status_connection(False, "Discovering Pi...")
            conn_bar = self.query_one("#conn-bar", ConnectionBar)
            conn_bar.conn_label = "Scanning..."
            self._start_wifi_prescan()
            discover_async(
                callback=self._on_auto_discover_result,
                on_status=self._on_auto_discover_status,
            )

    def _start_wifi_prescan(self) -> None:
        """Start a periodic WiFi scan that keeps the fleet cache warm.
        First scan runs immediately; subsequent scans every 45s so the
        picker opens instantly with fresh data."""
        import threading
        from time import monotonic, sleep

        def _loop() -> None:
            while True:
                try:
                    scan = wifi_scan.scan_pi_aps()
                except Exception:
                    log.exception("prescan failed")
                    scan = None
                with self._prescan_lock:
                    self._prescan_result = scan
                    self._prescan_at = monotonic()
                if scan and scan.aps:
                    self._fleet_cache = scan.aps
                sleep(45)

        threading.Thread(target=_loop, daemon=True, name="wifi-prescan").start()

    def _take_prescan(self, max_age_s: float = 60.0):
        """Return the prescan result if fresh, else None. Single-consumer."""
        from time import monotonic
        with self._prescan_lock:
            if self._prescan_result is None:
                return None
            if monotonic() - self._prescan_at > max_age_s:
                self._prescan_result = None
                return None
            result = self._prescan_result
            self._prescan_result = None  # single-consumer
            return result

    def _on_auto_discover_status(self, msg: str) -> None:
        try:
            self.call_from_thread(self._update_discover_status, msg)
        except Exception:
            pass

    def _update_discover_status(self, msg: str) -> None:
        try:
            conn_bar = self.query_one("#conn-bar", ConnectionBar)
            conn_bar.conn_label = msg
        except Exception:
            pass

    def _on_auto_discover_result(self, ip: Optional[str]) -> None:
        try:
            self.call_from_thread(self._handle_auto_discover, ip)
        except Exception:
            pass

    def _handle_auto_discover(self, ip: Optional[str]) -> None:
        if ip:
            self._do_connect(ip, self._initial_port)
        else:
            # Auto-discovery failed, show manual dialog. Pass the launch-time
            # WiFi prescan so the dialog can populate the fleet list instantly.
            prescan = self._take_prescan()
            self.push_screen(
                ConnectDialog(auto_discover=False, prescan=prescan),
                self._on_connect_dialog_result,
            )

    def _on_connect_dialog_result(self, result: str) -> None:
        if result:
            self._do_connect(result, self._initial_port)
        else:
            # User cancelled; run disconnected
            self._update_connection_ui(ConnectionState.DISCONNECTED)

    def _do_connect(self, host: str, port: int) -> None:
        self._probe.reset()
        if not self._current_pi_name:
            ssid = wifi_scan.current_ssid() or ""
            if ssid.startswith("pi_"):
                self._current_pi_name = ssid.replace("pi_", "")
        display_host = self._current_pi_name or f"{host}:{port}"
        conn_bar = self.query_one("#conn-bar", ConnectionBar)
        conn_bar.host = display_host
        conn_bar.conn_label = "Connecting..."
        self._update_status_connection(False, "Connecting...")

        # Connect in a worker to avoid blocking the UI
        self.run_worker(
            self._connect_worker(host, port),
            name="connect",
            group="connect",
            exclusive=True,
        )

    async def _connect_worker(self, host: str, port: int) -> None:
        """Worker coroutine that performs the blocking connect."""
        import asyncio

        loop = asyncio.get_event_loop()
        assert self._client is not None
        connected = await loop.run_in_executor(
            None, self._client.connect, host, port
        )
        if connected:
            save_host(host)
            # Pre-subscribe sensor-profile fetch. Safe now that the recv
            # thread doesn't start until subscribe() — send_command can
            # readline() its reply directly without the race that froze
            # the TUI on first connect. Without this the header would sit
            # on its placeholder max_hz until the user manually cycles
            # AVG/bus_every.
            try:
                profile = await loop.run_in_executor(
                    None,
                    self._client.send_command,
                    {"cmd": "get_sensor_profile"},
                )
            except Exception:
                profile = None
            # Subscribe to the state stream (also starts the recv thread).
            await loop.run_in_executor(None, self._client.subscribe)
            if profile:
                try:
                    plot = self.query_one("#sensor-plot", SensorPlot)
                    self._apply_profile_reply(plot, profile)
                except Exception:
                    pass

    # -- State stream callback (called from background thread) ---------------

    def _on_state_update(self, data: dict) -> None:
        """Called from the PiClient recv thread with each state event.
        Rate-limited during startup warmup so layout can stabilize."""
        from time import monotonic
        now = monotonic()
        if self._first_state_time == 0.0:
            self._first_state_time = now
            if not self._offset_worker_started:
                self._offset_worker_started = True
                self._start_offset_worker()
        # During warmup, drop frames to let Textual settle layout caches
        if now - self._first_state_time < self._WARMUP_S:
            interval = 1.0 / self._WARMUP_FPS
            if now - self._last_apply_time < interval:
                return  # skip this frame
        self._last_apply_time = now
        if self._state_pending:
            return  # prior frame still in Textual's queue — skip this one
        self._state_pending = True
        try:
            self.call_from_thread(self._apply_state, data)
        except Exception:
            self._state_pending = False

    def _start_offset_worker(self) -> None:
        """Background thread that re-measures Pi↔Mac clock offset every 60s
        to keep the latency probe's `net` timer honest against clock drift."""
        import threading
        from time import monotonic_ns, sleep

        def _loop() -> None:
            # First measurement: take the min of several quick pings for a
            # tighter estimate; later re-measures are single pings.
            if self._client is None:
                return
            sleep(0.5)  # let the subscription settle
            best = None
            for _ in range(5):
                off = self._client.ping_server(timeout=1.0)
                if off is not None and (best is None or abs(off) < abs(best)):
                    best = off
                sleep(0.05)
            if best is not None:
                self._probe.set_offset(best)
            while self._client is not None:
                sleep(60.0)
                if self._client is None:
                    break
                off = self._client.ping_server(timeout=1.0)
                if off is not None:
                    self._probe.set_offset(off)

        t = threading.Thread(target=_loop, name="latency-offset", daemon=True)
        t.start()

    def _apply_state(self, data: dict) -> None:
        """Apply a state update to all widgets (runs on the UI thread).

        Uses batch_update() to coalesce all property changes into a single
        render pass instead of triggering 6+ separate refreshes.
        """
        from time import monotonic_ns
        self._state_pending = False
        t_apply_start_ns = monotonic_ns()

        mode = data.get("mode", "idle")
        seq = data.get("sequence", 0)
        step = data.get("step", 0)
        freq = data.get("frequency", 1.0)
        fets = data.get("fet_states", [False, False, False, False])

        self._circuit_mode = mode
        self._current_freq = freq
        self._current_seq = seq
        self._last_fets = fets

        state_idx = self._fets_to_state_index(fets)

        with self.batch_update():
            circuit = self.query_one("#circuit", CircuitDiagram)
            circuit.update_from_server(fets, state_idx, mode=mode)

            rpanel = self.query_one("#right-panel", RightPanel)
            rpanel.mode = mode
            rpanel.sequence = seq
            rpanel.frequency = freq
            rpanel.step = step
            rpanel.current_path = STATE_PATHS[state_idx] if 0 <= state_idx <= 5 else ""

            sensors = data.get("sensors", {})
            cal_sink = getattr(self, "_cal_sample_sink", None)
            if cal_sink is not None:
                try:
                    cal_sink(sensors)
                except Exception:
                    pass
            plot = self.query_one("#sensor-plot", SensorPlot)
            # Feed the live switching frequency so cycle-window mode can
            # convert "N cycles" to a wallclock filter. Cheap when the
            # value is unchanged (reactive deduplicates).
            plot.switching_freq = float(freq)
            t_plot0 = monotonic_ns()
            plot.append_data(sensors)
            plot.commit()  # refresh inside batch_update so it coalesces
            t_plot_ns = monotonic_ns() - t_plot0

            auto_panel = self.query_one("#auto-panel", AutoPanel)
            conn_bar = self.query_one("#conn-bar", ConnectionBar)
            af = data.get("auto_follow")
            if af:
                self._latest_auto_follow = af
                rpanel.auto_follow = af
            monitor_data = data.get("schedule_monitor", {}) or {}
            auto_panel.monitor_data = monitor_data

            schedule_loaded = bool(monitor_data.get("loaded"))
            conn_bar.update_auto_status(monitor_data if schedule_loaded else {})

            if schedule_loaded and not self._showing_auto_panel:
                self._showing_auto_panel = True
                rpanel.add_class("hidden")
                auto_panel.remove_class("hidden")
            elif not schedule_loaded and self._showing_auto_panel:
                self._showing_auto_panel = False
                auto_panel.add_class("hidden")
                rpanel.remove_class("hidden")

            self._prev_mode = mode

            # Data logging (pure computation — widget touches stay in batch)
            if self._data_logger.is_logging and self._data_logger.tier and not self._stop_in_progress:
                from tui.data_logger import RecordTier
                tier = self._data_logger.tier

                if tier == RecordTier.MAC:
                    still_going = self._data_logger.record(data)
                    if not still_going:
                        self._on_recording_done()
                        return
                elif tier == RecordTier.PI:
                    if self._data_logger.check_pi_done():
                        self._on_recording_done()
                        return

                elapsed = self._data_logger.elapsed
                dur = self._data_logger.duration_s
                remaining = max(0, dur - elapsed)
                conn_bar.conn_label = f"\u25cf REC [{tier.value}] {remaining:.0f}s left"

            mascot = self.query_one("#mascot", Mascot)
            mascot.circuit_mode = mode

        t_apply_end_ns = monotonic_ns()
        self._probe.record(
            t_emit_pi_ns=int(data.get("t_emit_ns", 0)),
            t_recv_mac_ns=int(data.get("_t_recv_ns", t_apply_start_ns)),
            t_apply_start_ns=t_apply_start_ns,
            t_apply_end_ns=t_apply_end_ns,
            t_plot_ns=t_plot_ns,
            seq=int(data.get("bcast_seq", -1)),
        )
        if self._probe.enabled:
            # Refresh the probe readout at most twice a second. Setting
            # probe_text fires a reactive watcher which triggers an extra
            # render pass per state event, making things worse when the
            # probe is on at high broadcast rates.
            if t_apply_end_ns - self._last_probe_display_ns >= 500_000_000:
                self._last_probe_display_ns = t_apply_end_ns
                self._update_probe_display()

    def _update_probe_display(self) -> None:
        """Refresh the compact probe readout in the ConnectionBar."""
        s = self._probe.summary()
        if not s or s.get("_count", 0) == 0:
            return
        net_p50 = s["net"][0]
        net_p95 = s["net"][1]
        q_p95 = s["queue"][1]
        apply_p95 = s["apply"][1]
        total_p50 = s["total"][0]
        total_p95 = s["total"][1]
        ready = s.get("_offset_ready", False)
        hz = s.get("_hz", 0.0)
        drops = s.get("_drops", 0)
        net_str = f"{net_p50:.0f}/{net_p95:.0f}" if ready else "—/—"
        text = (
            f"{hz:.0f}Hz "
            f"net:{net_str} q:{q_p95:.0f} apply:{apply_p95:.0f} "
            f"tot:{total_p50:.0f}/{total_p95:.0f}ms"
        )
        if drops > 0:
            text += f" [{drops}drops]"
        try:
            conn_bar = self.query_one("#conn-bar", ConnectionBar)
            conn_bar.probe_text = text
        except Exception:
            pass

    def action_toggle_probe(self) -> None:
        """Toggle the E2E latency probe display (D key)."""
        on = self._probe.toggle()
        try:
            conn_bar = self.query_one("#conn-bar", ConnectionBar)
            if on:
                conn_bar.probe_text = "probe: sampling…"
            else:
                conn_bar.probe_text = ""
        except Exception:
            pass

    @staticmethod
    def _fets_to_state_index(fets: list[bool]) -> int:
        """Map a FET state list to a state definition index."""
        tup = tuple(fets)
        for i, sd in enumerate(STATE_DEFS):
            if sd == tup:
                return i
        return 5  # default to all-off

    # -- Connection change callback ------------------------------------------

    def _on_connection_change(self, state: ConnectionState) -> None:
        """Called from PiClient thread on connection state changes."""
        self.call_from_thread(self._update_connection_ui, state)

    def _update_connection_ui(self, state: ConnectionState) -> None:
        conn_bar = self.query_one("#conn-bar", ConnectionBar)
        rpanel = self.query_one("#right-panel", RightPanel)

        name = self._current_pi_name
        if state == ConnectionState.CONNECTED:
            conn_bar.connected = True
            conn_bar.conn_label = f"Connected — {name}" if name else "Connected"
            rpanel.connected = True
            rpanel.conn_status = f"Connected — {name}" if name else "Connected"
        elif state == ConnectionState.CONNECTING:
            conn_bar.connected = False
            label = f"Connecting to {name}..." if name else "Connecting..."
            conn_bar.conn_label = label
            rpanel.connected = False
            rpanel.conn_status = label
        else:
            conn_bar.connected = False
            conn_bar.conn_label = "Disconnected"
            rpanel.connected = False
            rpanel.conn_status = "Disconnected"

    def _update_status_connection(self, connected: bool, label: str) -> None:
        rpanel = self.query_one("#right-panel", RightPanel)
        rpanel.connected = connected
        rpanel.conn_status = label

    # -- Actions: Mode -------------------------------------------------------

    def _send_mode(self, mode: str) -> None:
        if self._client and self._client.connection_state == ConnectionState.CONNECTED:
            self._client.send_async({"cmd": "set_mode", "mode": mode})

    def action_toggle_run(self) -> None:
        new_mode = "charge" if self._circuit_mode == "idle" else "idle"
        self._send_mode(new_mode)

    def action_cycle_mode(self) -> None:
        try:
            idx = CIRCUIT_MODES.index(self._circuit_mode)
        except ValueError:
            idx = 0
        new_mode = CIRCUIT_MODES[(idx + 1) % len(CIRCUIT_MODES)]
        self._send_mode(new_mode)

    def action_mode_charge(self) -> None:
        self._send_mode("charge")

    def action_mode_idle(self) -> None:
        self._send_mode("idle")

    def action_mode_discharge(self) -> None:
        self._send_mode("discharge")

    def action_mode_pulse(self) -> None:
        self._send_mode("pulse_charge")

    def action_mode_debug(self) -> None:
        self._send_mode("debug")

    def action_load_schedule(self) -> None:
        """Open the schedule picker. Loading hands the schedule to the
        passive monitor — does not change the circuit mode."""
        self.push_screen(SchedulePickerScreen(), self._on_schedule_file_picked)

    def _on_schedule_file_picked(self, path: str) -> None:
        """Stage 1 callback: file selected, load JSON and show preview."""
        if not path:
            return
        try:
            import json
            from pathlib import Path as P
            with open(P(path)) as f:
                raw = json.load(f)
        except Exception as e:
            self.notify(f"Failed to read {path}: {e}", title="Error", severity="error")
            return

        # Run semantic validation for warnings
        from server.schedule import _parse_schedule, validate_schedule, validate_schedule_semantics
        try:
            sched = _parse_schedule(raw)
            errors = validate_schedule(sched)
            if errors:
                self.notify("\n".join(errors), title="Invalid Schedule", severity="error")
                return
            warnings = validate_schedule_semantics(sched)
        except Exception as e:
            self.notify(f"Invalid schedule: {e}", title="Error", severity="error")
            return

        self.push_screen(
            SchedulePreviewScreen(raw, path=path, warnings=warnings),
            self._on_schedule_confirmed,
        )

    def _on_schedule_confirmed(self, result) -> None:
        """Stage 2 callback: user confirmed (with possible edits) or cancelled.
        The schedule is handed to the passive monitor; circuit mode is
        not changed."""
        if result is None or not self._client:
            return
        resp = self._client.send_command({"cmd": "load_schedule", "schedule": result})
        if resp and resp.get("ok"):
            warnings = resp.get("warnings", [])
            for w in warnings:
                self.notify(w, title="Schedule Warning", severity="warning")
            self.notify(
                f"Loaded {resp.get('schedule_name', '?')} "
                f"({resp.get('steps', 0)} steps × {resp.get('repeat', 1)})",
                title="Schedule Monitor",
            )
        else:
            err = resp.get("error", "Unknown error") if resp else "No response"
            self.notify(f"Failed to load schedule: {err}", title="Error", severity="error")

    def action_toggle_right_panel(self) -> None:
        """Toggle between Status and Auto panel in the right column."""
        rpanel = self.query_one("#right-panel", RightPanel)
        auto_panel = self.query_one("#auto-panel", AutoPanel)

        self._showing_auto_panel = not self._showing_auto_panel
        if self._showing_auto_panel:
            rpanel.add_class("hidden")
            auto_panel.remove_class("hidden")
        else:
            auto_panel.add_class("hidden")
            rpanel.remove_class("hidden")

    # -- Actions: Debug FET control ------------------------------------------

    def _toggle_fet(self, index: int) -> None:
        """Toggle a single FET on/off. Only works in debug mode."""
        if self._client and self._client.connection_state == ConnectionState.CONNECTED:
            current = self._last_fets[index] if hasattr(self, "_last_fets") else False
            self._client.send_async({"cmd": "set_fet", "index": index, "on": not current})

    # -- Actions: Sequence ---------------------------------------------------

    def _send_sequence(self, seq: int) -> None:
        if self._client and self._client.connection_state == ConnectionState.CONNECTED:
            self._client.send_async({"cmd": "set_sequence", "sequence": seq})

    def action_seq_1(self) -> None:
        if self._circuit_mode == "debug":
            self._toggle_fet(0)
        else:
            self._send_sequence(0)

    def action_seq_2(self) -> None:
        if self._circuit_mode == "debug":
            self._toggle_fet(1)
        else:
            self._send_sequence(1)

    def action_seq_3(self) -> None:
        if self._circuit_mode == "debug":
            self._toggle_fet(2)
        else:
            self._send_sequence(2)

    def action_seq_4(self) -> None:
        if self._circuit_mode == "debug":
            self._toggle_fet(3)
        else:
            self._send_sequence(3)

    def action_seq_5(self) -> None:
        self._send_sequence(4)

    def action_seq_6(self) -> None:
        self._send_sequence(5)

    def action_seq_7(self) -> None:
        self._send_sequence(6)

    def action_seq_8(self) -> None:
        self._send_sequence(7)

    # -- Actions: Frequency --------------------------------------------------

    def _adjust_freq(self, delta: float) -> None:
        new_freq = round(max(MIN_FREQ, min(MAX_FREQ, self._current_freq + delta)), 1)
        if self._client and self._client.connection_state == ConnectionState.CONNECTED:
            self._client.send_async({"cmd": "set_frequency", "frequency": new_freq})
            self._current_freq = new_freq

    def action_freq_up_fine(self) -> None:
        self._adjust_freq(0.1)

    def action_freq_down_fine(self) -> None:
        self._adjust_freq(-0.1)

    def action_freq_up_med(self) -> None:
        self._adjust_freq(1.0)

    def action_freq_down_med(self) -> None:
        self._adjust_freq(-1.0)

    def action_freq_up_coarse(self) -> None:
        self._adjust_freq(10.0)

    def action_freq_down_coarse(self) -> None:
        self._adjust_freq(-10.0)

    # -- Actions: Sensor Rate -------------------------------------------------

    # C firmware delivers ~999 Hz at AVG=4 / bus_every=1 (measured 2026-04-25
    # post-port; was ~230 Hz clamped on the MicroPython firmware). The TUI plot
    # caps at 15 fps regardless, so values above ~50 Hz mostly matter for CSV
    # recording / DOE captures. Header shows live max_hz echoed from firmware.
    SENSOR_RATES = [0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0, 1000.0]

    def _set_sensor_rate(self, rate: float) -> None:
        plot = self.query_one("#sensor-plot", SensorPlot)
        plot.sensor_rate = rate
        if self._client and self._client.connection_state == ConnectionState.CONNECTED:
            self._client.send_async({"cmd": "set_sensor_rate", "rate": rate})

    def action_sensor_rate_up(self) -> None:
        plot = self.query_one("#sensor-plot", SensorPlot)
        current = plot.sensor_rate
        for r in self.SENSOR_RATES:
            if r > current + 0.01:
                self._set_sensor_rate(r)
                return

    def action_sensor_rate_down(self) -> None:
        plot = self.query_one("#sensor-plot", SensorPlot)
        current = plot.sensor_rate
        for r in reversed(self.SENSOR_RATES):
            if r < current - 0.01:
                self._set_sensor_rate(r)
                return

    def action_cycle_viz(self) -> None:
        plot = self.query_one("#sensor-plot", SensorPlot)
        plot.cycle_mode()

    def action_cycle_cycle_window(self) -> None:
        """Step the cycle-window filter: off → 1 → 2 → 5 → 10 → off …
        Useful at high switching freq to lock the visible window to a
        small integer number of switching cycles for transient analysis."""
        plot = self.query_one("#sensor-plot", SensorPlot)
        plot.cycle_cycle_window()

    def action_toggle_plot_range(self) -> None:
        plot = self.query_one("#sensor-plot", SensorPlot)
        auto = plot.auto_v_range
        plot.auto_v_range = not auto
        plot.auto_i_range = not auto
        mode = "Auto" if not auto else "Fixed (2-4.5V, ±500mA)"
        self.notify(f"Plot range: {mode}", title="Range")

    # -- Actions: INA226 Sensor Profile --------------------------------------

    # User-facing AVG choices — intentionally a subset of firmware's full
    # 1/4/16/64/128/256/512/1024 ladder. 128+ averaging is slower than the
    # typical battery timescale and rarely useful in the live TUI.
    INA_AVG_STEPS = [1, 4, 16, 64]
    # Bus-voltage decimation cycle. Ordered most-data → least-data.
    # Firmware accepts any integer 0..1000; this list is just the TUI
    # shortcut. "v=off" (0) is rarely useful interactively but kept for
    # the DOE tool; the CLI flag bypasses this list entirely.
    BUS_EVERY_STEPS = [1, 2, 3, 5, 10, 0]

    def _apply_profile_reply(self, plot: "SensorPlot", reply: Optional[dict]) -> None:
        """Pull firmware-echoed max_hz / sensor_rate / avg / bus_every out of
        the reply so the header reflects real state, not just what we asked
        for. Same shape works for set_ina226_avg, set_bus_every, and the
        post-connect get_sensor_profile bootstrap. Silent no-op if the
        command failed or we're disconnected."""
        if not reply or not reply.get("ok"):
            return
        max_hz = reply.get("max_hz")
        if max_hz is not None:
            plot.max_hz = float(max_hz)
        sensor_rate = reply.get("sensor_rate")
        if sensor_rate is not None and sensor_rate > 0:
            plot.sensor_rate = float(sensor_rate)
        avg = reply.get("avg")
        if avg is not None:
            plot.ina_avg = int(avg)
        bus_every = reply.get("bus_every")
        if bus_every is not None:
            plot.bus_every = int(bus_every)
        # Force an immediate redraw. The sensor stream triggers a rate-
        # limited render on its own, but that can be up to ~70 ms away
        # at 15 fps — this way the header ticks as soon as the user taps.
        plot.refresh()

    def action_cycle_ina_avg(self) -> None:
        plot = self.query_one("#sensor-plot", SensorPlot)
        try:
            idx = self.INA_AVG_STEPS.index(plot.ina_avg)
        except ValueError:
            idx = 0
        new_avg = self.INA_AVG_STEPS[(idx + 1) % len(self.INA_AVG_STEPS)]
        plot.ina_avg = new_avg
        if self._client and self._client.connection_state == ConnectionState.CONNECTED:
            self._client.send_async({"cmd": "set_ina226_avg", "avg": new_avg})

    def action_cycle_bus_every(self) -> None:
        plot = self.query_one("#sensor-plot", SensorPlot)
        try:
            idx = self.BUS_EVERY_STEPS.index(plot.bus_every)
        except ValueError:
            idx = 0
        new_every = self.BUS_EVERY_STEPS[(idx + 1) % len(self.BUS_EVERY_STEPS)]
        plot.bus_every = new_every
        if self._client and self._client.connection_state == ConnectionState.CONNECTED:
            self._client.send_async({"cmd": "set_bus_every", "every": new_every})

    LOG_DURATIONS = [5, 10, 30, 60, 120, 300, 600, 1800, 3600]

    _stop_in_progress = False

    def _on_recording_done(self, desc: Optional[str] = None) -> None:
        """Called when any recording tier finishes.

        If a description is supplied the stop already happened (e.g. Mac
        tier finished via record() returning False). Otherwise we run the
        stop in a worker thread because the Pi-tier SCP can take several
        seconds and would otherwise freeze the UI at end-of-duration.
        """
        if desc is not None:
            self._stop_in_progress = False
            conn_bar = self.query_one("#conn-bar", ConnectionBar)
            conn_bar.conn_label = "Connected"
            self.notify(desc, title="Recording complete")
            return
        if self._stop_in_progress:
            return
        self._stop_in_progress = True
        conn_bar = self.query_one("#conn-bar", ConnectionBar)
        conn_bar.conn_label = "Finalizing log…"
        self.run_worker(self._stop_recording_worker(), exclusive=True, group="recording")

    async def _stop_recording_worker(self) -> None:
        """Run DataLogger.stop on an executor thread so the UI stays
        responsive during SCP / writer-thread drain."""
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            _, desc = await loop.run_in_executor(None, self._data_logger.stop)
            conn_bar = self.query_one("#conn-bar", ConnectionBar)
            conn_bar.conn_label = "Connected"
            self.notify(desc or "done", title="Recording complete")
        except Exception as exc:
            log.warning("stop-recording worker failed: %s", exc)
            try:
                conn_bar = self.query_one("#conn-bar", ConnectionBar)
                conn_bar.conn_label = "Connected"
            except Exception:
                pass
        finally:
            self._stop_in_progress = False

    def action_toggle_log(self) -> None:
        if self._data_logger.is_logging:
            # Early stop — kick off the worker-based stop flow (same as
            # the auto-stop path) so the UI doesn't block on SCP.
            self._on_recording_done()
        else:
            self._stop_in_progress = False
            plot = self.query_one("#sensor-plot", SensorPlot)
            tier, desc = self._data_logger.start(
                mode=self._circuit_mode,
                freq=self._current_freq,
                seq=self._current_seq,
                sensor_hz=plot.sensor_rate,
                client=self._client,
            )
            self.notify(desc, title=f"Recording [{tier.value}]")

    def action_log_duration_up(self) -> None:
        current = self._data_logger.duration_s
        for d in self.LOG_DURATIONS:
            if d > current + 0.5:
                self._data_logger.duration_s = d
                self._show_duration()
                return

    def action_log_duration_down(self) -> None:
        current = self._data_logger.duration_s
        for d in reversed(self.LOG_DURATIONS):
            if d < current - 0.5:
                self._data_logger.duration_s = d
                self._show_duration()
                return

    def _show_duration(self) -> None:
        from tui.data_logger import select_tier
        dur = self._data_logger.duration_s
        tier = select_tier(dur)
        if dur >= 60:
            dur_str = f"{dur/60:.0f}min"
        else:
            dur_str = f"{dur:.0f}s"
        self.notify(f"Record: {dur_str} -> {tier.value}", title="Duration")

    # -- Actions: Help & Reconnect -------------------------------------------

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_reconnect(self) -> None:
        if self._client:
            self._client.disconnect()
        # Kick off a fresh prescan; if an earlier one is still fresh the
        # dialog will pick it up immediately, otherwise the dialog falls
        # back to its own scan and the new prescan becomes useful on the
        # next pass.
        self._start_wifi_prescan()
        self.push_screen(
            ConnectDialog(prescan=self._take_prescan()),
            self._on_connect_dialog_result,
        )

    # -- Calibration --------------------------------------------------------

    def action_calibrate(self) -> None:
        if not self._client or self._client.connection_state != ConnectionState.CONNECTED:
            self.notify("Connect to a Pi first", severity="warning")
            return
        if self._data_logger.is_logging:
            self.notify("Stop recording before calibrating", severity="warning")
            return
        self.push_screen(CalibrationScreen(), self._on_calibration_result)

    def _on_calibration_result(self, result) -> None:
        if result is None:
            return
        if result.get("reset"):
            resp = self._client.reset_calibration()
            if resp and resp.get("ok"):
                self.notify("Calibration reset to defaults", title="Calibration")
            else:
                self.notify("Reset failed", severity="error")
            return
        names = ["P1", "P2", "N1", "N2"]
        for ch, gain, offset in result.get("calibrations", []):
            resp = self._client.set_calibration(ch, gain, offset)
            if resp and resp.get("ok"):
                self.notify(
                    f"{names[ch]}: gain={gain:.4f}, offset={offset*1000:.3f}mA",
                    title="Calibration Saved",
                )
            else:
                self.notify(f"{names[ch]} calibration failed", severity="error")

    # -- Data offload -------------------------------------------------------

    def action_offload(self) -> None:
        if not self._client or self._client.connection_state != ConnectionState.CONNECTED:
            self.notify("Not connected to a Pi", severity="warning")
            return
        if self._data_logger.is_logging:
            self.notify("Stop recording first", severity="warning")
            return
        self.notify("Offloading recordings from Pi...", title="Offload")
        self.run_worker(self._offload_worker(), exclusive=True, group="offload")

    async def _offload_worker(self) -> None:
        import asyncio
        import subprocess
        from pathlib import Path

        loop = asyncio.get_event_loop()
        host = self._client.host if self._client else ""
        if not host:
            return

        local_dir = Path.home() / "SwitchingCircuitV2_logs"
        local_dir.mkdir(parents=True, exist_ok=True)
        pi_dir = "/home/pi/SwitchingCircuitV2_logs"
        ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "ConnectTimeout=5"]

        # List remote files
        result = await loop.run_in_executor(None, lambda: subprocess.run(
            ["ssh", *ssh_opts, f"pi@{host}", f"ls {pi_dir}/pi_*.csv 2>/dev/null"],
            capture_output=True, text=True, timeout=10,
        ))
        if result.returncode != 0 or not result.stdout.strip():
            self.notify("No recordings found on Pi", title="Offload")
            return
        remote_files = [f.strip() for f in result.stdout.strip().splitlines()]

        # SCP all files
        copied = 0
        for remote_path in remote_files:
            fname = remote_path.split("/")[-1]
            local_path = local_dir / fname
            scp_result = await loop.run_in_executor(None, lambda rp=remote_path, lp=local_path: subprocess.run(
                ["scp", *ssh_opts, f"pi@{host}:{rp}", str(lp)],
                capture_output=True, timeout=30,
            ))
            if scp_result.returncode == 0:
                copied += 1

        if copied == 0:
            self.notify("Failed to copy any files", title="Offload", severity="error")
            return

        # Verify and delete
        deleted = 0
        for remote_path in remote_files:
            fname = remote_path.split("/")[-1]
            local_path = local_dir / fname
            if local_path.exists() and local_path.stat().st_size > 0:
                await loop.run_in_executor(None, lambda rp=remote_path: subprocess.run(
                    ["ssh", *ssh_opts, f"pi@{host}", f"rm {rp}"],
                    capture_output=True, timeout=10,
                ))
                deleted += 1

        self.notify(
            f"Copied {copied} file(s) to ~/SwitchingCircuitV2_logs/\nDeleted {deleted} from Pi",
            title="Offload complete",
        )

    # -- Schedule monitor ---------------------------------------------------

    def action_restart_monitor(self) -> None:
        """Reset the schedule monitor's PLAN clock back to step 0 cycle 0."""
        if not self._client or self._client.connection_state != ConnectionState.CONNECTED:
            return
        resp = self._client.send_command({"cmd": "schedule_monitor_restart"})
        if resp and resp.get("ok"):
            self.notify("Monitor PLAN clock restarted", title="Monitor")
        else:
            err = (resp or {}).get("error", "no schedule loaded")
            self.notify(f"Restart failed: {err}", title="Monitor", severity="warning")

    # -- Auto-follow (current-driven mode switching) ------------------------

    def _send_auto_follow(self, payload: dict) -> None:
        """Send an auto_follow_* command. Fire-and-forget: the next state
        broadcast (~33 ms later) carries the updated status."""
        if not self._client or self._client.connection_state != ConnectionState.CONNECTED:
            return
        self._client.send_async(payload)

    def action_auto_follow_toggle(self) -> None:
        new = not bool(self._latest_auto_follow.get("enabled", False))
        self._send_auto_follow({"cmd": "auto_follow_set_enabled", "enabled": new})

    def action_auto_follow_cycle_target(self) -> None:
        cur = self._latest_auto_follow.get("target_mode", "charge")
        nxt = "pulse_charge" if cur == "charge" else "charge"
        self._send_auto_follow({"cmd": "auto_follow_set_target", "target_mode": nxt})

    def action_auto_follow_enter_up(self) -> None:
        self._adjust_auto_follow(d_enter=0.001)

    def action_auto_follow_enter_down(self) -> None:
        self._adjust_auto_follow(d_enter=-0.001)

    def action_auto_follow_exit_up(self) -> None:
        self._adjust_auto_follow(d_exit=0.0005)

    def action_auto_follow_exit_down(self) -> None:
        self._adjust_auto_follow(d_exit=-0.0005)

    def _adjust_auto_follow(self, d_enter: float = 0.0, d_exit: float = 0.0) -> None:
        af = self._latest_auto_follow or {}
        i_enter = max(0.0001, af.get("i_enter_a", 0.005) + d_enter)
        i_exit = max(0.0, af.get("i_exit_a", 0.002) + d_exit)
        if i_enter <= i_exit:
            i_enter = i_exit + 0.0001
        self._send_auto_follow({
            "cmd": "auto_follow_set_thresholds",
            "i_enter_a": round(i_enter, 6),
            "i_exit_a": round(i_exit, 6),
        })

    CC_SETPOINT_STEPS = [
        0.0, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0,
    ]

    def _set_cc_setpoint(self, cc_a: float) -> None:
        """Set CC setpoint and auto-apply recommended enter/exit thresholds."""
        self._send_auto_follow({"cmd": "auto_follow_set_cc_setpoint", "cc_setpoint_a": cc_a})
        if cc_a > 0:
            i_enter = round(cc_a * 0.35, 6)
            i_exit = round(cc_a * 0.15, 6)
            self._send_auto_follow({
                "cmd": "auto_follow_set_thresholds",
                "i_enter_a": i_enter,
                "i_exit_a": i_exit,
            })

    def action_auto_follow_cc_up(self) -> None:
        af = self._latest_auto_follow or {}
        cur = af.get("cc_setpoint_a", 0.0)
        for s in self.CC_SETPOINT_STEPS:
            if s > cur + 0.0001:
                self._set_cc_setpoint(s)
                return

    def action_auto_follow_cc_down(self) -> None:
        af = self._latest_auto_follow or {}
        cur = af.get("cc_setpoint_a", 0.0)
        for s in reversed(self.CC_SETPOINT_STEPS):
            if s < cur - 0.0001:
                self._set_cc_setpoint(s)
                return

    # -- Network mode (client <-> AP) ---------------------------------------

    def action_ap_mode(self) -> None:
        """Ask the Pi to flip to AP mode. Confirms, sends the command, and
        retargets the client at 10.42.0.1 so the TUI reconnects once the
        MacBook is moved onto the new SSID."""
        if not self._client or self._client.connection_state != ConnectionState.CONNECTED:
            return
        self.push_screen(
            NetworkModeConfirm(
                mode="ap",
                message=(
                    "Flip Pi to AP mode?\n\n"
                    f"The Pi will drop its current WiFi and broadcast its own\n"
                    f"AP. Your MacBook will lose connection. To continue:\n\n"
                    f"   1. Join WiFi '[bold]pi_SW#[/]' (password: switching)\n"
                    f"   2. TUI will reconnect at [bold]{AP_GATEWAY}:5555[/]\n"
                ),
            ),
            self._on_network_mode_confirm,
        )

    def action_client_mode(self) -> None:
        """Ask the Pi to drop AP mode and return to client WiFi."""
        if not self._client or self._client.connection_state != ConnectionState.CONNECTED:
            return
        self.push_screen(
            NetworkModeConfirm(
                mode="client",
                message=(
                    "Return Pi to client WiFi?\n\n"
                    "The Pi will deactivate its AP and autoconnect to a known\n"
                    "WiFi (iPhone / Aquino). Both MacBook and Pi need to be on\n"
                    "the same WiFi for the TUI to reconnect — rediscovery runs\n"
                    "automatically via mDNS once you move your MacBook too.\n"
                ),
            ),
            self._on_network_mode_confirm,
        )

    def _on_network_mode_confirm(self, result: Optional[str]) -> None:
        if result not in ("ap", "client"):
            return  # user cancelled
        assert self._client is not None
        resp = self._client.send_command({"cmd": "set_network_mode", "mode": result})
        if not resp or not resp.get("ok"):
            err = (resp or {}).get("error", "no response")
            self._update_status_connection(
                self._client.connection_state == ConnectionState.CONNECTED,
                f"Network flip rejected: {err}",
            )
            return

        conn_bar = self.query_one("#conn-bar", ConnectionBar)
        if result == "ap":
            conn_bar.conn_label = f"Flipping to AP — join pi_SW#, then reconnecting at {AP_GATEWAY}..."
            # Point the client at 10.42.0.1; retry loop will patiently wait
            # for the MacBook to move to the new SSID.
            self._client.reconnect_to(AP_GATEWAY, self._initial_port)
        else:
            conn_bar.conn_label = "Flipping to client WiFi — rediscovering Pi..."
            # The Pi's AP-side address goes away; rediscover on shared WiFi.
            self._client.disconnect()
            discover_async(
                callback=self._on_auto_discover_result,
                on_status=self._on_auto_discover_status,
            )

    # -- Pi picker (swap which Pi the TUI is talking to) --------------------

    def action_switch_pi(self) -> None:
        """Open the Pi picker to swap the active Pi."""
        if self._data_logger.is_logging:
            self.notify(
                "Stop the active recording (l) before switching Pis.",
                title="Recording active",
                severity="warning",
            )
            return
        current_host = self._client.host if self._client else ""
        self.push_screen(
            PiPicker(current_host=current_host, cached_aps=self._fleet_cache or None),
            self._on_pi_picker_result,
        )

    def _on_pi_picker_result(self, ssid: Optional[str]) -> None:
        if not ssid:
            return
        self._current_pi_name = ssid.replace("pi_", "") if ssid.startswith("pi_") else ssid
        self._switch_pi(AP_GATEWAY)

    # -- Ethernet Pi picker (swap between statically-addressed Pis on the
    #    shared switch network — no WiFi join needed, all simultaneously
    #    reachable) ------------------------------------------------------

    def action_switch_pi_eth(self) -> None:
        """Open the Ethernet picker to swap the active Pi."""
        if self._data_logger.is_logging:
            self.notify(
                "Stop the active recording (l) before switching Pis.",
                title="Recording active",
                severity="warning",
            )
            return
        current_host = self._client.host if self._client else ""
        self.push_screen(
            EthPicker(current_host=current_host),
            self._on_eth_picker_result,
        )

    def _on_eth_picker_result(self, host: Optional[str]) -> None:
        if not host:
            return
        self._current_pi_name = host
        self._switch_pi(host)

    def _switch_pi(self, new_host: str) -> None:
        """Retarget the existing PiClient at a different Pi.

        Resets per-Pi state (mode/freq/seq/auto-panel) so the bootstrap
        flow on the new connection populates clean values rather than
        flashing the previous Pi's last-known frame.
        """
        if not self._client:
            return
        self._reset_pi_state()
        self._client.disconnect()
        self._client._stop_event.clear()
        save_host(new_host)
        self._do_connect(new_host, self._initial_port)

    def _reset_pi_state(self) -> None:
        """Clear per-Pi cached state ahead of a Pi switch. The next
        on-connect bootstrap (`_apply_profile_reply` + first state event)
        refills it from the new server."""
        self._circuit_mode = "idle"
        self._current_freq = 1.0
        self._current_seq = 0
        self._prev_mode = "idle"
        self._showing_auto_panel = False
        self._first_state_time = 0.0
        self._last_apply_time = 0.0
