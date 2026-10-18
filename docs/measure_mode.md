# Measurement mode

A held, precision-sampled board state for two jobs:

1. Calibrating the four INA226 current sensors against a known cycler current.
2. Characterising a 5-layer cell one electrode pair at a time.

The code is in `server/measure.py`, with the hooks in `server/mode_controller.py` and `server/command_server.py`. The
driver script is `tools/measure_run.py`.

## What it does

- **Stops switching and holds one FET configuration:**

  | config | terminals | FETs | board state |
  |---|---|---|---|
  | `pair1` | +A / −B | P1, N2 | 1 |
  | `pair2` | +A / −A | P1, N1 | 0 |
  | `pair3` | +B / −A | P2, N1 | 2 |
  | `pair4` | +B / −B | P2, N2 | 3 |
  | `pass`  | all     | all on | 4 |
  | `open`  | none    | all off | 5 |

  Aliases: `state0`..`state3`, `all_on`, `all_off`. Pairs are named by the interface they face directly.
- **Never opens the circuit when changing configuration.** Changes are make-before-break: the union of the old and new
  FET sets goes on first, then the new set. Entering from `discharge` (pass-through) starts in `pass` with no FET change.
  Entering from a switching mode stops the engine (all off for one dead time), so enter during a cycler rest.
  Leaving to `discharge` turns all FETs on, a superset of every configuration, so there is no gap either.
- **Switches the sensors to a precision profile:** INA226 averaging 64, bus voltage read on every sample, stream at
  20 Hz (`MEASURE_*` in `config.py`). The previous profile is restored on exit. To use a different profile, call
  `measure_set_profile` before entering.
- **Logs every sensor frame** to `~/measure_logs/measure_<timestamp>.csv`, together with the configuration that was
  live, and with `config`, `mark` and `stop` events. The header records the firmware calibration in force, so raw
  readings can be recovered: `raw = (reading − offset) / gain`.
- **Turns auto-follow off on entry.** Measurement mode is never restored after a power cycle: the board comes back in
  the mode it was in before the measurement, normally `discharge`.

## Commands (JSON on port 5555)

| cmd | fields | notes |
|---|---|---|
| `set_mode` | `mode: "measure"` | enter; any other mode leaves |
| `measure_set` | `config` | measure mode only |
| `measure_mark` | `label` | writes a marker row |
| `measure_stats` | `settle_s` | mean / s.d. / n of all 8 channels since the last config change, skipping `settle_s` |
| `measure_reset_stats` | | restart the stats window |
| `measure_status` | | read-only: active, config, profile, log path, frame and marker counts |
| `measure_set_profile` | `avg`, `bus_every`, `sensor_hz` | for the next entry; refused while active |

## Running a plan with the Neware

```
python3 tools/measure_run.py table calibration     # Neware steps to enter: ~16 steps with one Cycle step
python3 tools/measure_run.py run calibration --label "SW10, spare cell X"
```

1. The script enters measurement mode and waits in `pass`.
2. Start the Neware schedule. It opens with a rest and a 50 mA, 10 s sync charge. The script takes the sync onset as
   t = 0.
3. From then on, the script changes configuration 5 s into each block's opening rest, and writes a marker at every
   Neware step: `step blk=… cfg=… j=… kind=… mA=… dur=…`. The log alone then defines the analysis windows.

Plans:

| plan | use |
|---|---|
| `calibration` | ±25…308 mA ladder in each pair and in pass-through |
| `soak` | 30 min warm-up at ±308 mA |
| `pairs` | ±10 mA pulses through each pair (stage C) |
| `nucleation` | 20 mA, 30 s, each pair, at end of discharge |

Analysis lives in the Cycle_Toughness repo:

- `board_calibration.py fit <log>` gives gain and offset per sensor, the pass-through checks, and the bus-voltage
  slopes.
- `ring_solver.py` gives the four interfaces from the pair measurements.

**Calibration wiring:** one spare cell as the load. Both + leads (+A, +B) go to its + tab, and both − leads (−A, −B) to
its − tab, with equal leads. In every pair configuration, the two driven sensors then carry exactly the Neware current.
In `pass`, the split shows the board's own +A/+B path imbalance.

Manual work: `measure_run.py hold pair3`, then `stats --settle 2`, then `exit`.

## From the TUI

| key | what it does |
|---|---|
| `n` | Enter measurement mode, or leave it to pass-through. The right column switches to the MEASURE panel; Tab toggles between that panel and the status panel. |
| `1`–`4` | Hold pair1 … pair4 (+A/−B, +A/−A, +B/−A, +B/−B). |
| `5` | Hold pass-through. |
| `6` | Open: breaks the circuit, so use it only at zero current. |
| `t` | Write a marker (`TUI mark N`) into the board's log. |
| `o` | Cycle the pause presets: off → 0.5 s every 15 s → 0.5 s/30 s → 1 s/15 s → 1 s/60 s. |

The panel shows:

- the held configuration and how long it has been held;
- the sensor profile;
- live mean and s.d. of each sensor's current and voltage since the last change, skipping the first 1 s
  (`MEASURE_LIVE_SETTLE_S`);
- the log file name, with its frame and marker counts.

**Pauses in the sweep editor** (`f`): write an entry as `307~15/0.5`, meaning 307 Hz with a 0.5 s pass-through every
15 s, for that cycle only.
