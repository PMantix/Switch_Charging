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

Pauses (default off): while engaged, every pause_every_s the board drops
to pass-through ("discharge", all FETs on) for pause_s and then resumes
the mode it engaged with. The cycler current keeps flowing through the
pause, so the exit logic is unaffected and a pause is never a new
engagement (the sweep does not see it as a re-engage). The settings come
from the sweep entry when it has them, otherwise from set_pause(). Added
to test whether the brief pass-through gaps the old single-reading exit
produced at 307 Hz (about every 15 s, under 1 s each) change CE.

Frequency blocks (sweep entry "blocks": [[hz, seconds], ...]; hz 0 =
pass-through): at engage the board starts the list and changes the
switching frequency at each block boundary, live, without opening the
circuit; a pass-through block drops to all-on like a pause and the next
switched block resumes the engaged mode at its own frequency. The list
wraps if the charge outlasts it and restarts at every engagement. Each
block start is logged ("block:<i>"). Built for the frequency response
map (Cycle_Toughness/FreqMap): many frequencies compared within one
charge. An entry has blocks or pauses, not both.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from time import monotonic, sleep
from typing import Callable, Optional

from server.config import (
    FAST_STREAM_HZ_LIMITS, FAST_STREAM_FALLBACK_HZ,
    AUTO_FOLLOW_EXIT_CONFIRM_S,
    AUTO_FOLLOW_PAUSE_EVERY_LIMITS,
    AUTO_FOLLOW_PAUSE_LIMITS,
    AUTO_FOLLOW_I_ENTER_A,
    AUTO_FOLLOW_I_EXIT_A,
    AUTO_FOLLOW_LOOP_HZ,
)
from server.cycler_detector import CyclerDetector

log = logging.getLogger(__name__)


ALLOWED_TARGET_MODES = ("charge", "pulse_charge")


