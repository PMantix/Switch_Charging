"""
Switching Circuit V2 - Measurement Panel.

Right-column view shown while the board is in MEASURE mode (server/measure.py):
the held configuration, how long it has been held, live mean / s.d. of all
four sensors (after the server's settle time), and the log the board is
writing. Keys: 1-4 pairs, 5 pass, 6 open, t marker, n leave.
"""

import json
import re

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget

# key -> (config, terminals, FETs)
CONFIG_KEYS = [
    ("1", "pair1", "+A/-B", "P1 N2"),
    ("2", "pair2", "+A/-A", "P1 N1"),
    ("3", "pair3", "+B/-A", "P2 N1"),
    ("4", "pair4", "+B/-B", "P2 N2"),
    ("5", "pass", "all", "all on"),
    ("6", "open", "none", "all off"),
]
KEY_TO_CONFIG = {k: c for k, c, _, _ in CONFIG_KEYS}
SENSOR_TERMINAL = (("P1", "+A"), ("P2", "+B"), ("N1", "-A"), ("N2", "-B"))


def _fmt(x, scale, fmt):
    return format(x * scale, fmt) if isinstance(x, (int, float)) else "—"


def render_measure(t: Text, m: dict, coincell: bool = False) -> None:
    """Append the measurement view for a measure_status dict."""
    t.append(" MEASURE", style="bold magenta underline")
    t.append("  held state\n\n", style="dim")
    if not m or not m.get("active"):
        t.append(" Not in measurement mode.\n", style="dim")
        t.append(" Press ", style="dim")
        t.append(" n ", style="bold white on dark_blue")
        t.append(" to enter (from pass-through:\n no break in the circuit).\n", style="dim")
        return

    cfg = m.get("config")
    for key, name, term, fets in CONFIG_KEYS:
        sel = name == cfg
        t.append(" ● " if sel else " ○ ", style="bold magenta" if sel else "dim")
        t.append(f" {key} ", style="bold white on dark_blue")
        t.append(f" {name:<6}", style="bold white" if sel else "dim")
        t.append(f"{term:<7}", style="white" if sel else "dim")
        t.append(f"{fets}\n", style="dim")

    since = m.get("since_s")
    t.append("\n Held   ", style="dim")
    t.append(f"{since:,.0f} s\n" if isinstance(since, (int, float)) else "—\n", style="bold white")
    prof = m.get("profile") or {}
    t.append(" Sensor ", style="dim")
    hz = prof.get("hz")
    t.append(f"AVG {prof.get('avg', '?')} · bus {prof.get('bus_every', '?')} · "
             f"{format(hz, 'g') if isinstance(hz, (int, float)) else '?'} Hz\n", style="white")

    live = m.get("live") or {}
    n = live.get("n", 0)
    settle = m.get("live_settle_s", 0)
    t.append("\n LIVE", style="bold cyan underline")
    t.append(f"  n={n} (skips {settle:g} s)\n", style="dim")
    i_unit, i_scale, i_fmt = ("µA", 1e6, "+9.1f") if coincell else ("mA", 1e3, "+8.3f")
    t.append(f"        I {i_unit:<3}    s.d.       V\n", style="dim")
    for s, term in SENSOR_TERMINAL:
        iv = live.get(f"{s}_i") or [None, None]
        vv = live.get(f"{s}_v") or [None, None]
        t.append(f" {s} {term} ", style="bold white")
        t.append(f"{_fmt(iv[0], i_scale, i_fmt)}", style="white")
        t.append(f" {_fmt(iv[1], i_scale, '7.3f' if not coincell else '7.1f')}", style="dim")
        t.append(f"  {_fmt(vv[0], 1, '7.4f')}\n", style="white")

    t.append("\n Log    ", style="dim")
    log = m.get("log") or "—"
    t.append(f"{re.split(r'[/\\]', log)[-1]}\n", style="white")
    t.append(f"        {m.get('frames', 0)} frames · {m.get('markers', 0)} markers\n", style="dim")

    t.append("\n ", style="dim")
    t.append(" t ", style="bold white on dark_blue")
    t.append(" marker  ", style="dim")
    t.append(" n ", style="bold white on dark_blue")
    t.append(" leave\n", style="dim")
    t.append(" Changes never break the circuit,\n except 'open': zero current only.\n",
             style="dim italic")


class MeasurePanel(Widget):
    """Right-column measurement view (shown automatically in MEASURE)."""

    DEFAULT_CSS = """
    MeasurePanel {
        width: 100%;
        height: auto;
        padding: 0 1;
    }
    """

    measure: reactive[dict] = reactive({}, layout=True)
    coincell: reactive[bool] = reactive(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._cache_key = None
        self._cache = None

    def render(self) -> Text:
        m = dict(self.measure or {})
        if isinstance(m.get("since_s"), (int, float)):
            m["since_s"] = int(m["since_s"])           # redraw once a second, not every broadcast
        try:
            key = json.dumps([m, self.coincell], sort_keys=True, default=str)
        except (TypeError, ValueError):
            key = None
        if key is not None and key == self._cache_key and self._cache is not None:
            return self._cache
        t = Text()
        render_measure(t, m, self.coincell)
        self._cache_key, self._cache = key, t
        return t
