"""
Switching Circuit V2 - Frequency Sweep panel + editor.

SweepPanel shows the server's per-cycle frequency sweep (see
server/freq_sweep.py) in the right column: the entry list with the
running / next entry marked, end behaviour, cycle count and whether a
discharge has been seen yet. SweepScreen is the modal editor opened
with `f`.

Entry shorthand used in both (positions shown 1-based):
    300      charge at 300 Hz
    500p     pulse_charge at 500 Hz
    pass     passthrough control cycle (no switching)

Edits are sent straight to the server as sweep_* commands and apply at
the next charge onset. Commands go out fire-and-forget, so the server's
reply (and any validation error) never reaches the TUI — entries are
validated here with the server's own normalize_entry before sending.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Static

from server.freq_sweep import normalize_entry

_MODE_LABEL = {"charge": "charge", "pulse_charge": "pulse", "passthrough": "pass"}


def parse_entry(text: str) -> dict:
    """'300' / '500p' / 'pass' -> normalized entry dict. Raises ValueError."""
    s = text.strip().lower()
    if not s:
        raise ValueError("empty entry")
    if s in ("pass", "passthrough", "-"):
        return normalize_entry({"mode": "passthrough"})
    mode = None
    if s.endswith("p"):
        mode, s = "pulse_charge", s[:-1].strip()
    elif s.endswith("c"):
        mode, s = "charge", s[:-1].strip()
    s = s.removesuffix("hz").strip()
    try:
        hz = float(s)
    except ValueError:
        raise ValueError(f"{text.strip()!r} is not a frequency (try 300, 500p or pass)")
    entry = {"hz": hz}
    if mode:
        entry["mode"] = mode
    return normalize_entry(entry)


def parse_entry_list(text: str) -> list[dict]:
    """Comma/space separated entries -> list of normalized entries."""
    parts = [p for p in text.replace(",", " ").split() if p]
    if not parts:
        raise ValueError("no entries given")
    return [parse_entry(p) for p in parts]


def format_entry(entry: dict, default_mode: str = "charge") -> str:
    mode = entry.get("mode", default_mode)
    if mode == "passthrough":
        return "pass"
    hz = entry.get("hz")
    hz_s = f"{hz:g} Hz" if hz is not None else "? Hz"
    return f"{hz_s} pulse" if mode == "pulse_charge" else hz_s


def render_entry_list(t: Text, fs: dict) -> None:
    """Append the numbered entry list with ▶ running / → next markers."""
    entries = fs.get("entries") or []
    if not entries:
        t.append("  (empty — press ", style="dim")
        t.append(" f ", style="bold white on dark_blue")
        t.append(" to add entries)\n", style="dim")
        return
    cur = fs.get("current_index")
    nxt = fs.get("next_index")
    dm = fs.get("default_mode", "charge")
    for i, e in enumerate(entries):
        if i == cur:
            mark, style = " ▶ ", "bold green"
        elif i == nxt:
            mark, style = " → ", "bold yellow"
        else:
            mark, style = "   ", ""
        t.append(mark, style=style)
        t.append(f"{i + 1:>2}. ", style="dim")
        t.append(f"{format_entry(e, dm)}\n", style=style or "white")


class SweepPanel(Widget):
    """Right-column view of the server's frequency sweep."""

    DEFAULT_CSS = """
    SweepPanel {
        width: 100%;
        height: auto;
        padding: 0 1;
    }
    """

    freq_sweep: reactive[dict] = reactive({}, layout=True)
    auto_follow_enabled: reactive[bool] = reactive(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._text_cache: Text | None = None
        self._text_cache_key: str | None = None

    def render(self) -> Text:
        # Broadcasts arrive at ~30 Hz but the sweep changes a few times
        # per cycle; cache the Text on a snapshot of the inputs.
        try:
            key = json.dumps([self.freq_sweep, self.auto_follow_enabled],
                             sort_keys=True, default=str)
        except (TypeError, ValueError):
            key = None
        if key is not None and key == self._text_cache_key and self._text_cache is not None:
            return self._text_cache
        t = self._render_impl()
        if key is not None:
            self._text_cache, self._text_cache_key = t, key
        return t

    def _render_impl(self) -> Text:
        t = Text()
        t.append(" Tab", style="bold white on dark_blue")
        t.append(" → Status panel\n\n", style="dim")
        t.append(" FREQUENCY SWEEP\n", style="bold cyan underline")
        fs = self.freq_sweep or {}
        if not fs:
            t.append("\n Server has no sweep support\n", style="dim")
            t.append(" (deploy the latest server code)\n", style="dim")
            return t

        enabled = fs.get("enabled")
        t.append(" Sweep:  ", style="dim")
        t.append("ON\n" if enabled else "OFF\n", style="bold green" if enabled else "bold red")
        t.append(" End:    ", style="dim")
        t.append(f"{fs.get('end', 'loop')}\n", style="bold white")
        t.append(" Cycle:  ", style="dim")
        t.append(f"{fs.get('cycle', 0)}\n", style="bold white")

        last = fs.get("last_applied")
        if last:
            t.append(" Now:    ", style="dim")
            t.append(f"#{last['index'] + 1} ", style="dim")
            t.append(f"{format_entry({k: v for k, v in last.items() if k in ('hz', 'mode')})}\n",
                     style="bold green")
        if enabled and fs.get("entries"):
            t.append(" Next:   ", style="dim")
            if fs.get("discharge_seen"):
                t.append("advances at next charge\n", style="yellow")
            else:
                t.append("waiting for a discharge\n", style="dim")

        if enabled and not self.auto_follow_enabled:
            t.append("\n ⚠ Auto-follow is OFF", style="bold yellow")
            t.append(" — the sweep\n   only acts while it is on (", style="yellow")
            t.append(" F ", style="bold white on dark_blue")
            t.append(")\n", style="yellow")

        t.append("\n ENTRIES\n", style="bold white")
        render_entry_list(t, fs)

        t.append("\n Discharge confirm: ", style="dim")
        t.append(f"< -{fs.get('discharge_a', 0) * 1000:g} mA "
                 f"for {fs.get('discharge_confirm_s', 0):g} s\n", style="dim")

        t.append("\n ")
        t.append(" f ", style="bold white on dark_blue")
        t.append(" Edit sweep\n", style="dim")
        return t


class SweepScreen(ModalScreen[None]):
    """Live editor for the frequency sweep.

    Every action is sent immediately via ``send`` (a sweep_* command
    dict); the list shown here refreshes from the state broadcast, which
    the app forwards into ``freq_sweep``."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
    ]

    DEFAULT_CSS = """
    SweepScreen { align: center middle; }
    #sweep-box {
        width: 72; max-width: 95%;
        height: auto; max-height: 90%;
        border: thick $accent; background: $surface; padding: 1 2;
        overflow-y: auto;
    }
    #sweep-title { text-align: center; width: 100%; margin-bottom: 1; }
    #sweep-list { width: 100%; margin-bottom: 1; }
    #sweep-help { width: 100%; color: $text-muted; margin-bottom: 1; }
    .field-label { margin-top: 1; }
    .row { width: 100%; height: auto; }
    .row Input { width: 1fr; }
    .row Button { min-width: 12; margin-left: 1; }
    #toggle-row { width: 100%; height: 3; margin-top: 1; }
    #toggle-row Button { margin-right: 1; min-width: 14; }
    #sweep-msg { width: 100%; margin-top: 1; }
    """

    freq_sweep: reactive[dict] = reactive({})

    def __init__(self, freq_sweep: dict, send: Callable[[dict], None]):
        super().__init__()
        self._send = send
        self.set_reactive(SweepScreen.freq_sweep, dict(freq_sweep or {}))

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="sweep-box"):
            yield Label("[bold cyan]Frequency sweep[/]", id="sweep-title")
            yield Static(self._list_text(), id="sweep-list")
            yield Label(
                "Entries: [bold]300[/] = charge 300 Hz, [bold]500p[/] = pulse 500 Hz, "
                "[bold]pass[/] = no switching (control). Numbers are positions. "
                "Changes apply from the next charge.",
                id="sweep-help",
            )

            yield Label("Add entries (appended; or 'at N' to insert):", classes="field-label")
            with Horizontal(classes="row"):
                yield Input(placeholder="e.g. 300   or   500p at 2", id="in-add")
                yield Button("Add", id="btn-add", variant="primary")

            yield Label("Remove entry number:", classes="field-label")
            with Horizontal(classes="row"):
                yield Input(placeholder="e.g. 3", id="in-remove")
                yield Button("Remove", id="btn-remove", variant="warning")

            yield Label("Run this entry number next:", classes="field-label")
            with Horizontal(classes="row"):
                yield Input(placeholder="e.g. 1", id="in-next")
                yield Button("Set next", id="btn-next")

            yield Label("Replace the whole list (restarts at entry 1):", classes="field-label")
            with Horizontal(classes="row"):
                yield Input(placeholder="e.g. 10, 20, 50, 100, 300, 1000", id="in-list")
                yield Button("Replace", id="btn-list", variant="error")

            with Horizontal(id="toggle-row"):
                yield Button(self._enable_label(), id="btn-enable", variant="success")
                yield Button(self._end_label(), id="btn-end")
                yield Button("Reset to #1", id="btn-reset")
                yield Button("Close", id="btn-close")
            # Feedback stays inside the dialog: toasts would land on top of
            # the buttons and swallow the next click.
            yield Label("", id="sweep-msg")

    # -- live refresh --------------------------------------------------------

    def watch_freq_sweep(self, _old, _new) -> None:
        if not self.is_mounted:
            return
        self.query_one("#sweep-list", Static).update(self._list_text())
        self.query_one("#btn-enable", Button).label = self._enable_label()
        self.query_one("#btn-end", Button).label = self._end_label()

    def _list_text(self) -> Text:
        fs = self.freq_sweep or {}
        t = Text()
        on = fs.get("enabled")
        t.append("Sweep ", style="dim")
        t.append("ON" if on else "OFF", style="bold green" if on else "bold red")
        t.append(f"   end: {fs.get('end', 'loop')}   cycle: {fs.get('cycle', 0)}\n\n",
                 style="dim")
        render_entry_list(t, fs)
        t.append("\n ▶ running   → next", style="dim")
        return t

    def _enable_label(self) -> str:
        return "Disable sweep" if (self.freq_sweep or {}).get("enabled") else "Enable sweep"

    def _end_label(self) -> str:
        end = (self.freq_sweep or {}).get("end", "loop")
        return "End: loop ↻" if end == "loop" else "End: hold ↦"

    # -- actions -------------------------------------------------------------

    def action_close(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        button = {"in-add": "btn-add", "in-remove": "btn-remove",
                  "in-next": "btn-next", "in-list": "btn-list"}.get(event.input.id)
        if button:
            self._do(button)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._do(event.button.id)

    def _do(self, button_id: Optional[str]) -> None:
        try:
            msg = self._handle(button_id)
        except ValueError as exc:
            self._message(f"[bold red]⚠ {exc}[/]")
            return
        if msg:
            self._message(f"[green]✓ {msg}[/]")

    def _handle(self, button_id: Optional[str]) -> Optional[str]:
        fs = self.freq_sweep or {}
        n = len(fs.get("entries") or [])
        if button_id == "btn-close":
            self.dismiss(None)
            return None
        if button_id == "btn-add":
            box = self.query_one("#in-add", Input)
            text, position = box.value, None
            if " at " in f" {text.lower()} ":
                text, _, pos_s = text.lower().rpartition(" at ")
                position = self._position(pos_s, n + 1) - 1
            entries = parse_entry_list(text)
            for k, e in enumerate(entries):
                payload = {"cmd": "sweep_add", "entry": e}
                if position is not None:
                    payload["position"] = position + k
                self._send(payload)
            box.value = ""
            return f"Added {', '.join(format_entry(e) for e in entries)}"
        if button_id == "btn-remove":
            box = self.query_one("#in-remove", Input)
            idx = self._position(box.value, n) - 1
            self._send({"cmd": "sweep_remove", "index": idx})
            box.value = ""
            return f"Removed entry {idx + 1}"
        if button_id == "btn-next":
            box = self.query_one("#in-next", Input)
            idx = self._position(box.value, n) - 1
            self._send({"cmd": "sweep_set_index", "index": idx})
            box.value = ""
            return f"Entry {idx + 1} runs at the next charge"
        if button_id == "btn-list":
            box = self.query_one("#in-list", Input)
            entries = parse_entry_list(box.value)
            self._send({"cmd": "sweep_configure", "entries": entries})
            box.value = ""
            return f"List replaced ({len(entries)} entries), starting at entry 1"
        if button_id == "btn-enable":
            on = not fs.get("enabled")
            if on and n == 0:
                raise ValueError("Add some entries before enabling the sweep")
            self._send({"cmd": "sweep_enable", "enabled": on})
            return "Sweep enabled" if on else "Sweep disabled"
        if button_id == "btn-end":
            end = "hold" if fs.get("end", "loop") == "loop" else "loop"
            self._send({"cmd": "sweep_set_end", "end": end})
            return f"End of list: {end}"
        if button_id == "btn-reset":
            self._send({"cmd": "sweep_reset"})
            return "Sweep reset to entry 1, cycle count cleared"
        return None

    @staticmethod
    def _position(text: str, count: int) -> int:
        """Parse a 1-based position in [1, count]."""
        try:
            pos = int(text.strip())
        except ValueError:
            raise ValueError(f"{text.strip()!r} is not an entry number")
        if count < 1:
            raise ValueError("The list is empty")
        if not 1 <= pos <= count:
            raise ValueError(f"Entry number must be 1-{count}")
        return pos

    def _message(self, markup: str) -> None:
        self.query_one("#sweep-msg", Label).update(markup)
