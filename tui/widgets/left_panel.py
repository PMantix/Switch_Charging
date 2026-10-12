"""
Switching Circuit V2 - Left Panel: Key Bindings Reference.
"""

from textual.widget import Widget
from rich.text import Text


class LeftPanel(Widget):
    """Keyboard controls reference (mode/sequence shown in right panel)."""

    # Set by the app in --coincell mode: finer auto-follow steps + banner.
    coincell: bool = False

    DEFAULT_CSS = """
    LeftPanel {
        width: 100%;
        height: auto;
        padding: 0 1;
    }
    """

    def render(self) -> Text:
        t = Text()

        t.append(" KEY BINDINGS\n", style="bold cyan underline")
        t.append("\n")
        if self.coincell:
            t.append(" COIN-CELL MODE\n", style="bold black on yellow")
            t.append(" AVG 128, current in \u00b5A\n", style="dim")
            t.append(" i then z to zero offsets\n", style="dim")
            t.append(" u to leave\n\n", style="dim")
        enter_step = "0.1mA" if self.coincell else "1mA"
        exit_step = "0.05mA" if self.coincell else "0.5mA"

        sections = [
            ("FREQUENCY", [
                (" w/s ", "+/- 0.1 Hz"),
                (" e/d ", "+/- 1.0 Hz"),
                (" W/S ", "+/- 10 Hz"),
                (" =/- ", "+/- 0.1 Hz"),
            ]),
            ("SENSORS", [
                (" / * ", "Rate -/+"),
                ("  j  ", "INA226 AVG"),
                ("  k  ", "Bus-V decim"),
                ("  v  ", "Cycle plot mode"),
                ("  y  ", "Cycle window N"),
                ("  R  ", "Range auto/fixed"),
            ]),
            ("RECORD", [
                ("  l  ", "Start/stop"),
                (" [ ] ", "Duration -/+"),
                ("  O  ", "Offload to Mac"),
            ]),
            ("AUTO-FOLLOW", [
                ("  F  ", "Toggle"),
                ("  T  ", "Cycle target"),
                (" ,/. ", f"I_enter -/+{enter_step}"),
                (" ;/' ", f"I_exit -/+{exit_step}"),
                (" </> ", "CC set 0-100mA" if self.coincell else "CC setpoint"),
            ]),
            ("SCHEDULE MONITOR", [
                ("  a  ", "Load schedule"),
                ("  M  ", "Restart clock"),
            ]),
            ("CALIBRATE", [
                ("  B  ", "INA226 Cal Wizard"),
                ("  z  ", "Zero current (idle)"),
                ("  u  ", "Coin-cell mode"),
            ]),
            ("NETWORK", [
                ("  P  ", "Switch Pi (WiFi)"),
                ("  E  ", "Switch Pi (Eth)"),
                ("  r  ", "Reconnect"),
                (" A/C ", "Pi AP on/off"),
            ]),
            ("SCROLL", [
                ("PgUp/Dn", "Center column"),
                ("S+PgUp/Dn", "Right column"),
                ("S+\u2190/\u2192", "Sideways"),
            ]),
            ("OTHER", [
                (" Tab ", "Status/Monitor"),
                ("  D  ", "Latency probe"),
                ("  ?  ", "Full help"),
                ("  q  ", "Quit"),
            ]),
        ]

        for section_name, keys in sections:
            t.append(f" {section_name}\n", style="bold white")
            for key, desc in keys:
                t.append("  ")
                t.append(f" {key} ", style="bold white on dark_blue")
                t.append(f" {desc}\n", style="dim")
            t.append("\n")

        return t
