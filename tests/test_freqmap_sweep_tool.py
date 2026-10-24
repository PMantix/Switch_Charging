"""tools/freqmap_sweep.py: block orders. Map 1-2 orders must not change; map 3's full-rounds orders cover every
setting in every round, never repeat a setting back to back, and start on a switched setting >= first_min_hz."""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import freqmap_sweep as T  # noqa: E402

MAP2_SETTINGS = [0, 1, 2, 5, 10, 23, 200, 1000, 2000]
MAP3_SETTINGS = [1, 1.5, 2, 3, 5, 7, 10, 23, 200, 1000]


class TestOrders(unittest.TestCase):

    def test_map2_orders_unchanged(self):
        # SW1 map 2, seed 12, as loaded on 2026-10-23
        _, orders, n = T.build(1, 60.0, [12], MAP2_SETTINGS, 0.70 * 205.0, 308.0)
        self.assertEqual(n, 28)
        self.assertEqual(orders[0][:10], [200, 0, 5, 1, 2, 2000, 23, 10, 1000, 10])

    def test_full_rounds(self):
        for seed in (31, 32, 33, 41, 42, 43):
            _, orders, n = T.build(1, 15.0, [seed], MAP3_SETTINGS, 0.70 * 205.0, 308.0, full_rounds=True,
                                   first_min_hz=10)
            o = orders[0]
            self.assertEqual(len(o), n)
            self.assertGreaterEqual(o[0], 10)
            self.assertTrue(all(a != b for a, b in zip(o, o[1:])))
            k = len(MAP3_SETTINGS)
            for r in range(n // k):                                             # every full round has every setting once
                self.assertEqual(sorted(o[r * k:(r + 1) * k]), sorted(MAP3_SETTINGS))
            self.assertEqual(math.ceil(n / k), 12)

    def test_deterministic(self):
        a = T.block_order(31, MAP3_SETTINGS, n=112, first_min_hz=10)
        b = T.block_order(31, MAP3_SETTINGS, n=112, first_min_hz=10)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
