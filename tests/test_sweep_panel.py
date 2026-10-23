"""Tests for the TUI frequency-sweep panel's entry parsing and rendering."""

import unittest

from rich.text import Text

from tui.widgets.sweep_panel import (
    SweepPanel, format_entry, parse_entry, parse_entry_list, render_block_status, render_entry_list,
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


class TestFreqMapDetail(unittest.TestCase):
    """Frequency-map entries: block order listed under the entry, running block highlighted,
    live 'Block:' line from the auto-follow status."""

    BLOCKS = [[200.0, 60.0], [307.0, 60.0], [0.0, 60.0], [1000.0, 60.0], [10.0, 60.0],
              [23.0, 60.0], [50.0, 60.0], [0.0, 60.0]]
    FS = {"enabled": True, "end": "hold", "cycle": 1, "default_mode": "charge", "current_index": 0,
          "next_index": 1, "entries": [{"blocks": BLOCKS}, {"mode": "passthrough"}]}
    AF = {"enabled": True, "active": True, "blocks_active": True, "block_index": 3, "block_hz": 1000.0,
          "block_remaining_s": 41.2, "blocks_this_charge": 4}

    def test_block_order_listed(self):
        t = Text()
        render_entry_list(t, self.FS)
        s = t.plain
        self.assertIn("freq map 8 × 60 s (2 P)", s)
        self.assertIn(" 1│  200  307    P 1000   10", s)
        self.assertIn(" 6│   23   50    P", s)

    def test_running_block_highlighted(self):
        t = Text()
        render_entry_list(t, self.FS, self.AF)
        hi = [t.plain[sp.start:sp.end] for sp in t.spans if "on green" in str(sp.style)]
        self.assertEqual(hi, ["1000"])

    def test_block_status_line(self):
        t = Text()
        render_block_status(t, self.AF, self.FS)
        self.assertIn("Block:  4/8 1000 Hz · 41 s left", t.plain)
        self.assertIn("4 blocks this charge", t.plain)
        t = Text()
        render_block_status(t, dict(self.AF, block_hz=0.0, block_index=2), self.FS)
        self.assertIn("3/8 pass-through", t.plain)
        t = Text()
        render_block_status(t, {"blocks_active": False}, self.FS)
        self.assertEqual(t.plain, "")

    def test_panel_shows_live_block(self):
        p = SweepPanel()
        p.freq_sweep = self.FS
        p.auto_follow_enabled = True
        p.auto_follow = self.AF
        s = p._render_impl().plain
        self.assertIn("Block:  4/8 1000 Hz", s)
        self.assertIn(" 1│  200", s)
