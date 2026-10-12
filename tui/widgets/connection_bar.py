"""
Switching Circuit V2 - Connection Bar Widget.

Shows connection status, Pi hostname/IP, and latency.
"""

from textual.reactive import reactive
from textual.widget import Widget
from rich.text import Text


AUTO_STATE_STYLES = {
    "cc_charge": ("CC CHG", "bold green"),
    "cv_charge": ("CV CHG", "bold yellow"),
    "rest": ("REST", "bold dim"),
    "discharge": ("DISCH", "bold red"),
    "unknown": ("???", "bold magenta"),
}


class ConnectionBar(Widget):
    """Top bar showing connection info and auto mode status."""

    DEFAULT_CSS = """
    ConnectionBar {
        width: 100%;
        height: 1;
        background: $surface;
    }
    """

    connected: reactive[bool] = reactive(False)
    host: reactive[str] = reactive("")
    latency_ms: reactive[float] = reactive(0.0)
    conn_label: reactive[str] = reactive("Disconnected")
    # Pi reachable but its RP2040 link is down (sensor data frozen).
    link_down: reactive[bool] = reactive(False)
    probe_text: reactive[str] = reactive("")
    _auto_data: dict = {}

    def render(self) -> Text:
        t = Text()

        if self.connected and self.link_down:
            t.append(" \u26a0 ", style="bold yellow")
            t.append("RP2040 LINK DOWN — sensor data frozen", style="bold yellow")
            t.append(f"  {self.host}", style="bold white")
        elif self.connected:
            t.append(" \u25cf ", style="bold green")
            t.append("Connected", style="green")
            t.append(f"  {self.host}", style="bold white")
            if self.latency_ms > 0:
                t.append(f"  ({self.latency_ms:.0f}ms)", style="dim")
        else:
            t.append(" \u25cb ", style="bold red")
            t.append(self.conn_label, style="red")
            if self.host:
                t.append(f"  {self.host}", style="dim")

        # Schedule monitor status (right side of bar)
        ad = self._auto_data
        if ad and ad.get("loaded"):
            plan = ad.get("plan", {}) or {}
            obs = ad.get("observed", {}) or {}
            step_name = plan.get("step_name", "?")
            cycle = plan.get("cycle", 0) + 1
            total_cycles = ad.get("total_cycles", 1)
            detected = obs.get("state", "unknown")
            divergence = ad.get("divergence", "unknown")
            running = ad.get("running", False)
            det_label, det_style = AUTO_STATE_STYLES.get(detected, ("?", "white"))

            t.append("  \u2502 ", style="dim")
            t.append("MON ", style="bold blue")
            if not running:
                t.append("STOPPED ", style="bold yellow")
            t.append(f"{step_name}", style="bold white")
            t.append(f" [{cycle}/{total_cycles}]", style="dim")
            t.append("  ", style="dim")
            t.append(f"{det_label}", style=det_style)
            if divergence == "match":
                t.append(" \u2714", style="bold green")
            elif divergence == "mismatch":
                t.append(" \u2718", style="bold red")
            else:
                t.append(" ?", style="dim yellow")

        if self.probe_text:
            t.append("  │ ", style="dim")
            t.append(self.probe_text, style="bold cyan")

        return t

    def update_auto_status(self, auto_data: dict) -> None:
        """Store auto status data. Only refreshes if data actually changed."""
        if auto_data == self._auto_data:
            return
        self._auto_data = auto_data
        self.refresh()

    def watch_connected(self, _: bool) -> None:
        self.refresh()

    def watch_host(self, _: str) -> None:
        self.refresh()

    def watch_latency_ms(self, _: float) -> None:
        self.refresh()

    def watch_conn_label(self, _: str) -> None:
        self.refresh()

    def watch_link_down(self, _: bool) -> None:
        self.refresh()

    def watch_probe_text(self, _: str) -> None:
        self.refresh()
