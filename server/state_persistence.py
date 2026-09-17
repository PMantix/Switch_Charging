"""
Switching Circuit V2 - Settings persistence across reboots.

By default ModeController starts every boot in IDLE with everything
off, as a safety measure. This module lets that be overridden: saves
the operator-facing settings (mode, frequency, sequence, pulse mode,
auto-follow config) to a small JSON file on every change, so a power
cycle can restore exactly where things left off instead of resetting.

Writes are atomic (write to a temp file, then rename) so a power loss
mid-write can't corrupt the state file.

save_state() is fire-and-forget: the actual disk write happens on a
background thread, debounced by _DEBOUNCE_S. command_server.py calls
this synchronously from the command-handling path after every set_mode/
set_frequency/etc — an earlier version wrote to disk inline there,
which on a slow SD card added real, noticeable latency to every single
command (reported as "the whole TUI feels slow whenever it has to
compute something"). Debouncing also means rapid successive changes
(e.g. holding a frequency-adjust key) coalesce into one write instead
of hammering the card once per keystroke.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

STATE_FILE = Path.home() / ".switching_circuit_state.json"
_DEBOUNCE_S = 0.5

_lock = threading.Lock()
_pending_state: Optional[dict] = None
_writer_thread: Optional[threading.Thread] = None


def save_state(state: dict) -> None:
    """Queue `state` to be written to disk shortly — returns immediately,
    never blocks the caller on I/O."""
    global _pending_state, _writer_thread
    with _lock:
        _pending_state = state
        if _writer_thread is None or not _writer_thread.is_alive():
            _writer_thread = threading.Thread(target=_writer_loop, daemon=True)
            _writer_thread.start()


def _writer_loop() -> None:
    time.sleep(_DEBOUNCE_S)
    with _lock:
        state = _pending_state
    _write_now(state)


def _write_now(state: Optional[dict]) -> None:
    if state is None:
        return
    try:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(STATE_FILE)
    except OSError as exc:
        log.warning("Failed to save persisted state: %s", exc)


def load_state() -> Optional[dict]:
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("Failed to load persisted state: %s", exc)
        return None
