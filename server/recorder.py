"""
Switching Circuit V2 - Pi-side CSV Recorder.

Records sensor and circuit state data directly on the Pi filesystem
for medium-duration captures with better timing than Mac-side logging.

CSV writes happen on a dedicated daemon thread so the broadcast loop
(which drives circuit timing) is never blocked by file I/O. The queue
is unbounded; at 15 Hz × ~100 B per row, even minutes of disk stall
stay within a few MB of memory. If the queue depth ever exceeds
DEPTH_WARN_THRESHOLD we log one warning so backpressure is visible.
"""

from __future__ import annotations

import csv
import logging
import queue
import re
import threading
from datetime import datetime
from pathlib import Path
from time import monotonic

log = logging.getLogger(__name__)

DEFAULT_LOG_DIR = Path.home() / "SwitchingCircuitV2_logs"
_DEPTH_WARN_THRESHOLD = 1000  # queue depth at which we log a backpressure warning
_STOP_SENTINEL = object()
_STOP_JOIN_TIMEOUT = 5.0  # seconds to wait for writer to drain before warning
_LABEL_MAX = 40


def sanitize_label(label) -> str:
    """Make a user-typed run label safe for a filename on Linux and Windows.

    Spaces become underscores; anything outside [A-Za-z0-9._-] is dropped.
    Must match tui/data_logger.py's sanitize_label so both tiers agree.
    """
    if not label:
        return ""
    s = re.sub(r"\s+", "_", str(label).strip())
    s = re.sub(r"[^A-Za-z0-9._-]", "", s).strip("._-")
    return s[:_LABEL_MAX]


