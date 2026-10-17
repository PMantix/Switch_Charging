"""
Switching Circuit V2 - Configuration constants and definitions.

All pin assignments, state definitions, sequences, and tuning parameters
live here so every other module imports from a single source of truth.
"""

# ---------------------------------------------------------------------------
# RP2040-Zero GPIO pin assignments (gate drive + sensing)
#
# The Pi no longer drives MOSFETs directly. It sends commands to the
# RP2040-Zero over USB serial. These pin numbers are the RP2040 GPIOs
# and are used as reference / for RP2040 firmware configuration.
# ---------------------------------------------------------------------------
RP2040_PIN_P1 = 2          # GP2 → UCC5304 (U1) → P1 high-side MOSFET
RP2040_PIN_P2 = 3          # GP3 → UCC5304 (U2) → P2 high-side MOSFET
RP2040_PIN_N1 = 4          # GP4 → UCC5304 (U3) → N1 low-side MOSFET
RP2040_PIN_N2 = 5          # GP5 → UCC5304 (U4) → N2 low-side MOSFET

RP2040_PIN_I2C_SDA = 6     # GP6 → INA226 shared I2C bus (SDA)
RP2040_PIN_I2C_SCL = 7     # GP7 → INA226 shared I2C bus (SCL)

RP2040_PIN_ADC0 = 26       # GP26 → Built-in ADC ch0 (transient capture)
RP2040_PIN_ADC1 = 27       # GP27 → Built-in ADC ch1 (transient capture)
RP2040_PIN_ADC2 = 28       # GP28 → Built-in ADC ch2 (transient capture)

RP2040_PIN_ROTARY_A_CLK = 8   # GP8  → Rotary dial A clock
RP2040_PIN_ROTARY_A_DT = 9    # GP9  → Rotary dial A data
RP2040_PIN_ROTARY_A_SW = 10   # GP10 → Rotary dial A switch

RP2040_PIN_7SEG_CLK = 14   # GP14 → 7-segment display clock (TM1637)
RP2040_PIN_7SEG_DIO = 15   # GP15 → 7-segment display data

# All MOSFET outputs are active-high (V2 uses UCC5304 MOSFET driver ICs)
MOSFET_ACTIVE_HIGH = True

# ---------------------------------------------------------------------------
# Legacy aliases — used by GPIODriver and server __main__ until the server
# is refactored to communicate with the RP2040 over USB serial instead of
# driving GPIOs directly.  These map to the *Pi* GPIO pins for mock/dev use.
# ---------------------------------------------------------------------------
PIN_P1 = 17          # Pi GPIO17 (dev/mock only — production uses RP2040 GP2)
PIN_P2 = 27          # Pi GPIO27 (dev/mock only — production uses RP2040 GP3)
PIN_N1 = 22          # Pi GPIO22 (dev/mock only — production uses RP2040 GP4)
PIN_N2 = 23          # Pi GPIO23 (dev/mock only — production uses RP2040 GP5)

PIN_TM1637_CLK = 18  # Pi GPIO18 (moving to RP2040 GP14)
PIN_TM1637_DIO = 24  # Pi GPIO24 (moving to RP2040 GP15)

PIN_ROTARY_CLK = 16  # Pi GPIO16 (moving to RP2040 GP8)
PIN_ROTARY_DT = 20   # Pi GPIO20 (moving to RP2040 GP9)
PIN_ROTARY_BTN = 21  # Pi GPIO21 (moving to RP2040 GP10)

# ---------------------------------------------------------------------------
# INA226 I2C addresses (D-FLIFE breakout modules)
# ---------------------------------------------------------------------------
INA226_ADDR_P1 = 0x40      # High-side left  (A1=GND, A0=GND)
INA226_ADDR_P2 = 0x41      # High-side right (A1=GND, A0=VS)
INA226_ADDR_N1 = 0x43      # Low-side left   (A0=SCL, A1=GND)
INA226_ADDR_N2 = 0x45      # Low-side right  (A1=VS,  A0=VS)

# ---------------------------------------------------------------------------
# USB serial connection to RP2040-Zero
# ---------------------------------------------------------------------------
RP2040_SERIAL_PORT = "/dev/ttyACM0"   # default on Pi
RP2040_SERIAL_BAUD = 115200

