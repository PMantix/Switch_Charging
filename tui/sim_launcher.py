"""
Switching Circuit V2 - Simulation server launcher (``python -m tui --sim``).

Starts ``python -m server --sim`` as a child process on 127.0.0.1 so the
TUI can be demoed with no Raspberry Pi. The server needs Python >= 3.10
(stdlib only) while the TUI also runs on the Mac's system 3.9, so the
child uses this interpreter when it's new enough and otherwise the first
python3.1x found on PATH / Homebrew (override with SC_SIM_PYTHON).

The server's output goes to a log file (it would corrupt the TUI's
screen otherwise) and the child is always terminated when the TUI exits
— normally, on a crash (atexit), or by the server itself noticing its
parent is gone.
"""

import atexit
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SIM_HOST = "127.0.0.1"
DEFAULT_PORT = 5555
LOG_FILE = Path.home() / "SwitchingCircuitV2_logs" / "sim_server.log"
_STARTUP_TIMEOUT_S = 20.0
_MIN_SERVER_PY = (3, 10)
_PY_CANDIDATES = ("python3.13", "python3.12", "python3.11", "python3.10")
_PY_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


class SimLaunchError(RuntimeError):
    pass


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((SIM_HOST, port))
            return True
        except OSError:
            return False


def _py_ok(exe: str) -> bool:
    try:
        r = subprocess.run(
            [exe, "-c", f"import sys; sys.exit(sys.version_info < {_MIN_SERVER_PY})"],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def find_server_python() -> str:
    """Interpreter for the server child (needs Python >= 3.10)."""
    override = os.environ.get("SC_SIM_PYTHON")
    if override:
        if not _py_ok(override):
            raise SimLaunchError(f"SC_SIM_PYTHON={override} is not a Python >= 3.10")
        return override
    if sys.version_info >= _MIN_SERVER_PY:
        return sys.executable
    seen = set()
    for name in _PY_CANDIDATES:
        for exe in [shutil.which(name)] + [os.path.join(d, name) for d in _PY_DIRS]:
            if exe and exe not in seen and os.path.exists(exe):
                seen.add(exe)
                if _py_ok(exe):
                    return exe
    raise SimLaunchError(
        "Simulation mode runs the server, which needs Python >= 3.10, but "
        f"this is {sys.version.split()[0]} and no python3.10+ was found on "
        "PATH or in /opt/homebrew/bin. Install one (brew install "
        "python@3.11) or point SC_SIM_PYTHON at it."
    )


def _pick_port() -> int:
    """5555 when it's free (matches the real server), else any free port."""
    if _port_free(DEFAULT_PORT):
        return DEFAULT_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((SIM_HOST, 0))
        return s.getsockname()[1]


class SimServer:
    """Owns the simulated server subprocess."""

    def __init__(self, proc: subprocess.Popen, port: int, log_path: Path):
        self.proc = proc
        self.host = SIM_HOST
        self.port = port
        self.log_path = log_path

    @classmethod
    def start(cls, log_path: Path = LOG_FILE) -> "SimServer":
        python = find_server_python()
        port = _pick_port()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logf = open(log_path, "w")
        proc = subprocess.Popen(
            [python, "-m", "server", "--sim", "--port", str(port)],
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
        )
        logf.close()  # the child holds its own handle
        sim = cls(proc, port, log_path)
        atexit.register(sim.stop)
        try:
            sim._wait_ready()
        except Exception:
            sim.stop()
            raise
        return sim

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise SimLaunchError(
                    f"Simulated server exited with code {self.proc.returncode}.\n"
                    f"--- {self.log_path} (tail) ---\n{self._log_tail()}"
                )
            try:
                with socket.create_connection((self.host, self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.2)
        raise SimLaunchError(
            f"Simulated server did not start listening on {self.host}:{self.port} "
            f"within {_STARTUP_TIMEOUT_S:.0f}s. See {self.log_path}"
        )

    def _log_tail(self, lines: int = 15) -> str:
        try:
            return "\n".join(self.log_path.read_text().splitlines()[-lines:])
        except OSError:
            return "(log unavailable)"

    def stop(self) -> None:
        """Terminate the server (idempotent)."""
        proc: Optional[subprocess.Popen] = self.proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("Simulated server ignored SIGTERM — killing")
            proc.kill()
            proc.wait(timeout=5)
