"""
Switching Circuit V2 - Sensor Timeseries Plot Widget.

Two display modes:
  Compact — two overlaid plots (Voltage, Current) with all 4 sensors.
  Expanded — 8 individual plots in a 2-column grid (Voltage | Current)
             with rows +A (P1), +B (P2), -A (N1), -B (N2).

The app toggles `expanded` based on available terminal width.
Uses braille characters for sub-character resolution.
"""

from collections import deque
from time import monotonic
from typing import Optional

from textual.reactive import reactive
from textual.widget import Widget
from rich.text import Text


SENSOR_COLORS = {
    "P1": "green",
    "P2": "cyan",
    "N1": "yellow",
    "N2": "magenta",
}

SENSOR_LABELS = {
    "P1": "+A",
    "P2": "+B",
    "N1": "-A",
    "N2": "-B",
}

VIZ_MODES = ["line", "dot", "bar"]

# Cycle-window zoom steps. 0 = off (deque holds whatever it holds); >0 zooms
# the x-axis so the plot width spans exactly that many switching cycles. Data
# still scrolls left-to-right; this just controls how much time fits across
# the plot width — useful at high switching freq to keep a couple cycles in
# view instead of a blur.
CYCLE_WINDOWS = [0, 1, 2, 3, 5, 10]

# Braille character encoding
# Each char is 2 columns x 4 rows of dots
# Left col bits:  row0=0x01 row1=0x02 row2=0x04 row3=0x40
# Right col bits: row0=0x08 row1=0x10 row2=0x20 row3=0x80
BRAILLE_BASE = 0x2800
BRAILLE_DOT = [
    [0x01, 0x02, 0x04, 0x40],  # left column
    [0x08, 0x10, 0x20, 0x80],  # right column
]

# Block elements for bar mode (8 levels, bottom-up)
BAR_BLOCKS = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"

# Bit flags for colors — used in the bytearray braille grid to avoid set() allocations
COLOR_BITS = {"green": 1, "cyan": 2, "yellow": 4, "magenta": 8}
BIT_TO_COLOR = {1: "green", 2: "cyan", 4: "yellow", 8: "magenta"}


def _append_run(t: Text, cells) -> None:
    """Append a row of (char, style) cells, batching consecutive same-style
    runs into one Text.append() instead of one per character.

    Rich resolves/combines a Style at every span boundary during render, so
    one append per character (as this used to do) makes that cost scale
    with cell count instead of with the number of color changes — with
    plots hundreds to thousands of cells wide, that was the dominant cost
    in a py-spy profile of the live TUI. Runs of the same color (which is
    most of any plot — background "dim" cells, or several same-color dots
    in a row) now cost one span instead of N.
    """
    if not cells:
        return
    run_chars = [cells[0][0]]
    run_style = cells[0][1]
    for ch, style in cells[1:]:
        if style == run_style:
            run_chars.append(ch)
        else:
            t.append("".join(run_chars), style=run_style)
            run_chars = [ch]
            run_style = style
    t.append("".join(run_chars), style=run_style)