class PiRecorder:
    """Records state+sensor data to CSV on the Pi filesystem."""

    def __init__(self, log_dir: Path = DEFAULT_LOG_DIR):
        self._log_dir = log_dir
        self._path = None
        self._sample_count = 0
        self._max_samples = 0
        self._start_time = 0.0
        self._lock = threading.Lock()
        self._active = False

        # Step-boundary alignment. When set to an integer N, the recorder
        # discards frames until it has seen a step != N followed by a step
        # == N — i.e. a transition INTO step N — so every recording begins
        # at a consistent cycle phase. Useful for DOE analysis where the
        # first partial step would otherwise be garbage. Set to None to
        # disable and capture from the first frame.
        self._align_to_step: int | None = None
        self._saw_non_target = False

        # Writer thread + queue
        self._queue: "queue.Queue" = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._warned_backpressure = False

        # Firmware emit-sequence tracking. The firmware stamps every D line
        # with a monotonic 32-bit ``seq``; we log one warning per session
        # if we see a non-contiguous step (drop or out-of-order). One-shot
        # like ``_warned_backpressure`` so a chronic problem doesn't spam.
        self._last_seq: int | None = None
        self._warned_seq_gap = False

    @property
    def is_recording(self):
        return self._active

    @property
    def sample_count(self):
        return self._sample_count

    @property
    def max_samples(self):
        return self._max_samples

    @property
    def file_path(self):
        return self._path

    @property
    def log_dir(self) -> Path:
        return self._log_dir

    def start(self, max_samples: int, mode: str = "unknown",
              freq: float = 1.0, seq: int = 0, sensor_hz: float = 15.0,
              align_to_step: int | None = None, label: str = ""):
        """Start recording. Returns the file path.

        `align_to_step` (default 1) makes the recorder wait for a transition
        into the named step before committing the first sample — every
        recording starts at a consistent cycle phase. Pass None to disable
        and capture from the first frame.
        """
        # Make sure any prior writer thread has fully drained before we
        # re-open. stop() does this outside our setup lock.
        if self._active:
            self.stop()

        with self._lock:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            freq_str = f"{freq:.1f}Hz".replace(".", "p")
            sensor_str = f"{sensor_hz:.0f}sps"
            label = sanitize_label(label)
            label_str = f"{label}_" if label else ""
            self._path = self._log_dir / (
                f"pi_{label_str}{mode}_seq{seq+1}_{freq_str}_{sensor_str}_{ts}.csv"
            )

            self._sample_count = 0
            self._max_samples = max_samples
            self._start_time = monotonic()
            self._warned_backpressure = False
            self._last_seq = None
            self._warned_seq_gap = False
            self._align_to_step = align_to_step
            self._saw_non_target = False
            self._queue = queue.Queue()  # fresh queue per session
            self._active = True
            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                args=(self._path, self._queue),
                name="PiRecorder-Writer",
                daemon=True,
            )
            self._writer_thread.start()
            log.info("Pi recording started: %s (%d max samples)", self._path, max_samples)
            return str(self._path)

    def record(self, status: dict, sample_pi_s: float | None = None,
               fw_ticks_us: int | None = None, fw_seq: int | None = None):
        """Record one sample. Auto-stops when max_samples reached.
        Returns True if still recording, False if just stopped.

        ``sample_pi_s`` is the Pi-monotonic anchor for the row (translated
        from the firmware's sample-capture ticks_us via the clock-offset
        estimate). ``fw_ticks_us`` is the raw firmware stamp, written to
        the CSV so analysis can reconstruct timing without re-running the
        clock-offset arithmetic. ``fw_seq`` is the firmware emit counter
        — successive values let us spot USB CDC drops.

        Producer-side only: builds the row and enqueues it. Never blocks
        on I/O; the writer thread handles the actual file write.
        """
        with self._lock:
            if not self._active:
                return False

            step = status.get("step", 0)
            # Gate the first sample on a transition INTO align_to_step —
            # this guarantees every recording starts at the same cycle
            # phase, which makes cycle-aligned analysis straightforward.
            if self._align_to_step is not None:
                if step != self._align_to_step:
                    self._saw_non_target = True
                    return True
                if not self._saw_non_target:
                    # Happened to be on the target step when start() was
                    # called; wait until step leaves, then comes back.
                    return True
                # Transition into target step detected — begin recording
                # from this frame, reset elapsed clock so t=0 = first row.
                self._align_to_step = None
                self._start_time = monotonic()

            now = monotonic()
            recv_elapsed = now - self._start_time
            # Prefer the firmware-clock-anchored sample time as the canonical
            # elapsed_s — receipt time can lag sample time by ~4.5 ms (and
            # by much more if USB CDC bursts coalesce small frames). The
            # raw sample_pi_s and recv_elapsed are also written so anyone
            # debugging the timing chain has both anchors.
            sample_elapsed = (
                sample_pi_s - self._start_time
                if sample_pi_s is not None else recv_elapsed
            )
            mode = status.get("mode", "")
            seq = status.get("sequence", 0)
            freq = status.get("frequency", 0.0)
            fets = status.get("fet_states", [False] * 4)
            sensors = status.get("sensors", {})

            def _sv(name, field):
                s = sensors.get(name, {})
                return s.get(field, 0.0) if isinstance(s, dict) and "error" not in s else 0.0

            auto = status.get("auto", {})
            auto_step = auto.get("step_name", "") if auto else ""
            auto_detected = auto.get("detected_state", "") if auto else ""
            auto_match = auto.get("match", "") if auto else ""

            # Firmware-seq gap detection. Compare against the previous seq
            # we saw; any non-+1 step (after handling 32-bit wrap) means a
            # D line went missing on the wire. One-shot warning per session
            # — same gating pattern as backpressure so chronic gaps don't
            # flood the log.
            if fw_seq is not None and self._last_seq is not None:
                expected = (self._last_seq + 1) & 0xFFFFFFFF
                if fw_seq != expected and not self._warned_seq_gap:
                    log.warning(
                        "PiRecorder firmware seq gap: expected %d, got %d "
                        "(USB CDC drop or out-of-order); subsequent gaps "
                        "in this session will be silent",
                        expected, fw_seq,
                    )
                    self._warned_seq_gap = True
            if fw_seq is not None:
                self._last_seq = fw_seq

            row = [
                f"{sample_elapsed:.6f}",
                "" if sample_pi_s is None else f"{sample_pi_s:.6f}",
                "" if fw_ticks_us is None else fw_ticks_us,
                "" if fw_seq is None else fw_seq,
                f"{recv_elapsed:.6f}",
                mode, seq, step, f"{freq:.2f}",
                int(fets[0]), int(fets[1]), int(fets[2]), int(fets[3]),
                f"{_sv('P1', 'voltage'):.6f}", f"{_sv('P1', 'current'):.8f}",
                f"{_sv('P2', 'voltage'):.6f}", f"{_sv('P2', 'current'):.8f}",
                f"{_sv('N1', 'voltage'):.6f}", f"{_sv('N1', 'current'):.8f}",
                f"{_sv('N2', 'voltage'):.6f}", f"{_sv('N2', 'current'):.8f}",
                auto_step, auto_detected, auto_match,
            ]
            self._queue.put_nowait(row)
            self._sample_count += 1

            depth = self._queue.qsize()
            if depth > _DEPTH_WARN_THRESHOLD and not self._warned_backpressure:
                log.warning(
                    "PiRecorder writer backlog at %d rows — disk may be slow; "
                    "no samples dropped",
                    depth,
                )
                self._warned_backpressure = True

            hit_max = self._max_samples > 0 and self._sample_count >= self._max_samples
            if hit_max:
                # Signal writer to finish, but don't block the broadcast
                # loop on join(); stop() in the caller does that.
                self._active = False
                self._queue.put(_STOP_SENTINEL)
                return False
            return True

    def stop(self):
        """Stop recording: drain queue, close file, return path. Safe to
        call from any thread; blocks up to _STOP_JOIN_TIMEOUT on drain."""
        with self._lock:
            path = self._path
            thread = self._writer_thread
            was_active = self._active
            self._active = False
            self._writer_thread = None
            if was_active:
                self._queue.put(_STOP_SENTINEL)
            count = self._sample_count

        if thread is not None:
            thread.join(timeout=_STOP_JOIN_TIMEOUT)
            if thread.is_alive():
                log.warning(
                    "PiRecorder writer did not drain within %ss — rows may be queued",
                    _STOP_JOIN_TIMEOUT,
                )
            else:
                log.info("Pi recording stopped: %s (%d samples)", path, count)
        return str(path) if path else None

    def _writer_loop(self, path: Path, q: "queue.Queue") -> None:
        """Drain queue → CSV until stop sentinel. Runs on its own thread."""
        try:
            f = open(path, "w", newline="")
        except OSError as exc:
            log.error("PiRecorder failed to open %s: %s", path, exc)
            # Drain queue so producers don't see false backpressure if the
            # open failed after records were already queued.
            while True:
                item = q.get()
                if item is _STOP_SENTINEL:
                    return
        try:
            w = csv.writer(f)
            w.writerow([
                "elapsed_s",
                "sample_pi_s", "fw_ticks_us", "fw_seq",
                "recv_elapsed_s",
                "mode", "sequence", "step", "frequency_hz",
                "p1_on", "p2_on", "n1_on", "n2_on",
                "p1_voltage", "p1_current_a",
                "p2_voltage", "p2_current_a",
                "n1_voltage", "n1_current_a",
                "n2_voltage", "n2_current_a",
                "auto_step", "auto_detected_state", "auto_match",
            ])
            rows_since_flush = 0
            while True:
                item = q.get()
                if item is _STOP_SENTINEL:
                    break
                try:
                    w.writerow(item)
                except (OSError, ValueError) as exc:
                    log.warning("PiRecorder write error (row dropped): %s", exc)
                    continue
                rows_since_flush += 1
                if rows_since_flush >= 50:
                    try:
                        f.flush()
                    except OSError as exc:
                        log.warning("PiRecorder flush error: %s", exc)
                    rows_since_flush = 0
        finally:
            try:
                f.flush()
                f.close()
            except OSError:
                pass
