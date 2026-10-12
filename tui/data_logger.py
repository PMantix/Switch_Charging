"""
Switching Circuit V2 - Data Logger.

Two recording tiers:
  - Pi (≤ 10min): Records on Pi SD card at full stream rate, SCPs to Mac when done
  - Mac (> 10min): Records directly on Mac from TUI state stream

In simulation (``python -m tui --sim``) everything records on the Mac
tier, files are named SIM_*.csv, and every row carries a trailing
``source=SIMULATION`` column so simulated data can't pass for real.

Mac-tier CSV writes happen on a dedicated daemon thread so the UI
thread's _apply_state is never blocked by file I/O. Queue is unbounded;
at 15 Hz × ~120 B per row, a multi-minute disk stall is still only a
few MB. We log one warning if queue depth ever exceeds the threshold.
"""

import csv
import logging
import queue
import subprocess
import threading
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_LOG_DIR = Path.home() / "SwitchingCircuitV2_logs"
SIM_PREFIX = "SIM_"
SIM_SOURCE = "SIMULATION"
PI_MAX_SECONDS = 600  # 10 minutes
_DEPTH_WARN_THRESHOLD = 1000
_STOP_SENTINEL = object()
_STOP_JOIN_TIMEOUT = 5.0
_DURATION_CACHE = Path.home() / ".switching-circuit-duration"


class RecordTier(Enum):
    PI = "pi"
    MAC = "mac"


def select_tier(duration_s: float) -> RecordTier:
    if duration_s <= PI_MAX_SECONDS:
        return RecordTier.PI
    return RecordTier.MAC


