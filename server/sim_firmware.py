"""
Switching Circuit V2 - Simulated RP2040 firmware (``--sim`` only).

Stands in for the RP2040 so the server and TUI can be demoed without a
Raspberry Pi or any hardware. GPIODriver routes every serial command
through ``SimFirmware.handle()`` instead of the port, and a stream thread
emits ``D`` lines into the same ``_handle_stream_line()`` path real
serial data takes — so plots, recording, auto-follow and the schedule
monitor all see the synthetic data.

The numbers are plausible, not physical: one shared state of charge
drifts up while switching (charge / pulse), down while all-on
(discharge) and holds while all-off (idle). Per-sensor current follows
which FETs the programmed cycle has on at that instant, so the KCL sum
the auto-follow controller uses stays smooth while the individual
channels chop with the switching pattern.

SC_SIM_CYCLER="charge_s,rest_s,discharge_s,rest_s" (e.g. "60,10,60,10")
replaces that with an external cycler on a fixed timetable: the cycler
current follows the clock (+charge / 0 / -discharge / 0) whatever the
FETs do, as long as both sides have a path. That is what auto-follow
and the frequency sweep see on a real Neware, so it is the mode to use
for exercising them; the default model derives the current from the
FETs, which is circular for auto-follow.
"""

import json
import logging
import os
import random
import threading
import time

log = logging.getLogger(__name__)

# Order of the 4 FET bits in a packed state, MSB first (matches
# sequence_engine._pack_state and SENSOR_ORDER).
_CHANNELS = ("P1", "P2", "N1", "N2")

_MAX_HZ = 250.0          # stream-rate cap reported to the host (like the firmware's)
_CHARGE_A = 0.250        # cycler current while switching
_DISCHARGE_A = -0.300    # cycler current while all-on
_CAPACITY_AS = 240.0     # amp-seconds per full SOC swing — ~16 min at 0.25 A
_R_INTERNAL = 0.08       # ohm — IR rise/sag on the per-cell voltage
_V_OFFSETS = (0.000, 0.012, -0.008, 0.005)  # per-channel cell mismatch


def _parse_cycler_env(text):
    """'60,10,60,10' -> (60.0, 10.0, 60.0, 10.0), or None if unset/invalid."""
    text = text.strip()
    if not text:
        return None
    try:
        parts = tuple(float(x) for x in text.split(","))
    except ValueError:
        log.warning("SC_SIM_CYCLER=%r is not 4 comma-separated numbers — ignored", text)
        return None
    if len(parts) != 4 or any(x < 0 for x in parts) or sum(parts) <= 0:
        log.warning("SC_SIM_CYCLER=%r needs charge,rest,discharge,rest seconds — ignored", text)
        return None
    return parts


def _unpack(packed):
    return tuple(bool(packed & (1 << (3 - i))) for i in range(4))


