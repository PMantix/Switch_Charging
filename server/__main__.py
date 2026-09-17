"""
Switching Circuit V2 - Server Entry Point.

Run with:  python -m server

Instantiates all subsystems, wires them together, sets up the rotary
encoder and TM1637 display for local Pi control, and runs until
SIGINT / SIGTERM.
"""

import logging
import signal
import sys
from time import sleep, time

from server.config import (
    PIN_TM1637_CLK, PIN_TM1637_DIO,
    PIN_ROTARY_CLK, PIN_ROTARY_DT, PIN_ROTARY_BTN,
    SEQUENCES, NUM_SEQUENCES,
    MIN_FREQ, MAX_FREQ,
)
from server.gpio_driver import GPIODriver
from server.sequence_engine import SequenceEngine
from server.mode_controller import ModeController, Mode
from server.command_server import CommandServer
from server.state_persistence import load_state

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("server")

# ---------------------------------------------------------------------------
# Optional hardware imports (display + rotary encoder)
# ---------------------------------------------------------------------------
_HAS_LOCAL_HW = True
try:
    from gpiozero import RotaryEncoder, Button
    from tm1637 import TM1637
except ImportError:
    _HAS_LOCAL_HW = False
    log.warning(
        "gpiozero/tm1637 not available — local rotary encoder and display disabled"
    )


# ---------------------------------------------------------------------------
# TM1637 display helpers (mirrors switching_circuit_v2.py logic)
# ---------------------------------------------------------------------------
class DisplayManager:
    """Manages the TM1637 4-digit 7-segment display."""

    def __init__(self, engine):
        self._engine = engine
        self._display = TM1637(clk=PIN_TM1637_CLK, dio=PIN_TM1637_DIO)
        self._display.set_brightness(3)

        # Extend segment table: index 10 = dash, then 10..19 = digits with DP
        if len(self._display._segments) < 11:
            self._display._segments.append(0x40)  # index 10 = dash
        self.BLANK = 10
        self.DECIMAL_START = len(self._display._segments)
        for i in range(10):
            self._display._segments.append(
                self._display._segments[i] | 0x80
            )  # digits with decimal point

        self._cached = None

        # Sequence-selection display state
        self.sequence_display_detailed = False

    def update(self, switch_sequence_mode, sequence_sel):
        """Compute and push digits if changed.

        Args:
            switch_sequence_mode: True when in sequence-selection mode.
            sequence_sel: 1-based sequence index.
        """
        digits = self._compute_digits(switch_sequence_mode, sequence_sel)
        if digits != self._cached:
            self._display.display(digits)
            self._cached = digits

    def _compute_digits(self, switch_sequence_mode, sequence_sel):
        if not switch_sequence_mode:
            freq = self._engine.get_frequency()
            freq_disp = max(int(MIN_FREQ * 10), int(freq * 10))
            return [int(d) for d in f"{freq_disp:04}"]
        else:
            if not self.sequence_display_detailed:
                return [
                    self.BLANK, self.BLANK, self.BLANK,
                    self.DECIMAL_START + sequence_sel,
                ]
            else:
                if sequence_sel == 1:
                    return [0, 0, 0, 0]
                elif sequence_sel == NUM_SEQUENCES:
                    return [1, 1, 1, 1]
                else:
                    return [s + 1 for s in SEQUENCES[sequence_sel - 1]]