class SensorPlot(Widget):
    """Rolling timeseries plots with compact and expanded modes."""

    DEFAULT_CSS = """
    SensorPlot {
        width: 100%;
        height: auto;
        min-height: 14;
        padding: 0 2;
    }
    """

    sensor_rate: reactive[float] = reactive(50.0)
    viz_mode: reactive[str] = reactive("line")
    expanded: reactive[bool] = reactive(False)
    ina_avg: reactive[int] = reactive(4)           # INA226 sample averaging
    bus_every: reactive[int] = reactive(1)         # bus-voltage decimation
    max_hz: reactive[float] = reactive(376.0)      # firmware-computed cap
    # Live switching frequency, fed from the state stream. Used by the
    # cycle-window filter below — without it we can't translate "N cycles"
    # to a wallclock window.
    switching_freq: reactive[float] = reactive(1.0)
    # 0 = time-based (show whatever fits in the plot width). >0 = show only
    # the last N switching cycles. The braille engine still slices to the
    # plot width as a final cap, so this is a wallclock filter on the deque.
    cycle_window: reactive[int] = reactive(0)
    v_range: reactive[tuple] = reactive((2.0, 4.5))
    i_range: reactive[tuple] = reactive((-500.0, 500.0))
    auto_v_range: reactive[bool] = reactive(False)
    auto_i_range: reactive[bool] = reactive(False)
    # Compact-mode sparkline height in terminal rows. Each sensor's bar is
    # rendered as bar_row_height stacked block characters (BAR_BLOCKS gives
    # 8 levels per row, so total resolution = bar_row_height * 8).
    bar_row_height: reactive[int] = reactive(3)

    # Expanded plot sizing is driven by the available_width / available_height
    # reactives set by the app's on_resize handler.
    available_width: reactive[int] = reactive(0)
    available_height: reactive[int] = reactive(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._plot_width = 50
        self._plot_height = 6
        # 2000 samples is enough for ~10 cycles at 0.65 Hz at the firmware's
        # max stream rate (1300 sps), well past where cycle-window mode is
        # useful. Memory cost: 4 sensors * 2000 * 24 B ≈ 190 KB total.
        self._history: dict[str, deque] = {
            name: deque(maxlen=2000)
            for name in ["P1", "P2", "N1", "N2"]
        }
        self._last_update = 0.0
        self._last_render_time = 0.0
        self._min_render_interval = 1.0 / 15  # cap plot redraws at ~15 fps

    def push_data(self, sensors: dict) -> None:
        """Compat shim: ingest + rate-limited refresh in one call.

        Prefer append_data() + commit() from inside the app's batch_update()
        so the plot's refresh is coalesced with the other widget updates.
        """
        self.append_data(sensors)
        self.commit()

    def append_data(self, sensors: dict) -> None:
        """Ingest sensor data only. No refresh — pure deque append."""
        if not sensors:
            return
        now = monotonic()
        for name in ["P1", "P2", "N1", "N2"]:
            data = sensors.get(name)
            if data and "error" not in data:
                v = data.get("voltage", 0.0)
                i = data.get("current", 0.0)
                self._history[name].append((now, v, i))
        self._last_update = now

    def commit(self) -> None:
        """Rate-limited refresh. Call inside batch_update() to coalesce with
        other widget updates in the same tick."""
        now = monotonic()
        if now - self._last_render_time >= self._min_render_interval:
            self._last_render_time = now
            self.refresh()

    def cycle_mode(self) -> None:
        idx = VIZ_MODES.index(self.viz_mode)
        self.viz_mode = VIZ_MODES[(idx + 1) % len(VIZ_MODES)]

    def cycle_cycle_window(self) -> None:
        """Step through CYCLE_WINDOWS: off → 1 → 2 → 5 → 10 → off …"""
        try:
            idx = CYCLE_WINDOWS.index(self.cycle_window)
        except ValueError:
            idx = 0
        self.cycle_window = CYCLE_WINDOWS[(idx + 1) % len(CYCLE_WINDOWS)]

    def _filtered_history(self) -> dict:
        """Return the per-sensor history with the cycle-window filter applied.
        When cycle_window is 0 (off) or switching_freq is unknown, returns
        the deques as lists unchanged.

        Note on cycle period: matches recording_doe.py — actual cycle period
        is 2/freq because the engine's step_time formula maps frequency to
        steps, not whole cycles. Window duration = N * 2 / freq.
        """
        if self.cycle_window <= 0 or self.switching_freq <= 0:
            return {name: list(deq) for name, deq in self._history.items()}
        window_s = self.cycle_window * 2.0 / self.switching_freq
        cutoff = monotonic() - window_s
        out: dict = {}
        for name, deq in self._history.items():
            kept: list = []
            # Reverse-walk the deque so we stop as soon as we cross the cutoff.
            for entry in reversed(deq):
                if entry[0] < cutoff:
                    break
                kept.append(entry)
            kept.reverse()
            out[name] = kept
        return out

    # Server-side broadcast cap (server/command_server.py:_MAX_SUBSCRIBE_HZ).
    # When sensor_rate exceeds this, the TUI sees only the most recent
    # broadcast frame; in-between sensor samples are dropped before they
    # reach the deque. Pi-side recording still captures everything — only
    # the live plot is affected.
    _BROADCAST_HZ = 30

    def _render_footer(self, t: Text, width: int) -> None:
        """Bottom-right info box: samples/cycle and a truncation warning
        when the sensor rate exceeds the broadcast cap (so the user knows
        the live display is undersampled relative to the recorded data).
        """
        if self.switching_freq > 0:
            # Match recording_doe.py: cycle period = 2/freq.
            spc = 2.0 * self.sensor_rate / self.switching_freq
            spc_str = f"samples/cycle: {spc:.1f}"
        else:
            spc_str = "samples/cycle: —"

        truncated = self.sensor_rate > self._BROADCAST_HZ
        trunc_str = (
            f"  TRUNC: TUI={int(self._BROADCAST_HZ)}Hz < sensor={int(self.sensor_rate)}Hz"
            if truncated else ""
        )

        full_len = len(spc_str) + len(trunc_str)
        pad = max(1, width - full_len - 1)
        t.append(" " * pad, style="dim")
        t.append(spc_str, style="dim")
        if truncated:
            t.append(trunc_str, style="bold red")
        t.append("\n")

    def _stretch_active(self) -> bool:
        """True when the cycle-window zoom is on. The braille/bar builders
        use this to switch from left-padded sequential placement to
        index-proportional placement so N cycles' worth of data fills the
        full plot width."""
        return self.cycle_window > 0 and self.switching_freq > 0

    # -- Braille plot engine ---------------------------------------------------

    def _calc_range(self, all_series: dict[str, list[float]], data_width: int,
                     fixed_range: Optional[tuple] = None):
        """Get min/max across all series for the visible window."""
        if fixed_range is not None:
            return fixed_range[0], fixed_range[1]
        all_vals = []
        for vals in all_series.values():
            all_vals.extend(vals[-data_width:])
        if not all_vals:
            return 0.0, 1.0
        mn, mx = min(all_vals), max(all_vals)
        if mn == mx:
            mn -= 0.5
            mx += 0.5
        return mn, mx

    def _build_braille_plot(self, all_series: dict[str, list[float]],
                            width: int, height: int, connect: bool,
                            stretch: bool = False, fixed_range: Optional[tuple] = None):
        """Build a braille plot using a flat bytearray grid.

        ``stretch=False`` (default): each sample takes one column, anchored
        to the right edge of the plot — classic scrolling timeseries.

        ``stretch=True``: place sample i at column ``i*(data_width-1)/(n-1)``
        so the entire series is stretched to fill the plot width. Used by
        cycle-window zoom (``y``) so N cycles' worth of data spans the full
        x-axis. The connect logic still draws the dashed-vertical jump line
        (rows between prev_row and row at prev_col) and additionally
        diagonally interpolates rows across multi-column gaps.
        """
        data_width = width * 2
        dot_rows = height * 4
        mn, mx = self._calc_range(all_series, data_width, fixed_range)
        rng = mx - mn

        # Flat grid: grid[col * dot_rows + row] = color bit mask
        grid = bytearray(data_width * dot_rows)

        for color, vals in all_series.items():
            bit = COLOR_BITS.get(color, 1)
            if stretch:
                series = vals
            else:
                series = vals[-data_width:]
            n = len(series)
            if n == 0:
                continue

            offset = 0 if stretch else (data_width - n)

            prev_col = None
            prev_row = None
            for i, v in enumerate(series):
                if stretch:
                    if n == 1:
                        col = 0
                    else:
                        col = (i * (data_width - 1)) // (n - 1)
                else:
                    col = offset + i
                norm = (v - mn) / rng
                row = int((1.0 - norm) * (dot_rows - 1) + 0.5)
                if row < 0:
                    row = 0
                elif row >= dot_rows:
                    row = dot_rows - 1
                grid[col * dot_rows + row] |= bit

                if prev_col is not None and prev_row is not None:
                    # "Big jump" = row delta exceeds column delta (slope
                    # steeper than 45°). Render as a vertical fill at
                    # prev_col regardless of viz mode — so dot mode also
                    # picks up the dashed-vertical jump line on rapid
                    # magnitude changes. Smooth diagonals only fire when
                    # NOT a big jump and connect=True (line mode).
                    big_jump = abs(row - prev_row) > abs(col - prev_col)
                    if big_jump:
                        if prev_row != row:
                            r0, r1 = (prev_row, row) if prev_row < row else (row, prev_row)
                            for r in range(r0 + 1, r1):
                                grid[prev_col * dot_rows + r] |= bit
                    elif connect and abs(col - prev_col) > 1:
                        steps = abs(col - prev_col)
                        col_step = 1 if col > prev_col else -1
                        for s in range(1, steps):
                            cc = prev_col + s * col_step
                            cr = prev_row + (row - prev_row) * s // steps
                            if cr < 0:
                                cr = 0
                            elif cr >= dot_rows:
                                cr = dot_rows - 1
                            grid[cc * dot_rows + cr] |= bit

                prev_col = col
                prev_row = row

        # Render to braille characters with color
        rows = []
        for char_row in range(height):
            row_chars = []
            for char_col in range(width):
                pattern = BRAILLE_BASE
                cell_bits = 0
                for sub_col in range(2):
                    dc = char_col * 2 + sub_col
                    if dc >= data_width:
                        continue
                    base = dc * dot_rows
                    for sub_row in range(4):
                        dr = char_row * 4 + sub_row
                        if dr >= dot_rows:
                            continue
                        b = grid[base + dr]
                        if b:
                            pattern |= BRAILLE_DOT[sub_col][sub_row]
                            cell_bits |= b

                if pattern == BRAILLE_BASE:
                    row_chars.append((" ", "dim"))
                else:
                    # Pick the lowest-bit color (priority: green > cyan > yellow > magenta)
                    c = "dim"
                    for bit_val, color_name in BIT_TO_COLOR.items():
                        if cell_bits & bit_val:
                            c = color_name
                            break
                    row_chars.append((chr(pattern), c))
            rows.append(row_chars)

        return rows, (mn, mx)

    def _build_bar_plot(self, all_series: dict[str, list[float]],
                        width: int, height: int, stretch: bool = False,
                        fixed_range: Optional[tuple] = None):
        """Build a multi-row bar/sparkline plot.

        Each entry in the returned list is ``(color, [row_chars, ...])`` —
        ``height`` rows of BAR_BLOCKS chars stacked top-to-bottom. Total
        vertical resolution per cell is ``height * 8``, since each row
        carries one BAR_BLOCKS level (8 fill levels).

        ``stretch=False`` (default): take the last ``width`` samples and
        render left-padded — classic scrolling sparkline.

        ``stretch=True``: place the entire filtered series across the full
        plot width by mapping sample i to column ``i*(width-1)/(n-1)``.
        Last-write-wins per column. Used by cycle-window zoom.
        """
        data_width = width
        mn, mx = self._calc_range(all_series, data_width, fixed_range)
        rng = mx - mn
        if rng == 0:
            rng = 1.0
        if height < 1:
            height = 1
        total_levels = height * 8

        rows_out = []
        for color, vals in all_series.items():
            if stretch:
                # Sample → column with proportional spacing, then sample-
                # and-hold (forward-fill) so the bar stays continuous
                # instead of breaking up between sparsely-mapped columns.
                series = vals
                n = len(series)
                col_value: list = [None] * data_width
                if n > 0:
                    if n == 1:
                        col_value[0] = series[0]
                    else:
                        for i, v in enumerate(series):
                            c = (i * (data_width - 1)) // (n - 1)
                            col_value[c] = v
                    last = col_value[0]
                    for c in range(data_width):
                        if col_value[c] is None:
                            if last is not None:
                                col_value[c] = last
                        else:
                            last = col_value[c]
            else:
                # Original left-pad behavior.
                series = vals[-data_width:]
                n = len(series)
                pad = data_width - n
                col_value = [None] * pad + list(series)

            sensor_rows: list = []
            # Render top-to-bottom. Row r=0 is at the top; bars fill from
            # the bottom row upward. row_floor = how many total dot-rows
            # have to be filled before row r starts contributing.
            for r in range(height):
                row_chars = []
                row_floor = (height - 1 - r) * 8
                for c in range(data_width):
                    v = col_value[c]
                    if v is None:
                        row_chars.append((" ", "dim"))
                        continue
                    norm = (v - mn) / rng
                    if norm < 0.0:
                        norm = 0.0
                    elif norm > 1.0:
                        norm = 1.0
                    total = int(round(norm * total_levels))
                    level = total - row_floor
                    if level <= 0:
                        row_chars.append((" ", "dim"))
                    else:
                        if level > 8:
                            level = 8
                        row_chars.append((BAR_BLOCKS[level], color))
                sensor_rows.append(row_chars)
            rows_out.append((color, sensor_rows))

        return rows_out, (mn, mx)

    # -- Render helpers --------------------------------------------------------

    def _render_plot_header(self, t: Text, label: str, style: str,
                            mn: float, mx: float, unit: str) -> None:
        t.append(f" {label}", style=style)
        t.append(f"  {mn:.2f}{unit} \u2014 {mx:.2f}{unit}\n", style="dim")

    def _render_braille_rows(self, t: Text, rows: list, width: int) -> None:
        for row in rows:
            t.append(" \u2502", style="dim")
            _append_run(t, row)
            t.append("\u2502\n", style="dim")
        t.append(" \u2514", style="dim")
        t.append("\u2500" * width, style="dim")
        t.append("\u2518\n", style="dim")

    def _render_bar_rows(self, t: Text, bar_data: list, width: int) -> None:
        """Render multi-row sparklines. Each entry is ``(color, rows)`` where
        rows is a list of ``[(ch, color), ...]`` \u2014 one terminal line each."""
        for color, sensor_rows in bar_data:
            name = [n for n, c in SENSOR_COLORS.items() if c == color]
            label = name[0] if name else "?"
            for row_idx, row_chars in enumerate(sensor_rows):
                # Label only on the top row of each sensor's block.
                if row_idx == 0:
                    t.append(f" {label} ", style=f"bold {color}")
                else:
                    t.append("    ", style="dim")
                t.append("\u2502", style="dim")
                _append_run(t, row_chars)
                t.append("\u2502\n", style="dim")
        t.append("    \u2514", style="dim")
        t.append("\u2500" * width, style="dim")
        t.append("\u2518\n", style="dim")

    # -- Compact render (original: 2 overlaid plots) --------------------------

    def _render_compact_from(self, history: dict = None) -> Text:
        if history is None:
            history = self._history
        t = Text()
        pw = self._plot_width
        ph = self._plot_height
        mode = self.viz_mode

        # Header with legend
        t.append(" SENSORS ", style="bold cyan underline")
        t.append(f"[{self.sensor_rate:.0f}/{self.max_hz:.0f}Hz] ", style="dim")
        t.append(f"avg={self.ina_avg}", style="bold yellow")
        t.append(" ", style="dim")
        bus_tag = "v=off" if self.bus_every == 0 else f"v÷{self.bus_every}"
        t.append(bus_tag, style="bold yellow")
        t.append(f"  [{mode}]", style="dim cyan")
        if self.cycle_window > 0:
            t.append(f" cyc={self.cycle_window}", style="bold magenta")
        t.append("  ")
        t.append("/", style="bold white on dark_blue")
        t.append("- ", style="dim")
        t.append("*", style="bold white on dark_blue")
        t.append("+ ", style="dim")
        t.append("j", style="bold white on dark_blue")
        t.append("avg ", style="dim")
        t.append("k", style="bold white on dark_blue")
        t.append("vdec ", style="dim")
        t.append("v", style="bold white on dark_blue")
        t.append("viz  ", style="dim")
        for name in ["P1", "P2", "N1", "N2"]:
            color = SENSOR_COLORS[name]
            t.append("\u2588", style=color)
            t.append(f"{name} ", style=f"bold {color}")
        t.append("\n")

        # Gather series data
        v_series = {}
        i_series = {}
        for name in ["P1", "P2", "N1", "N2"]:
            hist = history.get(name, [])
            if hist:
                v_series[SENSOR_COLORS[name]] = [v for _, v, _ in hist]
                i_series[SENSOR_COLORS[name]] = [i * 1000 for _, _, i in hist]

        stretch = self._stretch_active()
        v_fixed = None if self.auto_v_range else self.v_range
        i_fixed = None if self.auto_i_range else self.i_range

        # -- Voltage --
        if mode in ("line", "dot"):
            connect = (mode == "line")
            v_rows, (v_mn, v_mx) = self._build_braille_plot(
                v_series, pw, ph, connect, stretch=stretch, fixed_range=v_fixed)
            self._render_plot_header(t, "VOLTAGE", "bold yellow", v_mn, v_mx, "V")
            self._render_braille_rows(t, v_rows, pw)
        else:
            bh = max(1, self.bar_row_height)
            bar_data, (v_mn, v_mx) = self._build_bar_plot(
                v_series, pw, bh, stretch=stretch, fixed_range=v_fixed)
            self._render_plot_header(t, "VOLTAGE", "bold yellow", v_mn, v_mx, "V")
            self._render_bar_rows(t, bar_data, pw)

        # -- Current --
        if mode in ("line", "dot"):
            i_rows, (i_mn, i_mx) = self._build_braille_plot(
                i_series, pw, ph, connect, stretch=stretch, fixed_range=i_fixed)
            self._render_plot_header(t, "CURRENT", "bold green", i_mn, i_mx, "mA")
            self._render_braille_rows(t, i_rows, pw)
        else:
            bh = max(1, self.bar_row_height)
            bar_data, (i_mn, i_mx) = self._build_bar_plot(
                i_series, pw, bh, stretch=stretch, fixed_range=i_fixed)
            self._render_plot_header(t, "CURRENT", "bold green", i_mn, i_mx, "mA")
            self._render_bar_rows(t, bar_data, pw)

        # Bottom-right info: samples/cycle + truncation warning.
        # Width matches the bottom border of the plot (" └" + pw + "┘").
        self._render_footer(t, pw + 3)

        return t

    # -- Expanded render (8 individual plots: 2 cols x 4 rows) ----------------

    def _expanded_dims(self) -> tuple:
        """Calculate per-plot width and height from available space.

        Layout overhead per row:
          "  │" + col_w + "│ │" + col_w + "│"  => col_w*2 + 8 chars
        So col_w = (avail_width - 8) // 2

        Height overhead: 2 lines header + 4 sensors * (1 label + ph plot + 1 border)
          => total rows = 2 + 4*(ph+2)   => ph = (avail_height - 2) / 4 - 2
        """
        aw = self.available_width if self.available_width > 0 else 70
        ah = self.available_height if self.available_height > 0 else 30

        col_w = max(16, (aw - 8) // 2)
        ph = max(3, (ah - 2) // 4 - 2)

        return col_w, ph

    def _render_expanded_from(self, history: dict = None) -> Text:
        if history is None:
            history = self._history
        t = Text()
        mode = self.viz_mode
        connect = (mode == "line")

        col_w, ph = self._expanded_dims()

        # Header
        t.append(" SENSORS ", style="bold cyan underline")
        t.append(f"[{self.sensor_rate:.0f}/{self.max_hz:.0f}Hz] ", style="dim")
        t.append(f"avg={self.ina_avg}", style="bold yellow")
        t.append(" ", style="dim")
        bus_tag = "v=off" if self.bus_every == 0 else f"v÷{self.bus_every}"
        t.append(bus_tag, style="bold yellow")
        t.append(f"  [{mode}]", style="dim cyan")
        if self.cycle_window > 0:
            t.append(f" cyc={self.cycle_window}", style="bold magenta")
        t.append("  ")
        t.append("j", style="bold white on dark_blue")
        t.append("avg ", style="dim")
        t.append("k", style="bold white on dark_blue")
        t.append("vdec ", style="dim")
        t.append("v", style="bold white on dark_blue")
        t.append("viz\n", style="dim")

        # Column headers — align to plot positions
        v_header = "VOLTAGE"
        i_header = "CURRENT"
        pad = col_w - len(v_header) + 2
        t.append(f"      {v_header}", style="bold yellow")
        t.append(" " * max(1, pad), style="dim")
        t.append(f"  {i_header}\n", style="bold green")

        sensor_order = ["P1", "P2", "N1", "N2"]
        stretch = self._stretch_active()
        v_fixed = None if self.auto_v_range else self.v_range
        i_fixed = None if self.auto_i_range else self.i_range

        for sensor in sensor_order:
            hist = history.get(sensor, [])
            color = SENSOR_COLORS[sensor]
            label = SENSOR_LABELS[sensor]

            # Extract voltage and current series
            v_vals = [v for _, v, _ in hist] if hist else []
            i_vals = [i * 1000 for _, _, i in hist] if hist else []

            v_series = {color: v_vals}
            i_series = {color: i_vals}

            if mode == "bar":
                # Bar mode in expanded view: each sensor gets its own block,
                # so use the full per-block height ph (matches line/dot).
                v_bar, (v_mn, v_mx) = self._build_bar_plot(
                    v_series, col_w, ph, stretch=stretch, fixed_range=v_fixed)
                i_bar, (i_mn, i_mx) = self._build_bar_plot(
                    i_series, col_w, ph, stretch=stretch, fixed_range=i_fixed)

                # Sensor label + range header
                t.append(f" {label} ", style=f"bold {color}")
                t.append(f"{v_mn:.2f}-{v_mx:.2f}V", style="dim")
                range_gap = col_w - 10
                t.append(" " * max(1, range_gap), style="dim")
                t.append(f"  {i_mn:.1f}-{i_mx:.1f}mA\n", style="dim")

                # Single-series builds \u2014 one entry per side, ph rows each.
                _, v_rows = v_bar[0] if v_bar else ("", [])
                _, i_rows = i_bar[0] if i_bar else ("", [])
                for row_idx in range(ph):
                    t.append("  \u2502", style="dim")
                    if row_idx < len(v_rows):
                        _append_run(t, v_rows[row_idx])
                    else:
                        t.append(" " * col_w, style="dim")
                    t.append("\u2502 \u2502", style="dim")
                    if row_idx < len(i_rows):
                        _append_run(t, i_rows[row_idx])
                    else:
                        t.append(" " * col_w, style="dim")
                    t.append("\u2502\n", style="dim")

                # Bottom border
                t.append("  \u2514", style="dim")
                t.append("\u2500" * col_w, style="dim")
                t.append("\u2518", style="dim")
                t.append(" ", style="dim")
                t.append("\u2514", style="dim")
                t.append("\u2500" * col_w, style="dim")
                t.append("\u2518\n", style="dim")
            else:
                # Line/dot mode: use braille characters
                v_rows, (v_mn, v_mx) = self._build_braille_plot(
                    v_series, col_w, ph, connect, stretch=stretch, fixed_range=v_fixed)
                i_rows, (i_mn, i_mx) = self._build_braille_plot(
                    i_series, col_w, ph, connect, stretch=stretch, fixed_range=i_fixed)

                # Sensor label + range header
                t.append(f" {label} ", style=f"bold {color}")
                t.append(f"{v_mn:.2f}-{v_mx:.2f}V", style="dim")
                range_gap = col_w - 10
                t.append(" " * max(1, range_gap), style="dim")
                t.append(f"  {i_mn:.1f}-{i_mx:.1f}mA\n", style="dim")

                # Render braille rows side-by-side
                for row_idx in range(ph):
                    t.append("  \u2502", style="dim")
                    _append_run(t, v_rows[row_idx])
                    t.append("\u2502", style="dim")

                    t.append(" ", style="dim")

                    t.append("\u2502", style="dim")
                    _append_run(t, i_rows[row_idx])
                    t.append("\u2502\n", style="dim")

                # Bottom border
                t.append("  \u2514", style="dim")
                t.append("\u2500" * col_w, style="dim")
                t.append("\u2518", style="dim")
                t.append(" ", style="dim")
                t.append("\u2514", style="dim")
                t.append("\u2500" * col_w, style="dim")
                t.append("\u2518\n", style="dim")

        # Bottom-right info: samples/cycle + truncation warning. Width is
        # both columns ("  \u2502" + col_w + "\u2502 \u2502" + col_w + "\u2502") = 2*col_w + 8.
        self._render_footer(t, 2 * col_w + 8)

        return t

    # -- Main render -----------------------------------------------------------

    def render(self) -> Text:
        history = self._filtered_history()
        if self.expanded:
            return self._render_expanded_from(history)
        return self._render_compact_from(history)

    def watch_sensor_rate(self, _: float) -> None:
        self._dirty = True

    def watch_ina_avg(self, _: int) -> None:
        self._dirty = True

    def watch_bus_every(self, _: int) -> None:
        self._dirty = True

    def watch_max_hz(self, _: float) -> None:
        self._dirty = True

    def watch_viz_mode(self, _: str) -> None:
        self._dirty = True

    def watch_expanded(self, _: bool) -> None:
        self._dirty = True

    def watch_available_width(self, _: int) -> None:
        self._dirty = True

    def watch_available_height(self, _: int) -> None:
        self._dirty = True

    def watch_switching_freq(self, _: float) -> None:
        # Only matters when cycle_window > 0; cheap to invalidate either way.
        self._dirty = True

    def watch_cycle_window(self, _: int) -> None:
        self._dirty = True

    def watch_bar_row_height(self, _: int) -> None:
        self._dirty = True
