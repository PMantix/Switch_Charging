#!/usr/bin/env python3
"""Drive the board's measurement mode in step with a Neware schedule.

The Neware runs a short, loop-based schedule (printed by ``table``); this
script holds the matching board configuration for each block and writes
a marker into the board's measure log at every Neware step, so the log
alone is enough for the analysis (board_calibration.py / ring_solver.py
in the Cycle_Toughness repo).

Synchronisation: the schedule opens with a rest and then a short sync
charge. The script waits in ``pass`` (all FETs on) until the board's P1+P2
current rises above a threshold, takes that as t = 0, and follows the
plan's timeline from there. Configuration changes always fall a few
seconds into a Neware rest, and are make-before-break on the board, so
the cycler never sees an open circuit.

Run it on the Pi (default 127.0.0.1:5555) or anywhere with --host.

  python3 tools/measure_run.py table calibration      # Neware steps to enter (+ CSV with --csv)
  python3 tools/measure_run.py dry calibration        # the board timeline, without a board
  python3 tools/measure_run.py run calibration --label "SW10 cal, spare cell X"
  python3 tools/measure_run.py hold pair2             # enter measure mode and hold one config (manual work)
  python3 tools/measure_run.py stats --settle 2       # mean / s.d. of every channel in the current hold
  python3 tools/measure_run.py exit                   # back to pass-through (discharge mode)
"""
from __future__ import annotations

import argparse
import csv
import json
import socket
import sys
import time

# ---------------------------------------------------------------- plans
# A plan = sync preamble + one block repeated once per configuration. Each
# block starts with a rest (the config change happens SWITCH_AT seconds
# into it), so the Neware side is a handful of steps and one Cycle step.

SWITCH_AT = 5.0          # s into each block's opening rest
SYNC = {"rest_s": 30, "ma": 50, "s": 10}

LADDER = [25, 50, 100, 200, 308]

PLANS = {
    # Stage A: sensor gain / offset. In a pair configuration both driven
    # sensors carry exactly the Neware current; every rest gives offsets.
    "calibration": {
        "desc": "Sensor calibration: +/- current ladder in each pair and in pass-through",
        "configs": ["pair2", "pair1", "pair3", "pair4", "pass"],
        "block": [("rest", 0, 30)] + [s for ma in LADDER for s in (("chg", ma, 20), ("dchg", ma, 20))],
        "tail_rest_s": 30,
        "record_s": 0.1,
    },
    # Stage A, step 5: gain after the shunts have warmed up (run after a
    # 30 min, 308 mA charge-neutral soak in pair2: see the 'soak' plan).
    "soak": {
        "desc": "Shunt warm-up: +/-308 mA, 60 s each, 15 times (30 min, charge-neutral) in pair2",
        "configs": ["pair2"] * 15,
        "block": [("rest", 0, 10), ("chg", 308, 60), ("dchg", 308, 60)],
        "tail_rest_s": 10,
        "record_s": 1.0,
    },
    # Stage C: small charge-neutral pulses through each electrode pair.
    "pairs": {
        "desc": "Pair resistance through the board: +/-10 mA, 10 s pulses in each pair",
        "configs": ["pair1", "pair2", "pair3", "pair4"],
        "block": [("rest", 0, 60), ("chg", 10, 10), ("rest", 0, 30), ("dchg", 10, 10), ("rest", 0, 30)],
        "tail_rest_s": 30,
        "record_s": 0.01,
    },
    # End-of-discharge check for a face stripped to bare copper.
    "nucleation": {
        "desc": "Nucleation check (end of discharge): 20 mA, 30 s charge then the same back, each pair",
        "configs": ["pair1", "pair2", "pair3", "pair4"],
        "block": [("rest", 0, 60), ("chg", 20, 30), ("rest", 0, 30), ("dchg", 20, 30), ("rest", 0, 30)],
        "tail_rest_s": 30,
        "record_s": 0.01,
    },
}


