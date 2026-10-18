"""Measurement mode: held configurations, make-before-break changes, precision profile, per-frame log."""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

from server.config import STATE_DEFS
from server.measure import CONFIGS, MeasureSession, config_for_fets, normalize_config
from server.mode_controller import Mode, ModeController


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _gpio():
    gpio = MagicMock()
    gpio.live_commands_supported = True
    gpio.get_sensor_data.return_value = {}
    gpio.sensor_data_fresh.return_value = False
    gpio.get_sensor_profile.return_value = {"avg": 4, "bus_every": 3, "max_hz": 350.0}
    gpio.get_sensor_rate.return_value = 15.0
    gpio.set_ina226_avg.side_effect = lambda a: (a, 23.0)
    gpio.set_bus_every.side_effect = lambda b: (b, 23.0)
    gpio.get_calibration.return_value = {0: (0.61, 0.001), 1: (0.6, 0.0), 2: (0.57, -0.002), 3: (0.58, 0.0)}
    state = {"fets": [False] * 4}

    def apply_state(t):
        state["fets"] = [bool(x) for x in t]
    gpio.apply_state.side_effect = apply_state
    gpio.all_on.side_effect = lambda: apply_state(STATE_DEFS[4])
    gpio.all_off.side_effect = lambda: apply_state(STATE_DEFS[5])
    gpio.get_fet_states.side_effect = lambda: list(state["fets"])
    return gpio


def _frame(i_p1=0.0, i_p2=0.0, i_n1=0.0, i_n2=0.0, v=3.8):
    return {"P1": {"voltage": v, "current": i_p1}, "P2": {"voltage": v, "current": i_p2},
            "N1": {"voltage": 0.01, "current": i_n1}, "N2": {"voltage": 0.01, "current": i_n2}}


class TestConfigs(unittest.TestCase):

    def test_names_map_to_board_states(self):
        self.assertEqual(CONFIGS["pair2"], tuple(STATE_DEFS[0]))   # +A/-A = P1 N1
        self.assertEqual(CONFIGS["pair1"], tuple(STATE_DEFS[1]))   # +A/-B = P1 N2
        self.assertEqual(CONFIGS["pair3"], tuple(STATE_DEFS[2]))   # +B/-A = P2 N1
        self.assertEqual(CONFIGS["pair4"], tuple(STATE_DEFS[3]))   # +B/-B = P2 N2
        self.assertEqual(normalize_config("STATE0"), "pair2")
        self.assertEqual(config_for_fets([True] * 4), "pass")
        with self.assertRaises(ValueError):
            normalize_config("pair5")


