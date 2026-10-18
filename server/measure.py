"""
Switching Circuit V2 - Measurement mode.

A held, precision-sampled board state for characterising a cell one
electrode pair at a time and for calibrating the four current sensors
against a known cycler current (see docs/measure_mode.md).

While MEASURE is active:
  - the switching engine is stopped and the FETs hold one named
    configuration (a single electrode pair, pass-through, or open);
  - configuration changes are make-before-break (union of the old and
    new FET sets first), so the cycler never sees an open circuit
    between two connected configurations;
  - the INA226s run a slow, heavily averaged profile with the bus
    voltage read on every sample;
  - every sensor frame is logged to a CSV with the configuration that
    was live, plus operator / script markers. The header records the
    firmware calibration in force so raw readings can be recovered:
    raw = (reading - offset) / gain.

Configurations are named by the interface they drive directly
(interface k = the electrode pair facing each other):

    pair1  +A / -B   (P1, N2)   = STATE_DEFS[1]
    pair2  +A / -A   (P1, N1)   = STATE_DEFS[0]
    pair3  +B / -A   (P2, N1)   = STATE_DEFS[2]
    pair4  +B / -B   (P2, N2)   = STATE_DEFS[3]
    pass   all on               = STATE_DEFS[4]
    open   all off              = STATE_DEFS[5]

The four electrodes form a closed ring of interfaces inside the cell
(+A -1- -B -4- +B -3- -A -2- +A), so a pair configuration is NOT one
interface in isolation: the other three in series form a parallel
path. In a pair configuration the two driven sensors still carry
exactly the external (cycler) current — that is what makes them
calibratable.
"""

from __future__ import annotations

import csv
import logging
import math
import threading
from datetime import datetime
from pathlib import Path
from time import monotonic

from server.config import (
    STATE_DEFS, MEASURE_INA226_AVG, MEASURE_BUS_EVERY, MEASURE_SENSOR_HZ,
    MEASURE_LOG_DIR,
)

log = logging.getLogger(__name__)

SENSORS = ("P1", "P2", "N1", "N2")

CONFIGS = {
    "pair1": tuple(STATE_DEFS[1]),
    "pair2": tuple(STATE_DEFS[0]),
    "pair3": tuple(STATE_DEFS[2]),
    "pair4": tuple(STATE_DEFS[3]),
    "pass": tuple(STATE_DEFS[4]),
    "open": tuple(STATE_DEFS[5]),
}
ALIASES = {"state0": "pair2", "state1": "pair1", "state2": "pair3", "state3": "pair4",
           "all_on": "pass", "all_off": "open"}
TERMINALS = {"pair1": "+A/-B", "pair2": "+A/-A", "pair3": "+B/-A", "pair4": "+B/-B",
             "pass": "all", "open": "none"}


def normalize_config(name) -> str:
    key = str(name).strip().lower()
    key = ALIASES.get(key, key)
    if key not in CONFIGS:
        raise ValueError(f"Unknown measure config {name!r}. Valid: "
                         f"{', '.join(list(CONFIGS) + list(ALIASES))}")
    return key


def config_for_fets(fets) -> str | None:
    t = tuple(bool(f) for f in fets)
    for name, cfg in CONFIGS.items():
        if tuple(bool(c) for c in cfg) == t:
            return name
    return None


_WINDOW_MAX = 50_000          # frames kept for stats() (~40 min at the precision rate)


def _mean_sd(xs):
    n = len(xs)
    if n == 0:
        return {"mean": None, "sd": None, "n": 0}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    return {"mean": m, "sd": sd, "n": n}