def block_len(plan) -> float:
    return sum(s for _, _, s in plan["block"])


def timeline(plan):
    """Events relative to the sync-charge onset (t = 0):
    (t, 'config', name) and (t, 'mark', label)."""
    ev = [(0.0, "mark", f"sync chg mA={SYNC['ma']} dur={SYNC['s']}")]
    t = float(SYNC["s"])
    for b, cfg in enumerate(plan["configs"]):
        ev.append((t + SWITCH_AT, "config", cfg))
        tt = t
        for j, (kind, ma, dur) in enumerate(plan["block"]):
            ev.append((tt, "mark", f"step blk={b} cfg={cfg} j={j} kind={kind} mA={ma} dur={dur}"))
            tt += dur
        t += block_len(plan)
    ev.append((t, "mark", f"tail rest dur={plan['tail_rest_s']}"))
    ev.append((t + plan["tail_rest_s"], "mark", "end"))
    return sorted(ev, key=lambda e: e[0])


def neware_table(plan):
    """Steps to enter in BTS: (step, type, current_mA, time_s, note)."""
    rows = [(1, "Rest", "", SYNC["rest_s"], "board waits in pass-through"),
            (2, "CC Chg", SYNC["ma"], SYNC["s"], "sync pulse: the script starts its clock here")]
    n = 3
    first = n
    for j, (kind, ma, dur) in enumerate(plan["block"]):
        typ = {"rest": "Rest", "chg": "CC Chg", "dchg": "CC DChg"}[kind]
        note = f"board changes configuration {SWITCH_AT:.0f} s in" if j == 0 else ""
        rows.append((n, typ, ma if ma else "", dur, note))
        n += 1
    rows.append((n, "Cycle", "", "", f"to step {first}, {len(plan['configs'])} times in all"))
    rows.append((n + 1, "Rest", "", plan["tail_rest_s"], ""))
    rows.append((n + 2, "End", "", "", ""))
    return rows


# ---------------------------------------------------------------- board link

