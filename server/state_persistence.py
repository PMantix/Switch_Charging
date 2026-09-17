"""
Switching Circuit V2 - Settings persistence across reboots.

By default ModeController starts every boot in IDLE with everything
off, as a safety measure. This module lets that be overridden: saves
the operator-facing settings (mode, frequency, sequence, pulse mode,
auto-follow config) to a small JSON file on every change, so a power
cycle can restore exactly where things left off instead of resetting.

Writes are atomic (write to a temp file, then rename) so a power loss
mid-write can't corrupt the state file.
"""

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

STATE_FILE = Path.home() / ".switching_circuit_state.json"


def save_state(state: dict) -> None:
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