class MeasureSession:
    """Owns the FET configuration, sensor profile and log while MEASURE
    is active. ``gpio`` is the GPIODriver (or a stand-in in tests)."""

    def __init__(self, gpio, log_dir: Path | None = None, clock=monotonic):
        self._gpio = gpio
        self._log_dir = Path(log_dir) if log_dir else Path(MEASURE_LOG_DIR).expanduser()
        self._clock = clock
        self._lock = threading.Lock()
        self.active = False
        self.config = None
        self._config_since = 0.0
        self._saved_profile = None
        self._profile = None
        self._path = None
        self._file = None
        self._writer = None
        self._frames = 0
        self._markers = 0
        self._win = []            # (t, (P1v, P1i, P2v, P2i, N1v, N1i, N2v, N2i)) since the window start
        self._win_start = 0.0

    # -- lifecycle -------------------------------------------------------

    def start(self, initial_config: str, avg=None, bus_every=None, sensor_hz=None, label=""):
        """Enter measurement: hold ``initial_config`` (normally the FET set
        already live, so entering is seamless), switch the sensors to the
        precision profile and open the log."""
        initial_config = normalize_config(initial_config)
        with self._lock:
            if self.active:
                return self.status_locked()
            self._saved_profile = self._read_profile()
            self._profile = self._apply_profile(
                MEASURE_INA226_AVG if avg is None else int(avg),
                MEASURE_BUS_EVERY if bus_every is None else int(bus_every),
                MEASURE_SENSOR_HZ if sensor_hz is None else float(sensor_hz))
            self._gpio.apply_state(CONFIGS[initial_config])
            self.config = initial_config
            self._config_since = self._clock()
            self._reset_window()
            self._open_log(label)
            self.active = True
            self._write_event("config", initial_config)
            log.info("Measure mode on: %s, profile %s, log %s", initial_config, self._profile, self._path)
            return self.status_locked()

    def stop(self):
        """Leave measurement: restore the sensor profile, close the log.
        The caller drives the FETs for the next mode."""
        with self._lock:
            if not self.active:
                return None
            self._write_event("stop", self.config)
            self.active = False
            path = self._path
            try:
                if self._file:
                    self._file.close()
            finally:
                self._file = self._writer = None
            if self._saved_profile:
                p = self._saved_profile
                self._apply_profile(p.get("avg"), p.get("bus_every"), p.get("hz"))
            log.info("Measure mode off (%d frames, log %s)", self._frames, path)
            return str(path) if path else None

    # -- operations ------------------------------------------------------

    def set_config(self, name):
        """Change the held configuration, make-before-break."""
        name = normalize_config(name)
        with self._lock:
            if not self.active:
                raise ValueError("measure_set only available in measure mode")
            old = CONFIGS[self.config]
            new = CONFIGS[name]
            union = tuple(a or b for a, b in zip(old, new))
            if union not in (old, new):
                self._gpio.apply_state(union)     # both paths conducting for one command round-trip
            if new != old:
                self._gpio.apply_state(new)
            self.config = name
            self._config_since = self._clock()
            self._reset_window()
            self._write_event("config", name)
            return {"config": name, "terminals": TERMINALS[name],
                    "fet_states": [bool(f) for f in new], "t": self._config_since}

    def mark(self, label: str):
        with self._lock:
            if not self.active:
                raise ValueError("measure_mark only available in measure mode")
            self._markers += 1
            self._write_event("mark", str(label)[:120])
            return {"marker": self._markers, "t": self._clock()}

    def stats(self, settle_s: float = 0.0):
        """Mean / s.d. / n of every sensor channel since the last config
        change (or the last stats reset), skipping the first ``settle_s``."""
        with self._lock:
            if not self.active:
                raise ValueError("measure_stats only available in measure mode")
            t0 = self._win_start + float(settle_s)
            rows = [v for t, v in self._win if t >= t0]
            out = {"config": self.config, "since_s": round(self._clock() - self._config_since, 3),
                   "settle_s": settle_s, "channels": {}}
            for j, s in enumerate(SENSORS):
                out["channels"][f"{s}_v"] = _mean_sd([r[2 * j] for r in rows])
                out["channels"][f"{s}_i"] = _mean_sd([r[2 * j + 1] for r in rows])
            if not rows:
                out["warning"] = "no frames after settle_s yet"
            return out

    def reset_stats(self):
        with self._lock:
            self._reset_window()
            self._write_event("reset_stats", self.config)

    # -- sensor frames (GPIO reader thread) ------------------------------

    def on_frame(self, data, sample_pi_s, fw_ts_us, seq):
        with self._lock:
            if not self.active:
                return
            self._frames += 1
            vals = []
            for s in SENSORS:
                d = data.get(s, {})
                vals += [float(d.get("voltage", 0.0)), float(d.get("current", 0.0))]
            if self._writer:
                self._writer.writerow([f"{sample_pi_s:.6f}", fw_ts_us, seq, "frame", self.config, ""]
                                      + [f"{v:.7f}" for v in vals])
            if sample_pi_s >= self._win_start and len(self._win) < _WINDOW_MAX:
                self._win.append((sample_pi_s, tuple(vals)))

    # -- status ----------------------------------------------------------

    def status(self):
        with self._lock:
            return self.status_locked()

    def status_locked(self):
        return {
            "active": self.active,
            "config": self.config,
            "terminals": TERMINALS.get(self.config),
            "since_s": round(self._clock() - self._config_since, 3) if self.active else None,
            "profile": self._profile,
            "log": str(self._path) if self._path else None,
            "frames": self._frames,
            "markers": self._markers,
        }

    # -- internals -------------------------------------------------------

    def _reset_window(self):
        self._win = []
        self._win_start = self._clock()

    def _read_profile(self):
        p = self._gpio.get_sensor_profile() or {}
        return {"avg": p.get("avg"), "bus_every": p.get("bus_every"),
                "hz": self._gpio.get_sensor_rate()}

    def _apply_profile(self, avg, bus_every, hz):
        out = {}
        if avg is not None:
            a, _ = self._gpio.set_ina226_avg(int(avg))
            out["avg"] = a
        if bus_every is not None:
            b, max_hz = self._gpio.set_bus_every(int(bus_every))
            out["bus_every"] = b
            out["max_hz"] = max_hz
        if hz is not None:
            self._gpio.set_sensor_rate(float(hz))
            out["hz"] = self._gpio.get_sensor_rate()
        return out

    def _open_log(self, label):
        self._frames = self._markers = 0
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._path = self._log_dir / f"measure_{stamp}.csv"
            self._file = open(self._path, "w", newline="")
            cal = self._gpio.get_calibration() or {}
            self._file.write(f"# measure log; label={label}\n")
            self._file.write(f"# started_pi_time={datetime.now().isoformat(timespec='seconds')} "
                             f"started_monotonic={self._clock():.6f}\n")
            self._file.write(f"# profile={self._profile}\n")
            self._file.write("# calibration=" + " ".join(
                f"{SENSORS[ch]}:{g:.6f}:{o:.6f}" for ch, (g, o) in sorted(cal.items()) if 0 <= ch < 4) + "\n")
            self._file.write("# raw = (reading - offset) / gain; sensor order P1=+A P2=+B N1=-A N2=-B\n")
            self._writer = csv.writer(self._file)
            self._writer.writerow(["pi_monotonic_s", "fw_ts_us", "seq", "kind", "config", "label"]
                                  + [f"{s}_{q}" for s in SENSORS for q in ("v", "i")])
        except OSError:
            log.exception("Could not open measure log; continuing without a file")
            self._path = self._file = self._writer = None

    def _write_event(self, kind, label):
        if self._writer:
            self._writer.writerow([f"{self._clock():.6f}", "", "", kind, self.config, label] + [""] * 8)
            self._file.flush()
