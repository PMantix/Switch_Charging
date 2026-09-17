"""
Switching Circuit V2 - TCP JSON Command Server.

Listens on a configurable port for line-delimited JSON commands.
Supports multiple concurrent clients and a streaming subscription mode
that pushes state updates at ~15 Hz.
"""

import json
import logging
import socket
import threading
import time
from time import sleep

from pathlib import Path

from server import network_mode
from server.config import SERVER_HOST, SERVER_PORT, AUTO_SCHEDULE_DIR
from server.fleet import AP_GATEWAY, my_ap_profile
from server.recorder import PiRecorder
from server.schedule import load_schedule, load_schedule_inline, validate_schedule_semantics

log = logging.getLogger(__name__)

# Broadcast rate bounds (Hz). Counter-intuitive tuning note: lower is
# NOT gentler over WiFi. Sparse traffic (5 Hz → 200 ms gaps) lets the
# Mac WiFi radio drop into power-save between packets and lets the TCP
# congestion window collapse; each new frame then pays a wake-up and
# slow-start penalty measured in tens-to-hundreds of ms, which
# accumulates into the TCP buffer and eventually kills the connection.
# A steady 30 Hz keeps the radio and TCP window fully open and is
# actually cheaper per-frame. Do not lower this without measuring.
_DEFAULT_SUBSCRIBE_HZ = 30
_MAX_SUBSCRIBE_HZ = 30