class DataLogger:
    def __init__(self, log_dir: Path = DEFAULT_LOG_DIR, simulated: bool = False):
        self._log_dir = log_dir
        self._simulated = simulated
        self._tier: Optional[RecordTier] = None
        self._duration_s: float = self._load_duration()

        # Mac-side state
        self._path: Optional[Path] = None
        self._sample_count = 0
        self._max_samples = 0
        self._start_time: Optional[datetime] = None

        # Writer thread + queue (Mac tier)
        self._mac_active = False
        self._queue: "queue.Queue" = queue.Queue()
        self._writer_thread: Optional[threading.Thread] = None
        self._warned_backpressure = False

        # Pi recording state
        self._pi_recording = False
        self._pi_path: Optional[str] = None
        self._client = None

    @property
    def is_logging(self) -> bool:
        return self._mac_active or self._pi_recording

    @property
    def file_path(self) -> Optional[Path]:
        return self._path

    @property
    def sample_count(self) -> int:
        return self._sample_count

    @property
    def duration_s(self) -> float:
        return self._duration_s

    @duration_s.setter
    def duration_s(self, val: float):
        self._duration_s = max(1.0, val)
        self._save_duration(self._duration_s)

    @staticmethod
    def _load_duration() -> float:
        try:
            return float(_DURATION_CACHE.read_text().strip())
        except Exception:
            return 10.0

    @staticmethod
    def _save_duration(val: float) -> None:
        try:
            _DURATION_CACHE.write_text(str(val))
        except Exception:
            pass

    @property
    def tier(self) -> Optional[RecordTier]:
        return self._tier

    def tier_for(self, duration_s: float) -> RecordTier:
        """Tier a recording of this length would use. Simulation has no
        Pi to record on, so it is always the Mac tier."""
        return RecordTier.MAC if self._simulated else select_tier(duration_s)

    @property
    def elapsed(self) -> float:
        if self._start_time:
            return (datetime.now() - self._start_time).total_seconds()
        return 0.0

    def start(self, mode: str = "idle", freq: float = 1.0,
              seq: int = 0, sensor_hz: float = 2.0,
              client=None) -> tuple[RecordTier, str]:
        if self.is_logging:
            self.stop()

        self._client = client
        self._tier = self.tier_for(self._duration_s)
        self._start_time = datetime.now()
        self._sample_count = 0
        total_samples = int(self._duration_s * sensor_hz)

        if self._tier == RecordTier.PI:
            return self._start_pi(total_samples, mode, freq, seq, sensor_hz)
        else:
            return self._start_mac(total_samples, mode, freq, seq, sensor_hz)

    def _start_pi(self, count, mode, freq, seq, sensor_hz):
        if self._client:
            # Build expected filename (matches server/recorder.py naming)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            freq_str = f"{freq:.1f}Hz".replace(".", "p")
            sensor_str = f"{sensor_hz:.0f}sps"
            expected_name = f"pi_{mode}_seq{seq+1}_{freq_str}_{sensor_str}_{ts}.csv"
            expected_path = f"/home/pi/SwitchingCircuitV2_logs/{expected_name}"

            self._client.send_command({
                "cmd": "pi_record_start",
                "max_samples": count,
                "rec_mode": mode,
                "rec_freq": freq,
                "rec_seq": seq,
                "rec_sensor_hz": sensor_hz,
            })
            self._pi_recording = True
            self._pi_path = expected_path
            self._max_samples = count
            dur = self._duration_s
            return RecordTier.PI, f"Pi: {count} samples ({dur:.0f}s)"

        # Fallback to Mac
        self._tier = RecordTier.MAC
        return self._start_mac(count, mode, freq, seq, sensor_hz)

    def _start_mac(self, count, mode, freq, seq, sensor_hz):
        self._log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        freq_str = f"{freq:.1f}Hz".replace(".", "p")
        sensor_str = f"{sensor_hz:.0f}sps"
        prefix = SIM_PREFIX if self._simulated else ""
        self._path = self._log_dir / f"{prefix}{mode}_seq{seq+1}_{freq_str}_{sensor_str}_{ts}.csv"
        self._max_samples = count
        self._warned_backpressure = False
        self._queue = queue.Queue()
        self._mac_active = True
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            args=(self._path, self._queue),
            name="DataLogger-Writer",
            daemon=True,
        )
        self._writer_thread.start()
        return RecordTier.MAC, f"Mac: {count} samples -> {self._path.name}"

    def record(self, data: dict) -> bool:
        """Record one sample (Mac-side only). Returns False if auto-stopped.

        Producer-side only: builds the row and enqueues it. All file I/O
        happens on the writer thread.
        """
        if not self._mac_active:
            return True

        now = datetime.now()
        elapsed = (now - self._start_time).total_seconds() if self._start_time else 0.0
        sensors = data.get("sensors", {})

        def _sv(name, field):
            s = sensors.get(name, {})
            return s.get(field, 0.0) if isinstance(s, dict) and "error" not in s else 0.0

        fets = data.get("fet_states", [False] * 4)
        row = [
            now.isoformat(timespec="milliseconds"),
            f"{elapsed:.3f}",
            data.get("mode", ""), data.get("sequence", 0),
            data.get("step", 0), f"{data.get('frequency', 0.0):.2f}",
            int(fets[0]), int(fets[1]), int(fets[2]), int(fets[3]),
            f"{_sv('P1', 'voltage'):.6f}", f"{_sv('P1', 'current'):.8f}",
            f"{_sv('P2', 'voltage'):.6f}", f"{_sv('P2', 'current'):.8f}",
            f"{_sv('N1', 'voltage'):.6f}", f"{_sv('N1', 'current'):.8f}",
            f"{_sv('N2', 'voltage'):.6f}", f"{_sv('N2', 'current'):.8f}",
        ]
        if self._simulated:
            row.append(SIM_SOURCE)
        self._queue.put_nowait(row)
        self._sample_count += 1

        depth = self._queue.qsize()
        if depth > _DEPTH_WARN_THRESHOLD and not self._warned_backpressure:
            log.warning(
                "DataLogger writer backlog at %d rows — disk may be slow; "
                "no samples dropped", depth,
            )
            self._warned_backpressure = True

        if self._max_samples > 0 and self._sample_count >= self._max_samples:
            return False
        return True

    def check_pi_done(self) -> bool:
        """Check if Pi recording has finished (elapsed > duration)."""
        if self._tier != RecordTier.PI or not self._pi_recording:
            return False
        return self.elapsed >= self._duration_s + 1

    def stop(self) -> tuple[Optional[RecordTier], str]:
        tier = self._tier
        try:
            if tier == RecordTier.PI and self._pi_recording:
                desc = self._stop_pi()
            elif self._mac_active:
                desc = self._stop_mac()
            else:
                desc = "Not recording"
        except Exception as exc:
            log.warning("stop() failed: %s", exc)
            desc = f"Stop failed: {exc}"
        finally:
            self._tier = None
            self._pi_recording = False
            self._mac_active = False
            self._client = None
        return tier, desc

    def _stop_pi(self) -> str:
        # Use the actually-connected Pi IP (e.g. 10.42.0.1 in AP mode)
        # rather than `raspberrypi.local`, which may not resolve over a
        # local AP network.
        pi_host = "raspberrypi.local"
        if self._client is not None:
            host_attr = getattr(self._client, "host", "")
            if host_attr:
                pi_host = host_attr

        # Tell the Pi to stop recording.
        if self._client:
            try:
                self._client.send_command({"cmd": "pi_record_stop"})
            except Exception:
                pass

        # Brief pause to let Pi flush the CSV writer queue.
        import time
        time.sleep(0.5)

        ssh_target = f"pi@{pi_host}"
        ssh_opts = [
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=3",
            "-o", "BatchMode=yes",  # fail fast if auth would prompt
        ]

        # Find the most recent pi_*.csv on the Pi.
        try:
            result = subprocess.run(
                ["ssh", *ssh_opts, ssh_target,
                 "ls -t ~/SwitchingCircuitV2_logs/pi_*.csv 2>/dev/null | head -1"],
                capture_output=True, text=True, timeout=8,
            )
            remote_path = result.stdout.strip()
        except Exception:
            remote_path = self._pi_path

        if not remote_path:
            return "No data on Pi"

        # SCP from Pi to Mac.
        self._log_dir.mkdir(parents=True, exist_ok=True)
        local_name = Path(remote_path).name
        local_path = self._log_dir / local_name
        try:
            result = subprocess.run(
                ["scp", *ssh_opts, f"{ssh_target}:{remote_path}", str(local_path)],
                capture_output=True, timeout=20,
            )
            if result.returncode == 0:
                with open(local_path) as f:
                    samples = sum(1 for _ in f) - 1
                self._path = local_path
                self._sample_count = samples
                subprocess.run(
                    ["ssh", *ssh_opts, ssh_target, f"rm {remote_path}"],
                    capture_output=True, timeout=5,
                )
                return f"{samples} samples -> {local_name}"
            err = result.stderr.decode(errors="replace").strip()
            return f"SCP failed: {err}"
        except subprocess.TimeoutExpired:
            return "SCP timed out — check Pi reachability"
        except Exception as e:
            log.warning("SCP failed: %s", e)
            return f"Transfer failed: {e}"

    def _stop_mac(self) -> str:
        path = self._path
        count = self._sample_count
        thread = self._writer_thread
        self._writer_thread = None
        self._mac_active = False
        self._queue.put(_STOP_SENTINEL)
        if thread is not None:
            thread.join(timeout=_STOP_JOIN_TIMEOUT)
            if thread.is_alive():
                log.warning(
                    "DataLogger writer did not drain within %ss", _STOP_JOIN_TIMEOUT,
                )
        return f"{count} samples -> {path.name}" if path else "No data"

    def _writer_loop(self, path: Path, q: "queue.Queue") -> None:
        try:
            f = open(path, "w", newline="")
        except OSError as exc:
            log.error("DataLogger failed to open %s: %s", path, exc)
            while True:
                item = q.get()
                if item is _STOP_SENTINEL:
                    return
        try:
            w = csv.writer(f)
            header = [
                "timestamp", "elapsed_s",
                "mode", "sequence", "step", "frequency_hz",
                "p1_on", "p2_on", "n1_on", "n2_on",
                "p1_voltage", "p1_current_a",
                "p2_voltage", "p2_current_a",
                "n1_voltage", "n1_current_a",
                "n2_voltage", "n2_current_a",
            ]
            if self._simulated:
                # A column rather than a "# simulated" comment line, so
                # pd.read_csv in tools/ still parses the file unchanged.
                header.append("source")
            w.writerow(header)
            rows_since_flush = 0
            while True:
                item = q.get()
                if item is _STOP_SENTINEL:
                    break
                try:
                    w.writerow(item)
                except (OSError, ValueError) as exc:
                    log.warning("DataLogger write error (row dropped): %s", exc)
                    continue
                rows_since_flush += 1
                if rows_since_flush >= 50:
                    try:
                        f.flush()
                    except OSError as exc:
                        log.warning("DataLogger flush error: %s", exc)
                    rows_since_flush = 0
        finally:
            try:
                f.flush()
                f.close()
            except OSError:
                pass