def validate_pause(every_s, pause_s) -> tuple:
    """(every_s, pause_s) as floats; every_s = 0 means off (pause_s then ignored). Raises ValueError."""
    every_s = float(every_s or 0.0)
    pause_s = float(pause_s or 0.0)
    if every_s == 0.0:
        return 0.0, 0.0
    lo, hi = AUTO_FOLLOW_PAUSE_EVERY_LIMITS
    if not lo <= every_s <= hi:
        raise ValueError(f"pause interval must be 0 (off) or {lo:g}-{hi:g} s, got {every_s}")
    lo, hi = AUTO_FOLLOW_PAUSE_LIMITS
    if not lo <= pause_s <= hi or pause_s >= every_s:
        raise ValueError(f"pause length must be {lo:g}-{hi:g} s and shorter than the interval, got {pause_s}")
    return every_s, pause_s


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
        set_stream_rate_fn: Optional[Callable[[float], None]] = None,
        get_stream_rate_fn: Optional[Callable[[], float]] = None,
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
        self._pause_every_s = 0.0           # board-wide pause setting (0 = off)
        self._pause_s = 0.0
        self._pause_cfg: Optional[tuple] = None   # (every, length) for the current engagement
        self._engaged_mode: Optional[str] = None  # the switching mode a pause resumes to
        self._next_pause: Optional[float] = None
        self._pause_until: Optional[float] = None
        self._pauses = 0                    # pauses in the current engagement
        self._blocks: Optional[list] = None  # [[hz, s], ...] for the current engagement
        self._block_i = 0
        self._block_until: Optional[float] = None
        self._block_pass = False            # the current block is pass-through
        self._blocks_started = 0
        self._blocks_t0: Optional[float] = None   # when the current engagement's first block started
        self._blocks_total_s = 0.0                # one pass through its block list
        # Fast sensor stream inside frequency blocks (set_fast_stream): the sensor stream runs at fast_hz only
        # between the start of block `start_block` and `stop_before_end_s` before the block list's first pass
        # ends, so every mode change (engage, exit, pass-through) happens at the base rate. Above ~60/s the
        # RP2040 can miss mode-change commands (SW10, 2026-10-24).
        self._set_stream_rate = set_stream_rate_fn
        self._get_stream_rate = get_stream_rate_fn
        self._fast_hz = 0.0
        self._fast_start_block = 1
        self._fast_stop_before_s = 90.0
        self._fast_on = False
        self._fast_base: Optional[float] = None

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

    def set_pause(self, every_s: float, pause_s: float):
        """Board-wide pause setting, used when the sweep entry has none. every_s = 0 turns pauses off."""
        every_s, pause_s = validate_pause(every_s, pause_s)
        with self._lock:
            self._pause_every_s, self._pause_s = every_s, pause_s
        log.info("AutoFollow: pauses %s", "off" if not every_s else
                 "%.2f s every %.1f s" % (pause_s, every_s))

    def set_fast_stream(self, hz: float, start_block: int = 1, stop_before_end_s: float = 90.0):
        """Fast sensor stream inside frequency blocks. hz = 0 turns it off. Only used for engagements whose block
        list has no pass-through block (a pass block is a mode change)."""
        hz, start_block, stop = float(hz), int(start_block), float(stop_before_end_s)
        if hz != 0 and not FAST_STREAM_HZ_LIMITS[0] <= hz <= FAST_STREAM_HZ_LIMITS[1]:
            raise ValueError(f"fast stream rate must be 0 (off) or {FAST_STREAM_HZ_LIMITS[0]:g}-"
                             f"{FAST_STREAM_HZ_LIMITS[1]:g} /s, got {hz}")
        if start_block < 1:
            raise ValueError(f"start_block must be >= 1 (block 0 holds the engage), got {start_block}")
        if not 10.0 <= stop <= 600.0:
            raise ValueError(f"stop_before_end_s must be 10-600 s, got {stop}")
        with self._lock:
            self._fast_hz, self._fast_start_block, self._fast_stop_before_s = hz, start_block, stop
        log.info("AutoFollow: fast stream %s", "off" if not hz else
                 "%g /s from block %d to %g s before the end" % (hz, start_block, stop))

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
                "pause_every_s": self._pause_every_s,
                "pause_s": self._pause_s,
                # what the current engagement is doing (from the sweep entry or the board setting)
                "pause_active_cfg": list(self._pause_cfg) if self._pause_cfg else None,
                "in_pause": self._pause_until is not None,
                "pauses_this_charge": self._pauses,
                "blocks_active": bool(self._blocks),
                "block_index": self._block_i if self._blocks else None,
                "block_hz": self._blocks[self._block_i][0] if self._blocks else None,
                "block_remaining_s": (round(max(0.0, self._block_until - self._clock()), 1)
                                      if self._blocks and self._block_until is not None else None),
                "blocks_this_charge": self._blocks_started,
                "fast_stream_hz": self._fast_hz,
                "fast_stream_start_block": self._fast_start_block,
                "fast_stream_stop_before_end_s": self._fast_stop_before_s,
                "fast_stream_on": self._fast_on,
                "fast_stream_base_hz": self._fast_base,
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
            if self._fast_on:
                self._stream_down("stale sensor data")
            return
        sensor_data = self._get_sensor_data()
        if not sensor_data:
            return
        result = self._detector.feed(sensor_data)
        avg_i = result.avg_current
        avg_v = result.avg_voltage

        target = None
        pause_action = None
        block_action = None
        fast = None                                   # "up" / "down": change the sensor stream rate
        with self._lock:
            self._latest_avg_i = avg_i
            self._latest_avg_v = avg_v
            if not self._enabled:
                fast_down = self._fast_on
            else:
                fast_down = None
        if fast_down:
            self._stream_down("auto-follow disabled")
        if fast_down is not None:
            return
        with self._lock:
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
                    self._clear_pauses()
            elif not self._is_active:
                self._above_since = None
            elif self._should_exit(avg_i):
                target = "discharge"
                self._is_active = False
                self._active_i.clear()
                self._clear_pauses()
                if self._fast_on:
                    fast = "down"
            elif self._blocks:
                block_action = self._block_step()
                fast = self._fast_step()
            else:
                pause_action = self._pause_step()

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

        first_block = None
        if target is not None and target != "discharge":
            sweeping = sweep is not None and sweep.enabled
            blocks = sweep.current_blocks() if sweeping else None
            cfg = None if blocks else (sweep.current_pause() if sweeping else None)
            with self._lock:
                if cfg is None and not blocks and self._pause_every_s > 0:
                    cfg = (self._pause_every_s, self._pause_s)
                self._engaged_mode = target
                self._pause_cfg = cfg
                self._next_pause = self._clock() + cfg[0] if cfg else None
                if blocks:
                    self._blocks = blocks
                    self._block_i = 0
                    self._block_until = self._clock() + blocks[0][1]
                    self._block_pass = blocks[0][0] == 0
                    self._blocks_started = 1
                    self._blocks_t0 = self._clock()
                    self._blocks_total_s = float(sum(d for _, d in blocks))
                    first_block = blocks[0][0]

        if first_block is not None:
            # First block: set its frequency before switching starts, or stay in pass-through.
            sweep.log_block(0, first_block, avg_i, avg_v)
            if first_block == 0:
                log.info("AutoFollow: engaged, block 0 pass-through (i=%.4f A)", avg_i)
                self._set_mode("discharge")
                return
            sweep.set_block_frequency(first_block)

        if fast == "down":                            # before any mode change: back to the base rate first
            self._stream_down("leaving the fast window", avg_i, avg_v)
        if target is not None:
            log.info("AutoFollow: -> %s (i=%.4f A)", target, avg_i)
            self._set_mode(target)
        elif block_action is not None:
            i, hz, was_pass, mode = block_action
            if hz == 0:
                if not was_pass:
                    self._set_mode("discharge")
            else:
                if sweep is not None:
                    sweep.set_block_frequency(hz)
                if was_pass:
                    self._set_mode(mode)
            if sweep is not None:
                sweep.log_block(i, hz, avg_i, avg_v)
        elif pause_action == "pause":
            self._set_mode("discharge")
            if sweep is not None and sweep.enabled:
                sweep.log_pause(avg_i, avg_v)
        elif pause_action is not None:
            self._set_mode(pause_action)            # resume the engaged mode
        if fast == "up":                              # after this tick's block change
            self._stream_up(avg_i, avg_v)

    # -- fast sensor stream inside blocks ---------------------------------------

    def _fast_step(self) -> Optional[str]:
        """Engaged with blocks: enter or leave the fast-stream window? Caller holds the lock."""
        if self._set_stream_rate is None or not self._blocks or self._blocks_t0 is None:
            return None
        if any(hz == 0 for hz, _ in self._blocks):
            return "down" if self._fast_on else None      # pass-through blocks are mode changes: never fast
        now = self._clock()
        window_end = self._blocks_t0 + self._blocks_total_s - self._fast_stop_before_s
        in_window = (self._fast_hz > 0 and self._blocks_started - 1 >= self._fast_start_block
                     and now < window_end)
        if in_window and not self._fast_on:
            return "up"
        if self._fast_on and not in_window:
            return "down"
        return None

    def _stream_up(self, avg_i=0.0, avg_v=0.0):
        with self._lock:
            if self._fast_on or self._fast_hz <= 0:
                return
            hz = self._fast_hz
        base = self._get_stream_rate() if self._get_stream_rate else None
        try:
            self._set_stream_rate(hz)
        except Exception:
            log.exception("AutoFollow: fast stream: could not set %g /s", hz)
            return
        with self._lock:
            self._fast_on = True
            self._fast_base = base
        log.info("AutoFollow: fast stream on (%g /s, base %s /s)", hz, base)
        if self._sweep is not None:
            self._sweep.log_event(f"fast_stream:{hz:g}", avg_i, avg_v)

    def _stream_down(self, why: str, avg_i=0.0, avg_v=0.0):
        with self._lock:
            if not self._fast_on:
                return
            base = self._fast_base if self._fast_base else FAST_STREAM_FALLBACK_HZ
        try:
            self._set_stream_rate(base)
        except Exception:
            log.exception("AutoFollow: fast stream: could not restore %g /s", base)
        with self._lock:
            self._fast_on = False
        log.info("AutoFollow: fast stream off (%s; back to %g /s)", why, base)
        if self._sweep is not None:
            self._sweep.log_event("fast_stream:off", avg_i, avg_v)

    def persist_stream_rate(self, current: float) -> float:
        """The rate to save across a power cycle: the base rate while the fast window is on."""
        with self._lock:
            return self._fast_base if self._fast_on and self._fast_base else current

    def _clear_pauses(self):
        """Caller holds the lock. Also clears the frequency blocks."""
        self._pause_cfg = None
        self._engaged_mode = None
        self._next_pause = None
        self._pause_until = None
        self._pauses = 0
        self._blocks = None
        self._block_i = 0
        self._block_until = None
        self._block_pass = False
        self._blocks_started = 0
        self._blocks_t0 = None
        self._blocks_total_s = 0.0

    def _block_step(self) -> Optional[tuple]:
        """Engaged with frequency blocks: next block due? Caller holds the lock.
        Returns (index, hz, previous_was_pass, engaged_mode) or None."""
        if not self._blocks or self._block_until is None:
            return None
        now = self._clock()
        if now < self._block_until:
            return None
        was_pass = self._block_pass
        self._block_i = (self._block_i + 1) % len(self._blocks)
        hz, dur = self._blocks[self._block_i]
        self._block_until = now + dur
        self._block_pass = hz == 0
        self._blocks_started += 1
        return self._block_i, hz, was_pass, self._engaged_mode or self._target_mode

    def _pause_step(self) -> Optional[str]:
        """Engaged and staying engaged: start or end a pause? Caller holds the lock.
        Returns "pause", the mode to resume, or None."""
        if not self._pause_cfg or self._engaged_mode not in ALLOWED_TARGET_MODES:
            return None
        now = self._clock()
        if self._pause_until is not None:
            if now >= self._pause_until:
                self._pause_until = None
                self._next_pause = now + self._pause_cfg[0]
                return self._engaged_mode
            return None
        if self._next_pause is not None and now >= self._next_pause:
            self._pause_until = now + self._pause_cfg[1]
            self._pauses += 1
            return "pause"
        return None

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
