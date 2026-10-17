"""
Switching Circuit V2 - Auto-Follow Controller.

Watches the cycler-current estimate (KCL on the HV bus, via
CyclerDetector) and toggles between a user-selected switching mode
and "discharge" (all FETs on, transparent) using two-threshold
hysteresis.

Direction-aware: only positive (charging) current above i_enter
engages switching. Negative current (discharge), rest, or low-current
CV taper all leave the FETs in pass-through, which is the natural
state when no switching circuit is between cycler and cells.

Lifecycle: a single daemon thread polls sensor data at AUTO_FOLLOW_LOOP_HZ.
The thread runs whenever the controller is started; the `enabled` flag
gates whether transitions actually fire.

Optional FreqSweep (server/freq_sweep.py): when one is attached and
enabled, each engage asks it which mode/frequency to use for that
charge instead of using target_mode.

engage_delay_s (default 0) requires the current to stay above i_enter
for that long before switching engages. It keeps short cycler pulses
(e.g. a 1 s DCIR pulse) in pass-through, and on a long CC step it
leaves a pass-through window at full current right before switching
starts — the voltage step at engage then measures the resistance the
switching adds.

exit_confirm_s (default AUTO_FOLLOW_EXIT_CONFIRM_S) debounces exit
while charging: switching stops when the mean current over the last
exit_confirm_s is below i_exit, not on one low reading. The current
estimate is a single KCL sample per tick, and at high switching
frequencies the four sensors catch different phases, so it sags for a
tick or two at full current. A reversal to discharge (i < -i_exit) is
not debounced: it exits on the next tick, as the direction-aware
contract requires.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from time import monotonic, sleep
from typing import Callable, Optional

from server.config import (
    AUTO_FOLLOW_EXIT_CONFIRM_S,
    AUTO_FOLLOW_I_ENTER_A,
    AUTO_FOLLOW_I_EXIT_A,
    AUTO_FOLLOW_LOOP_HZ,
)
from server.cycler_detector import CyclerDetector

log = logging.getLogger(__name__)


ALLOWED_TARGET_MODES = ("charge", "pulse_charge")


class AutoFollow:
    """Hysteresis-based mode switcher driven by cycler current."""

    def __init__(
        self,
        get_sensor_data_fn: Callable[[], dict],
        set_mode_fn: Callable[[str], None],
        i_enter_a: float = AUTO_FOLLOW_I_ENTER_A,
        i_exit_a: float = AUTO_FOLLOW_I_EXIT_A,
        loop_hz: float = AUTO_FOLLOW_LOOP_HZ,
        is_data_fresh_fn: Optional[Callable[[], bool]] = None,
        sweep=None,
        clock: Callable[[], float] = monotonic,
    ):
        if i_enter_a <= i_exit_a:
            raise ValueError(
                f"i_enter ({i_enter_a}) must be greater than i_exit ({i_exit_a})"
            )
        self._get_sensor_data = get_sensor_data_fn
        self._set_mode = set_mode_fn
        # When provided and returning False, ticks are skipped so a dead
        # sensor link can't drive mode changes off frozen values — the
        # current mode is held until live data returns.
        self._is_data_fresh = is_data_fresh_fn
        self._detector = CyclerDetector()  # used only for its KCL avg_current
        self._sweep = sweep
        self._clock = clock

        self._lock = threading.Lock()
        self._enabled = False
        self._i_enter_a = float(i_enter_a)
        self._i_exit_a = float(i_exit_a)
        self._target_mode = "charge"
        self._is_active = False
        self._latest_avg_i = 0.0
        self._latest_avg_v = 0.0
        self._cc_setpoint_a = 0.0  # user-specified CC setpoint for recommendations
        self._engage_delay_s = 0.0
        self._above_since: Optional[float] = None  # when avg_i first rose above i_enter
        self._exit_confirm_s = float(AUTO_FOLLOW_EXIT_CONFIRM_S)
        self._active_i: deque = deque()  # (t, avg_i) while engaged, last exit_confirm_s only
        self._loop_dt = 1.0 / float(loop_hz)

        self._loop_period = 1.0 / float(loop_hz)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="AutoFollow", daemon=True,
        )
        self._thread.start()
        log.info("AutoFollow: thread started")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        log.info("AutoFollow: thread stopped")

    # -- configuration -------------------------------------------------------

    def set_enabled(self, enabled: bool):
        """Enable or disable threshold-driven mode switching.

        On enable: reset to transparent baseline (mode=discharge); next
        loop tick will engage switching if current is already above
        i_enter. This makes "enable auto" deterministic regardless of
        whatever mode was active before.

        On disable: leave the current mode untouched. The user may
        choose to take manual control at the same time by issuing a
        set_mode call externally.
        """
        do_reset = False
        with self._lock:
            was_enabled = self._enabled
            self._enabled = bool(enabled)
            self._above_since = None
            if not was_enabled and self._enabled:
                do_reset = True
                self._is_active = False
            elif was_enabled and not self._enabled:
                self._is_active = False
        if do_reset:
            self._set_mode("discharge")
        log.info("AutoFollow: enabled=%s", enabled)

    def set_thresholds(self, i_enter_a: float, i_exit_a: float):
        if i_enter_a <= i_exit_a:
            raise ValueError(
                f"i_enter ({i_enter_a}) must be greater than i_exit ({i_exit_a})"
            )
        if i_enter_a <= 0:
            raise ValueError(f"i_enter must be positive, got {i_enter_a}")
        if i_exit_a < 0:
            raise ValueError(f"i_exit must be non-negative, got {i_exit_a}")
        with self._lock:
            self._i_enter_a = float(i_enter_a)
            self._i_exit_a = float(i_exit_a)
        log.info("AutoFollow: thresholds enter=%.4f A exit=%.4f A",
                 i_enter_a, i_exit_a)

    def set_engage_delay(self, seconds: float):
        seconds = float(seconds)
        if not 0.0 <= seconds <= 600.0:
            raise ValueError(f"engage delay must be 0-600 s, got {seconds}")
        with self._lock:
            self._engage_delay_s = seconds
        log.info("AutoFollow: engage delay=%.1f s", seconds)

    def set_exit_confirm(self, seconds: float):
        seconds = float(seconds)
        if not 0.0 <= seconds <= 10.0:
            raise ValueError(f"exit confirm must be 0-10 s, got {seconds}")
        with self._lock:
            self._exit_confirm_s = seconds
            self._active_i.clear()
        log.info("AutoFollow: exit confirm=%.2f s", seconds)

    def set_cc_setpoint(self, amps: float):
        with self._lock:
            self._cc_setpoint_a = max(0.0, float(amps))
        log.info("AutoFollow: cc_setpoint=%.4f A", amps)

    def set_target_mode(self, mode: str):
        """Update which switching mode auto-follow applies when active.

        If currently engaged, the mode change is applied immediately.
        Modes other than `charge` / `pulse_charge` are silently ignored.
        """
        if mode not in ALLOWED_TARGET_MODES:
            return
        do_apply = False
        sweeping = self._sweep is not None and self._sweep.enabled
        with self._lock:
            if self._target_mode != mode:
                self._target_mode = mode
                # While a sweep is running it owns the mode of the
                # current charge; the new target only matters once the
                # sweep is turned off.
                if self._enabled and self._is_active and not sweeping:
                    do_apply = True
        if do_apply:
            self._set_mode(mode)
        log.info("AutoFollow: target_mode=%s", mode)

    # -- status --------------------------------------------------------------

    def get_status(self) -> dict:
        with self._lock:
            cc = self._cc_setpoint_a
            status = {
                "enabled": self._enabled,
                "i_enter_a": self._i_enter_a,
                "i_exit_a": self._i_exit_a,
                "target_mode": self._target_mode,
                "active": self._is_active,
                "avg_current_a": round(self._latest_avg_i, 6),
                "avg_voltage_v": round(self._latest_avg_v, 4),
                "cc_setpoint_a": cc,
                "engage_delay_s": self._engage_delay_s,
                "exit_confirm_s": self._exit_confirm_s,
                # Seconds the current has been above i_enter while
                # waiting out the engage delay (None when not waiting).
                "engage_pending_s": (
                    round(self._clock() - self._above_since, 1)
                    if self._above_since is not None and not self._is_active else None
                ),
            }
            if cc > 0:
                status["rec_enter_a"] = round(cc * 0.35, 6)
                status["rec_exit_a"] = round(cc * 0.15, 6)
            return status

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def active(self) -> bool:
        with self._lock:
            return self._is_active

    # -- main loop -----------------------------------------------------------

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("AutoFollow: tick failed")
            sleep(self._loop_period)

    def _tick(self):
        if self._is_data_fresh is not None and not self._is_data_fresh():
            return
        sensor_data = self._get_sensor_data()
        if not sensor_data:
            return
        result = self._detector.feed(sensor_data)
        avg_i = result.avg_current
        avg_v = result.avg_voltage

        target = None
        with self._lock:
            self._latest_avg_i = avg_i
            self._latest_avg_v = avg_v
            if not self._enabled:
                return
            sweep = self._sweep
            if not self._is_active and avg_i > self._i_enter_a:
                now = self._clock()
                if self._above_since is None:
                    self._above_since = now
                if now - self._above_since >= self._engage_delay_s:
                    target = self._target_mode
                    self._is_active = True
                    self._above_since = None
                    self._active_i.clear()
            elif not self._is_active:
                self._above_since = None
            elif self._should_exit(avg_i):
                target = "discharge"
                self._is_active = False
                self._active_i.clear()

        if sweep is not None:
            sweep.observe(avg_i)
            if target is not None and target != "discharge":
                # Charge onset: the sweep sets the frequency now, while the
                # engine is still paused in pass-through, and picks the mode.
                swept = sweep.on_charge_onset(avg_i, avg_v)
                if swept == "passthrough":
                    log.info("AutoFollow: sweep control cycle — staying transparent "
                             "(i=%.4f A)", avg_i)
                    return
                if swept is not None:
                    target = swept

        if target is not None:
            log.info("AutoFollow: -> %s (i=%.4f A)", target, avg_i)
            self._set_mode(target)

    def _should_exit(self, avg_i: float) -> bool:
        """Engaged: leave switching? Caller holds the lock.

        Reversal exits at once. Otherwise the mean over the last
        exit_confirm_s must be below i_exit, and the samples must span
        that window, so a sag of a few ticks at full current can't do it.
        """
        if avg_i < -self._i_exit_a:
            return True
        if self._exit_confirm_s <= 0.0:
            return avg_i < self._i_exit_a
        now = self._clock()
        q = self._active_i
        q.append((now, avg_i))
        while q and q[0][0] < now - self._exit_confirm_s:
            q.popleft()
        if now - q[0][0] < self._exit_confirm_s - 1.5 * self._loop_dt:
            return False  # window not full yet
        return sum(i for _, i in q) / len(q) < self._i_exit_a
