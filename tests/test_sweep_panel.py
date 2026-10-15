"""Tests for the TUI frequency-sweep panel's entry parsing and rendering."""

import unittest

from rich.text import Text

from tui.widgets.sweep_panel import (
    SweepPanel, format_entry, parse_entry, parse_entry_list, render_entry_list,
)


class TestParseEntry(unittest.TestCase):

    def test_shorthands(self):
        self.assertEqual(parse_entry("300"), {"hz": 300.0})
        self.assertEqual(parse_entry("500p"), {"hz": 500.0, "mode": "pulse_charge"})
        self.assertEqual(parse_entry("70c"), {"hz": 70.0, "mode": "charge"})
        self.assertEqual(parse_entry(" 1000Hz "), {"hz": 1000.0})
        self.assertEqual(parse_entry("pass"), {"mode": "passthrough"})

    def test_rejects(self):
        for bad in ("", "abc", "5000", "0", "p"):
            with self.assertRaises(ValueError, msg=bad):
                parse_entry(bad)

    def test_list(self):
        self.assertEqual([e.get("hz") for e in parse_entry_list("10, 20 50,,100")],
                         [10.0, 20.0, 50.0, 100.0])
        with self.assertRaises(ValueError):
            parse_entry_list(" , ")

    def test_format_round_trip(self):
        for s, shown in (("300", "300 Hz"), ("500p", "500 Hz pulse"), ("pass", "pass")):
            self.assertEqual(format_entry(parse_entry(s)), shown)


class TestRender(unittest.TestCase):

    STATUS = {
        "enabled": True, "end": "loop", "cycle": 3, "default_mode": "charge",
        "entries": [{"hz": 10.0}, {"hz": 20.0}, {"mode": "passthrough"}],
        "current_index": 1, "next_index": 2, "discharge_seen": False,
        "last_applied": {"cycle": 3, "index": 1, "mode": "charge", "hz": 20.0},
        "discharge_a": 0.01, "discharge_confirm_s": 10.0,
    }

    def test_entry_markers(self):
        t = Text()
        render_entry_list(t, self.STATUS)
        lines = t.plain.splitlines()
        self.assertIn("▶", lines[1])   # running: entry 2
        self.assertIn("→", lines[2])   # next: entry 3
        self.assertIn("pass", lines[2])

    def test_panel_text(self):
        panel = SweepPanel()
        panel.set_reactive(SweepPanel.freq_sweep, self.STATUS)
        panel.set_reactive(SweepPanel.auto_follow_enabled, False)
        text = panel._render_impl().plain
        self.assertIn("Cycle:  3", text)
        self.assertIn("#2 20 Hz", text)
        self.assertIn("Auto-follow is OFF", text)

    def test_panel_without_server_support(self):
        panel = SweepPanel()
        self.assertIn("no sweep support", panel._render_impl().plain)


if __name__ == "__main__":
    unittest.main()
