"""
Switching Circuit V2 - "what is driving the switching" strip, shown under the
H-bridge diagram.

The special modes (per-charge frequency sweep, intermittent pass-through pauses,
frequency-map blocks) are configured in other views and are hard to follow from
the FET pattern alone. This strip summarises, in a few lines no wider than the
diagram, whichever of them is in force right now:

    FREQ MAP charge 1 of 3 · block 9/28 · 23 Hz · 37 s left
       … 1000   10 [  23]  600  200   23  107 …       (window of the block list)
    PAUSES 0.5 s pass every 15 s · 12 this charge
    SWEEP · entry 3/6: 307 Hz · next charge: 23 Hz
    AUTO-FOLLOW · waiting for charge current > 0.10 A

Everything comes from the state broadcast (auto_follow + freq_sweep); nothing is
sent to the board. mode_strip_text() is a pure function for the tests.
"""

from __future__ import annotations

import json
from typing import Optional

from rich.text import Text
from textual.widgets import Static

from tui.widgets.sweep_panel import format_entry

WIDTH = 58          # the diagram is ~60 columns wide
WINDOW = 8          # blocks shown around the running one


def _tok(hz: float) -> str:
    return "P" if hz == 0 else f"{hz:g}"


def _block_window(t: Text, blocks: list, live: Optional[int]) -> None:
    """One line: a window of the block list with the running block bracketed;
    when nothing runs yet, the first blocks."""
    n = len(blocks)
    if live is None:
        lo = 0
    else:
        lo = max(0, min(live - 3, n - WINDOW))
    hi = min(n, lo + WINDOW)
    t.append("   " + ("… " if lo > 0 else "  "), style="dim")
    for i in range(lo, hi):
        tok = _tok(blocks[i][0])
        if i == live:
            t.append(f"[{tok:>4}]", style="bold black on green")
        else:
            style = "dim" if live is not None and i < live else ("grey62" if blocks[i][0] == 0 else "white")
            t.append(f" {tok:>4} ", style=style)
    t.append(" …" if hi < n else "", style="dim")
    t.append("\n")


def _entry_pos(fs: dict, idx: Optional[int]) -> tuple[int, int]:
    """(k, n): this is mapped charge k of n among the sweep's block entries."""
    entries = fs.get("entries") or []
    blk = [i for i, e in enumerate(entries) if e.get("blocks")]
    if idx in blk:
        return blk.index(idx) + 1, len(blk)
    return 0, len(blk)


def mode_strip_text(af: Optional[dict], fs: Optional[dict], mode: str = "") -> Text:
    """The strip's text, or an empty Text when no special mode is set up."""
    af, fs = af or {}, fs or {}
    t = Text()
    entries = fs.get("entries") or []
    sweeping = bool(fs.get("enabled")) and bool(entries)
    cur, nxt = fs.get("current_index"), fs.get("next_index")

    # ---- frequency map (blocks) ------------------------------------------
    if af.get("blocks_active") and isinstance(cur, int) and 0 <= cur < len(entries) \
            and entries[cur].get("blocks"):
        blocks = entries[cur]["blocks"]
        k, n = _entry_pos(fs, cur)
        i, hz = af.get("block_index") or 0, af.get("block_hz")
        t.append(" FREQ MAP", style="bold black on cyan")
        t.append(f" charge {k} of {n} · block {i + 1}/{len(blocks)} · ", style="white")
        t.append("pass-through" if hz == 0 else f"{hz:g} Hz", style="bold green")
        rem = af.get("block_remaining_s")
        if rem is not None:
            t.append(f" · {rem:.0f} s left", style="white")
        t.append("\n")
        _block_window(t, blocks, i)
        return t
    if sweeping and isinstance(nxt, int) and 0 <= nxt < len(entries) and entries[nxt].get("blocks"):
        k, n = _entry_pos(fs, nxt)
        bl = entries[nxt]["blocks"]
        t.append(" FREQ MAP", style="bold black on cyan")
        t.append(f" armed · charge {k} of {n} next", style="white")
        t.append(f" · {len(bl)} × {bl[0][1]:g} s\n", style="dim")
        _block_window(t, bl, None)
        if not af.get("enabled"):
            t.append("   ⚠ auto-follow is OFF: blocks only run while it is on\n", style="bold yellow")
        return t

    # ---- intermittent pass-through pauses ----------------------------------
    cfg = af.get("pause_active_cfg")
    every, pause = (cfg if isinstance(cfg, (list, tuple)) and len(cfg) == 2
                    else (af.get("pause_every_s"), af.get("pause_s")))
    if af.get("active") and every:
        t.append(" PAUSES", style="bold black on yellow")
        t.append(f" {pause:g} s pass every {every:g} s", style="white")
        t.append(f" · {af.get('pauses_this_charge') or 0} this charge", style="dim")
        if af.get("in_pause"):
            t.append(" · IN PAUSE", style="bold yellow")
        t.append("\n")

    # ---- per-charge sweep ----------------------------------------------------
    if sweeping:
        dm = fs.get("default_mode", "charge")
        t.append(" SWEEP", style="bold black on magenta")
        if isinstance(cur, int) and 0 <= cur < len(entries):
            t.append(f" entry {cur + 1}/{len(entries)}: ", style="white")
            t.append(format_entry(entries[cur], dm), style="bold green")
        else:
            t.append(" not started", style="white")
        if isinstance(nxt, int) and 0 <= nxt < len(entries) and nxt != cur:
            t.append(f" · next charge: {format_entry(entries[nxt], dm)}", style="dim")
        elif fs.get("end") == "hold" and isinstance(cur, int) and cur == len(entries) - 1:
            t.append(" · holds (end of list)", style="dim")
        t.append("\n")

    # ---- auto-follow on its own ----------------------------------------------
    if af.get("enabled") and not t.plain:
        t.append(" AUTO-FOLLOW", style="bold black on blue")
        if af.get("active"):
            t.append(f" switching while charge current flows ({af.get('target_mode', 'charge')})",
                     style="white")
        else:
            ie = af.get("i_enter_a")
            t.append(" waiting for charge current" + (f" > {ie:.2f} A" if ie else ""), style="white")
        t.append("\n")
    return t


class ModeStrip(Static):
    """Compact summary of the active switching schedule, under the circuit diagram.
    The app calls update_state() from the state broadcast; hidden when nothing special is set up."""

    DEFAULT_CSS = """
    ModeStrip {
        width: 60;
        height: auto;
        padding: 0;
        margin-top: 1;
    }
    """

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._key: Optional[str] = None

    def update_state(self, af: Optional[dict], fs: Optional[dict], mode: str = "") -> None:
        # The broadcast arrives ~30 Hz; only redraw when something shown changes.
        af = af or {}
        rem = af.get("block_remaining_s")
        shown = {k: af.get(k) for k in ("enabled", "active", "blocks_active", "block_index", "block_hz",
                                         "pause_every_s", "pause_s", "pause_active_cfg", "in_pause",
                                         "pauses_this_charge", "target_mode", "i_enter_a")}
        shown["rem"] = round(rem) if rem is not None else None
        try:
            key = json.dumps([shown, fs, mode], sort_keys=True, default=str)
        except (TypeError, ValueError):
            key = None
        if key is not None and key == self._key:
            return
        self._key = key
        t = mode_strip_text(af, fs, mode)
        self.display = bool(t.plain)
        self.update(t)
