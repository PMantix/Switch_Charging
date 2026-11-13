"""
Switching Circuit V2 - Pi recording transfer over the command socket.

Copies pi_*.csv recordings off the Pi using the server's
list_recordings / read_recording / delete_recording commands, so the
client needs no ssh/scp (Windows laptops typically have neither keys
nor a working scp setup for the Pi).

Uses its own short-lived TCP connection rather than the TUI's main
PiClient: once the main client subscribes, command replies on that
socket are swallowed by the state-stream recv loop.

Everything here is blocking; call it from a worker thread.
"""

from __future__ import annotations

import base64
import json
import os
import socket
from pathlib import Path
from typing import Callable, Optional

_CHUNK = 256 * 1024
_TIMEOUT_S = 10.0


class PiFilesError(Exception):
    """Transfer failed; message is fit to show the user."""


class ServerTooOld(PiFilesError):
    """Pi server predates the recording-transfer commands."""


class PiFiles:
    def __init__(self, host: str, port: int = 5555, timeout: float = _TIMEOUT_S):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._rfile = None

    def __enter__(self) -> "PiFiles":
        try:
            self._sock = socket.create_connection((self._host, self._port), timeout=self._timeout)
        except OSError as exc:
            raise PiFilesError(f"Cannot reach Pi at {self._host}:{self._port}: {exc}") from exc
        self._sock.settimeout(self._timeout)
        self._rfile = self._sock.makefile("r", encoding="utf-8", newline="\n")
        return self

    def __exit__(self, *exc) -> None:
        for obj in (self._rfile, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except OSError:
                pass
        self._sock = self._rfile = None

    def _call(self, msg: dict) -> dict:
        if self._sock is None:
            raise PiFilesError("Not connected")
        try:
            self._sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            line = self._rfile.readline()
        except (OSError, socket.timeout) as exc:
            raise PiFilesError(f"Connection to Pi failed: {exc}") from exc
        if not line:
            raise PiFilesError("Pi closed the connection")
        try:
            resp = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PiFilesError(f"Bad reply from Pi: {exc}") from exc
        if not resp.get("ok"):
            err = str(resp.get("error", "unknown error"))
            if err.startswith("Unknown command"):
                raise ServerTooOld(
                    "Pi server is too old for file transfer; redeploy server/ to this Pi"
                )
            raise PiFilesError(err)
        return resp

    # -- commands -----------------------------------------------------------

    def call(self, msg: dict) -> dict:
        """Send any server command on this connection and return the reply."""
        return self._call(msg)

    def list(self) -> list[dict]:
        """Recordings on the Pi, newest first: {name, size, mtime, recording}."""
        return list(self._call({"cmd": "list_recordings"}).get("files", []))

    def fetch(self, name: str, dest_dir: Path,
              progress: Optional[Callable[[int, int], None]] = None) -> Path:
        """Copy one recording into dest_dir. Writes to a .part file and
        renames on success, so a failed copy never leaves a truncated CSV."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        final = dest_dir / name
        part = dest_dir / (name + ".part")
        offset = 0
        size = 0
        try:
            with open(part, "wb") as f:
                while True:
                    resp = self._call({"cmd": "read_recording", "name": name,
                                       "offset": offset, "length": _CHUNK})
                    data = base64.b64decode(resp.get("data", ""))
                    size = int(resp.get("size", 0))
                    f.write(data)
                    offset += len(data)
                    if progress:
                        progress(offset, size)
                    if resp.get("eof") or not data:
                        break
            if offset < size:
                raise PiFilesError(f"{name}: got {offset} of {size} bytes")
            os.replace(part, final)
        except BaseException:
            try:
                part.unlink()
            except OSError:
                pass
            raise
        return final

    def delete(self, name: str) -> None:
        self._call({"cmd": "delete_recording", "name": name})
