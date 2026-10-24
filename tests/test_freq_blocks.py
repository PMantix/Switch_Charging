"""Frequency blocks within one charge (frequency response map): a sweep entry with
"blocks": [[hz, seconds], ...] makes auto-follow step the switching frequency at each
block boundary; hz 0 = pass-through. Fake clock, SW7-style thresholds, 15 Hz ticks."""

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from server.auto_follow import AutoFollow
from server.freq_sweep import normalize_blocks, normalize_entry
from tests.test_freq_sweep import _make_sweep, _sensor

DT = 1 / 15


class _Rig:
    def __init__(self, tmp, entries):
        self.sweep, self.set_freq, self.clock = _make_sweep(tmp, entries=entries)
        self.sensor = _sensor(0.0)
        self.modes = []
        self.af = AutoFollow(lambda: self.sensor, MagicMock(side_effect=self.modes.append),
                             i_enter_a=0.100, i_exit_a=0.075, sweep=self.sweep, clock=self.clock)
        self.af.set_exit_confirm(1.0)
        self.af.set_enabled(True)
        self.modes.clear()
        self.tmp = tmp

    def run(self, i, seconds):
        self.sensor = _sensor(i)
        for _ in range(int(round(seconds / DT))):
            self.clock.t += DT
            self.af._tick()

    def freqs(self):
        return [c.args[0] for c in self.set_freq.call_args_list]

    def log(self):
        with open(Path(self.tmp) / "sweep.csv", newline="") as f:
            return list(csv.DictReader(f))


