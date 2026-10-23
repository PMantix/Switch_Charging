"""Tests for the strip under the circuit diagram that summarises the active switching schedule."""

import unittest

from tui.widgets.mode_strip import WIDTH, mode_strip_text

BLOCKS = [[h, 60.0] for h in (200, 307, 600, 107, 50, 0, 1000, 10, 23, 600, 200, 23)]
FS_MAP = {"enabled": True, "end": "hold", "default_mode": "charge", "current_index": 0, "next_index": 1,
          "entries": [{"blocks": BLOCKS}, {"blocks": BLOCKS}, {"mode": "passthrough"}]}
AF_ON = {"enabled": True, "active": False, "i_enter_a": 0.1, "target_mode": "charge"}


def lines(t):
    return t.plain.rstrip("\n").split("\n")


class TestModeStrip(unittest.TestCase):

    def test_nothing_special_is_empty(self):
        self.assertEqual(mode_strip_text({}, {}).plain, "")
        self.assertEqual(mode_strip_text({"enabled": False}, {"enabled": False, "entries": []}).plain, "")

    def test_auto_follow_only(self):
        s = mode_strip_text(AF_ON, {}).plain
        self.assertIn("AUTO-FOLLOW waiting for charge current > 0.10 A", s)
        s = mode_strip_text(dict(AF_ON, active=True), {}).plain
        self.assertIn("switching while current flows", s)

    def test_freq_map_armed(self):
        fs = dict(FS_MAP, current_index=None, next_index=0)
        s = mode_strip_text(AF_ON, fs).plain
        self.assertIn("FREQ MAP armed · charge 1 of 2 next · 12 × 60 s", s)
        self.assertIn("200   307", s)
        self.assertNotIn("auto-follow is OFF", s)
        self.assertIn("auto-follow is OFF", mode_strip_text({"enabled": False}, fs).plain)

    def test_freq_map_running(self):
        af = dict(AF_ON, active=True, blocks_active=True, block_index=8, block_hz=23.0, block_remaining_s=36.6)
        t = mode_strip_text(af, FS_MAP)
        L = lines(t)
        self.assertEqual(L[0], " FREQ MAP 1/2 · block 9/12 · 23 Hz · 37 s left")
        self.assertIn("[  23]", L[1])
        self.assertTrue(L[1].lstrip().startswith("…"))          # window scrolled past the start
        hi = [t.plain[sp.start:sp.end] for sp in t.spans if "on green" in str(sp.style)]
        self.assertEqual(hi, ["[  23]"])
        pas = mode_strip_text(dict(af, block_index=5, block_hz=0.0), FS_MAP).plain
        self.assertIn("block 6/12 · pass-through", pas)
        self.assertIn("[   P]", pas)

    def test_pauses(self):
        af = dict(AF_ON, active=True, pause_active_cfg=[15.0, 0.5], pauses_this_charge=12, in_pause=True)
        s = mode_strip_text(af, {}).plain
        self.assertIn("PAUSES 0.5 s pass / 15 s · 12 this charge · IN PAUSE", s)

    def test_plain_sweep(self):
        fs = {"enabled": True, "end": "loop", "default_mode": "charge", "current_index": 2, "next_index": 3,
              "entries": [{"hz": 23.0}, {"hz": 107.0}, {"hz": 307.0}, {"hz": 23.0}]}
        s = mode_strip_text(AF_ON, fs).plain
        self.assertIn("SWEEP entry 3/4: 307 Hz · next charge: 23 Hz", s)
        self.assertNotIn("AUTO-FOLLOW", s)

    def test_fits_under_the_diagram(self):
        af = dict(AF_ON, active=True, blocks_active=True, block_index=4, block_hz=50.0, block_remaining_s=59)
        cases = [mode_strip_text(af, FS_MAP), mode_strip_text({"enabled": False}, dict(FS_MAP, current_index=None)),
                 mode_strip_text(dict(af, block_index=0, block_hz=1000.0, block_remaining_s=600), FS_MAP),
                 mode_strip_text(dict(AF_ON, active=True, pause_active_cfg=[15.0, 0.5], pauses_this_charge=120,
                                      in_pause=True), {})]
        for t in cases:
            for line in lines(t):
                self.assertLessEqual(len(line), WIDTH, line)


if __name__ == "__main__":
    unittest.main()


class TestCrop(unittest.TestCase):

    def test_long_sweep_entry_is_cropped(self):
        e = {"hz": 307.0, "pause_every_s": 15.0, "pause_s": 0.5}
        fs = {"enabled": True, "end": "loop", "default_mode": "charge", "current_index": 0, "next_index": 1,
              "entries": [e, dict(e, hz=1000.0)]}
        L = mode_strip_text({"enabled": True}, fs).plain.rstrip("\n").split("\n")
        self.assertTrue(all(len(x) <= WIDTH for x in L), L)
        self.assertTrue(L[0].endswith("…"))


class TestDiagramStaysVisible(unittest.IsolatedAsyncioTestCase):
    """Regression: hiding the strip with display:none collapsed the auto-width column it
    shares with the H-bridge diagram, so boards without a special mode lost the diagram."""

    async def test_empty_strip_keeps_diagram(self):
        from tui.app import SwitchingCircuitApp
        from tui.widgets.mode_strip import ModeStrip
        app = SwitchingCircuitApp(host="127.0.0.1", port=1)
        async with app.run_test(size=(230, 57)) as pilot:
            strip = app.query_one("#mode-strip", ModeStrip)
            for af in ({}, {"enabled": True}, {}):
                strip.update_state(af, {})
                await pilot.pause(0.2)
                circuit = app.query_one("#circuit")
                self.assertGreater(circuit.region.width, 40)
                self.assertGreater(circuit.region.height, 20)
            self.assertEqual(strip.region.height, 0)
