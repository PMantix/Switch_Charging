"""
Switching Circuit V2 - Right Panel: Status, Frequency, Sequence, Mode.
"""

from textual.reactive import reactive
from textual.widget import Widget
from rich.text import Text


SEQUENCES = [
    [5, 5, 5, 5],
    [0, 1, 2, 3],
    [0, 1, 3, 2],
    [0, 2, 1, 3],
    [0, 2, 3, 1],
    [0, 3, 1, 2],
    [0, 3, 2, 1],
    [4, 4, 4, 4],
]

MODE_STYLES = {
    "idle": "bold red",
    "charge": "bold green",
    "discharge": "bold yellow",
    "pulse_charge": "bold magenta",
    "debug": "bold cyan",
    "measure": "bold magenta",
}

MODE_LABELS = {
    "idle": "IDLE",
    "charge": "CHARGE",
    "discharge": "DISCHARGE",
    "pulse_charge": "PULSE",
    "debug": "DEBUG",
    "measure": "MEASURE",
}

MODE_KEYS = {
    "idle": "i",
    "charge": "c",
    "discharge": "x",
    "pulse_charge": "p",
    "debug": "g",
    "measure": "n",
}


def _fmt_ma(ma: float) -> str:
    """mA with enough decimals for coin-cell (0.1 mA steps, thresholds at
    35%/15% of that) without cluttering the normal tens-of-mA values."""
    if abs(ma) < 10:
        return f"{ma:.2f}"
    if abs(ma) < 100:
        return f"{ma:.1f}"
    return f"{ma:.0f}"


