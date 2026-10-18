"""
Switching Circuit V2 - Per-cycle frequency sweep.

Steps the switching frequency (and optionally the switching mode)
through an editable list, one entry per cycler cycle, on top of
AutoFollow. AutoFollow already engages switching exactly once per CC
charge (when the cycler current rises above i_enter) and drops to
transparent pass-through for discharge, so its engage edge is the cycle
boundary:

  1. Discharge: AutoFollow keeps the FETs all-on. Never switches.
  2. Charge onset: before AutoFollow engages, it asks on_charge_onset()
     which mode to engage. The sweep sets the engine frequency here,
     while the engine is still paused in pass-through, so the RP2040
     never sees a frequency change mid-switching.
  3. Charge end: AutoFollow drops back to pass-through as usual.

The sweep only advances to the next entry once a real discharge has
been seen since the previous onset. Without that, threshold chatter
(or a current blip) mid-charge would skip entries; instead a re-entry
in the same charge re-applies the same entry.

Each entry is {"hz": float, "mode": str} plus, optionally, pauses:
{"pause_every_s": float, "pause_s": float} makes AutoFollow drop to
pass-through for pause_s every pause_every_s during that cycle's charge
(see auto_follow.py); each pause is logged with event "pause", so the
log's "reengage" rows still count only real drop-outs. mode is one of "charge" /
"pulse_charge" / "passthrough" and falls back to default_mode; hz is
required unless the entry is "passthrough" — a non-switching control
cycle where AutoFollow stays all-on for that whole charge.

Edits (add / remove / replace / reorder via configure) take effect at
the next onset, never mid-charge. At the end of the list, end="loop"
wraps to entry 0 and end="hold" keeps re-applying the last entry.

Every onset appends one row to a CSV log on the Pi. Match that log to
cycler data by cycle number, not wall time: Pi clocks on this fleet
have been seen days off (no NTP on the isolated network).
"""

from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Callable, Optional

from server.config import MAX_FREQ, MIN_FREQ

log = logging.getLogger(__name__)

SWEEP_MODES = ("charge", "pulse_charge", "passthrough")
END_BEHAVIORS = ("loop", "hold")

DEFAULT_LOG_FILE = Path.home() / "freq_sweep_log.csv"
# A discharge counts once the cycler current has stayed below
# -discharge_a for discharge_confirm_s (rejects brief negative blips).
DEFAULT_DISCHARGE_A = 0.010
DEFAULT_DISCHARGE_CONFIRM_S = 10.0

_LOG_FIELDS = (
    "cycle", "entry_index", "mode", "frequency_hz", "event",
    "pi_time", "pi_monotonic_s", "avg_current_a", "avg_voltage_v",
)


def normalize_entry(entry) -> dict:
    """Validate one entry and return it in canonical form.

    Accepts a dict ({"hz": .., "mode": ..}), or a bare number as a
    shorthand for {"hz": number}. Raises ValueError on bad input."""
    if isinstance(entry, (int, float)) and not isinstance(entry, bool):
        entry = {"hz": entry}
    if not isinstance(entry, dict):
        raise ValueError(f"Entry must be a number or an object, got {entry!r}")
    out = {}
    mode = entry.get("mode")
    if mode is not None:
        if mode not in SWEEP_MODES:
            raise ValueError(
                f"Entry mode must be one of {', '.join(SWEEP_MODES)}, got {mode!r}")
        out["mode"] = mode
    hz = entry.get("hz")
    if hz is not None:
        try:
            hz = float(hz)
        except (TypeError, ValueError):
            raise ValueError(f"Entry hz must be a number, got {hz!r}")
        if not MIN_FREQ <= hz <= MAX_FREQ:
            raise ValueError(f"Entry hz must be in [{MIN_FREQ}, {MAX_FREQ}], got {hz}")
        out["hz"] = hz
    if ("pause_every_s" in entry) != ("pause_s" in entry):
        raise ValueError(f"Entry needs both pause_every_s and pause_s, or neither: {entry!r}")
    if "pause_every_s" in entry:
        from server.auto_follow import validate_pause    # deferred: auto_follow imports this module's users
        every, length = validate_pause(entry["pause_every_s"], entry["pause_s"])
        if every:
            out["pause_every_s"], out["pause_s"] = every, length
    if "hz" not in out and out.get("mode") != "passthrough":
        # A switching entry with no frequency would silently reuse
        # whatever was set last — almost certainly a typo in the list.
        raise ValueError(f"Entry needs 'hz' (or mode 'passthrough'): {entry!r}")
    return out