class TestMeasureMode(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.gpio = _gpio()
        self.engine = MagicMock()
        self.mc = ModeController(self.gpio, self.engine)
        self.addCleanup(self.mc._auto_follow.stop)
        self.addCleanup(self.mc._schedule_monitor.stop_thread)
        self.clock = _Clock()
        self.mc._measure = MeasureSession(self.gpio, log_dir=Path(self._tmp.name), clock=self.clock)
        self.mc.set_mode("discharge")
        self.gpio.apply_state.reset_mock()
        self.gpio.all_off.reset_mock()
        self.engine.reset_mock()

    def _enter(self):
        self.mc.set_mode("measure")
        self.addCleanup(lambda: self.mc.get_mode() == Mode.MEASURE and self.mc.set_mode("discharge"))

    def test_enter_from_pass_through_is_seamless(self):
        self._enter()
        self.gpio.all_off.assert_not_called()
        self.engine.pause.assert_not_called()
        st = self.mc.get_measure_status()
        self.assertTrue(st["active"])
        self.assertEqual(st["config"], "pass")
        self.gpio.set_ina226_avg.assert_called_with(64)
        self.gpio.set_bus_every.assert_called_with(1)
        self.gpio.set_sensor_rate.assert_called_with(20.0)

    def test_enter_from_switching_stops_engine(self):
        self.mc.set_mode("charge")
        self.engine.reset_mock()
        self._enter()
        self.engine.pause.assert_called_once()
        self.assertEqual(self.mc.get_measure_status()["config"], "open")

    def test_make_before_break(self):
        self._enter()
        self.gpio.apply_state.reset_mock()
        self.mc.measure_set("pair2")                       # subset of pass: direct
        self.assertEqual(self.gpio.apply_state.call_args_list, [call(CONFIGS["pair2"])])
        self.gpio.apply_state.reset_mock()
        self.mc.measure_set("pair1")                       # P1 N1 -> P1 N2 via P1 N1 N2
        self.assertEqual(self.gpio.apply_state.call_args_list,
                         [call((True, False, True, True)), call(CONFIGS["pair1"])])
        self.mc.measure_set("open")
        self.gpio.apply_state.reset_mock()
        self.mc.measure_set("pair3")                       # superset of open: direct
        self.assertEqual(self.gpio.apply_state.call_args_list, [call(CONFIGS["pair3"])])
        self.assertEqual(self.gpio.get_fet_states(), list(CONFIGS["pair3"]))

    def test_stats_skip_settle_and_reset_on_change(self):
        self._enter()
        self.mc.measure_set("pair2")
        t0 = self.clock.t
        for k in range(20):                                # 1 s of frames at 20 Hz
            self.mc.on_sensor_frame(_frame(i_p1=0.0 if k < 5 else 0.185, i_n1=0.17), t0 + k * 0.05, k, k)
        self.clock.t = t0 + 1.0
        st = self.mc.measure_stats(settle_s=0.25)
        self.assertEqual(st["channels"]["P1_i"]["n"], 15)
        self.assertAlmostEqual(st["channels"]["P1_i"]["mean"], 0.185)
        self.assertAlmostEqual(st["channels"]["P1_i"]["sd"], 0.0)
        self.mc.measure_set("pair4")
        self.assertEqual(self.mc.measure_stats()["channels"]["P1_i"]["n"], 0)

    def test_log_has_calibration_frames_and_events(self):
        self._enter()
        self.mc.measure_set("pair2")
        self.mc.measure_mark("ladder 1")
        self.mc.on_sensor_frame(_frame(i_p1=0.1), self.clock.t + 0.01, 7, 1)
        path = self.mc.get_measure_status()["log"]
        self.mc.set_mode("discharge")
        text = Path(path).read_text().splitlines()
        self.assertTrue(any(l.startswith("# calibration=P1:0.610000:0.001000") for l in text))
        rows = list(csv.DictReader(l for l in text if not l.startswith("#")))
        self.assertEqual([r["kind"] for r in rows], ["config", "config", "mark", "frame", "stop"])
        self.assertEqual(rows[3]["config"], "pair2")
        self.assertAlmostEqual(float(rows[3]["P1_i"]), 0.1)

    def test_leave_restores_profile_and_pass_through(self):
        self._enter()
        self.mc.measure_set("pair4")
        self.gpio.set_ina226_avg.reset_mock()
        self.mc.set_mode("discharge")
        self.assertEqual(self.gpio.get_fet_states(), [True] * 4)
        self.gpio.set_ina226_avg.assert_called_with(4)
        self.gpio.set_bus_every.assert_called_with(3)
        self.gpio.set_sensor_rate.assert_called_with(15.0)
        self.assertFalse(self.mc.get_measure_status()["active"])

    def test_not_persisted_and_guards(self):
        with self.assertRaises(ValueError):
            self.mc.measure_set("pair2")
        self._enter()
        self.assertEqual(self.mc.mode_to_persist(), Mode.DISCHARGE)
        with self.assertRaises(ValueError):
            self.mc.set_measure_profile(avg=16)

    def test_disables_auto_follow(self):
        self.mc.set_auto_follow_enabled(True)
        self._enter()
        self.assertFalse(self.mc.get_auto_follow_status()["enabled"])
        self.assertEqual(self.mc.get_mode(), Mode.MEASURE)

    def test_profile_override_and_reconnect(self):
        self.mc.set_measure_profile(avg=256, sensor_hz=5)
        self._enter()
        self.gpio.set_ina226_avg.assert_called_with(256)
        self.gpio.set_sensor_rate.assert_called_with(5.0)
        self.mc.measure_set("pair3")
        self.gpio.apply_state.reset_mock()
        self.mc._reapply_mode()
        self.gpio.apply_state.assert_called_once_with(CONFIGS["pair3"])


if __name__ == "__main__":
    unittest.main()