class CommandServer:
    """
    TCP server that accepts JSON-line commands and dispatches them
    to the ModeController and SequenceEngine.
    """

    def __init__(self, mode_controller, sequence_engine, host=SERVER_HOST, port=SERVER_PORT):
        self._mc = mode_controller
        self._engine = sequence_engine
        self._host = host
        self._port = port

        self._server_socket = None
        self._stop_event = threading.Event()
        self._broadcast_hz = _DEFAULT_SUBSCRIBE_HZ
        self._recorder = PiRecorder()

        # Active subscribers (set of socket objects)
        self._subscribers = set()
        self._sub_lock = threading.Lock()

        self._threads = []

        # Drive Pi-side recording from the serial reader thread so the
        # recording rate equals the RP2040's sensor stream rate — not the
        # TUI broadcast rate, which used to cap everything at 30 Hz.
        self._mc._gpio.on_sensor_tick = self._on_sensor_tick

    # -- state persistence (survive a power cycle) ---------------------------

    def _persist_state(self) -> None:
        """Save every operator-set value that would otherwise reset on a
        power cycle, so a reboot resumes exactly where it left off.

        pulse_mode isn't saved separately — it's a side effect of `mode`
        (CHARGE vs PULSE_CHARGE) and gets set correctly when `mode` is
        restored via set_mode().

        Calibration is deliberately NOT included here — it's already
        written to the RP2040's own flash (see cal_persist() in
        firmware-c/src/ina226.c) and survives a power cycle on its own.
        sensor_rate/ina226_avg/bus_every are NOT firmware-persisted
        (in-RAM only, e.g. `s_avg` in ina226.c defaults to 4 on every
        boot), so those do need saving here.
        """
        from server.state_persistence import save_state
        try:
            engine_state = self._engine.get_state()
            af = self._mc.get_auto_follow_status()
            profile = self._mc._gpio.get_sensor_profile() or {}
            save_state({
                "mode": self._mc.get_mode().value,
                "frequency": engine_state["frequency"],
                "sequence": engine_state["sequence"],
                "sensor_rate": self._mc._gpio.get_sensor_rate(),
                "ina226_avg": profile.get("avg"),
                "bus_every": profile.get("bus_every"),
                "auto_follow": {
                    "enabled": af.get("enabled", False),
                    "target_mode": af.get("target_mode", "charge"),
                    "i_enter_a": af.get("i_enter_a"),
                    "i_exit_a": af.get("i_exit_a"),
                    "cc_setpoint_a": af.get("cc_setpoint_a"),
                },
            })
        except Exception:
            log.exception("Failed to persist state")

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        """Bind and start accepting connections in a background thread."""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self._host, self._port))
        self._server_socket.listen(5)
        self._server_socket.settimeout(1.0)  # allow periodic stop checks

        accept_t = threading.Thread(
            target=self._accept_loop, name="CmdServer-Accept", daemon=True,
        )
        accept_t.start()
        self._threads.append(accept_t)

        broadcast_t = threading.Thread(
            target=self._broadcast_loop, name="CmdServer-Broadcast", daemon=True,
        )
        broadcast_t.start()
        self._threads.append(broadcast_t)

        log.info("CommandServer listening on %s:%d", self._host, self._port)

    def stop(self):
        """Shut down the server and disconnect all clients."""
        self._stop_event.set()
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
        with self._sub_lock:
            for sock in list(self._subscribers):
                try:
                    sock.close()
                except OSError:
                    pass
            self._subscribers.clear()
        for t in self._threads:
            t.join(timeout=3.0)
        log.info("CommandServer stopped")

    # -- accept loop --------------------------------------------------------

    def _accept_loop(self):
        while not self._stop_event.is_set():
            try:
                client_sock, addr = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            log.info("Client connected: %s:%d", *addr)
            try:
                # Disable Nagle — our broadcast writes are small state-frame
                # JSON lines; with Nagle on they sit for ~200ms waiting for
                # the next write, and combine with delayed ACKs to produce
                # multi-100ms pseudo-latency that looks like a stall.
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as exc:
                log.warning("TCP_NODELAY setsockopt failed: %s", exc)
            t = threading.Thread(
                target=self._handle_client,
                args=(client_sock, addr),
                name=f"CmdServer-Client-{addr[0]}:{addr[1]}",
                daemon=True,
            )
            t.start()

    # -- client handler -----------------------------------------------------

    def _handle_client(self, sock, addr):
        """Read line-delimited JSON commands from a single client."""
        buf = ""
        sock.settimeout(1.0)
        try:
            while not self._stop_event.is_set():
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break

                if not data:
                    break  # client disconnected

                buf += data.decode("utf-8", errors="replace")

                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    response = self._dispatch(line, sock)
                    if response is not None:
                        self._send_line(sock, response)
        except Exception:
            log.exception("Unexpected error handling client %s:%d", *addr)
        finally:
            with self._sub_lock:
                self._subscribers.discard(sock)
            try:
                sock.close()
            except OSError:
                pass
            log.info("Client disconnected: %s:%d", *addr)

    # -- command dispatch ---------------------------------------------------

    def _dispatch(self, line, sock):
        """Parse a JSON command line and return a response dict (or None for subscribe)."""
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"Invalid JSON: {exc}"}

        cmd = msg.get("cmd")
        if not cmd:
            return {"ok": False, "error": "Missing 'cmd' field"}

        try:
            if cmd == "get_status":
                status = self._mc.get_status()
                return {"ok": True, **status}

            elif cmd == "set_mode":
                mode_str = msg.get("mode")
                if mode_str is None:
                    return {"ok": False, "error": "Missing 'mode' field"}
                new_mode = self._mc.set_mode(mode_str)
                self._persist_state()
                return {"ok": True, "mode": new_mode.value}

            elif cmd == "set_sequence":
                seq = msg.get("sequence")
                if seq is None:
                    return {"ok": False, "error": "Missing 'sequence' field"}
                self._engine.set_sequence(int(seq))
                self._persist_state()
                return {"ok": True, "sequence": self._engine.get_sequence()}

            elif cmd == "set_frequency":
                freq = msg.get("frequency")
                if freq is None:
                    return {"ok": False, "error": "Missing 'frequency' field"}
                self._engine.set_frequency(float(freq))
                self._persist_state()
                return {"ok": True, "frequency": self._engine.get_frequency()}

            elif cmd == "set_fet":
                index = msg.get("index")
                on = msg.get("on")
                if index is None or on is None:
                    return {"ok": False, "error": "Missing 'index' or 'on' field"}
                self._mc.set_fet(int(index), bool(on))
                return {"ok": True, "fet_states": self._mc.get_status()["fet_states"]}

            elif cmd == "debug_step":
                step = self._mc.debug_step()
                return {
                    "ok": True,
                    "debug_step": step,
                    "fet_states": self._mc.get_status()["fet_states"],
                }

            elif cmd == "set_sensor_rate":
                rate = msg.get("rate")
                if rate is None:
                    return {"ok": False, "error": "Missing 'rate' field"}
                rate = float(rate)
                self._mc._gpio.set_sensor_rate(rate)
                # Broadcast to TUI capped at _MAX_SUBSCRIBE_HZ
                self._broadcast_hz = min(max(rate, _DEFAULT_SUBSCRIBE_HZ), _MAX_SUBSCRIBE_HZ)
                self._persist_state()
                return {"ok": True, "sensor_rate": self._mc._gpio.get_sensor_rate()}

            elif cmd == "set_ina226_avg":
                avg = msg.get("avg")
                if avg is None:
                    return {"ok": False, "error": "Missing 'avg' field"}
                actual, max_hz = self._mc._gpio.set_ina226_avg(int(avg))
                if actual is None:
                    return {"ok": False, "error": "Failed to set AVG"}
                self._persist_state()
                return {"ok": True, "avg": actual, "max_hz": max_hz,
                        "sensor_rate": self._mc._gpio.get_sensor_rate()}

            elif cmd == "set_bus_every":
                every = msg.get("every")
                if every is None:
                    return {"ok": False, "error": "Missing 'every' field"}
                actual, max_hz = self._mc._gpio.set_bus_every(int(every))
                if actual is None:
                    return {"ok": False, "error": "Failed to set bus decimation"}
                self._persist_state()
                return {"ok": True, "bus_every": actual, "max_hz": max_hz,
                        "sensor_rate": self._mc._gpio.get_sensor_rate()}

            elif cmd == "get_sensor_profile":
                profile = self._mc._gpio.get_sensor_profile()
                if profile is None:
                    return {"ok": False, "error": "Failed to query profile"}
                return {"ok": True, **profile,
                        "sensor_rate": self._mc._gpio.get_sensor_rate()}

            elif cmd == "profile_emit":
                n = int(msg.get("n", 100))
                result = self._mc._gpio.profile_emit(n)
                if result is None:
                    return {"ok": False, "error": "profile_emit returned no parseable reply"}
                return {"ok": True, **result}

            elif cmd == "set_cnvr":
                enabled = bool(msg.get("enabled", True))
                actual, armed = self._mc._gpio.set_cnvr_enabled(enabled)
                if actual is None:
                    return {"ok": False, "error": "Firmware did not accept N command"}
                return {"ok": True, "enabled": actual, "armed": armed}

            elif cmd == "pi_record_start":
                max_samples = int(msg.get("max_samples", 0))
                rec_mode = msg.get("rec_mode", "unknown")
                rec_freq = float(msg.get("rec_freq", 1.0))
                rec_seq = int(msg.get("rec_seq", 0))
                rec_sensor_hz = float(msg.get("rec_sensor_hz", 15.0))
                # Optional override of the recorder's step-alignment gate.
                # Default (1) waits for a transition into step=1, which gives
                # cycle-aligned recordings — but aliases to "never starts"
                # when sample rate happens to land on the same step every
                # frame (e.g. 100 Hz × 10/50 sps in pulse mode). Pass null
                # to begin recording on the first sample regardless.
                align_to_step = msg.get("align_to_step", None)
                path = self._recorder.start(
                    max_samples, mode=rec_mode, freq=rec_freq,
                    seq=rec_seq, sensor_hz=rec_sensor_hz,
                    align_to_step=align_to_step,
                )
                return {"ok": True, "path": path}

            elif cmd == "set_calibration":
                ch = msg.get("channel")
                gain = msg.get("gain")
                offset = msg.get("offset")
                if ch is None or gain is None or offset is None:
                    return {"ok": False, "error": "Need channel, gain, offset"}
                c, g, o = self._mc._gpio.set_calibration(int(ch), float(gain), float(offset))
                if c is None:
                    return {"ok": False, "error": "Firmware rejected calibration"}
                return {"ok": True, "channel": c, "gain": g, "offset": o}

            elif cmd == "get_calibration":
                cal = self._mc._gpio.get_calibration()
                if cal is None:
                    return {"ok": False, "error": "Failed to query calibration"}
                return {"ok": True, "calibration": cal}

            elif cmd == "reset_calibration":
                ok = self._mc._gpio.reset_calibration()
                if not ok:
                    return {"ok": False, "error": "Failed to reset calibration"}
                return {"ok": True}

            elif cmd == "pi_record_stop":
                path = self._recorder.stop()
                count = self._recorder.sample_count
                return {"ok": True, "path": path, "samples": count}

            elif cmd == "pi_record_status":
                return {
                    "ok": True,
                    "recording": self._recorder.is_recording,
                    "samples": self._recorder.sample_count,
                    "max_samples": self._recorder.max_samples,
                    "path": str(self._recorder.file_path) if self._recorder.file_path else None,
                }

            elif cmd == "subscribe":
                with self._sub_lock:
                    self._subscribers.add(sock)
                log.info("Client subscribed to state updates")
                return {"ok": True, "subscribed": True}

            elif cmd == "ping":
                # Latency-probe clock-offset round-trip. Echoes the client
                # timestamp and returns the server's monotonic_ns so the TUI
                # can compute offset = mac_ns - pi_ns. Sent inline on the
                # subscriber socket as event=pong so it flows through the
                # recv loop alongside state frames.
                t_client_ns = msg.get("t_client_ns", 0)
                pong = {
                    "event": "pong",
                    "t_server_ns": time.monotonic_ns(),
                    "t_client_ns": t_client_ns,
                }
                with self._sub_lock:
                    subscribed = sock in self._subscribers
                if subscribed:
                    try:
                        self._send_line(sock, pong)
                    except (OSError, BrokenPipeError):
                        pass
                    return None  # already sent
                return {"ok": True, **pong}

            # -- auto mode commands ------------------------------------------

            elif cmd == "load_schedule":
                path = msg.get("path")
                inline = msg.get("schedule")
                if inline:
                    sched = load_schedule_inline(inline)
                elif path:
                    sched = load_schedule(path)
                else:
                    return {"ok": False, "error": "Provide 'path' or 'schedule'"}
                self._mc.load_schedule(sched)
                warnings = validate_schedule_semantics(sched)
                return {
                    "ok": True,
                    "schedule_name": sched.name,
                    "steps": sched.total_steps_per_cycle,
                    "repeat": sched.repeat,
                    "warnings": warnings,
                }

            elif cmd == "list_schedules":
                sched_dir = Path(AUTO_SCHEDULE_DIR)
                files = []
                if sched_dir.is_dir():
                    files = sorted(
                        str(p) for p in sched_dir.glob("*.json")
                    )
                return {"ok": True, "schedules": files}

            # -- auto-follow (current-threshold mode switcher) ---------------

            elif cmd == "auto_follow_status":
                return {"ok": True, "auto_follow": self._mc.get_auto_follow_status()}

            elif cmd == "auto_follow_set_enabled":
                enabled = bool(msg.get("enabled", False))
                self._mc.set_auto_follow_enabled(enabled)
                self._persist_state()
                return {"ok": True, "auto_follow": self._mc.get_auto_follow_status()}

            elif cmd == "auto_follow_set_thresholds":
                try:
                    i_enter = float(msg["i_enter_a"])
                    i_exit = float(msg["i_exit_a"])
                except (KeyError, TypeError, ValueError) as e:
                    return {"ok": False, "error": f"Need numeric i_enter_a and i_exit_a: {e}"}
                try:
                    self._mc.set_auto_follow_thresholds(i_enter, i_exit)
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                self._persist_state()
                return {"ok": True, "auto_follow": self._mc.get_auto_follow_status()}

            elif cmd == "auto_follow_set_target":
                target = msg.get("target_mode")
                if target not in ("charge", "pulse_charge"):
                    return {"ok": False, "error": "target_mode must be 'charge' or 'pulse_charge'"}
                self._mc.set_auto_follow_target(target)
                self._persist_state()
                return {"ok": True, "auto_follow": self._mc.get_auto_follow_status()}

            elif cmd == "auto_follow_set_cc_setpoint":
                try:
                    amps = float(msg["cc_setpoint_a"])
                except (KeyError, TypeError, ValueError) as e:
                    return {"ok": False, "error": f"Need numeric cc_setpoint_a: {e}"}
                self._mc._auto_follow.set_cc_setpoint(amps)
                self._persist_state()
                return {"ok": True, "auto_follow": self._mc.get_auto_follow_status()}

            # -- schedule monitor (passive PLAN/OBSERVED tracker) ------------

            elif cmd == "schedule_monitor_status":
                sm = self._mc.get_schedule_monitor()
                return {"ok": True, "schedule_monitor": sm.get_status()}

            elif cmd == "schedule_monitor_restart":
                try:
                    self._mc.restart_schedule_monitor()
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                return {"ok": True, "schedule_monitor": self._mc.get_schedule_monitor().get_status()}

            elif cmd == "schedule_monitor_stop":
                self._mc.stop_schedule_monitor()
                return {"ok": True, "schedule_monitor": self._mc.get_schedule_monitor().get_status()}

            # -- network mode (client <-> AP) --------------------------------

            elif cmd == "set_network_mode":
                target = msg.get("mode")
                if target not in ("ap", "client"):
                    return {"ok": False, "error": "mode must be 'ap' or 'client'"}
                ack = {
                    "ok": True,
                    "mode": target,
                    "profile": my_ap_profile(),
                    "ap_address": AP_GATEWAY if target == "ap" else None,
                }
                # Defer the nmcli call — sending the ack happens synchronously
                # when we return from _dispatch, and activating the AP tears
                # down this socket. 0.5s is plenty for the TCP ack to flush.
                def _flip():
                    try:
                        result = network_mode.set_mode(target)
                        log.info("network mode flip to %s: %s", target, result)
                    except Exception:
                        log.exception("network mode flip to %s failed", target)
                threading.Timer(0.5, _flip).start()
                return ack

            else:
                return {"ok": False, "error": f"Unknown command: {cmd!r}"}

        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            log.exception("Error processing command %r", cmd)
            return {"ok": False, "error": f"Internal error: {exc}"}

    # -- sensor-driven recording --------------------------------------------

    def _on_sensor_tick(self, sensor_data, sample_pi_s, fw_ts_us, seq):
        """Called from the GPIO reader thread after each fresh sensor frame
        is cached. Records one CSV row per frame (bounded by the queue in
        PiRecorder itself) so recording runs at the sensor stream rate,
        not at broadcast cadence.

        ``sample_pi_s`` is the Pi-monotonic translation of the firmware's
        sample-capture ticks_us, so we ask mode_controller for the status
        as it was at *that* moment rather than now — otherwise the ~4.5 ms
        emit latency between INA capture and D-line receive shows up as
        step labels lagging reality by up to a full step at 100 Hz, which
        inverts the recorded labels relative to the actual current sign.

        ``fw_ts_us`` is the raw firmware ticks_us stamp (passed straight
        through to the CSV for offline reconstruction independent of the
        clock-offset estimate). ``seq`` is the monotonic 32-bit emit
        counter the firmware stamps on every D line — recorder uses it
        to detect gaps from USB CDC drops.

        ``sensor_data`` is the freshly-parsed dict but we pull full status
        via mode_controller to pick up mode / sequence / step / fet_states,
        which change independently of the sensor cadence.
        """
        if not self._recorder.is_recording:
            return
        try:
            status = self._mc.get_status_at(sample_pi_s)
            still_going = self._recorder.record(status, sample_pi_s, fw_ts_us, seq)
            if not still_going:
                log.info(
                    "Pi recording auto-stopped at %d samples",
                    self._recorder.sample_count,
                )
        except Exception:
            log.exception("recording tick failed")

    # -- subscription broadcast ---------------------------------------------

    def _broadcast_loop(self):
        """Push state snapshots to all subscribers when fresh sensor data arrives."""
        from time import monotonic
        last_broadcast = 0.0
        bcast_seq = 0
        while not self._stop_event.is_set():
            # Wait for fresh sensor data, but enforce minimum interval
            min_interval = 1.0 / self._broadcast_hz
            gpio = self._mc._gpio
            got_new = gpio.wait_for_new_sensor_data(timeout=min_interval)
            if self._stop_event.is_set():
                break

            # Throttle: skip if we broadcasted too recently
            now = monotonic()
            if now - last_broadcast < min_interval * 0.9:
                continue
            last_broadcast = now

            with self._sub_lock:
                if not self._subscribers:
                    continue
                subscribers = list(self._subscribers)

            # Pi-side recording is driven from _on_sensor_tick (reader
            # thread) at the full sensor rate — not here. The broadcast
            # loop is purely for pushing state to TUI subscribers.
            status = self._mc.get_status()
            bcast_seq += 1
            payload = {"event": "state", "t_emit_ns": time.monotonic_ns(),
                       "bcast_seq": bcast_seq, **status}

            dead = []
            line = json.dumps(payload, separators=(",", ":")) + "\n"
            line_bytes = line.encode("utf-8")
            for sock in subscribers:
                try:
                    # 200ms send timeout: tight enough to not stall the
                    # broadcast loop for other subscribers, loose enough
                    # that transient WiFi jitter doesn't murder healthy
                    # connections.
                    sock.settimeout(0.2)
                    sock.sendall(line_bytes)
                except socket.timeout:
                    log.warning(
                        "broadcast sendall timed out — dropping subscriber"
                    )
                    dead.append(sock)
                except (OSError, BrokenPipeError):
                    dead.append(sock)

            if dead:
                with self._sub_lock:
                    for sock in dead:
                        self._subscribers.discard(sock)
                        try:
                            sock.close()
                        except OSError:
                            pass

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _send_line(sock, obj):
        """Serialize a dict as a JSON line and send it."""
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        sock.sendall(line.encode("utf-8"))