class FreqSweep:
    """Editable per-cycle frequency/mode list driven by AutoFollow."""

    def __init__(
        self,
        set_frequency_fn: Callable[[float], None],
        log_file: Optional[Path] = None,
        clock: Callable[[], float] = monotonic,
    ):
        self._set_frequency = set_frequency_fn
        # Resolved here, not as a default arg, so --sim can redirect
        # DEFAULT_LOG_FILE before the controller is built.
        self._log_file = Path(log_file) if log_file else DEFAULT_LOG_FILE
        self._clock = clock
        # Called (outside the lock) after any change worth persisting.
        self.on_change: Optional[Callable[[], None]] = None

        self._lock = threading.Lock()
        self._enabled = False
        self._entries: list[dict] = []
        self._default_mode = "charge"
        self._end = "loop"
        self._next = 0                  # entry applied at the next new cycle
        self._current: Optional[int] = None   # entry applied at the last onset
        self._cycle = 0                 # onsets that started a new cycle
        self._discharge_seen = True     # first onset after enable is a new cycle
        self._neg_since: Optional[float] = None
        self._discharge_a = DEFAULT_DISCHARGE_A
        self._discharge_confirm_s = DEFAULT_DISCHARGE_CONFIRM_S
        self._last_applied: Optional[dict] = None

    # -- configuration -------------------------------------------------------

    def configure(self, entries=None, default_mode=None, end=None,
                  discharge_a=None, discharge_confirm_s=None):
        """Replace the list and/or settings. Fields left None are kept.
        Replacing the list restarts it at entry 0."""
        norm = [normalize_entry(e) for e in entries] if entries is not None else None
        if default_mode is not None and default_mode not in SWEEP_MODES:
            raise ValueError(f"default_mode must be one of {', '.join(SWEEP_MODES)}")
        if end is not None and end not in END_BEHAVIORS:
            raise ValueError(f"end must be one of {', '.join(END_BEHAVIORS)}")
        if discharge_a is not None and float(discharge_a) <= 0:
            raise ValueError("discharge_a must be positive")
        if discharge_confirm_s is not None and float(discharge_confirm_s) < 0:
            raise ValueError("discharge_confirm_s must be non-negative")
        with self._lock:
            if norm is not None:
                self._entries = norm
                self._next = 0
                self._current = None
            if default_mode is not None:
                self._default_mode = default_mode
            if end is not None:
                self._end = end
            if discharge_a is not None:
                self._discharge_a = float(discharge_a)
            if discharge_confirm_s is not None:
                self._discharge_confirm_s = float(discharge_confirm_s)
        log.info("FreqSweep: configured %s", self.get_status())
        self._changed()

    def add_entry(self, entry, position: Optional[int] = None):
        """Insert an entry (append when position is None)."""
        e = normalize_entry(entry)
        with self._lock:
            n = len(self._entries)
            pos = n if position is None else int(position)
            if not 0 <= pos <= n:
                raise ValueError(f"position must be in [0, {n}], got {pos}")
            self._entries.insert(pos, e)
            # Keep pointing at the same logical entries.
            if n > 0 and pos <= self._next:
                self._next += 1
            if self._current is not None and pos <= self._current:
                self._current += 1
        log.info("FreqSweep: added %s at %d", e, pos)
        self._changed()

    def remove_entry(self, index: int):
        with self._lock:
            n = len(self._entries)
            i = int(index)
            if not 0 <= i < n:
                raise ValueError(f"index must be in [0, {n - 1}], got {i}")
            removed = self._entries.pop(i)
            if i < self._next:
                self._next -= 1
            # Removing the next entry leaves _next on the one after it.
            if self._next >= len(self._entries):
                self._next = 0 if self._end == "loop" else max(0, len(self._entries) - 1)
            if self._current is not None:
                if i == self._current:
                    self._current = None
                elif i < self._current:
                    self._current -= 1
        log.info("FreqSweep: removed entry %d %s", i, removed)
        self._changed()

    def replace_entry(self, index: int, entry):
        e = normalize_entry(entry)
        with self._lock:
            n = len(self._entries)
            i = int(index)
            if not 0 <= i < n:
                raise ValueError(f"index must be in [0, {n - 1}], got {i}")
            self._entries[i] = e
        log.info("FreqSweep: replaced entry %d with %s", i, e)
        self._changed()

    def set_next_index(self, index: int):
        """Choose which entry the next new cycle uses."""
        with self._lock:
            n = len(self._entries)
            i = int(index)
            if not 0 <= i < n:
                raise ValueError(f"index must be in [0, {n - 1}], got {i}")
            self._next = i
        log.info("FreqSweep: next entry set to %d", i)
        self._changed()

    def set_end(self, end: str):
        self.configure(end=end)

    def set_enabled(self, enabled: bool):
        with self._lock:
            was = self._enabled
            self._enabled = bool(enabled)
            if self._enabled and not was:
                # The first onset after enabling always starts a new cycle.
                self._discharge_seen = True
                self._current = None
                self._neg_since = None
        log.info("FreqSweep: enabled=%s", enabled)
        self._changed()

    def reset(self):
        """Back to entry 0 with the cycle counter cleared."""
        with self._lock:
            self._next = 0
            self._current = None
            self._cycle = 0
            self._discharge_seen = True
            self._neg_since = None
        log.info("FreqSweep: reset")
        self._changed()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled and bool(self._entries)

    # -- AutoFollow hooks ----------------------------------------------------

    def observe(self, avg_i: float):
        """Fed every AutoFollow tick with the cycler current estimate."""
        newly_seen = False
        with self._lock:
            if not self._enabled or self._discharge_seen:
                self._neg_since = None
                return
            if avg_i < -self._discharge_a:
                now = self._clock()
                if self._neg_since is None:
                    self._neg_since = now
                if now - self._neg_since >= self._discharge_confirm_s:
                    self._discharge_seen = True
                    self._neg_since = None
                    newly_seen = True
            else:
                self._neg_since = None
        if newly_seen:
            log.info("FreqSweep: discharge confirmed — next onset advances")
            self._changed()

    def on_charge_onset(self, avg_i: float = 0.0, avg_v: float = 0.0) -> Optional[str]:
        """Called by AutoFollow just before it engages switching.

        Returns the mode to engage ("charge" / "pulse_charge" /
        "passthrough"), or None when the sweep is off or the list is
        empty (AutoFollow then uses its own target mode)."""
        with self._lock:
            if not self._enabled or not self._entries:
                return None
            if self._discharge_seen or self._current is None:
                idx = self._next
                self._current = idx
                self._cycle += 1
                self._discharge_seen = False
                self._neg_since = None
                if idx + 1 < len(self._entries):
                    self._next = idx + 1
                else:
                    self._next = 0 if self._end == "loop" else idx
                event = "cycle_start"
            else:
                idx = self._current
                event = "reengage"
            entry = self._entries[idx]
            mode = entry.get("mode", self._default_mode)
            hz = entry.get("hz")
            cycle = self._cycle
            self._last_applied = {"cycle": cycle, "index": idx, "mode": mode, "hz": hz}

        if hz is not None and mode != "passthrough":
            self._set_frequency(hz)
        log.info("FreqSweep: %s cycle %d entry %d -> %s%s", event, cycle, idx, mode,
                 f" @ {hz:g} Hz" if hz is not None and mode != "passthrough" else "")
        self._write_log(cycle, idx, mode, hz, event, avg_i, avg_v)
        self._changed()
        return mode

    def current_pause(self) -> Optional[tuple]:
        """(pause_every_s, pause_s) of the entry applied at the last onset, or None."""
        with self._lock:
            if not self._enabled or self._current is None or self._current >= len(self._entries):
                return None
            e = self._entries[self._current]
            return (e["pause_every_s"], e["pause_s"]) if "pause_every_s" in e else None

    def log_pause(self, avg_i: float = 0.0, avg_v: float = 0.0):
        """One log row per pause (event "pause"), tagged with the current cycle and entry."""
        with self._lock:
            la = dict(self._last_applied) if self._last_applied else None
        if la:
            self._write_log(la["cycle"], la["index"], la["mode"], la["hz"], "pause", avg_i, avg_v)

    # -- status / persistence -----------------------------------------------

    def get_status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "entries": [dict(e) for e in self._entries],
                "default_mode": self._default_mode,
                "end": self._end,
                "next_index": self._next if self._entries else None,
                "current_index": self._current,
                "cycle": self._cycle,
                "discharge_seen": self._discharge_seen,
                "last_applied": dict(self._last_applied) if self._last_applied else None,
                "discharge_a": self._discharge_a,
                "discharge_confirm_s": self._discharge_confirm_s,
                "log_file": str(self._log_file) if self._log_file else None,
            }

    def to_state(self) -> dict:
        """Everything needed to resume after a restart."""
        with self._lock:
            return {
                "enabled": self._enabled,
                "entries": [dict(e) for e in self._entries],
                "default_mode": self._default_mode,
                "end": self._end,
                "next_index": self._next,
                "current_index": self._current,
                "cycle": self._cycle,
                "discharge_seen": self._discharge_seen,
                "discharge_a": self._discharge_a,
                "discharge_confirm_s": self._discharge_confirm_s,
            }

    def restore(self, state: dict):
        """Inverse of to_state(). Invalid entries are dropped with a warning
        rather than failing the whole restore."""
        entries = []
        for e in state.get("entries") or []:
            try:
                entries.append(normalize_entry(e))
            except ValueError as exc:
                log.warning("FreqSweep: dropping invalid saved entry %r: %s", e, exc)
        with self._lock:
            self._entries = entries
            if state.get("default_mode") in SWEEP_MODES:
                self._default_mode = state["default_mode"]
            if state.get("end") in END_BEHAVIORS:
                self._end = state["end"]
            n = len(entries)
            nxt = state.get("next_index") or 0
            self._next = nxt if 0 <= nxt < n else 0
            cur = state.get("current_index")
            self._current = cur if isinstance(cur, int) and 0 <= cur < n else None
            self._cycle = int(state.get("cycle") or 0)
            self._discharge_seen = bool(state.get("discharge_seen", True))
            if state.get("discharge_a"):
                self._discharge_a = float(state["discharge_a"])
            if state.get("discharge_confirm_s") is not None:
                self._discharge_confirm_s = float(state["discharge_confirm_s"])
            self._enabled = bool(state.get("enabled"))
        log.info("FreqSweep: restored %d entries, cycle %d, next %d, enabled=%s",
                 n, self._cycle, self._next, self._enabled)

    # -- internals -----------------------------------------------------------

    def _changed(self):
        cb = self.on_change
        if cb is not None:
            try:
                cb()
            except Exception:
                log.exception("FreqSweep: on_change callback failed")

    def _write_log(self, cycle, idx, mode, hz, event, avg_i, avg_v):
        if self._log_file is None:
            return
        try:
            new = not self._log_file.exists()
            with self._log_file.open("a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(_LOG_FIELDS)
                w.writerow([
                    cycle, idx, mode, "" if hz is None else f"{hz:g}", event,
                    datetime.now().isoformat(timespec="seconds"),
                    f"{self._clock():.3f}", f"{avg_i:.6f}", f"{avg_v:.4f}",
                ])
        except OSError as exc:
            log.warning("FreqSweep: failed to write log %s: %s", self._log_file, exc)
