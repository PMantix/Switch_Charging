#!/usr/bin/env python3
"""Build (and optionally load) the frequency-response-map sweep for one board.

Each mapped charge is one sweep entry with a list of frequency blocks: pass-through (0) plus the
test frequencies, each once in every third of the main charge, in random order, never the same
setting twice in a row, with a new order for every charge (Cycle_Toughness/FreqMap/README.md).
After the mapped charges the sweep holds a pass-through entry, so later charges are not switched.

  python3 tools/freqmap_sweep.py plan                       # print the entries (JSON) and the order table
  python3 tools/freqmap_sweep.py plan --out freqmap_SW1.json
  python3 tools/freqmap_sweep.py apply --host 192.168.137.105 --label "cell 15"   # load + enable on a board
  python3 tools/freqmap_sweep.py apply --host 127.0.0.1 --port 5599 --block-s 10 --charges 1   # quick test

apply = sweep_configure (entries, end "hold") + sweep_reset + sweep_enable. It does not change
auto-follow; it warns if auto-follow is off. The plan file records the seeds and every block order.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import socket
import sys
from datetime import datetime

SETTINGS = [0, 10, 23, 50, 107, 200, 307, 600, 1000]     # Hz; 0 = pass-through
MAIN_MAH = 0.70 * 205.0                                   # fixed main charge (B.4F), mAh
I_MAIN_MA = 308.0


def block_order(seed: int, settings, thirds: int = 3, n: int | None = None, first_min_hz: float | None = None) -> list:
    """Each setting once per round, shuffled, never the same setting twice in a row. Default: `thirds` rounds. With
    ``n``: as many rounds as the n blocks need, cut to n (every block is drawn fresh, nothing repeats). With
    ``first_min_hz``: block 0 must be a switched setting >= that (the step-7 pulse runs at block 0's setting)."""
    rng = random.Random(seed)
    rounds = thirds if n is None else math.ceil(n / len(settings))
    while True:
        order = []
        for _ in range(rounds):
            third = list(settings)
            rng.shuffle(third)
            order += third
        if n is not None:
            order = order[:n]
        if all(a != b for a, b in zip(order, order[1:])) and (first_min_hz is None or order[0] >= first_min_hz):
            return order


def build(charges: int, block_s: float, seeds, settings, main_mah: float, i_ma: float, full_rounds: bool = False,
          first_min_hz: float | None = None):
    """full_rounds: draw rounds of the whole setting list for every block of the charge (frequency map 3: many
    short blocks). Default: three shuffled rounds, repeated to fill the charge (maps 1-2)."""
    n_blocks = math.ceil(main_mah / (i_ma * block_s / 3600.0))
    entries, orders = [], []
    for c in range(charges):
        if full_rounds:
            seq = block_order(seeds[c], settings, n=n_blocks, first_min_hz=first_min_hz)
        else:
            order = block_order(seeds[c], settings, first_min_hz=first_min_hz)
            seq = (order * (n_blocks // len(order) + 2))[:n_blocks]
        orders.append(seq)
        entries.append({"blocks": [[float(h), float(block_s)] for h in seq]})
    entries.append({"mode": "passthrough"})                 # after the map: hold in pass-through
    return entries, orders, n_blocks


class Board:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.r = self.sock.makefile("r", encoding="utf-8")
        self.w = self.sock.makefile("w", encoding="utf-8")

    def cmd(self, name, **kw):
        self.w.write(json.dumps({"cmd": name, **kw}) + "\n")
        self.w.flush()
        while True:
            msg = json.loads(self.r.readline())
            if msg.get("event"):
                continue
            if not msg.get("ok", False):
                raise RuntimeError(f"{name}: {msg.get('error', msg)}")
            return msg


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["plan", "apply"])
    ap.add_argument("--charges", type=int, default=3)
    ap.add_argument("--block-s", type=float, default=60.0)
    ap.add_argument("--seeds", type=int, nargs="*", default=None, help="one per charge (default 1, 2, 3, ...)")
    ap.add_argument("--settings", type=float, nargs="*", default=None, help="Hz list, 0 = pass-through")
    ap.add_argument("--main-mah", type=float, default=MAIN_MAH)
    ap.add_argument("--i-ma", type=float, default=I_MAIN_MA)
    ap.add_argument("--out", help="write the plan (entries + orders) to this JSON file")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--label", default="")
    ap.add_argument("--full-rounds", action="store_true",
                    help="rounds of the whole setting list for every block (no repeated order); for many short blocks")
    ap.add_argument("--first-min-hz", type=float, default=None,
                    help="block 0 must be a switched setting >= this (the step-7 pulse runs at block 0's setting)")
    a = ap.parse_args(argv)

    settings = a.settings if a.settings else SETTINGS
    seeds = a.seeds if a.seeds else list(range(1, a.charges + 1))
    if len(seeds) < a.charges:
        raise SystemExit("need one seed per charge")
    entries, orders, n = build(a.charges, a.block_s, seeds, settings, a.main_mah, a.i_ma, a.full_rounds, a.first_min_hz)
    plan = {"created": datetime.now().isoformat(timespec="seconds"), "label": a.label, "block_s": a.block_s,
            "blocks_per_charge": n, "settings": settings, "seeds": seeds[:a.charges], "orders": orders,
            "full_rounds": a.full_rounds, "first_min_hz": a.first_min_hz, "entries": entries, "end": "hold"}
    print(f"{a.charges} mapped charge(s), {n} blocks of {a.block_s:g} s each "
          f"({n * a.block_s / 60:.0f} min, main charge ~{a.main_mah / a.i_ma * 60:.0f} min), then pass-through")
    for c, o in enumerate(orders):
        print(f"  charge {c + 1} (seed {seeds[c]}): " + " ".join("P" if h == 0 else f"{h:g}" for h in o))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(plan, f, indent=1)
        print("wrote", a.out)
    if a.action == "plan":
        if not a.out:
            print(json.dumps(entries))
        return 0

    b = Board(a.host, a.port)
    b.cmd("sweep_configure", entries=entries, end="hold")
    b.cmd("sweep_reset")
    b.cmd("sweep_enable", enabled=True)
    st = b.cmd("sweep_status")
    fs = st.get("freq_sweep", st)
    af = b.cmd("auto_follow_status").get("auto_follow", {})
    print(f"loaded on {a.host}: {len(fs.get('entries', []))} entries, enabled={fs.get('enabled')}, "
          f"next={fs.get('next_index')}, end={fs.get('end')}")
    if not af.get("enabled"):
        print("WARNING: auto-follow is OFF on this board - blocks only run while auto-follow is engaged")
    else:
        print(f"auto-follow on: enter {af.get('i_enter_a')} A, exit {af.get('i_exit_a')} A, "
              f"exit_confirm {af.get('exit_confirm_s')} s, engage_delay {af.get('engage_delay_s')} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
