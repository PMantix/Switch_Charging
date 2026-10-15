"""Tests for the per-cycle frequency sweep and its AutoFollow hook."""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from server.auto_follow import AutoFollow
from server.freq_sweep import FreqSweep, normalize_entry


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _make_sweep(tmpdir, entries=(10, 70, 500), end="loop", confirm_s=10.0):
    clock = _Clock()
    set_freq = MagicMock()
    sw = FreqSweep(set_freq, log_file=Path(tmpdir) / "sweep.csv", clock=clock)
    sw.configure(entries=list(entries), end=end, discharge_confirm_s=confirm_s)
    sw.set_enabled(True)
    return sw, set_freq, clock


def _discharge(sw, clock, seconds=15.0, i=-0.135):
    """Hold a discharge current long enough to be confirmed."""
    sw.observe(i)
    clock.t += seconds
    sw.observe(i)


class TestNormalizeEntry(unittest.TestCase):

    def test_number_shorthand(self):
        self.assertEqual(normalize_entry(70), {"hz": 70.0})

    def test_mode_and_hz(self):
        self.assertEqual(normalize_entry({"hz": 500, "mode": "pulse_charge"}),
                         {"hz": 500.0, "mode": "pulse_charge"})

    def test_passthrough_needs_no_hz(self):
        self.assertEqual(normalize_entry({"mode": "passthrough"}), {"mode": "passthrough"})

    def test_rejects_bad_input(self):
        for bad in ({"hz": 0}, {"hz": 5000}, {"mode": "discharge", "hz": 10},
                    {"mode": "charge"}, {}, "fast", True, {"hz": "x"}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                normalize_entry(bad)


class TestSweepStepping(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_disabled_or_empty_returns_none(self):
        sw = FreqSweep(MagicMock(), log_file=Path(self.tmp) / "x.csv")
        self.assertIsNone(sw.on_charge_onset())
        sw.set_enabled(True)          # enabled but no entries
        self.assertIsNone(sw.on_charge_onset())
        self.assertFalse(sw.enabled)

    def test_first_onset_applies_entry_zero(self):
        sw, set_freq, _ = _make_sweep(self.tmp)
        self.assertEqual(sw.on_charge_onset(), "charge")
        set_freq.assert_called_once_with(10.0)
        st = sw.get_status()
        self.assertEqual((st["cycle"], st["current_index"], st["next_index"]), (1, 0, 1))

    def test_reengage_without_discharge_repeats_entry(self):
        sw, set_freq, _ = _make_sweep(self.tmp)
        sw.on_charge_onset()
        sw.on_charge_onset()          # threshold chatter in the same charge
        self.assertEqual([c.args[0] for c in set_freq.call_args_list], [10.0, 10.0])
        self.assertEqual(sw.get_status()["cycle"], 1)

    def test_confirmed_discharge_advances(self):
        sw, set_freq, clock = _make_sweep(self.tmp)
        sw.on_charge_onset()
        _discharge(sw, clock)
        sw.on_charge_onset()
        self.assertEqual(set_freq.call_args.args[0], 70.0)
        self.assertEqual(sw.get_status()["cycle"], 2)

    def test_brief_negative_blip_does_not_advance(self):
        sw, set_freq, clock = _make_sweep(self.tmp)
        sw.on_charge_onset()
        sw.observe(-0.2)
        clock.t += 3.0                # shorter than discharge_confirm_s
        sw.observe(-0.2)
        sw.observe(0.0)               # back to rest — timer resets
        clock.t += 20.0
        sw.observe(-0.2)
        sw.on_charge_onset()
        self.assertEqual(set_freq.call_args.args[0], 10.0)
        self.assertEqual(sw.get_status()["cycle"], 1)

    def test_small_negative_current_is_not_a_discharge(self):
        sw, set_freq, clock = _make_sweep(self.tmp)
        sw.on_charge_onset()
        _discharge(sw, clock, i=-0.005)   # below discharge_a (10 mA)
        sw.on_charge_onset()
        self.assertEqual(set_freq.call_args.args[0], 10.0)

    def _run_cycles(self, sw, clock, n):
        modes = []
        for _ in range(n):
            modes.append((sw.on_charge_onset(), sw.get_status()["last_applied"]["hz"]))
            _discharge(sw, clock)
        return modes

    def test_loop_wraps_to_start(self):
        sw, _, clock = _make_sweep(self.tmp, entries=(10, 70), end="loop")
        hz = [m[1] for m in self._run_cycles(sw, clock, 5)]
        self.assertEqual(hz, [10.0, 70.0, 10.0, 70.0, 10.0])

    def test_hold_stays_on_last(self):
        sw, _, clock = _make_sweep(self.tmp, entries=(10, 70), end="hold")
        hz = [m[1] for m in self._run_cycles(sw, clock, 4)]
        self.assertEqual(hz, [10.0, 70.0, 70.0, 70.0])

    def test_switching_end_from_hold_to_loop_mid_run(self):
        sw, _, clock = _make_sweep(self.tmp, entries=(10, 70), end="hold")
        self._run_cycles(sw, clock, 3)          # 10, 70, 70 (holding)
        sw.set_end("loop")
        # Holding left next on the last entry; loop carries on from there.
        hz = [m[1] for m in self._run_cycles(sw, clock, 2)]
        self.assertEqual(hz, [70.0, 10.0])

    def test_per_entry_mode_and_default_mode(self):
        sw, set_freq, clock = _make_sweep(
            self.tmp, entries=({"hz": 10}, {"hz": 500, "mode": "pulse_charge"},
                               {"mode": "passthrough"}))
        sw.configure(default_mode="charge")
        modes = [m[0] for m in self._run_cycles(sw, clock, 3)]
        self.assertEqual(modes, ["charge", "pulse_charge", "passthrough"])
        # The passthrough control cycle must not touch the frequency.
        self.assertEqual([c.args[0] for c in set_freq.call_args_list], [10.0, 500.0])

    def test_reset_returns_to_entry_zero(self):
        sw, _, clock = _make_sweep(self.tmp)
        self._run_cycles(sw, clock, 2)
        sw.reset()
        sw.on_charge_onset()
        st = sw.get_status()
        self.assertEqual((st["cycle"], st["current_index"]), (1, 0))

    def test_set_next_index(self):
        sw, set_freq, _ = _make_sweep(self.tmp)
        sw.set_next_index(2)
        sw.on_charge_onset()
        set_freq.assert_called_once_with(500.0)
        with self.assertRaises(ValueError):
            sw.set_next_index(3)


class TestSweepEditing(unittest.TestCase):
    """Edits take effect at the next onset and keep pointing at the same
    logical entries."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sw, self.set_freq, self.clock = _make_sweep(self._tmp.name, entries=(10, 70, 500))
        self.sw.on_charge_onset()          # running entry 0 (10 Hz), next = 1

    def tearDown(self):
        self._tmp.cleanup()

    def _next_hz(self):
        _discharge(self.sw, self.clock)
        self.sw.on_charge_onset()
        return self.set_freq.call_args.args[0]

    def test_append(self):
        self.sw.add_entry(1000)
        self.assertEqual([e["hz"] for e in self.sw.get_status()["entries"]],
                         [10.0, 70.0, 500.0, 1000.0])
        self.assertEqual(self._next_hz(), 70.0)

    def test_insert_before_next_keeps_next(self):
        self.sw.add_entry(25, position=1)
        self.assertEqual(self._next_hz(), 70.0)

    def test_insert_at_front_keeps_current(self):
        self.sw.add_entry(5, position=0)
        st = self.sw.get_status()
        self.assertEqual(st["entries"][st["current_index"]]["hz"], 10.0)
        self.assertEqual(self._next_hz(), 70.0)

    def test_remove_next_moves_to_following(self):
        self.sw.remove_entry(1)            # drop 70 Hz
        self.assertEqual(self._next_hz(), 500.0)

    def test_remove_last_while_next_wraps(self):
        self.sw.set_next_index(2)
        self.sw.remove_entry(2)            # drop 500 Hz, list is now [10, 70]
        self.assertEqual(self.sw.get_status()["next_index"], 0)
        self.assertEqual(self._next_hz(), 10.0)

    def test_remove_running_entry(self):
        self.sw.remove_entry(0)
        st = self.sw.get_status()
        self.assertIsNone(st["current_index"])
        self.assertEqual(st["next_index"], 0)
        self.assertEqual(self._next_hz(), 70.0)

    def test_replace(self):
        self.sw.replace_entry(1, {"hz": 130, "mode": "pulse_charge"})
        self.assertEqual(self._next_hz(), 130.0)
        self.assertEqual(self.sw.get_status()["last_applied"]["mode"], "pulse_charge")

    def test_bad_index_rejected(self):
        with self.assertRaises(ValueError):
            self.sw.remove_entry(3)
        with self.assertRaises(ValueError):
            self.sw.add_entry(10, position=9)

    def test_remove_all_disables_sweep(self):
        for _ in range(3):
            self.sw.remove_entry(0)
        self.assertFalse(self.sw.enabled)
        self.assertIsNone(self.sw.on_charge_onset())


class TestSweepPersistenceAndLog(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_state_round_trip_resumes_mid_charge(self):
        sw, _, clock = _make_sweep(self.tmp, end="hold")
        sw.on_charge_onset()
        _discharge(sw, clock)
        sw.on_charge_onset()               # cycle 2, entry 1 (70 Hz), mid-charge
        state = sw.to_state()

        set_freq = MagicMock()
        sw2 = FreqSweep(set_freq, log_file=Path(self.tmp) / "b.csv", clock=_Clock())
        sw2.restore(state)
        st = sw2.get_status()
        self.assertTrue(st["enabled"])
        self.assertEqual((st["cycle"], st["current_index"], st["next_index"], st["end"]),
                         (2, 1, 2, "hold"))
        # Re-engaging after a restart mid-charge repeats the running entry.
        sw2.on_charge_onset()
        set_freq.assert_called_once_with(70.0)
        self.assertEqual(sw2.get_status()["cycle"], 2)

    def test_restore_drops_invalid_entries(self):
        sw = FreqSweep(MagicMock(), log_file=Path(self.tmp) / "c.csv")
        sw.restore({"entries": [{"hz": 10}, {"hz": -1}], "next_index": 5})
        st = sw.get_status()
        self.assertEqual(st["entries"], [{"hz": 10.0}])
        self.assertEqual(st["next_index"], 0)

    def test_on_change_called(self):
        sw, _, _ = _make_sweep(self.tmp)
        cb = MagicMock()
        sw.on_change = cb
        sw.on_charge_onset()
        sw.add_entry(1000)
        self.assertEqual(cb.call_count, 2)

    def test_log_rows(self):
        sw, _, clock = _make_sweep(self.tmp, entries=(10, {"mode": "passthrough"}))
        sw.on_charge_onset(0.54, 3.9)
        sw.on_charge_onset(0.54, 3.9)      # re-engage
        _discharge(sw, clock)
        sw.on_charge_onset(0.54, 3.7)
        with open(Path(self.tmp) / "sweep.csv", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(r["cycle"], r["entry_index"], r["mode"], r["frequency_hz"], r["event"])
                          for r in rows],
                         [("1", "0", "charge", "10", "cycle_start"),
                          ("1", "0", "charge", "10", "reengage"),
                          ("2", "1", "passthrough", "", "cycle_start")])


def _sensor(i_a: float, v: float = 3.7):
    return {
        "P1": {"voltage": v, "current": i_a},
        "P2": {"voltage": 0.0, "current": 0.0},
        "N1": {"voltage": v, "current": i_a},
        "N2": {"voltage": 0.0, "current": 0.0},
    }


class TestAutoFollowWithSweep(unittest.TestCase):
    """2C charge / 0.5C discharge through AutoFollow's real tick path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sweep, self.set_freq, self.clock = _make_sweep(
            self._tmp.name,
            entries=({"hz": 10}, {"hz": 500, "mode": "pulse_charge"}, {"mode": "passthrough"}))
        self.sensor = _sensor(0.0)
        self.modes = []
        self.af = AutoFollow(lambda: self.sensor, MagicMock(side_effect=self.modes.append),
                             i_enter_a=0.065, i_exit_a=0.044, sweep=self.sweep)
        self.af.set_enabled(True)
        self.modes.clear()

    def tearDown(self):
        self._tmp.cleanup()

    def _feed(self, i, ticks=12):
        # The detector averages over a rolling window; feed enough ticks
        # for the average to settle on the new current.
        self.sensor = _sensor(i)
        for _ in range(ticks):
            self.af._tick()

    def _cycle(self):
        self._feed(0.54)                   # 2C CC charge
        self._feed(0.0)                    # rest
        self._feed(-0.135, ticks=4)        # 0.5C CC discharge...
        self.clock.t += 15.0
        self._feed(-0.135, ticks=12)       # ...held long enough to confirm
        self._feed(0.0)

    def test_sweep_drives_modes_and_never_switches_on_discharge(self):
        for _ in range(3):
            self._cycle()
        # Cycle 1 charge @10 Hz, cycle 2 pulse @500 Hz, cycle 3 passthrough
        # (never engages). Every exit is back to discharge (all-on); the
        # passthrough cycle's exit re-asserts it, which ModeController
        # treats as a no-op since it is already in discharge.
        self.assertEqual(self.modes, ["charge", "discharge", "pulse_charge", "discharge",
                                      "discharge"])
        self.assertNotIn("charge", self.modes[4:])
        self.assertEqual([c.args[0] for c in self.set_freq.call_args_list], [10.0, 500.0])
        self.assertEqual(self.sweep.get_status()["cycle"], 3)

    def test_negative_current_never_engages(self):
        self._feed(-0.135, ticks=30)
        self.assertEqual(self.modes, [])
        self.set_freq.assert_not_called()

    def test_target_change_mid_charge_ignored_while_sweeping(self):
        self._feed(0.54)                   # engaged "charge" via sweep entry 0
        self.af.set_target_mode("pulse_charge")
        self.assertEqual(self.modes, ["charge"])

    def test_sweep_disabled_falls_back_to_target_mode(self):
        self.sweep.set_enabled(False)
        self.af.set_target_mode("pulse_charge")
        self._feed(0.54)
        self.assertEqual(self.modes, ["pulse_charge"])
        self.set_freq.assert_not_called()



class TestEngageDelay(unittest.TestCase):
    """Schedule shape on 4-3: 1 s DCIR pulse (step 7), 5 s rest, then a
    30 min CC charge (step 9), all at 1.5C = 312 mA."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sweep, self.set_freq, self.clock = _make_sweep(self._tmp.name)
        self.sensor = _sensor(0.0)
        self.modes = []
        self.af = AutoFollow(lambda: self.sensor, MagicMock(side_effect=self.modes.append),
                             i_enter_a=0.0657, i_exit_a=0.0448, sweep=self.sweep,
                             clock=self.clock)
        self.af.set_engage_delay(3.0)
        self.af.set_enabled(True)
        self.modes.clear()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, i, seconds, dt=1 / 15):
        self.sensor = _sensor(i)
        for _ in range(int(round(seconds / dt))):
            self.clock.t += dt
            self.af._tick()

    def test_short_pulse_stays_passthrough(self):
        self._run(0.312, 1.0)              # step 7
        self._run(0.0, 5.0)                # step 8
        self.assertEqual(self.modes, [])
        self.set_freq.assert_not_called()
        self.assertEqual(self.sweep.get_status()["cycle"], 0)

    def test_long_step_engages_after_delay(self):
        self._run(0.312, 1.0)
        self._run(0.0, 5.0)
        self._run(0.312, 2.5)              # step 9, inside the delay
        self.assertEqual(self.modes, [])
        self.assertIsNotNone(self.af.get_status()["engage_pending_s"])
        self._run(0.312, 1.0)              # past 3 s above i_enter
        self.assertEqual(self.modes, ["charge"])
        self.set_freq.assert_called_once_with(10.0)
        self.assertIsNone(self.af.get_status()["engage_pending_s"])

    def test_exit_is_not_delayed(self):
        self._run(0.312, 4.0)
        self.modes.clear()
        self._run(0.0, 2 / 15)
        self.assertEqual(self.modes, ["discharge"])

    def test_dip_restarts_the_delay(self):
        self._run(0.312, 2.5)
        self._run(0.0, 1.0)                # current falls back below i_enter
        self._run(0.312, 2.5)
        self.assertEqual(self.modes, [])

    def test_zero_delay_engages_immediately(self):
        self.af.set_engage_delay(0.0)
        self._run(0.312, 1.0)
        self.assertEqual(self.modes[:1], ["charge"])

    def test_delay_validation(self):
        with self.assertRaises(ValueError):
            self.af.set_engage_delay(-1)
        with self.assertRaises(ValueError):
            self.af.set_engage_delay(601)


if __name__ == "__main__":
    unittest.main()