class RightPanel(Widget):
    """Status, frequency, sequence, and mode."""

    DEFAULT_CSS = """
    RightPanel {
        width: 100%;
        height: auto;
        padding: 0 1;
    }
    """

    mode: reactive[str] = reactive("idle")
    sequence: reactive[int] = reactive(0)
    frequency: reactive[float] = reactive(1.0)
    step: reactive[int] = reactive(0)
    connected: reactive[bool] = reactive(False)
    conn_status: reactive[str] = reactive("Disconnected")
    # Pi reachable but its RP2040 link is down (sensor data frozen).
    link_down: reactive[bool] = reactive(False)
    current_path: reactive[str] = reactive("")
    auto_follow: reactive[dict] = reactive({})

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._text_cache = None
        self._text_cache_key = None

    def render(self) -> Text:
        # Auto-follow goes into the cache key as a tuple of its load-bearing
        # fields — the live current/voltage tick on every broadcast and we
        # don't want to invalidate the cache for those alone.
        af = self.auto_follow or {}
        af_key = (
            bool(af.get("enabled")),
            bool(af.get("active")),
            af.get("target_mode"),
            round(af.get("i_enter_a", 0.0), 6),
            round(af.get("i_exit_a", 0.0), 6),
            round(af.get("avg_current_a", 0.0), 4),
            af.get("engage_delay_s"),
            af.get("engage_pending_s"),
            af.get("pause_every_s"), af.get("pause_s"), af.get("in_pause"), af.get("pauses_this_charge"),
        )
        key = (self.mode, self.sequence, self.frequency, self.step,
               self.connected, self.conn_status, self.link_down,
               self.current_path, af_key)
        if key == self._text_cache_key and self._text_cache is not None:
            return self._text_cache
        t = self._render_impl()
        self._text_cache = t
        self._text_cache_key = key
        return t

    def _render_impl(self) -> Text:
        t = Text()
        t.append(" Tab", style="bold white on dark_blue")
        t.append(" \u2192 Schedule monitor\n\n", style="dim")

        # -- Status --
        t.append(" STATUS\n", style="bold cyan underline")
        t.append("\n")

        ms = MODE_STYLES.get(self.mode, "white")
        t.append(" Mode   ", style="dim")
        t.append(f" {self.mode.upper():<10} ", style=f"{ms} reverse")
        t.append("\n")

        t.append(" Freq   ", style="dim")
        t.append(f" {self.frequency:6.1f} Hz ", style="bold white on dark_blue")
        t.append("\n")

        period = 1.0 / self.frequency if self.frequency > 0 else 0
        t.append(" Period ", style="dim")
        if period < 1.0:
            t.append(f" {period*1000:.1f} ms", style="white")
        else:
            t.append(f" {period:.3f} s", style="white")
        t.append("\n")

        t.append(" Step   ", style="dim")
        t.append(f" {self.step + 1}", style="bold white")
        t.append(" / 4", style="dim")
        t.append("\n")

        # Connection
        if self.connected and self.link_down:
            t.append(" \u26a0 ", style="bold yellow")
            t.append(self.conn_status, style="bold yellow")
        elif self.connected:
            t.append(" \u25cf ", style="bold green")
            t.append(self.conn_status, style="green")
        else:
            t.append(" \u25cb ", style="bold red")
            t.append(self.conn_status, style="red")
        t.append("\n\n")

        # -- Mode selector --
        t.append(" MODE", style="bold cyan underline")
        t.append("\n\n")

        for m in ["idle", "charge", "discharge", "pulse_charge", "debug", "measure"]:
            sel = (m == self.mode)
            radio = "\u25cf" if sel else "\u25cb"
            style = MODE_STYLES.get(m, "white")
            key = MODE_KEYS.get(m, "?")
            label = MODE_LABELS.get(m, m.upper())
            if sel:
                t.append(f" {radio} ", style=style)
                t.append(f"{label}", style=f"{style}")
            else:
                t.append(f" {radio} ", style="dim")
                t.append(f"{label}", style="dim")
            t.append(f"  ", style="dim")
            t.append(f" {key} ", style="bold white on dark_blue")
            t.append("\n")

        if self.mode == "debug":
            t.append("\n")
            t.append(" DEBUG: ", style="bold cyan")
            t.append("1", style="bold white on dark_blue")
            t.append(" P1  ", style="dim")
            t.append("2", style="bold white on dark_blue")
            t.append(" P2  ", style="dim")
            t.append("3", style="bold white on dark_blue")
            t.append(" N1  ", style="dim")
            t.append("4", style="bold white on dark_blue")
            t.append(" N2\n", style="dim")

        t.append("\n")

        # -- H-Bridge States --
        t.append(" H-BRIDGE STATES", style="bold cyan underline")
        t.append("\n\n")

        hb_states = [
            ("0", "P1+N1", "+A/-A",  "bold green"),
            ("1", "P1+N2", "+A/-B",  "bold green"),
            ("2", "P2+N1", "+B/-A",  "bold green"),
            ("3", "P2+N2", "+B/-B",  "bold green"),
            ("4", "ALL",   "All ON", "bold yellow"),
            ("5", "---",   "Idle",   "bold red"),
        ]
        for num, fets, desc, style in hb_states:
            t.append(f" {num} ", style="bold white")
            t.append(f"{fets:<6}", style=style)
            t.append(f" {desc}\n", style="dim")
        t.append("\n")

        # -- Sequences --
        t.append(" SEQUENCES", style="bold cyan underline")
        t.append("\n\n")

        for i in range(8):
            sel = (i == self.sequence)
            steps = str(SEQUENCES[i])
            if sel:
                t.append(f" \u25b8 ", style="bold green")
                t.append(f"{i+1}", style="bold green")
                t.append(f" {steps}", style="bold white")
            else:
                t.append(f"   ", style="dim")
                t.append(f"{i+1}", style="dim")
                t.append(f" {steps}", style="dim")
            t.append("\n")
        t.append("\n")
        t.append("  ", style="dim")
        t.append(" 1-8 ", style="bold white on dark_blue")
        t.append(" Select sequence\n", style="dim")

        # -- Auto-Follow --
        self._render_auto_follow(t)

        # -- Current Path --
        if self.current_path:
            t.append("\n")
            t.append(" PATH", style="bold cyan underline")
            t.append("\n")
            t.append(f" {self.current_path}\n", style="dim italic")

        return t

    def _render_auto_follow(self, t: Text) -> None:
        af = self.auto_follow or {}
        t.append("\n")
        t.append(" AUTO-FOLLOW", style="bold cyan underline")
        t.append("\n\n")

        enabled = bool(af.get("enabled", False))
        active = bool(af.get("active", False))
        target = af.get("target_mode", "charge")
        i_enter_ma = af.get("i_enter_a", 0.0) * 1000.0
        i_exit_ma = af.get("i_exit_a", 0.0) * 1000.0
        avg_i_ma = af.get("avg_current_a", 0.0) * 1000.0
        cc_ma = af.get("cc_setpoint_a", 0.0) * 1000.0
        rec_enter_ma = af.get("rec_enter_a", 0.0) * 1000.0
        rec_exit_ma = af.get("rec_exit_a", 0.0) * 1000.0

        t.append(" Enable ", style="dim")
        if enabled:
            t.append(" ON ", style="bold green reverse")
        else:
            t.append(" OFF ", style="dim reverse")
        t.append("  ", style="dim")
        t.append(" F ", style="bold white on dark_blue")
        t.append("\n")

        t.append(" Target ", style="dim")
        t.append(f" {target:<13}", style="bold white")
        t.append(" T ", style="bold white on dark_blue")
        t.append("\n")

        t.append(" Enter  ", style="dim")
        t.append(f" {_fmt_ma(i_enter_ma):>5} mA ", style="bold white")
        t.append("  ", style="dim")
        t.append(" , ", style="bold white on dark_blue")
        t.append(" / ", style="dim")
        t.append(" . ", style="bold white on dark_blue")
        t.append("\n")

        t.append(" Exit   ", style="dim")
        t.append(f" {_fmt_ma(i_exit_ma):>5} mA ", style="bold white")
        t.append("  ", style="dim")
        t.append(" ; ", style="bold white on dark_blue")
        t.append(" / ", style="dim")
        t.append(" ' ", style="bold white on dark_blue")
        t.append("\n")

        # CC setpoint + recommendations
        t.append(" CC set ", style="dim")
        if cc_ma > 0:
            t.append(f" {_fmt_ma(cc_ma):>5} mA ", style="bold white")
        else:
            t.append("   off  ", style="dim")
        t.append("  ", style="dim")
        t.append(" < ", style="bold white on dark_blue")
        t.append(" / ", style="dim")
        t.append(" > ", style="bold white on dark_blue")
        t.append("\n")

        if cc_ma > 0:
            t.append("  rec   ", style="dim")
            t.append(f" {_fmt_ma(rec_enter_ma)}/{_fmt_ma(rec_exit_ma)} mA", style="italic yellow")
            t.append("\n")

        delay_s = af.get("engage_delay_s") or 0.0
        t.append(" Delay  ", style="dim")
        t.append(f" {delay_s:g} s" if delay_s else "  off", style="bold white" if delay_s else "dim")
        t.append("   (sweep editor ", style="dim")
        t.append(" f ", style="bold white on dark_blue")
        t.append(")\n", style="dim")

        if "pause_every_s" in af:            # servers older than the pause option don't report it
            every, length = af.get("pause_every_s") or 0.0, af.get("pause_s") or 0.0
            t.append(" Pauses ", style="dim")
            if every:
                t.append(f" {length:g} s / {every:g} s ", style="bold white")
                cnt = af.get("pauses_this_charge") or 0
                if cnt:
                    t.append(f"x{cnt} ", style="dim")
            else:
                t.append("  off     ", style="dim")
            t.append(" o ", style="bold white on dark_blue")
            t.append("\n")

        t.append(" Live i ", style="dim")
        t.append(f" {avg_i_ma:+6.2f} mA\n", style="white")

        t.append(" State  ", style="dim")
        if not enabled:
            t.append(" off\n", style="dim")
        elif active and af.get("in_pause"):
            t.append(" PAUSE (pass-through)\n", style="bold yellow")
        elif active:
            t.append(" SWITCHING\n", style="bold green")
        elif af.get("engage_pending_s") is not None:
            t.append(f" starting in {max(0.0, delay_s - af['engage_pending_s']):.0f} s\n",
                     style="bold cyan")
        else:
            t.append(" transparent\n", style="bold yellow")

    def watch_auto_follow(self, _: dict) -> None:
        self.refresh()

    def watch_mode(self, _: str) -> None:
        self.refresh()

    def watch_sequence(self, _: int) -> None:
        self.refresh()

    def watch_frequency(self, _: float) -> None:
        self.refresh()

    def watch_step(self, _: int) -> None:
        self.refresh()

    def watch_connected(self, _: bool) -> None:
        self.refresh()

    def watch_conn_status(self, _: str) -> None:
        self.refresh()

    def watch_current_path(self, _: str) -> None:
        self.refresh()
