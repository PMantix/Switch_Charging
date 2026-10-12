"""
Switching Circuit V2 - Simulation Banner Widget.

Boxed, blinking "SIMULATION" strip docked at the top of the screen in
``python -m tui --sim`` so nobody mistakes simulated data for a real
circuit. The blink just flips a bool and refreshes this one widget —
no CSS class toggling, so it doesn't restyle the rest of the app.
"""

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget

SIM_TEXT = "SIMULATION — NOT CONNECTED TO HARDWARE"
_BLINK_S = 0.6


class SimBanner(Widget):
    """Blinking simulation warning."""

    DEFAULT_CSS = """
    SimBanner {
        dock: top;
        width: 100%;
        height: 3;
        border: heavy magenta;
        content-align: center middle;
    }
    """

    _on: reactive[bool] = reactive(True)

    def on_mount(self) -> None:
        self.set_interval(_BLINK_S, self._blink)

    def _blink(self) -> None:
        self._on = not self._on

    def render(self) -> Text:
        style = "bold reverse magenta" if self._on else "bold magenta"
        return Text(f"  ▶ {SIM_TEXT} ◀  ", style=style)