class TestValidation(unittest.TestCase):

    def test_normalize(self):
        self.assertEqual(normalize_blocks([[0, 60], [307, 60.0], {"hz": 23, "s": 30}]),
                         [[0.0, 60.0], [307.0, 60.0], [23.0, 30.0]])
        e = normalize_entry({"blocks": [[0, 60], [107, 60]]})
        self.assertEqual(e, {"blocks": [[0.0, 60.0], [107.0, 60.0]]})      # no hz needed
        from server.config import FREQ_BLOCK_MAX
        normalize_blocks([[107, 15]] * FREQ_BLOCK_MAX)                         # the limit itself is accepted
        for bad in ([], [[107, 2]], [[107, 700]], [[0.001, 60]], [[107]], "x", [[107, 60]] * (FREQ_BLOCK_MAX + 1)):
            with self.assertRaises(ValueError):
                normalize_blocks(bad)
        with self.assertRaises(ValueError):
            normalize_entry({"blocks": [[107, 60]], "pause_every_s": 15, "pause_s": 0.5})
        with self.assertRaises(ValueError):
            normalize_entry({"mode": "passthrough", "blocks": [[107, 60]]})

    def test_survives_json_state(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Rig(d, [{"blocks": [[0, 60], [307, 60]]}])
            state = json.loads(json.dumps(r.sweep.to_state()))
            other, _, _ = _make_sweep(d, entries=[10])
            other.restore(state)
            self.assertEqual(other.get_status()["entries"][0], {"blocks": [[0.0, 60.0], [307.0, 60.0]]})


class TestBlocks(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_switched_blocks_change_frequency_live(self):
        r = _Rig(self._tmp.name, [{"blocks": [[23, 10], [307, 10], [107, 10]]}])
        r.run(0.187, 1)
        self.assertEqual(r.modes, ["charge"])                 # engaged once, switching from the start
        self.assertEqual(r.freqs(), [23])
        r.run(0.187, 10)
        r.run(0.187, 10)
        self.assertEqual(r.freqs(), [23, 307, 107])
        self.assertEqual(r.modes, ["charge"])                 # frequency changes never leave switching
        st = r.af.get_status()
        self.assertEqual((st["block_index"], st["block_hz"], st["blocks_this_charge"]), (2, 107.0, 3))

    def test_pass_through_blocks(self):
        r = _Rig(self._tmp.name, [{"blocks": [[0, 10], [307, 10], [0, 10], [23, 10]]}])
        r.run(0.187, 1)
        self.assertEqual(r.modes, ["discharge"])              # block 0 = pass-through: stays all-on
        self.assertTrue(r.af.get_status()["active"])          # but engaged (blocks running)
        r.run(0.187, 10)
        self.assertEqual(r.modes, ["discharge", "charge"])
        self.assertEqual(r.freqs(), [307])                    # frequency set before switching resumes
        r.run(0.187, 10)
        self.assertEqual(r.modes, ["discharge", "charge", "discharge"])
        r.run(0.187, 10)
        self.assertEqual(r.modes, ["discharge", "charge", "discharge", "charge"])
        self.assertEqual(r.freqs(), [307, 23])

    def test_wraps_and_logs(self):
        r = _Rig(self._tmp.name, [{"blocks": [[23, 10], [0, 10]]}])
        r.run(0.187, 45)                                     # 23, pass, 23, pass, 23
        self.assertEqual(r.af.get_status()["blocks_this_charge"], 5)
        ev = [(x["event"], x["frequency_hz"], x["mode"]) for x in r.log()]
        self.assertEqual(ev[0][0], "cycle_start")
        self.assertEqual([e for e in ev if e[0].startswith("block")],
                         [("block:0", "23", "charge"), ("block:1", "0", "passthrough"), ("block:0", "23", "charge"),
                          ("block:1", "0", "passthrough"), ("block:0", "23", "charge")])

    def test_stop_during_pass_block_exits_and_restart_resets(self):
        r = _Rig(self._tmp.name, [{"blocks": [[307, 10], [0, 10], [23, 10]]}])
        r.run(0.187, 15)                                     # into the pass-through block
        r.run(0.0, 3)                                        # charge ends during it
        st = r.af.get_status()
        self.assertFalse(st["active"])
        self.assertFalse(st["blocks_active"])
        r.modes.clear()
        r.set_freq.reset_mock()
        r.run(0.187, 2)                                      # re-engage (same cycle): list restarts at block 0
        self.assertEqual(r.modes, ["charge"])
        self.assertEqual(r.freqs(), [307])
        self.assertEqual(r.af.get_status()["block_index"], 0)

    def test_reversal_in_switched_block_exits_next_tick(self):
        r = _Rig(self._tmp.name, [{"blocks": [[307, 10], [23, 10]]}])
        r.run(0.187, 5)
        r.modes.clear()
        r.run(-0.089, DT)
        self.assertEqual(r.modes, ["discharge"])
        self.assertFalse(r.af.get_status()["active"])

    def test_plain_entries_unchanged(self):
        r = _Rig(self._tmp.name, [107])
        r.run(0.187, 30)
        self.assertEqual(r.modes, ["charge"])
        self.assertEqual(r.freqs(), [107])
        st = r.af.get_status()
        self.assertFalse(st["blocks_active"])
        self.assertIsNone(st["block_index"])


if __name__ == "__main__":
    unittest.main()


class TestMap3Entry(unittest.TestCase):
    """Frequency map 3: 112 blocks of 15 s in one 28 min charge, no pass-through, every block stepped on time."""

    def test_112_short_blocks_step_through_in_order(self):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import freqmap_sweep as T
        settings = [1, 1.5, 2, 3, 5, 7, 10, 23, 200, 1000]
        entries, orders, n = T.build(1, 15.0, [31], settings, 0.70 * 205.0, 308.0, full_rounds=True, first_min_hz=10)
        self.assertEqual(n, 112)
        with tempfile.TemporaryDirectory() as d:
            r = _Rig(d, entries)
            r.run(0.2, n * 15.0 + 1.0)                                         # one engaged charge
            ran = r.freqs()
            self.assertEqual(ran[:n], [float(h) for h in orders[0]])           # every block, in plan order
            blocks = [x for x in r.log() if x["event"].startswith("block:")]
            self.assertEqual([int(x["event"].split(":")[1]) for x in blocks][:n], list(range(n)))


class TestFastStream(unittest.TestCase):
    """Fast sensor stream inside frequency blocks: up after the start block, down before the block list's first
    pass ends, down before an exit's mode change, never with pass-through blocks, down on stale data."""

    def _rig(self, d, entries, base=15.0):
        r = _Rig(d, entries)
        r.rates = []
        r.rate = [base]
        def set_rate(hz):
            r.rates.append((round(r.clock.t, 2), hz))
            r.rate[0] = hz
        r.af._set_stream_rate = set_rate
        r.af._get_stream_rate = lambda: r.rate[0]
        r.af._set_mode = MagicMock(side_effect=lambda m: r.modes.append((round(r.clock.t, 2), m, r.rate[0])))
        r.af.set_fast_stream(300, start_block=1, stop_before_end_s=30)
        return r

    def test_window_and_mode_changes_at_base_rate(self):
        blocks = [[23.0, 15.0], [10.0, 15.0], [200.0, 15.0], [1.0, 15.0], [5.0, 15.0], [1000.0, 15.0]]   # 90 s
        with tempfile.TemporaryDirectory() as d:
            r = self._rig(d, [{"blocks": blocks}])
            t0 = r.clock.t
            r.run(0.2, 95.0)                                   # engaged through the block list
            r.run(0.0, 3.0)                                    # charge ends: exit
            self.assertEqual([hz for _, hz in r.rates], [300, 15.0])
            t_up, t_down = r.rates[0][0] - t0, r.rates[1][0] - t0
            self.assertAlmostEqual(t_up, 15.0, delta=0.2)      # at the start of block 1
            self.assertAlmostEqual(t_down, 60.0, delta=0.2)    # 90 s list - 30 s margin
            self.assertTrue(all(rate == 15.0 for _, m, rate in r.modes))   # every mode change at the base rate
            ev = [x["event"] for x in r.log()]
            self.assertIn("fast_stream:300", ev)
            self.assertIn("fast_stream:off", ev)
            self.assertFalse(r.af.get_status()["fast_stream_on"])

    def test_early_exit_drops_rate_before_mode_change(self):
        blocks = [[23.0, 15.0]] * 8                           # 120 s
        with tempfile.TemporaryDirectory() as d:
            r = self._rig(d, [{"blocks": blocks}])
            r.run(0.2, 40.0)                                   # fast window open
            self.assertTrue(r.af.get_status()["fast_stream_on"])
            r.run(0.0, 3.0)                                    # unexpected end of charge
            exit_modes = [x for x in r.modes if x[1] == "discharge"]
            self.assertTrue(exit_modes and exit_modes[-1][2] == 15.0)
            self.assertEqual(r.rates[-1][1], 15.0)

    def test_never_fast_with_pass_blocks(self):
        blocks = [[23.0, 15.0], [0.0, 15.0], [10.0, 15.0], [200.0, 15.0]]
        with tempfile.TemporaryDirectory() as d:
            r = self._rig(d, [{"blocks": blocks}])
            r.run(0.2, 55.0)
            self.assertEqual(r.rates, [])

    def test_stale_data_drops_rate(self):
        blocks = [[23.0, 15.0]] * 8
        with tempfile.TemporaryDirectory() as d:
            r = self._rig(d, [{"blocks": blocks}])
            r.run(0.2, 30.0)
            self.assertTrue(r.af.get_status()["fast_stream_on"])
            r.af._is_data_fresh = lambda: False
            r.run(0.2, 0.5)
            self.assertFalse(r.af.get_status()["fast_stream_on"])
            self.assertEqual(r.rates[-1][1], 15.0)

    def test_persist_rate_is_base_while_fast(self):
        blocks = [[23.0, 15.0]] * 8
        with tempfile.TemporaryDirectory() as d:
            r = self._rig(d, [{"blocks": blocks}])
            r.run(0.2, 30.0)
            self.assertEqual(r.af.persist_stream_rate(r.rate[0]), 15.0)

    def test_validation(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Rig(d, [{"blocks": [[23.0, 15.0]]}])
            for bad in ((5, 1, 90), (400, 1, 90), (300, 0, 90), (300, 1, 5)):
                with self.assertRaises(ValueError):
                    r.af.set_fast_stream(*bad)
            r.af.set_fast_stream(0)                            # off is fine
