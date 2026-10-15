"""
Switching Circuit V2 - Mode Controller.

State machine with five modes: IDLE, CHARGE, DISCHARGE, PULSE_CHARGE, DEBUG.
Every transition enforces a dead-time interlock (all-off gap) to prevent
shoot-through.  All public methods are thread-safe.

Two cycler-aware subsystems run alongside:
  - AutoFollow: hysteresis-based current-driven mode switcher (active
    control). Optional, toggled by user from the TUI.
  - FreqSweep: optional per-cycle frequency/mode list that AutoFollow
    consults at each charge onset (server/freq_sweep.py).
  - ScheduleMonitor: passive PLAN-vs-OBSERVED tracker. Always running;
    fed by load_schedule(); does not control any modes.
"""

import enum
import logging
import threading
from time import sleep

from server.auto_follow import ALLOWED_TARGET_MODES, AutoFollow
from server.config import DEAD_TIME
from server.freq_sweep import FreqSweep
from server.schedule_monitor import ScheduleMonitor

log = logging.getLogger(__name__)


class Mode(enum.Enum):
    IDLE = "idle"
    CHARGE = "charge"
    DISCHARGE = "discharge"
    PULSE_CHARGE = "pulse_charge"
    DEBUG = "debug"


class ModeController:
    """Coordinates the GPIODriver, SequenceEngine, and system mode."""

    def __init__(self, gpio_driver, sequence_engine):
        self._gpio = gpio_driver
        self._engine = sequence_engine
        self._lock = threading.Lock()
        self._mode = Mode.IDLE
        self._loaded_schedule = None

        # Per-cycle frequency sweep, consulted by auto-follow at each
        # charge onset. Off until configured and enabled.
        self._freq_sweep = FreqSweep(set_frequency_fn=self._engine.set_frequency)

        # Auto-follow: hysteresis-based current-driven mode switcher.
        # Started here but disabled by default; user toggles via TUI.
        self._auto_follow = AutoFollow(
            get_sensor_data_fn=self._gpio.get_sensor_data,
            set_mode_fn=lambda m: self._set_mode_internal(Mode(m)),
            is_data_fresh_fn=self._gpio.sensor_data_fresh,
            sweep=self._freq_sweep,
        )
        self._auto_follow.start()

        # After the RP2040 link drops and comes back, the firmware may have
        # rebooted (FETs off, no sequence) — re-drive the current mode.
        self._gpio.on_reconnect = self._reapply_mode

        # Schedule monitor: passive PLAN-vs-OBSERVED tracker. Always
        # running; updates OBSERVED state from sensor data continuously
        # so the TUI can show live detection even before a schedule is
        # loaded.
        self._schedule_monitor = ScheduleMonitor(
            get_sensor_data_fn=self._gpio.get_sensor_data,
        )
        self._schedule_monitor.start_thread()

        # Ensure we start in a safe state
        self._gpio.all_off()
        self._engine.pause()
        log.info("ModeController initialised — mode=IDLE")

    # -- public API ---------------------------------------------------------

    def set_mode(self, mode):
        """
        Transition to a new mode.

        Accepts a Mode enum member or a string ('idle', 'charge', etc.).
        Returns the new Mode value.
        Raises ValueError for unknown mode strings.

        Interaction with auto-follow: while auto-follow is enabled,
        picking a switching target (`charge` / `pulse_charge`) updates
        the auto-follow target without forcing the mode (auto-follow's
        hysteresis decides when to engage). Picking any other mode
        (`idle` / `discharge` / `debug`) disables auto-follow first,
        then applies the requested mode.
        """
        if isinstance(mode, str):
            try:
                mode = Mode(mode.lower())
            except ValueError:
                raise ValueError(
                    f"Unknown mode {mode!r}. "
                    f"Valid modes: {', '.join(m.value for m in Mode)}"
                )

        # Route through auto-follow when enabled and the target is a
        # switching mode — let the controller's hysteresis decide
        # engagement instead of forcing the transition here.
        if self._auto_follow.enabled and mode.value in ALLOWED_TARGET_MODES:
            self._auto_follow.set_target_mode(mode.value)
            return self.get_mode()

        # Any other mode pick disables auto-follow before proceeding.
        if self._auto_follow.enabled:
            self._auto_follow.set_enabled(False)

        return self._set_mode_internal(mode)

    def _set_mode_internal(self, mode):
        """Internal mode transition — also used by AutoFollow to drive
        the hardware state directly."""
        with self._lock:
            if mode == self._mode:
                return self._mode
            prev = self._mode
            log.info("Mode transition: %s -> %s", prev.value, mode.value)

            # Auto-follow transitions between discharge (all-on /
            # transparent) and switching modes only widen or narrow the
            # set of conducting paths — no shoot-through risk.  Skip
            # the engine pause (which sends H = all-off) and dead-time
            # gap so the cycler NEVER sees an open circuit.
            skip_dead = (
                (prev == Mode.DISCHARGE and mode in (Mode.CHARGE, Mode.PULSE_CHARGE))
                or (prev in (Mode.CHARGE, Mode.PULSE_CHARGE) and mode == Mode.DISCHARGE)
            )

            if skip_dead:
                # Seamless: go directly from current FET state to new
                # state without any all-off intermediate.  Uses live
                # firmware commands (E, Y) that don't halt the timer
                # or turn FETs off.  Firmware older than 2026-05-20
                # rejects E/Y (pi-SW6 found 2026-10-10) — without a
                # fallback the board kept switching through discharge
                # while reporting all-on.
                live = self._gpio.live_commands_supported
                if mode == Mode.DISCHARGE:
                    if live:
                        # Live-reprogram cycle to all-on, then set FETs
                        # immediately.  Timer keeps running but every tick
                        # applies all-on.  resume() will reprogram later.
                        self._gpio.program_sequence_live([0xF])
                        self._gpio.all_on_no_halt()
                    else:
                        # S halts the timer and drives all-on in the same
                        # firmware call — still no open circuit.
                        self._gpio.all_on()
                    self._engine.pause_silent()
                elif mode in (Mode.CHARGE, Mode.PULSE_CHARGE):
                    self._engine.set_pulse_mode(mode == Mode.PULSE_CHARGE)
                    if live:
                        self._engine.resume_live()
                    else:
                        # C + F + G: correct sequence, but C opens all FETs
                        # for the ~10-50 ms until G lands.
                        self._engine.resume()
            else:
                self._engine.pause()
                self._gpio.all_off()
                sleep(DEAD_TIME)

                if mode == Mode.IDLE:
                    pass
                elif mode == Mode.CHARGE:
                    self._engine.set_pulse_mode(False)
                    self._engine.resume()
                elif mode == Mode.DISCHARGE:
                    self._gpio.all_on()
                elif mode == Mode.PULSE_CHARGE:
                    self._engine.set_pulse_mode(True)
                    self._engine.resume()
                elif mode == Mode.DEBUG:
                    self._debug_step = -1

            self._mode = mode
            return self._mode

    def _reapply_mode(self):
        """Re-drive the FETs for the current mode after an RP2040 reconnect.

        If the firmware kept running these are idempotent; if it rebooted
        the FETs are already off. DISCHARGE uses S (works on every
        firmware, no gap); switching modes use the live path when the
        firmware supports it."""
        with self._lock:
            mode = self._mode
            if mode == Mode.DISCHARGE:
                self._gpio.all_on()
            elif mode in (Mode.CHARGE, Mode.PULSE_CHARGE):
                if self._gpio.live_commands_supported:
                    self._engine.resume_live()
                else:
                    self._engine.resume()
            elif mode == Mode.IDLE:
                self._gpio.all_off()
            elif mode == Mode.DEBUG:
                self._gpio.apply_state(tuple(self._gpio.get_fet_states()))
        log.warning("Re-applied mode %s after RP2040 reconnect", mode.value)

    # -- schedule management -------------------------------------------------

    def load_schedule(self, schedule):
        """Load a schedule for the passive monitor."""
        self._loaded_schedule = schedule
        self._schedule_monitor.load_schedule(schedule)
        log.info("Schedule loaded: %r (%d steps, %d repeats)",
                 schedule.name, len(schedule.steps), schedule.repeat)

    def get_loaded_schedule(self):
        return self._loaded_schedule

    def get_schedule_monitor(self):
        return self._schedule_monitor

    def restart_schedule_monitor(self):
        """Reset the PLAN clock to step 0 of cycle 0."""
        self._schedule_monitor.restart()

    def stop_schedule_monitor(self):
        self._schedule_monitor.stop()

    def get_mode(self):
        with self._lock:
            return self._mode

    # -- auto-follow ---------------------------------------------------------

    def set_auto_follow_enabled(self, enabled: bool):
        """Enable or disable threshold-driven mode switching."""
        self._auto_follow.set_enabled(enabled)

    def set_auto_follow_thresholds(self, i_enter_a: float, i_exit_a: float):
        self._auto_follow.set_thresholds(i_enter_a, i_exit_a)

    def set_auto_follow_target(self, mode: str):
        self._auto_follow.set_target_mode(mode)

    def get_auto_follow_status(self) -> dict:
        return self._auto_follow.get_status()

    # -- frequency sweep -----------------------------------------------------

    def get_freq_sweep(self) -> FreqSweep:
        return self._freq_sweep

    # -- debug helpers -------------------------------------------------------

    def set_fet(self, index: int, on: bool):
        """Set an individual FET (0=P1, 1=P2, 2=N1, 3=N2). Only works in DEBUG mode."""
        with self._lock:
            if self._mode != Mode.DEBUG:
                raise ValueError("set_fet only available in debug mode")
            if not 0 <= index <= 3:
                raise ValueError(f"FET index must be 0-3, got {index}")
            states = self._gpio.get_fet_states()
            states[index] = bool(on)
            self._gpio.apply_state(tuple(states))
            log.info("Debug: FET %d -> %s", index, "ON" if on else "OFF")

    def debug_step(self):
        """Step through FETs one at a time: P1 -> P2 -> N1 -> N2 -> all off -> repeat."""
        FET_NAMES = ["P1", "P2", "N1", "N2"]
        with self._lock:
            if self._mode != Mode.DEBUG:
                raise ValueError("debug_step only available in debug mode")
            step = getattr(self, "_debug_step", -1)
            step = (step + 1) % 5  # 0-3 = one FET on, 4 = all off
            self._debug_step = step
            if step < 4:
                state = [False, False, False, False]
                state[step] = True
                self._gpio.apply_state(tuple(state))
                log.info("Debug step: %s ON (step %d)", FET_NAMES[step], step)
            else:
                self._gpio.all_off()
                log.info("Debug step: all OFF (step %d)", step)
            return step

    # -- status --------------------------------------------------------------

    def get_status(self):
        """Return full system state dict (engine step labelled at
        ``monotonic()`` — fine for live broadcasts/TUI)."""
        return self._build_status(self._engine.get_state())

    def get_status_at(self, sample_pi_s):
        """Return a status dict whose engine fields (step, fet_states) are
        labelled at ``sample_pi_s`` instead of ``monotonic()``. Used by
        the recorder so each captured row carries the step that was live
        at sample-capture time, not D-line receive time. Critical at
        higher switching freqs (100 Hz+) where the ~4.5 ms emit latency
        becomes a meaningful fraction of a step."""
        return self._build_status(self._engine.get_state_at(sample_pi_s))

    def _build_status(self, engine_state):
        with self._lock:
            mode = self._mode
            debug_step = getattr(self, "_debug_step", -1)
        status = {
            "mode": mode.value,
            "sequence": engine_state["sequence"],
            "step": engine_state["step"],
            "frequency": engine_state["frequency"],
            "fet_states": engine_state["fet_states"],
            "sensors": self._gpio.get_sensor_data(),
            # RP2040 link health — when "stale" is true the sensors above
            # are the last values received, not live readings.
            "link": self._gpio.link_status(),
        }
        if mode == Mode.DEBUG:
            status["debug_step"] = debug_step
        status["auto_follow"] = self._auto_follow.get_status()
        status["freq_sweep"] = self._freq_sweep.get_status()
        status["schedule_monitor"] = self._schedule_monitor.get_status()
        return status
