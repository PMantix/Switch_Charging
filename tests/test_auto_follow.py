"""Tests for the AutoFollow hysteresis controller."""

import unittest
from time import sleep
from unittest.mock import MagicMock

from server.auto_follow import AutoFollow


def _sensor(i_a: float, v: float = 3.7):
    """Build sensor data simulating one-cell-active (state-0) so the
    detector's KCL estimate equals i_a directly."""
    return {
        "P1": {"voltage": v, "current": i_a},
        "P2": {"voltage": 0.0, "current": 0.0},
        "N1": {"voltage": v, "current": i_a},
        "N2": {"voltage": 0.0, "current": 0.0},
    }


class _Harness:
    """Glue that lets a test feed sensor values one tick at a time."""
    def __init__(self):
        self.sensor = _sensor(0.0)
        self.mode_calls = []
        self.set_mode = MagicMock(side_effect=self.mode_calls.append)
        self.get_sensor = lambda: self.sensor


class TestAutoFollowBasics(unittest.TestCase):

    def test_thresholds_must_have_enter_above_exit(self):
        h = _Harness()
        with self.assertRaises(ValueError):
            AutoFollow(h.get_sensor, h.set_mode, i_enter_a=0.001, i_exit_a=0.005)

    def test_disabled_by_default(self):
        h = _Harness()
        af = AutoFollow(h.get_sensor, h.set_mode)
        self.assertFalse(af.enabled)
        self.assertFalse(af.active)

    def test_enable_resets_to_discharge(self):
        h = _Harness()
        af = AutoFollow(h.get_sensor, h.set_mode)
        af.set_enabled(True)
        self.assertEqual(h.mode_calls, ["discharge"])
        self.assertTrue(af.enabled)

    def test_disable_does_not_force_a_mode(self):
        h = _Harness()
        af = AutoFollow(h.get_sensor, h.set_mode)
        af.set_enabled(True)        # transient: discharge
        h.mode_calls.clear()
        af.set_enabled(False)
        self.assertEqual(h.mode_calls, [])

    def test_set_thresholds_validates(self):
        h = _Harness()
        af = AutoFollow(h.get_sensor, h.set_mode)
        with self.assertRaises(ValueError):
            af.set_thresholds(0.001, 0.005)
        with self.assertRaises(ValueError):
            af.set_thresholds(0.0, 0.0)
        af.set_thresholds(0.010, 0.005)  # valid
        self.assertEqual(af.get_status()["i_enter_a"], 0.010)