# Link supervision. A single serial read error used to kill the reader
# thread for good (2026-10-10: pi-SW2 went deaf 24 s after boot and kept
# serving frozen sensor values as if live). The reader now reopens the
# port on any error, and also forces a reopen when the D-line stream goes
# silent while streaming is expected.
RP2040_RECONNECT_INTERVAL_S = 2.0   # s between reopen attempts while down
SENSOR_STALE_MIN_S = 2.0            # s without a D line before data counts as stale
SENSOR_WATCHDOG_S = 10.0            # s without a D line before forcing a reopen

# ---------------------------------------------------------------------------
# H-bridge state definitions
#   Each tuple: (P1, P2, N1, N2) — True = on, False = off
#
#   0: +A / -A   (P1 + N1)
#   1: +A / -B   (P1 + N2)
#   2: +B / -A   (P2 + N1)
#   3: +B / -B   (P2 + N2)
#   4: All ON
#   5: All OFF
# ---------------------------------------------------------------------------
STATE_DEFS = [
    (True,  False, True,  False),   # 0
    (True,  False, False, True),    # 1
    (False, True,  True,  False),   # 2
    (False, True,  False, True),    # 3
    (True,  True,  True,  True),    # 4  all-on
    (False, False, False, False),   # 5  all-off
]

# ---------------------------------------------------------------------------
# 8 selectable switching sequences (each is 4 state-definition indices)
# ---------------------------------------------------------------------------
SEQUENCES = [
    [5, 5, 5, 5],   # 1: all-off (idle)
    [0, 1, 2, 3],   # 2
    [0, 1, 3, 2],   # 3
    [0, 2, 1, 3],   # 4
    [0, 2, 3, 1],   # 5
    [0, 3, 1, 2],   # 6
    [0, 3, 2, 1],   # 7
    [4, 4, 4, 4],   # 8: all-on
]

NUM_SEQUENCES = len(SEQUENCES)

# ---------------------------------------------------------------------------
# Pulse charge: alternates between state 0 (P1+N1) and state 3 (P2+N2)
# to pulse-charge two batteries connected on each half-bridge leg
# ---------------------------------------------------------------------------
PULSE_CHARGE_SEQUENCE = [0, 3]

# ---------------------------------------------------------------------------
# Frequency range and defaults
# ---------------------------------------------------------------------------
MIN_FREQ = 0.1       # Hz
# Firmware's hard floor is 50 µs step period, so with a 2-step pulse
# sequence the absolute ceiling is 10 kHz switching. 2 kHz keeps comfy
# headroom for the UCC5304 gate-drive rise/fall and leaves the INA226
# sampling cap (~500-1000 Hz) as the limiting factor rather than the
# switching cap — which is what we want for aliasing DOEs.
MAX_FREQ = 2000.0    # Hz
DEFAULT_FREQ = 10.0  # Hz

# ---------------------------------------------------------------------------
# Dead time inserted between mode transitions (seconds)
# ---------------------------------------------------------------------------
DEAD_TIME = 0.002    # 2 ms

# ---------------------------------------------------------------------------
# Auto-follow (current-threshold-driven mode switcher)
# ---------------------------------------------------------------------------
AUTO_FOLLOW_I_ENTER_A = 0.050    # A — enter switching when |i| rises above this
AUTO_FOLLOW_I_EXIT_A = 0.020     # A — drop back to transparent below this (or i<=0)
AUTO_FOLLOW_LOOP_HZ = 15.0       # controller poll rate
AUTO_FOLLOW_EXIT_CONFIRM_S = 1.0  # s — exit needs the MEAN current over this window below i_exit.
                                  # One reading is not enough: at 307 Hz+ the four INA226s sample
                                  # different switching phases and their KCL sum sags for a tick or two
                                  # (SW7 / cell 17, 2026-10: ~85 false exits per 30 min charge).
                                  # Reversal (i < -i_exit) still exits on the next tick. 0 = old behaviour.

# ---------------------------------------------------------------------------
# Schedule (loaded for monitoring; see schedule_monitor.py)
# ---------------------------------------------------------------------------
AUTO_SCHEDULE_DIR = "schedules"  # default directory for schedule files

# ---------------------------------------------------------------------------
# Command server
# ---------------------------------------------------------------------------
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 5555
