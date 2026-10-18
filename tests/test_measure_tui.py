"""TUI support for measurement mode and pauses: panel rendering, sweep-entry
pause syntax, and an end-to-end run of the real app against an in-process
simulated server (keys n / 1-6 / t / o / Tab)."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from rich.text import Text

from server.measure import MeasureSession
from tui.widgets.measure_panel import KEY_TO_CONFIG, render_measure
from tui.widgets.sweep_panel import format_entry, parse_entry


class TestPanelAndParsing(unittest.TestCase):

    def test_keys(self):
        self.assertEqual([KEY_TO_CONFIG[k] for k in "123456"],
                         ["pair1", "pair2", "pair3", "pair4", "pass", "open"])

    def test_render(self):
        m = {"active": True, "config": "pair3", "since_s": 12.4, "frames": 240, "markers": 2,
             "log": "/home/pi/measure_logs/measure_x.csv", "profile": {"avg": 64, "bus_every": 1, "hz": 20.0},
             "live_settle_s": 1.0,
             "live": {"n": 200, "P2_i": [0.0612, 0.0003], "P2_v": [3.81, 0.001], "N1_i": [0.0598, 0.0002],
                      "N1_v": [0.02, 0.0], "P1_i": [0.0, 0.0], "P1_v": [3.8, 0.0], "N2_i": [None, None],
                      "N2_v": [None, None]}}
        t = Text()
        render_measure(t, m)
        s = t.plain
        self.assertIn("pair3", s)
        self.assertIn("+61.200", s)          # mA
        self.assertIn("measure_x.csv", s)
        self.assertIn("n=200", s)
        t = Text()
        render_measure(t, {"active": False})
        self.assertIn("Not in measurement mode", t.plain)

    def test_sweep_pause_entries(self):
        e = parse_entry("307~15/0.5")
        self.assertEqual(e, {"hz": 307.0, "pause_every_s": 15.0, "pause_s": 0.5})
        self.assertEqual(format_entry(e), "307 Hz · pause 0.5/15 s")
        self.assertEqual(parse_entry("500p~30/1")["mode"], "pulse_charge")
        for bad in ("307~15", "307~a/b", "307~15/20"):
            with self.assertRaises(ValueError):
                parse_entry(bad)


class TestLiveStats(unittest.TestCase):

    def test_live_summary_skips_settle_and_resets(self):
        from unittest.mock import MagicMock
        gpio = MagicMock()
        gpio.get_sensor_profile.return_value = {"avg": 4, "bus_every": 1}
        gpio.get_sensor_rate.return_value = 15.0
        gpio.set_ina226_avg.side_effect = lambda a: (a, 23.0)
        gpio.set_bus_every.side_effect = lambda b: (b, 23.0)
        gpio.get_calibration.return_value = {}
        clock = [100.0]
        with tempfile.TemporaryDirectory() as d:
            s = MeasureSession(gpio, log_dir=Path(d), clock=lambda: clock[0])
            s.start("pass")
            s.set_config("pair2")
            for k in range(40):                      # 2 s at 20 Hz; first 1 s at 0, then 0.1 A
                i = 0.0 if k < 20 else 0.1
                s.on_frame({"P1": {"voltage": 3.8, "current": i}}, 100.0 + k * 0.05, k, k)
            live = s.status()["live"]
            self.assertEqual(live["n"], 20)
            self.assertAlmostEqual(live["P1_i"][0], 0.1)
            s.set_config("pair4")
            self.assertEqual(s.status()["live"]["n"], 0)
            s.stop()


class TestTuiEndToEnd(unittest.TestCase):
    """The real Textual app, driven by key presses, against a real command
    server on the simulated RP2040."""

    PORT = 15571

    @classmethod
    def setUpClass(cls):
        from server import measure as measure_mod
        from server.command_server import CommandServer
        from server.gpio_driver import GPIODriver
        from server.mode_controller import ModeController
        from server.sequence_engine import SequenceEngine
        from server import state_persistence
        cls._tmp = tempfile.TemporaryDirectory()
        cls._state_file = state_persistence.STATE_FILE
        state_persistence.STATE_FILE = Path(cls._tmp.name) / "state.json"   # never touch the real one
        cls.gpio = GPIODriver(sim=True)
        cls.engine = SequenceEngine(cls.gpio)
        cls.mc = ModeController(cls.gpio, cls.engine)
        cls.mc._measure = measure_mod.MeasureSession(cls.gpio, log_dir=Path(cls._tmp.name))
        cls.server = CommandServer(cls.mc, cls.engine, host="127.0.0.1", port=cls.PORT)
        cls.server.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.engine.stop()
        cls.gpio.cleanup()
        from server import state_persistence
        state_persistence.STATE_FILE = cls._state_file
        cls._tmp.cleanup()

    def test_keys_drive_measure_mode_and_pauses(self):
        from tui.app import SwitchingCircuitApp
        from tui.widgets.measure_panel import MeasurePanel

        async def wait_for(pilot, cond, timeout=5.0):
            t_end = time.monotonic() + timeout
            while time.monotonic() < t_end:
                await pilot.pause(0.1)
                if cond():
                    return True
            return False

        async def run():
            app = SwitchingCircuitApp(host="127.0.0.1", port=self.PORT)
            async with app.run_test(size=(200, 60)) as pilot:
                self.assertTrue(await wait_for(pilot, lambda: app._client is not None and app._circuit_mode == "idle"))
                await pilot.press("x")
                self.assertTrue(await wait_for(pilot, lambda: app._circuit_mode == "discharge"))
                await pilot.press("n")
                self.assertTrue(await wait_for(pilot, lambda: app._circuit_mode == "measure"))
                panel = app.query_one("#measure-panel", MeasurePanel)
                self.assertTrue(await wait_for(pilot, lambda: not panel.has_class("hidden")))
                self.assertTrue(app.query_one("#right-panel").has_class("hidden"))
                self.assertEqual(self.mc.get_measure_status()["config"], "pass")

                await pilot.press("3")
                self.assertTrue(await wait_for(pilot, lambda: self.mc.get_measure_status()["config"] == "pair3"))
                self.assertTrue(await wait_for(pilot, lambda: (panel.measure or {}).get("config") == "pair3"))
                self.assertTrue(await wait_for(pilot, lambda: ((panel.measure or {}).get("live") or {}).get("n", 0) > 3))
                await pilot.press("t")
                self.assertTrue(await wait_for(pilot, lambda: self.mc.get_measure_status()["markers"] == 1))

                await pilot.press("tab")                     # measure <-> status
                await pilot.pause(0.2)
                self.assertTrue(panel.has_class("hidden"))
                await pilot.press("tab")
                await pilot.pause(0.2)
                self.assertFalse(panel.has_class("hidden"))

                await pilot.press("n")                       # leave -> pass-through
                self.assertTrue(await wait_for(pilot, lambda: app._circuit_mode == "discharge"))
                self.assertTrue(await wait_for(pilot, lambda: panel.has_class("hidden")))
                self.assertFalse(app.query_one("#right-panel").has_class("hidden"))
                self.assertEqual(self.gpio.get_fet_states(), [True] * 4)

                await pilot.press("o")                       # pauses: off -> 0.5 s / 15 s
                self.assertTrue(await wait_for(
                    pilot, lambda: (self.mc.get_auto_follow_status()["pause_every_s"],
                                    self.mc.get_auto_follow_status()["pause_s"]) == (15.0, 0.5)))
                await pilot.press("o")
                self.assertTrue(await wait_for(
                    pilot, lambda: self.mc.get_auto_follow_status()["pause_every_s"] == 30.0))
                for _ in range(3):                           # back round to off
                    await pilot.press("o")
                    await pilot.pause(0.3)
                self.assertTrue(await wait_for(pilot, lambda: not self.mc.get_auto_follow_status()["pause_every_s"]))
                await pilot.press("i")
                await wait_for(pilot, lambda: app._circuit_mode == "idle")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