class TestAutoFollowHysteresis(unittest.TestCase):
    """Drive the controller via _tick() to exercise transitions deterministically.

    Leaving switching must be fast: the exit tests below call _tick()
    straight after engaging, so any post-engage hold-off/delay fails them.
    The low-current exit debounce is switched off here (exit_confirm_s=0)
    so these test the raw hysteresis; TestExitConfirm covers the debounce,
    and a reversal exits on the next tick with or without it."""

    def setUp(self):
        self.h = _Harness()
        self.af = AutoFollow(
            self.h.get_sensor, self.h.set_mode,
            i_enter_a=0.005, i_exit_a=0.002,
        )
        self.af.set_exit_confirm(0.0)
        self.af.set_enabled(True)   # initial reset → "discharge"
        self.h.mode_calls.clear()

    def test_low_current_stays_transparent(self):
        self.h.sensor = _sensor(0.001)
        self.af._tick()
        self.assertEqual(self.h.mode_calls, [])
        self.assertFalse(self.af.active)

    def test_rising_above_enter_engages_target(self):
        self.h.sensor = _sensor(0.010)
        self.af._tick()
        self.assertEqual(self.h.mode_calls, ["charge"])
        self.assertTrue(self.af.active)

    def test_in_band_does_not_change_state(self):
        # Enter switching at high current
        self.h.sensor = _sensor(0.010)
        self.af._tick()
        self.h.mode_calls.clear()
        # Drop into hysteresis band (below enter, above exit) — should hold
        self.h.sensor = _sensor(0.003)
        self.af._tick()
        self.assertEqual(self.h.mode_calls, [])
        self.assertTrue(self.af.active)

    def test_falling_below_exit_drops_to_transparent(self):
        self.h.sensor = _sensor(0.010)
        self.af._tick()
        self.h.mode_calls.clear()
        self.h.sensor = _sensor(0.001)
        self.af._tick()
        self.assertEqual(self.h.mode_calls, ["discharge"])
        self.assertFalse(self.af.active)

    def test_negative_current_drops_to_transparent(self):
        # Engage first
        self.h.sensor = _sensor(0.010)
        self.af._tick()
        self.h.mode_calls.clear()
        # Cycler discharging — should drop back even though magnitude
        # is high, because we're direction-aware.
        self.h.sensor = _sensor(-0.050)
        self.af._tick()
        self.assertEqual(self.h.mode_calls, ["discharge"])
        self.assertFalse(self.af.active)

    def test_reengage_after_exit(self):
        # Engage -> exit -> engage again, each on the next tick.
        self.h.sensor = _sensor(0.010)
        self.af._tick()
        self.h.sensor = _sensor(0.001)
        self.af._tick()
        self.h.sensor = _sensor(0.010)
        self.af._tick()
        self.assertEqual(self.h.mode_calls, ["charge", "discharge", "charge"])
        self.assertTrue(self.af.active)
        # ...and the second engagement exits just as fast as the first.
        self.h.sensor = _sensor(-0.050)
        self.af._tick()
        self.assertEqual(self.h.mode_calls[-1], "discharge")
        self.assertFalse(self.af.active)

    def test_negative_current_does_not_engage(self):
        # While transparent, large negative current must NOT trigger switching.
        self.h.sensor = _sensor(-0.050)
        self.af._tick()
        self.assertEqual(self.h.mode_calls, [])
        self.assertFalse(self.af.active)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestExitConfirm(unittest.TestCase):
    """Exit debounce, with SW7's settings (enter 0.100 A, exit 0.075 A).

    At 308 mA from the cycler the Pi reads ~0.187 A; at 307 Hz single
    ticks sag to 0.015-0.075 A and, before the debounce, each one ended
    switching (cell 17, ~85 drop-outs per 30 min charge)."""

    DT = 1 / 15

    def setUp(self):
        self.h = _Harness()
        self.clock = _Clock()
        self.af = AutoFollow(self.h.get_sensor, self.h.set_mode,
                             i_enter_a=0.100, i_exit_a=0.075, clock=self.clock)
        self.af.set_exit_confirm(1.0)
        self.af.set_enabled(True)
        self.h.mode_calls.clear()

    def _run(self, i, seconds):
        self.h.sensor = _sensor(i)
        n = 0
        for _ in range(int(round(seconds / self.DT))):
            self.clock.t += self.DT
            self.af._tick()
            n += 1
            if self.h.mode_calls[-1:] == ["discharge"]:
                return n * self.DT
        return None

    def _engage(self, seconds=5.0):
        self._run(0.187, seconds)
        self.assertEqual(self.h.mode_calls, ["charge"])
        self.h.mode_calls.clear()

    def test_single_tick_sags_do_not_exit(self):
        self._engage()
        for sag in (0.015, 0.039, 0.0, 0.074):
            self._run(sag, self.DT)
            self._run(0.187, 0.5)
        self.assertEqual(self.h.mode_calls, [])
        self.assertTrue(self.af.active)

    def test_half_second_sag_does_not_exit(self):
        self._engage()
        self._run(0.030, 0.5)
        self._run(0.187, 2.0)
        self.assertEqual(self.h.mode_calls, [])

    def test_real_stop_exits_within_the_window(self):
        self._engage()
        took = self._run(0.0, 2.0)
        self.assertIsNotNone(took)
        self.assertLessEqual(took, 1.0)
        self.assertFalse(self.af.active)

    def test_reversal_exits_next_tick(self):
        self._engage()
        took = self._run(-0.089, 1.0)
        self.assertAlmostEqual(took, self.DT)

    def test_stop_right_after_engage_still_exits(self):
        self._run(0.187, 0.2)
        self.h.mode_calls.clear()
        self.assertIsNotNone(self._run(0.0, 2.0))

    def test_zero_confirm_is_single_tick(self):
        self.af.set_exit_confirm(0.0)
        self._engage()
        self.assertAlmostEqual(self._run(0.030, 1.0), self.DT)

    def test_window_restarts_after_reengage(self):
        self._engage()
        self._run(0.0, 2.0)                    # exits
        self.h.mode_calls.clear()
        self._run(0.187, 0.2)                  # re-engages
        self.assertEqual(self.h.mode_calls, ["charge"])
        self._run(0.030, 0.3)                  # short sag: the old low samples are gone
        self.assertTrue(self.af.active)

    def test_status_and_validation(self):
        self.assertEqual(self.af.get_status()["exit_confirm_s"], 1.0)
        with self.assertRaises(ValueError):
            self.af.set_exit_confirm(-1)
        with self.assertRaises(ValueError):
            self.af.set_exit_confirm(11)


class TestAutoFollowTargetMode(unittest.TestCase):

    def setUp(self):
        self.h = _Harness()
        self.af = AutoFollow(
            self.h.get_sensor, self.h.set_mode,
            i_enter_a=0.005, i_exit_a=0.002,
        )

    def test_invalid_target_modes_ignored(self):
        self.af.set_target_mode("idle")
        self.af.set_target_mode("garbage")
        self.assertEqual(self.af.get_status()["target_mode"], "charge")  # default

    def test_target_change_while_idle_does_not_apply(self):
        self.af.set_enabled(True)
        self.h.mode_calls.clear()
        self.af.set_target_mode("pulse_charge")
        self.assertEqual(self.h.mode_calls, [])
        self.assertEqual(self.af.get_status()["target_mode"], "pulse_charge")

    def test_target_change_while_active_applies_immediately(self):
        self.af.set_enabled(True)
        self.h.sensor = _sensor(0.010)
        self.af._tick()  # engages "charge"
        self.h.mode_calls.clear()
        self.af.set_target_mode("pulse_charge")
        self.assertEqual(self.h.mode_calls, ["pulse_charge"])

    def test_target_change_to_same_mode_is_noop(self):
        self.af.set_enabled(True)
        self.h.sensor = _sensor(0.010)
        self.af._tick()
        self.h.mode_calls.clear()
        self.af.set_target_mode("charge")  # same as current
        self.assertEqual(self.h.mode_calls, [])


class TestAutoFollowDisabled(unittest.TestCase):

    def test_high_current_does_nothing_when_disabled(self):
        h = _Harness()
        af = AutoFollow(h.get_sensor, h.set_mode,
                        i_enter_a=0.005, i_exit_a=0.002)
        h.sensor = _sensor(0.100)
        af._tick()
        self.assertEqual(h.mode_calls, [])

    def test_status_still_reports_avg_current(self):
        h = _Harness()
        af = AutoFollow(h.get_sensor, h.set_mode)
        h.sensor = _sensor(0.050)
        af._tick()
        st = af.get_status()
        self.assertAlmostEqual(st["avg_current_a"], 0.050, places=5)
        self.assertFalse(st["enabled"])


if __name__ == "__main__":
    unittest.main()
