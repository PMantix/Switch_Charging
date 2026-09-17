"""
Switching Circuit V2 - GPIO Driver (RP2040 Serial).

Sends FET state commands to the RP2040-Zero over USB serial.
Receives streaming sensor data via push from the RP2040.
Falls back to a mock implementation when pyserial is unavailable
or the RP2040 is not connected.
"""

import json
import logging
import queue
import threading
import time

from server.config import (
    RP2040_SERIAL_PORT, RP2040_SERIAL_BAUD,
    STATE_DEFS,
)

log = logging.getLogger(__name__)

# Sensor names in the order the RP2040 streams them
SENSOR_ORDER = ["P1", "P2", "N1", "N2"]

try:
    import serial
    _HAS_SERIAL = True
except ImportError:
    _HAS_SERIAL = False
    log.warning("pyserial not available — using mock GPIO driver")


class GPIODriver:
    """Controls the four H-bridge MOSFET outputs via RP2040 serial.
    Receives streaming sensor data from the RP2040 push model."""

    def __init__(self, port=RP2040_SERIAL_PORT, baud=RP2040_SERIAL_BAUD):
        self._lock = threading.Lock()  # protects _ser for writes
        self._fet_states = [False, False, False, False]
        self._port_name = port
        self._ser = None
        self._mock = True

        # Pi↔firmware clock offset, populated by sync_firmware_clock(). Stored
        # as "firmware_seconds - pi_seconds" so an absolute firmware ticks_us
        # value V (in seconds) maps to Pi monotonic() via:
        #     pi_s = V/1e6 - _clock_offset_s
        # None until sync_firmware_clock has run successfully — callers must
        # treat absence as "fall back to midpoint anchor".
        self._clock_offset_s = None
        self._clock_offset_rtt_s = None  # last measured P round-trip (for logging)

        # Sensor data (updated by reader thread from stream)
        # Initialize with zero readings so mock mode returns valid data
        self._sensor_data = {
            name: {"voltage": 0.0, "current": 0.0}
            for name in SENSOR_ORDER
        }
        self._sensor_lock = threading.Lock()
        self._sensor_hz = 0.0
        self._sensor_new = threading.Event()  # set when fresh data arrives

        # Optional callback fired in the reader thread after each fresh
        # sensor frame is cached. Used by command_server to drive Pi-side
        # recording at the true sensor rate, independent of broadcast cadence.
        self.on_sensor_tick = None  # type: ignore[assignment]

        # Response queue for command replies
        self._response_q = queue.Queue()

        self._stop_event = threading.Event()

        if _HAS_SERIAL:
            ports_to_try = [port] + [f"/dev/ttyACM{i}" for i in range(4) if f"/dev/ttyACM{i}" != port]
            for try_port in ports_to_try:
                try:
                    self._ser = serial.Serial(try_port, baud, timeout=0.5)
                    self._wait_for_ready()
                    self._mock = False
                    self._port_name = try_port
                    log.info("GPIODriver connected to RP2040 on %s @ %d", try_port, baud)
                    break
                except (serial.SerialException, OSError) as e:
                    log.debug("Could not open %s: %s", try_port, e)
                    self._ser = None
                    continue
            if self._mock:
                log.warning("No RP2040 found on any serial port — using mock driver")
        else:
            log.info("GPIODriver running in mock mode (no pyserial)")

        # Start reader thread (reads all incoming lines from RP2040)
        if not self._mock:
            self._reader_thread = threading.Thread(
                target=self._reader_loop, name="RP2040-Reader", daemon=True,
            )
            self._reader_thread.start()

        self.all_off()

    def _wait_for_ready(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ser and self._ser.in_waiting:
                line = self._ser.readline().decode("utf-8", errors="replace").strip()
                log.debug("RP2040 boot: %s", line)
                if "OK READY" in line:
                    return
            time.sleep(0.05)
        log.warning("RP2040 did not send READY within %.1fs — continuing anyway", timeout)

    # -- Reader thread -------------------------------------------------------

    def _reader_loop(self):
        """Continuously read lines from RP2040. Route stream data to cache,
        command responses to the response queue."""
        while not self._stop_event.is_set():
            try:
                if not self._ser or not self._ser.is_open:
                    break
                raw = self._ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                if line.startswith("D "):
                    self._handle_stream_line(line)
                elif line.startswith("BR ") or line.startswith("OK ") or line.startswith("ERR"):
                    self._response_q.put(line)
                elif "BURST_DONE" in line:
                    self._response_q.put(line)
                    log.info("RP2040: %s", line)
                else:
                    # Boot messages, info, etc
                    log.debug("RP2040: %s", line)
            except (serial.SerialException, OSError) as e:
                if not self._stop_event.is_set():
                    log.error("Reader error: %s", e)
                break

    def _handle_stream_line(self, line):
        """Parse a D line: D <t_us> <seq> <P1v> <P1i> <P2v> <P2i> <N1v> <N1i> <N2v> <N2i>

        The leading ``t_us`` is the firmware ``time.ticks_us()`` snapshot
        taken at sample-capture time (just before the INA226 sweep). We
        translate it into the Pi's monotonic clock via ``_clock_offset_s``
        (set by ``sync_firmware_clock()``) and pass it through the
        ``on_sensor_tick`` callback so downstream consumers (recorder,
        sequence engine) can anchor the row in the firmware clock instead
        of stamping it at line-receive time. With ~4.5 ms of emit latency
        between capture and stdout, the receive-time anchor lags reality
        by up to a full step at 100 Hz switching, inverting the labels.

        ``seq`` is a monotonic 32-bit counter the firmware bumps on every
        emitted D line. The recorder uses successive seq deltas to detect
        gaps from USB CDC drops or write-buffer overflow — receipt-side
        timing alone can't tell a real drop from CDC burst-aggregation.
        """
        try:
            parts = line.split()
            if len(parts) != 11:  # "D" + t_us + seq + 8 values
                return
            fw_ts_us = int(parts[1])
            seq = int(parts[2])
            vals = [float(x) for x in parts[3:]]
            data = {}
            for i, name in enumerate(SENSOR_ORDER):
                data[name] = {
                    "voltage": round(vals[i * 2], 4),
                    "current": round(vals[i * 2 + 1], 6),
                }
            # Convert firmware ticks_us → Pi monotonic seconds when we have
            # an offset; otherwise fall back to monotonic() at receive time
            # (the legacy behaviour, which is wrong by ~4.5 ms but better
            # than dropping the row before sync_firmware_clock has run).
            if self._clock_offset_s is not None:
                sample_pi_s = fw_ts_us / 1_000_000.0 - self._clock_offset_s
            else:
                sample_pi_s = time.monotonic()
            with self._sensor_lock:
                self._sensor_data = data
            self._sensor_new.set()
            # Fire the optional per-frame callback (used by command_server
            # for Pi-side recording at the sensor rate). Kept tolerant of
            # callback errors so a bad recorder state can't kill the reader.
            cb = self.on_sensor_tick
            if cb is not None:
                try:
                    cb(data, sample_pi_s, fw_ts_us, seq)
                except Exception:
                    log.exception("on_sensor_tick callback failed")
        except (ValueError, IndexError):
            pass

    # -- Command interface ---------------------------------------------------

    def _send(self, cmd):
        """Send a command and wait for the response."""
        with self._lock:
            if self._mock or not self._ser:
                return None
            try:
                # Drain any old responses
                while not self._response_q.empty():
                    try:
                        self._response_q.get_nowait()
                    except queue.Empty:
                        break
                self._ser.write((cmd + "\n").encode("utf-8"))
                self._ser.flush()
                # Wait for response (reader thread will put it in the queue)
                try:
                    return self._response_q.get(timeout=0.5)
                except queue.Empty:
                    log.warning("No response for command: %s (timeout 0.5s)", cmd)
                    return None
            except (serial.SerialException, OSError) as e:
                log.error("Serial error: %s", e)
                return None

    # -- public API ---------------------------------------------------------

    def apply_state(self, state_tuple):
        vals = [int(bool(v)) for v in state_tuple]
        resp = self._send(f"S {vals[0]} {vals[1]} {vals[2]} {vals[3]}")
        self._fet_states = [bool(v) for v in vals]
        if resp is None and not self._mock:
            log.warning("RP2040 S command timed out — no response; FET state may not match hardware")
        elif resp and not resp.startswith("OK"):
            log.warning("RP2040 error: %s", resp)

    def all_on(self):
        self.apply_state(STATE_DEFS[4])

    def all_off(self):
        self.apply_state(STATE_DEFS[5])

    def get_fet_states(self):
        return list(self._fet_states)

    def read_sensors(self):
        """One-shot sensor read (for compatibility). Prefer get_sensor_data()."""
        resp = self._send("I")
        if resp and resp.startswith("OK I "):
            try:
                return json.loads(resp[5:])
            except (ValueError, json.JSONDecodeError):
                log.warning("Bad sensor response: %s", resp)
        return None

    def set_sensor_rate(self, hz):
        """Set the RP2040 streaming rate. Firmware caps the actual rate to
        whatever _max_stream_hz() returns for the current AVG + bus decimation
        profile; the OK T reply echoes the effective rate."""
        hz = max(0.0, float(hz))
        resp = self._send(f"T {hz:.1f}")
        if resp and resp.startswith("OK T"):
            try:
                actual = float(resp.split()[2])
            except (ValueError, IndexError):
                actual = hz
            self._sensor_hz = actual
            log.info("Sensor stream rate set to %.1f Hz (requested %.1f)", actual, hz)
        else:
            log.warning("Failed to set stream rate: %s", resp)

    def get_sensor_rate(self):
        return self._sensor_hz

    def set_ina226_avg(self, avg):
        """Set INA226 averaging count. Valid: 1/4/16/64/128/256/512/1024.
        Returns (actual_avg, max_hz) — firmware echoes the new cap so the
        caller can re-clamp the stream rate if needed."""
        resp = self._send(f"A {int(avg)}")
        if resp and resp.startswith("OK A"):
            parts = resp.split()
            try:
                actual = int(parts[2])
                max_hz = float(parts[3])
                log.info("INA226 AVG set to %d (max stream %.1f Hz)", actual, max_hz)
                return actual, max_hz
            except (ValueError, IndexError):
                pass
        log.warning("Failed to set INA226 AVG: %s", resp)
        return None, None

    def set_cnvr_enabled(self, enabled: bool):
        """Toggle CNVR-driven INA226 read scheduling. When enabled, the
        firmware gates each emit on a hardware ALERT-pin edge from the
        chips, so every emit is a fresh conversion. When disabled, the
        legacy blind-poll path runs and ~5/6 emits at AVG=1 are stale
        repeats. Returns (actual_enabled, armed) or (None, None) on
        failure."""
        resp = self._send(f"N {1 if enabled else 0}")
        if resp and resp.startswith("OK N"):
            parts = resp.split()
            try:
                actual = bool(int(parts[2]))
                armed = bool(int(parts[3]))
                log.info("CNVR set to %s (armed=%s)", actual, armed)
                return actual, armed
            except (ValueError, IndexError):
                pass
        log.warning("Failed to toggle CNVR: %s", resp)
        return None, None

    def set_bus_every(self, every):
        """Set bus-voltage decimation. 0 = never read bus, 1 = every sample,
        N = every Nth shunt sweep. Returns (actual_every, max_hz)."""
        resp = self._send(f"V {int(every)}")
        if resp and resp.startswith("OK V"):
            parts = resp.split()
            try:
                actual = int(parts[2])
                max_hz = float(parts[3])
                log.info("INA226 bus_every set to %d (max stream %.1f Hz)", actual, max_hz)
                return actual, max_hz
            except (ValueError, IndexError):
                pass
        log.warning("Failed to set bus decimation: %s", resp)
        return None, None

    def get_sensor_profile(self):
        """Query firmware for current AVG / bus_every / max_hz. Returns dict
        or None on failure."""
        resp = self._send("M")
        if resp and resp.startswith("OK M"):
            parts = resp.split()
            try:
                return {
                    "avg": int(parts[2]),
                    "bus_every": int(parts[3]),
                    "max_hz": float(parts[4]),
                }
            except (ValueError, IndexError):
                pass
        return None

    def profile_emit(self, n: int = 50):
        """Run the firmware Z command and parse the per-emit phase
        breakdown. Returns a dict with keys ``n``, ``avg_us``, ``i2c_us``,
        ``fmt_us``, ``write_us``, ``max_hz``, or None if the firmware
        didn't reply with a parseable line.

        New firmware format: ``OK Z <n> <avg_us> <i2c_us> <fmt_us>
        <write_us> <max_hz>``. Older firmware that only returns ``OK Z
        <n> <avg_us> <max_hz>`` is detected by token count and the phase
        fields are returned as None — callers can still use ``avg_us``.
        """
        n = max(10, min(500, int(n)))
        resp = self._send(f"Z {n}")
        if not resp or not resp.startswith("OK Z"):
            log.warning("profile_emit: bad reply: %s", resp)
            return None
        parts = resp.split()
        try:
            if len(parts) >= 8:
                # New format: OK Z <n> <avg_us> <i2c_us> <fmt_us> <write_us> <max_hz>
                return {
                    "n": int(parts[2]),
                    "avg_us": int(parts[3]),
                    "i2c_us": int(parts[4]),
                    "fmt_us": int(parts[5]),
                    "write_us": int(parts[6]),
                    "max_hz": float(parts[7]),
                }
            elif len(parts) >= 5:
                # Legacy: OK Z <n> <avg_us> <max_hz>
                return {
                    "n": int(parts[2]),
                    "avg_us": int(parts[3]),
                    "i2c_us": None,
                    "fmt_us": None,
                    "write_us": None,
                    "max_hz": float(parts[4]),
                }
        except (ValueError, IndexError):
            pass
        log.warning("profile_emit: unparseable reply: %s", resp)
        return None

    def set_calibration(self, channel: int, gain: float, offset: float):
        resp = self._send(f"W {channel} {gain:.6f} {offset:.6f}")
        if resp and resp.startswith("OK W"):
            parts = resp.split()
            try:
                return int(parts[2]), float(parts[3]), float(parts[4])
            except (ValueError, IndexError):
                pass
        log.warning("Failed to set calibration: %s", resp)
        return None, None, None

    def get_calibration(self):
        resp = self._send("U")
        if resp and resp.startswith("OK U"):
            result = {}
            for p in resp.split()[2:]:
                try:
                    ch_s, g_s, o_s = p.split(":")
                    result[int(ch_s)] = (float(g_s), float(o_s))
                except (ValueError, IndexError):
                    pass
            return result
        log.warning("Failed to get calibration: %s", resp)
        return None

    def reset_calibration(self):
        resp = self._send("W 4 0 0")
        if resp and resp.startswith("OK W"):
            return True
        log.warning("Failed to reset calibration: %s", resp)
        return False

    def get_sensor_data(self):
        with self._sensor_lock:
            return dict(self._sensor_data)

    def wait_for_new_sensor_data(self, timeout=0.1):
        """Block until fresh sensor data arrives. Returns True if new data, False on timeout."""
        got_new = self._sensor_new.wait(timeout=timeout)
        if got_new:
            self._sensor_new.clear()
        return got_new

    def ping(self):
        resp = self._send("P")
        return bool(resp and resp.startswith("OK P"))

    def sync_firmware_clock(self):
        """Measure the Pi↔firmware clock offset using the P command.

        The firmware now replies with `OK P <ticks_us>` where ticks_us is
        time.ticks_us() at the moment the reply is built. Under symmetric
        serial latency the firmware stamp lands at ~midpoint of (t_send,
        t_reply) Pi-monotonic time, so:
            offset = firmware_seconds - pi_seconds
                   = ticks_us/1e6 - 0.5*(t_send + t_reply)
        Stores the offset and round-trip time on self for later use by
        start_switching(). Returns the offset in seconds, or None on
        failure (mock mode, malformed reply, etc.).

        This sync underpins the inversion fix: with a known offset, the
        OK G ticks_us value translates straight to Pi monotonic, removing
        the asymmetric stdin-poll latency on the firmware side that the
        prior midpoint-only anchor couldn't see.
        """
        from time import monotonic
        if self._mock:
            return None
        t_send = monotonic()
        resp = self._send("P")
        t_reply = monotonic()
        if not resp or not resp.startswith("OK P"):
            log.warning("sync_firmware_clock: bad reply: %s", resp)
            return None
        parts = resp.split()
        if len(parts) < 3:
            # Old firmware that just replies "OK P" — can't sync.
            log.warning("sync_firmware_clock: firmware reply missing ticks_us; "
                        "old firmware? reply=%r", resp)
            return None
        try:
            fw_ticks_us = int(parts[2])
        except ValueError:
            log.warning("sync_firmware_clock: unparseable ticks_us in %r", resp)
            return None
        midpoint_pi_s = 0.5 * (t_send + t_reply)
        offset = fw_ticks_us / 1_000_000.0 - midpoint_pi_s
        rtt = t_reply - t_send
        self._clock_offset_s = offset
        self._clock_offset_rtt_s = rtt
        log.info("Firmware clock sync: offset=%.6fs rtt=%.3fms",
                 offset, rtt * 1000.0)
        return offset

    def get_clock_offset(self):
        """Return the most recent (offset_s, rtt_s) tuple or (None, None)."""
        return self._clock_offset_s, self._clock_offset_rtt_s

    # -- Firmware-resident switching cycle (C/F/G/H/K) ----------------------
    # The RP2040 owns periodic switching via machine.Timer. Pi-side code
    # just programs the cycle and period, then starts/stops the timer.

    def program_sequence(self, packed_states):
        """Program the RP2040's switching cycle.

        `packed_states` is an iterable of ints (each 0-15) where bits are
        P1<<3 | P2<<2 | N1<<1 | N2. Firmware resets its step index to 0
        on receipt.  NOTE: halts the switching timer and turns all FETs off.
        """
        parts = [str(int(s) & 0xF) for s in packed_states]
        if not parts:
            raise ValueError("program_sequence requires at least one state")
        cmd = "C " + str(len(parts)) + " " + " ".join(parts)
        resp = self._send(cmd)
        if resp is None and not self._mock:
            log.warning("RP2040 C command timed out — no response; sequence may not be programmed on hardware")
        elif resp and not resp.startswith("OK"):
            log.warning("RP2040 C error: %s", resp)

    def program_sequence_live(self, packed_states):
        """Reprogram the switching cycle without halting — FETs stay in
        whatever state the ISR last applied.  No all-off gap."""
        parts = [str(int(s) & 0xF) for s in packed_states]
        if not parts:
            raise ValueError("program_sequence_live requires at least one state")
        cmd = "E " + str(len(parts)) + " " + " ".join(parts)
        resp = self._send(cmd)
        if resp is None and not self._mock:
            log.warning("RP2040 E command timed out — no response; sequence may not be programmed on hardware")
        elif resp and not resp.startswith("OK"):
            log.warning("RP2040 E error: %s", resp)

    def all_on_no_halt(self):
        """Set all FETs on without halting the switching timer."""
        resp = self._send("Y 1 1 1 1")
        self._fet_states = [True, True, True, True]
        if resp is None and not self._mock:
            log.warning("RP2040 Y command timed out — no response; FET state may not match hardware")
        elif resp and not resp.startswith("OK"):
            log.warning("RP2040 Y error: %s", resp)

    def set_step_period_us(self, period_us):
        """Set the per-step period in microseconds. If switching is running,
        firmware re-arms the timer preserving its current step index."""
        period_us = max(50, int(period_us))
        resp = self._send(f"F {period_us}")
        if resp is None and not self._mock:
            log.warning("RP2040 F command timed out — no response; period may not be set on hardware")
        elif resp and not resp.startswith("OK"):
            log.warning("RP2040 F error: %s", resp)

    def start_switching(self):
        """Start periodic switching. Requires program_sequence() and
        set_step_period_us() to have been called first.

        Returns (anchor_pi_s, fw_ticks_us): Pi-monotonic estimate of when
        firmware applied state 0 on the FET pins, plus the firmware's
        own ticks_us stamp from the OK G reply.

        Anchor selection:
        - If sync_firmware_clock() has populated self._clock_offset_s AND
          the OK G reply included a usable ticks_us, anchor is computed
          DIRECTLY from the firmware stamp:
              anchor_pi_s = fw_ticks_us/1e6 - _clock_offset_s
          This is the authoritative anchor because it removes the
          asymmetric latency baked into _switching_start (the firmware's
          stdin-poll loop adds variable cost that midpoint-of-RTT can't
          model — symptom was 100 Hz inverting while 10/200 Hz aligned
          in the 2026-04-24 DOE).
        - Otherwise, fall back to the midpoint of send/reply monotonic
          times. Same correction the prior version did; works under the
          symmetric-latency assumption."""
        from time import monotonic
        t_send = monotonic()
        resp = self._send("G")
        t_reply = monotonic()
        if resp and not resp.startswith("OK"):
            log.warning("RP2040 G error: %s", resp)
            return None, None
        # Mock mode (and serial errors) return resp=None; no firmware stamp
        # to parse. Sequence engine handles (None, None) by falling back to
        # monotonic() for the anchor.
        if resp is None:
            return None, None
        fw_ticks_us = None
        try:
            parts = resp.split()
            if len(parts) >= 5:
                fw_ticks_us = int(parts[4])
        except (ValueError, IndexError):
            pass
        if self._clock_offset_s is not None and fw_ticks_us is not None:
            anchor_pi_s = fw_ticks_us / 1_000_000.0 - self._clock_offset_s
        else:
            anchor_pi_s = 0.5 * (t_send + t_reply)
        return anchor_pi_s, fw_ticks_us

    def stop_switching(self):
        """Halt switching and set all FETs off."""
        resp = self._send("H")
        if resp is None and not self._mock:
            log.warning("RP2040 H command timed out — no response; FETs may still be switching on hardware")
        elif resp and not resp.startswith("OK"):
            log.warning("RP2040 H error: %s", resp)
        # Firmware turns everything off; mirror that in our cache.
        self._fet_states = [False, False, False, False]

    def debug_step_cycle(self):
        """Advance one step in the programmed cycle (for DEBUG stepping)."""
        resp = self._send("K")
        if resp is None and not self._mock:
            log.warning("RP2040 K command timed out — no response; debug step may not have advanced on hardware")
        elif resp and not resp.startswith("OK"):
            log.warning("RP2040 K error: %s", resp)

    def cleanup(self):
        self._stop_event.set()
        # Stop streaming
        if self._ser and not self._mock:
            try:
                self._ser.write(b"T 0\n")
                self._ser.flush()
            except OSError:
                pass
        self.all_off()
        if self._ser:
            try:
                self._ser.close()
            except OSError:
                pass
        log.info("GPIODriver cleaned up")
