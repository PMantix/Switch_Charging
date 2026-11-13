"""
Switching Circuit V2 - Data Logger.

Two recording tiers:
  - Pi (≤ 10min): Records on Pi SD card at full stream rate, copied to this
    computer over the command socket when done (tui/pi_files.py)
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
import re
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
_LABEL_CACHE = Path.home() / ".switching-circuit-label"
_LABEL_MAX = 40


def sanitize_label(label) -> str:
    """Make a user-typed run label safe for a filename on Windows, macOS
    and Linux. Must match server/recorder.py's sanitize_label."""
    if not label:
        return ""
    s = re.sub(r"\s+", "_", str(label).strip())
    s = re.sub(r"[^A-Za-z0-9._-]", "", s).strip("._-")
    return s[:_LABEL_MAX]


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
        self._label: str = self._load_label()

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

    @property
    def label(self) -> str:
        """Run label prefixed to recording filenames ("" for none)."""
        return self._label

    @label.setter
    def label(self, val: str):
        self._label = sanitize_label(val)
        try:
            _LABEL_CACHE.write_text(self._label)
        except Exception:
            pass

    @staticmethod
    def _load_label() -> str:
        try:
            return sanitize_label(_LABEL_CACHE.read_text())
        except Exception:
            return ""

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
            # The main client is subscribed, so its reply doesn't carry
            # the path; _stop_pi gets the real path from pi_record_stop.
            self._client.send_command({
                "cmd": "pi_record_start",
                "max_samples": count,
                "rec_mode": mode,
                "rec_freq": freq,
                "rec_seq": seq,
                "rec_sensor_hz": sensor_hz,
                "rec_label": self._label,  # ignored by pre-label servers
            })
            self._pi_recording = True
            self._pi_path = None
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
        if self._label:
            prefix += f"{self._label}_"
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
        return RecordTier.MAC, f"Mac: {self._duration_s:.0f}s -> {self._path}"

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

        # Stop on wall-clock time, not sample count: Mac-tier rows arrive
        # at the TUI's broadcast rate (<= 30 Hz), not the sensor rate the
        # sample budget was computed from, so a count cap made long
        # recordings run far past the requested duration.
        if elapsed >= self._duration_s:
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
        pi_port = 5555
        if self._client is not None:
            pi_host = getattr(self._client, "host", "") or pi_host
            pi_port = getattr(self._client, "port", 0) or pi_port

        from tui.pi_files import PiFiles, PiFilesError, ServerTooOld

        # Stop and copy over a side connection: its pi_record_stop reply
        # carries the real file path (the subscribed main client's doesn't).
        # pi_record_stop drains the Pi's writer before replying, so the
        # file is complete by the time we read it.
        try:
            with PiFiles(pi_host, pi_port) as pf:
                resp = pf.call({"cmd": "pi_record_stop"})
                remote = resp.get("path") or ""
                name = Path(remote.replace("\\", "/")).name if remote else ""
                if not name:
                    return "No data on Pi"
                try:
                    local_path = pf.fetch(name, self._log_dir)
                except ServerTooOld:
                    return self._copy_via_ssh(pi_host, remote)
                with open(local_path, newline="") as f:
                    samples = max(0, sum(1 for _ in f) - 1)
                self._path = local_path
                self._sample_count = samples
                pf.delete(name)
                return f"{samples} samples -> {local_path}"
        except PiFilesError as exc:
            log.warning("Pi copy failed: %s", exc)
            return f"Copy failed: {exc} (file left on Pi; press O to retry)"

    def _copy_via_ssh(self, pi_host: str, remote_path: str) -> str:
        """Legacy copy for Pi servers without read_recording. Needs ssh/scp
        and key auth for pi@host; fails fast (BatchMode) otherwise."""
        ssh_target = f"pi@{pi_host}"
        ssh_opts = [
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=3",
            "-o", "BatchMode=yes",  # fail fast if auth would prompt
        ]
        self._log_dir.mkdir(parents=True, exist_ok=True)
        local_name = Path(remote_path).name
        local_path = self._log_dir / local_name
        try:
            result = subprocess.run(
                ["scp", *ssh_opts, f"{ssh_target}:{remote_path}", str(local_path)],
                capture_output=True, timeout=60,
            )
            if result.returncode == 0:
                with open(local_path, newline="") as f:
                    samples = max(0, sum(1 for _ in f) - 1)
                self._path = local_path
                self._sample_count = samples
                subprocess.run(
                    ["ssh", *ssh_opts, ssh_target, f"rm {remote_path}"],
                    capture_output=True, timeout=10,
                )
                return f"{samples} samples -> {local_name}"
            err = result.stderr.decode(errors="replace").strip()
            return f"SCP failed: {err}"
        except subprocess.TimeoutExpired:
            return "SCP timed out — check Pi reachability"
        except Exception as e:
            log.warning("SCP failed: %s", e)
            return f"Transfer failed: {e} (redeploy server/ to copy without ssh)"

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
        return f"{count} samples -> {path}" if path else "No data"

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