# ---------------------------------------------------------------------------
# Rotary encoder controller (mirrors switching_circuit_v2.py logic)
# ---------------------------------------------------------------------------
class RotaryController:
    """Handles rotary encoder input for frequency and sequence selection."""

    def __init__(self, engine, mode_controller):
        self._engine = engine
        self._mc = mode_controller

        self._rotary = RotaryEncoder(
            PIN_ROTARY_CLK, PIN_ROTARY_DT, max_steps=1000, wrap=False,
        )
        self._button = Button(PIN_ROTARY_BTN)
        self._button.hold_time = 0.5

        # State
        self.switch_sequence_mode = False
        self.sequence_sel = 1  # 1-based
        self.step_rate = 1  # 1 = fine (0.1 Hz), 10 = coarse (1 Hz)
        self.sequence_display_detailed = False

        self._freq_base = self._engine.get_frequency()
        self._dial_offset = self._rotary.steps
        self._last_rotary_steps = self._rotary.steps
        self._sequence_mode_base = 0

        # Button timing
        self._press_start = None
        self._last_freq_toggle_time = 0.0
        self._freq_toggle_hysteresis = 1.5

        # Wire callbacks
        self._button.when_pressed = self._button_pressed
        self._button.when_released = self._button_released
        self._button.when_held = self._toggle_mode

    def poll(self):
        """Called from the main loop to check for rotary changes."""
        if self._rotary.steps == self._last_rotary_steps:
            return

        self._last_rotary_steps = self._rotary.steps

        if not self.switch_sequence_mode:
            increment = 0.1 if self.step_rate == 1 else 1.0
            raw_freq = self._freq_base + (
                self._rotary.steps - self._dial_offset
            ) * increment
            freq = max(min(raw_freq, MAX_FREQ), MIN_FREQ)
            self._engine.set_frequency(freq)
            log.info("Rotary: frequency -> %.1f Hz", freq)
        else:
            self.sequence_sel = 1 + (
                self._rotary.steps - self._dial_offset - self._sequence_mode_base
            ) % NUM_SEQUENCES
            if self.sequence_sel == 0:
                self.sequence_sel = NUM_SEQUENCES
            # Update engine with 0-based index
            self._engine.set_sequence(self.sequence_sel - 1)
            log.info(
                "Rotary: sequence -> %d (%s)",
                self.sequence_sel,
                SEQUENCES[self.sequence_sel - 1],
            )

    def _button_pressed(self):
        self._press_start = time()

    def _button_released(self):
        self._freq_base = self._engine.get_frequency()
        self._dial_offset = self._rotary.steps

        duration = (
            (time() - self._press_start) if self._press_start is not None else 0
        )

        if duration < 0.5 and (
            time() - self._last_freq_toggle_time
        ) >= self._freq_toggle_hysteresis:
            if self.switch_sequence_mode:
                self.sequence_display_detailed = not self.sequence_display_detailed
                log.info(
                    "Sequence display detail toggled: %s",
                    self.sequence_display_detailed,
                )
            else:
                self.step_rate = 10 if self.step_rate == 1 else 1
                log.info("Frequency step rate set to x%d", self.step_rate)
            self._last_freq_toggle_time = time()

        self._press_start = None

    def _toggle_mode(self):
        """Long-press: switch between frequency and sequence-selection modes."""
        self.switch_sequence_mode = not self.switch_sequence_mode

        if self.switch_sequence_mode:
            log.info("Entered sequence selector mode")
            self._sequence_mode_base = (
                self._rotary.steps - self._dial_offset
            ) - (self.sequence_sel - 1)
            # Pause switching while selecting
            self._mc.set_mode(Mode.IDLE)
        else:
            log.info("Exited sequence selector mode")

        self._freq_base = self._engine.get_frequency()
        self._dial_offset = self._rotary.steps

    def cleanup(self):
        self._rotary.close()
        self._button.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("Switching Circuit V2 server starting")

    # Core subsystems
    gpio = GPIODriver()
    engine = SequenceEngine(gpio)
    mc = ModeController(gpio, engine)

    # Restore settings from before the last power cycle, if any (see
    # server/state_persistence.py — ModeController itself always starts
    # safe in IDLE with everything off; this deliberately overrides that
    # once we're ready, per explicit request to fully auto-resume,
    # including the active mode). Order matters: frequency/sequence and
    # auto-follow thresholds/target go first, then mode is forced via
    # set_mode() while auto-follow is still disabled (so it takes the
    # exact saved mode instead of being routed through auto-follow's
    # hysteresis), and auto-follow's enabled flag is restored last.
    saved_state = load_state()
    if saved_state:
        try:
            if "frequency" in saved_state:
                engine.set_frequency(saved_state["frequency"])
            if "sequence" in saved_state:
                engine.set_sequence(saved_state["sequence"])
            af = saved_state.get("auto_follow") or {}
            if af.get("i_enter_a") is not None and af.get("i_exit_a") is not None:
                mc.set_auto_follow_thresholds(af["i_enter_a"], af["i_exit_a"])
            if af.get("target_mode"):
                mc.set_auto_follow_target(af["target_mode"])
            if saved_state.get("mode"):
                mc.set_mode(saved_state["mode"])
            if af.get("enabled"):
                mc.set_auto_follow_enabled(True)
            log.info("Restored persisted settings: %s", saved_state)
        except Exception:
            log.exception("Failed to restore persisted state — starting in IDLE")

    # Command server
    cmd_server = CommandServer(mc, engine)
    cmd_server.start()

    # Start sensor streaming at default rate
    gpio.set_sensor_rate(15.0)

    # Local hardware (rotary + display)
    rotary_ctrl = None
    display_mgr = None
    if _HAS_LOCAL_HW:
        try:
            rotary_ctrl = RotaryController(engine, mc)
            display_mgr = DisplayManager(engine)
            log.info("Local hardware (rotary encoder + TM1637) initialised")
        except Exception:
            log.exception("Failed to initialise local hardware — continuing without")
            rotary_ctrl = None
            display_mgr = None

    # Shutdown handling
    _shutting_down = False

    def shutdown_handler(signum, frame):
        nonlocal _shutting_down
        if _shutting_down:
            return
        _shutting_down = True
        signame = signal.Signals(signum).name
        log.info("Received %s — shutting down", signame)

        mc.set_mode(Mode.IDLE)
        cmd_server.stop()
        engine.stop()
        if rotary_ctrl:
            rotary_ctrl.cleanup()
        gpio.cleanup()
        log.info("Shutdown complete")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    log.info("Server ready — listening on port %d", cmd_server._port)

    # Main loop: poll local hardware
    try:
        while True:
            if rotary_ctrl:
                rotary_ctrl.poll()
            if display_mgr and rotary_ctrl:
                display_mgr.sequence_display_detailed = (
                    rotary_ctrl.sequence_display_detailed
                )
                display_mgr.update(
                    rotary_ctrl.switch_sequence_mode,
                    rotary_ctrl.sequence_sel,
                )
            sleep(0.01)
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
