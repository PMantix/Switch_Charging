"""
Switching Circuit V2 - Ethernet fleet probe (laptop side).

Unlike wifi_scan.py's one-AP-at-a-time model, Pis reachable over the
shared Ethernet switch (static IPs — see Pi_information.md) are all up
simultaneously. This just TCP-probes each known host; no WiFi join step
is needed to switch between them.
"""

from __future__ import annotations

import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

PORT = 5555
PROBE_TIMEOUT = 1.0

# Static IPs currently assigned on the shared Ethernet switch subnet.
# Update as boards are added/reassigned — see the "Ethernet fleet
# networking" section of Pi_information.md for how these get set.
KNOWN_ETH_HOSTS: list[tuple[str, str]] = [
    ("pi-SW6", "192.168.137.104"),
    ("pi-SW7", "192.168.137.101"),
    ("pi-SW8", "192.168.137.102"),
    ("pi-SW9", "192.168.137.103"),
]


@dataclass(frozen=True)
class EthPi:
    label: str
    ip: str
    latency_ms: Optional[float]
    online: bool


def _probe(label: str, ip: str) -> EthPi:
    start = time.monotonic()
    try:
        with socket.create_connection((ip, PORT), timeout=PROBE_TIMEOUT):
            return EthPi(label=label, ip=ip,
                         latency_ms=(time.monotonic() - start) * 1000.0,
                         online=True)
    except OSError:
        return EthPi(label=label, ip=ip, latency_ms=None, online=False)


def scan_eth_pis() -> list[EthPi]:
    """Probe all known static Ethernet hosts in parallel, sorted by label."""
    if not KNOWN_ETH_HOSTS:
        return []
    with ThreadPoolExecutor(max_workers=len(KNOWN_ETH_HOSTS)) as ex:
        futures = [ex.submit(_probe, label, ip) for label, ip in KNOWN_ETH_HOSTS]
        results = [f.result() for f in as_completed(futures)]
    results.sort(key=lambda p: p.label)
    return results