class Board:
    def __init__(self, host, port, timeout=3.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.r = self.sock.makefile("r", encoding="utf-8")
        self.w = self.sock.makefile("w", encoding="utf-8")

    def cmd(self, name, **kw):
        self.w.write(json.dumps({"cmd": name, **kw}) + "\n")
        self.w.flush()
        while True:
            line = self.r.readline()
            if not line:
                raise ConnectionError("board closed the connection")
            msg = json.loads(line)
            if msg.get("event"):          # broadcast frame on a subscribed socket: skip
                continue
            if not msg.get("ok", False):
                raise RuntimeError(f"{name}: {msg.get('error', msg)}")
            return msg

    def close(self):
        self.sock.close()


def enter_measure(board, label=""):
    st = board.cmd("get_status")
    if st["mode"] != "measure":
        if st.get("auto_follow", {}).get("enabled"):
            print("note: auto-follow was on; entering measure mode turns it off")
        board.cmd("set_mode", mode="measure")
    if label:
        board.cmd("measure_mark", label=f"label {label}")
    return board.cmd("measure_status")


def wait_for_sync(board, threshold_a, poll_s=0.05, timeout_s=3600.0):
    """Block until |P1 + P2| exceeds threshold for 3 polls in a row; return
    the local monotonic time of the first of them."""
    t_end = time.monotonic() + timeout_s
    hits, first = 0, None
    while time.monotonic() < t_end:
        s = board.cmd("get_status")["sensors"]
        i = abs(s.get("P1", {}).get("current", 0.0) + s.get("P2", {}).get("current", 0.0))
        now = time.monotonic()
        if i > threshold_a:
            hits += 1
            first = first if first is not None else now
            if hits >= 3:
                return first
        else:
            hits, first = 0, None
        time.sleep(poll_s)
    raise TimeoutError("no sync pulse seen")


def run_plan(board, plan_name, label, threshold_a, stay):
    plan = PLANS[plan_name]
    enter_measure(board, label)
    board.cmd("measure_set", config="pass")
    board.cmd("measure_mark", label=f"plan {plan_name} configs={','.join(plan['configs'])}")
    total = SYNC["s"] + block_len(plan) * len(plan["configs"]) + plan["tail_rest_s"]
    print(f"[{plan_name}] holding pass-through; start the Neware schedule now "
          f"({total / 60:.1f} min after the sync pulse)")
    t0 = wait_for_sync(board, threshold_a)
    board.cmd("measure_mark", label="sync detected")
    print("sync detected; following the timeline")
    try:
        for t, kind, payload in timeline(plan):
            delay = t0 + t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if kind == "config":
                board.cmd("measure_set", config=payload)
                print(f"  t={t:7.1f} s  config -> {payload}")
            else:
                board.cmd("measure_mark", label=payload)
    except KeyboardInterrupt:
        board.cmd("measure_mark", label="aborted by operator")
        print("aborted; board left in measure mode, holding", board.cmd("measure_status")["config"])
        raise
    st = board.cmd("measure_status")
    print(f"done: {st['frames']} frames, {st['markers']} markers, log {st['log']}")
    if not stay:
        board.cmd("set_mode", mode="discharge")
        print("board back in pass-through (discharge mode)")


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5555)
    sub = ap.add_subparsers(dest="action", required=True)
    for name in ("table", "dry", "run"):
        p = sub.add_parser(name)
        p.add_argument("plan", choices=sorted(PLANS))
        if name == "table":
            p.add_argument("--csv", help="also write the steps to this CSV")
        if name == "run":
            p.add_argument("--label", default="")
            p.add_argument("--sync-threshold", type=float, default=0.010,
                           help="A, as the board reads it (the boards read ~0.6x true; 50 mA sync -> ~30 mA)")
            p.add_argument("--stay", action="store_true", help="stay in measure mode at the end")
    p = sub.add_parser("hold")
    p.add_argument("config")
    p.add_argument("--label", default="")
    p = sub.add_parser("stats")
    p.add_argument("--settle", type=float, default=0.0)
    p = sub.add_parser("mark")
    p.add_argument("label")
    sub.add_parser("status")
    sub.add_parser("exit")
    a = ap.parse_args(argv)

    if a.action == "table":
        plan = PLANS[a.plan]
        rows = neware_table(plan)
        print(f"{a.plan}: {plan['desc']}\nrecord every {plan['record_s']} s; set the voltage limits for the load cell\n")
        print(f"{'step':>4}  {'type':<8} {'mA':>5} {'time s':>7}  note")
        for r in rows:
            print(f"{r[0]:>4}  {r[1]:<8} {str(r[2]):>5} {str(r[3]):>7}  {r[4]}")
        if a.csv:
            with open(a.csv, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["step", "type", "current_mA", "time_s", "note"])
                w.writerows(rows)
            print("wrote", a.csv)
        return 0
    if a.action == "dry":
        for t, kind, payload in timeline(PLANS[a.plan]):
            print(f"{t:8.1f}  {kind:<6} {payload}")
        return 0

    board = Board(a.host, a.port)
    try:
        if a.action == "run":
            run_plan(board, a.plan, a.label, a.sync_threshold, a.stay)
        elif a.action == "hold":
            enter_measure(board, a.label)
            print(json.dumps(board.cmd("measure_set", config=a.config)))
        elif a.action == "stats":
            st = board.cmd("measure_stats", settle_s=a.settle)
            print(f"config {st['config']}, {st['since_s']:.1f} s held")
            for k, v in st["channels"].items():
                if v["n"]:
                    print(f"  {k:<5} {v['mean']:+.6f}  sd {v['sd']:.6f}  n {v['n']}")
        elif a.action == "mark":
            print(board.cmd("measure_mark", label=a.label))
        elif a.action == "status":
            print(json.dumps(board.cmd("measure_status"), indent=1))
        elif a.action == "exit":
            print(board.cmd("set_mode", mode="discharge"))
    finally:
        board.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