class SimFirmware:
    """Answers the RP2040 command set and streams synthetic D lines."""

    def __init__(self, on_line):
        self._on_line = on_line          # called with each "D ..." line
        self._lock = threading.Lock()
        self._t0 = time.monotonic()

        # Switching state, mirroring the firmware's C/E/F/G/H/S/Y semantics
        self._cycle = [0]
        self._period_us = 50_000
        self._running = False
        self._run_t0 = self._t0
        self._static = 0                 # packed FET state while not running

        # Stream / INA226 profile
        self._hz = 0.0
        self._avg = 4
        self._bus_every = 1
        self._cnvr = False
        self._cal = {ch: (1.0, 0.0) for ch in range(4)}

        # Plant model
        self._soc = 0.45
        self._v_relax = 0.0              # slow IR relaxation after current stops
        self._seq = 0
        self._cycler = _parse_cycler_env(os.environ.get("SC_SIM_CYCLER", ""))
        if self._cycler:
            log.warning("SIMULATION: timed external cycler %s s "
                        "(charge, rest, discharge, rest)", self._cycler)

        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._stream_loop, name="SimFirmware-Stream", daemon=True,
        )
        self._thread.start()
        log.warning("SIMULATION: synthetic RP2040 firmware running — no hardware")

    def stop(self):
        self._stop.set()

    # -- command interface ----------------------------------------------------

    def _ticks_us(self):
        return int((time.monotonic() - self._t0) * 1_000_000)

    def handle(self, cmd):
        """Return the reply line the real firmware would send for ``cmd``."""
        parts = cmd.split()
        if not parts:
            return "ERR empty"
        op, args = parts[0], parts[1:]
        try:
            with self._lock:
                return self._dispatch(op, args)
        except (ValueError, IndexError) as e:
            return f"ERR {op} {e}"

    def _dispatch(self, op, args):
        if op == "S":
            if len(args) != 4:
                return "ERR S needs 4 args"
            self._running = False
            self._static = sum(int(bool(int(a))) << (3 - i) for i, a in enumerate(args))
            return "OK S"
        if op == "Y":
            # Live set: FETs change, switching timer keeps going.
            if len(args) != 4:
                return "ERR Y needs 4 args"
            self._static = sum(int(bool(int(a))) << (3 - i) for i, a in enumerate(args))
            return "OK Y"
        if op in ("C", "E"):
            n = int(args[0])
            cycle = [int(a) & 0xF for a in args[1:1 + n]]
            if len(cycle) != n or n == 0:
                return f"ERR {op} bad length"
            self._cycle = cycle
            if op == "C":
                # C halts the timer and turns everything off
                self._running = False
                self._static = 0
            return f"OK {op} {n}"
        if op == "F":
            self._period_us = max(50, int(args[0]))
            return f"OK F {self._period_us}"
        if op == "G":
            self._running = True
            self._run_t0 = time.monotonic()
            return f"OK G {len(self._cycle)} {self._period_us} {self._ticks_us()}"
        if op == "H":
            self._running = False
            self._static = 0
            return "OK H"
        if op == "K":
            self._static = self._cycle[0]
            self._cycle = self._cycle[1:] + self._cycle[:1]
            return "OK K"
        if op == "P":
            return f"OK P {self._ticks_us()}"
        if op == "T":
            self._hz = min(_MAX_HZ, max(0.0, float(args[0])))
            return f"OK T {self._hz:.1f}"
        if op == "A":
            self._avg = int(args[0])
            return f"OK A {self._avg} {_MAX_HZ:.1f}"
        if op == "V":
            self._bus_every = int(args[0])
            return f"OK V {self._bus_every} {_MAX_HZ:.1f}"
        if op == "M":
            return f"OK M {self._avg} {self._bus_every} {_MAX_HZ:.1f}"
        if op == "N":
            self._cnvr = bool(int(args[0]))
            return f"OK N {int(self._cnvr)} {int(self._cnvr)}"
        if op == "Z":
            n = int(args[0])
            return f"OK Z {n} 900 700 120 80 {_MAX_HZ:.1f}"
        if op == "W":
            ch, gain, offset = int(args[0]), float(args[1]), float(args[2])
            if ch == 4:
                self._cal = {c: (1.0, 0.0) for c in range(4)}
                return "OK W 4 1.000000 0.000000"
            if not 0 <= ch <= 3:
                return "ERR W bad channel"
            self._cal[ch] = (gain, offset)
            return f"OK W {ch} {gain:.6f} {offset:.6f}"
        if op == "U":
            cal = " ".join(f"{c}:{g:.6f}:{o:.6f}" for c, (g, o) in sorted(self._cal.items()))
            return f"OK U {cal}"
        if op == "I":
            data = self._sample(time.monotonic(), 0.0)
            return "OK I " + json.dumps(data)
        return f"ERR unknown command {op}"

    # -- plant model ----------------------------------------------------------

    def _cycler_current(self, now):
        """Cycler current on the SC_SIM_CYCLER timetable."""
        charge, rest1, discharge, _rest2 = self._cycler
        t = (now - self._t0) % sum(self._cycler)
        if t < charge:
            return _CHARGE_A
        if t < charge + rest1:
            return 0.0
        if t < charge + rest1 + discharge:
            return _DISCHARGE_A
        return 0.0

    def _fets_now(self, now):
        """(fets tuple, switching?, step period s, cycle) at time ``now``."""
        if not self._running:
            return _unpack(self._static), False, 0.0, None
        period_s = self._period_us / 1_000_000.0
        step = int((now - self._run_t0) / period_s) % len(self._cycle)
        return _unpack(self._cycle[step]), True, period_s, self._cycle

    def _sample(self, now, dt):
        """Advance the plant by ``dt`` seconds and return one sensor frame
        as {name: {"voltage", "current"}}. Caller holds the lock."""
        fets, switching, period_s, cycle = self._fets_now(now)
        p_on = fets[0] or fets[1]
        n_on = fets[2] or fets[3]
        all_on = all(fets) and (not switching or all(c == 0xF for c in cycle))

        # Cycler current: discharges through the all-on (transparent)
        # bridge, charges while the bridge is switching, and has no path
        # when a side is fully open.
        if not (p_on and n_on):
            i_cyc = 0.0
        elif self._cycler:
            i_cyc = self._cycler_current(now)
        elif all_on:
            i_cyc = _DISCHARGE_A
        elif switching:
            i_cyc = _CHARGE_A
            if len(cycle) == 2:
                # Pulse charge: bursty, higher peaks
                i_cyc *= 1.0 + 0.4 * ((now * 3.0) % 1.0 < 0.5)
        else:
            i_cyc = 0.05  # DEBUG: a static partial path trickles a little

        # Taper near the ends like a CV phase / low-voltage cutoff
        if i_cyc > 0:
            i_cyc *= max(0.05, min(1.0, (0.97 - self._soc) / 0.10))
        elif i_cyc < 0:
            i_cyc *= max(0.05, min(1.0, (self._soc - 0.03) / 0.10))
        self._soc = min(0.99, max(0.01, self._soc + i_cyc * dt / _CAPACITY_AS))
        # IR relaxation: settles over ~20 s after the current changes
        self._v_relax += (i_cyc * _R_INTERNAL - self._v_relax) * min(1.0, dt / 20.0)

        # How each side splits the current. If the switching is faster than
        # the sample rate the INA226 averaging smears it into the duty.
        sample_dt = 1.0 / self._hz if self._hz > 0 else 0.1
        if switching and period_s < sample_dt:
            states = [_unpack(s) for s in cycle]
            duty = [sum(st[i] for st in states) / len(states) for i in range(4)]
        else:
            duty = [float(f) for f in fets]
        share = []
        for side in ((0, 1), (2, 3)):
            tot = duty[side[0]] + duty[side[1]]
            for i in side:
                share.append(duty[i] / tot if tot > 0 else 0.0)

        ocv = 3.60 + 0.42 * self._soc + 0.08 * (self._soc ** 3)
        data = {}
        for i, name in enumerate(_CHANNELS):
            gain, offset = self._cal[i]
            cur = i_cyc * share[i] + random.gauss(0.0, 0.0004)
            cur = cur * gain + offset
            volt = (ocv + _V_OFFSETS[i] + self._v_relax
                    + 0.6 * _R_INTERNAL * (i_cyc * share[i])
                    + random.gauss(0.0, 0.0008))
            data[name] = {"voltage": round(volt, 4), "current": round(cur, 6)}
        return data

    def _stream_loop(self):
        last = time.monotonic()
        next_t = last
        while not self._stop.is_set():
            with self._lock:
                hz = self._hz
            if hz <= 0:
                self._stop.wait(0.1)
                last = next_t = time.monotonic()
                continue
            now = time.monotonic()
            if now < next_t:
                self._stop.wait(next_t - now)
                continue
            with self._lock:
                data = self._sample(now, now - last)
                self._seq = (self._seq + 1) & 0xFFFFFFFF
                seq = self._seq
                ticks = self._ticks_us()
            last = now
            # Don't try to catch up after a stall — just resume the cadence.
            next_t = max(next_t + 1.0 / hz, now)
            vals = " ".join(
                f"{data[n]['voltage']:.4f} {data[n]['current']:.6f}" for n in _CHANNELS
            )
            try:
                self._on_line(f"D {ticks} {seq} {vals}")
            except Exception:
                log.exception("SimFirmware: stream callback failed")
