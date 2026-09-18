"""
Switching Circuit V2 - Boot-time static-IP enforcement for eth0.

The Ethernet fleet uses static IPs (see Pi_information.md) so the TUI's
Ethernet picker can reach every board simultaneously without DHCP. The
NetworkManager connection profile carrying that static config has been
observed to occasionally vanish and get replaced by a fresh DHCP-default
profile between boots on some boards — root cause unconfirmed (journald
on these images is volatile, so the triggering event was never captured
in logs). Rather than chase that further, this enforces convergence on
every boot: if eth0 isn't on the expected static IP, reapply it to
whatever connection profile is currently active on the interface.

Runs as a systemd oneshot after NetworkManager.service, mirroring
ap_fallback.service. Ordered before switching-circuit.service so the
server always starts with eth0 already on its correct address.
"""

import logging
import subprocess
import sys
from typing import Optional

from server.fleet import my_static_eth_ip

log = logging.getLogger("eth_static")

NMCLI = "/usr/bin/nmcli"
GATEWAY = "192.168.137.1"
DNS = "192.168.137.1"
IFACE = "eth0"


def _active_eth0_profile() -> Optional[str]:
    result = subprocess.run(
        [NMCLI, "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
        capture_output=True, text=True, timeout=5,
    )
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == IFACE:
            return parts[0]
    return None


def _current_ip() -> Optional[str]:
    result = subprocess.run(
        [NMCLI, "-t", "-f", "IP4.ADDRESS", "device", "show", IFACE],
        capture_output=True, text=True, timeout=5,
    )
    for line in result.stdout.splitlines():
        if line.startswith("IP4.ADDRESS"):
            return line.split(":", 1)[1].split("/")[0]
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    expected = my_static_eth_ip()
    if not expected:
        log.info("no static IP configured for this board (my_static_eth_ip() is None), skipping")
        return 0

    current = _current_ip()
    if current == expected:
        log.info("eth0 already on expected static IP %s", expected)
        return 0

    profile = _active_eth0_profile()
    if not profile:
        log.error("eth0 is on %s (expected %s) but no active connection profile was found — cannot fix", current, expected)
        return 1

    log.warning(
        "eth0 is on %s, expected %s — profile %r has drifted from static config, reapplying",
        current, expected, profile,
    )
    modify = subprocess.run(
        [NMCLI, "connection", "modify", profile,
         "ipv4.method", "manual",
         "ipv4.addresses", f"{expected}/24",
         "ipv4.gateway", GATEWAY,
         "ipv4.dns", DNS],
        capture_output=True, text=True, timeout=10,
    )
    if modify.returncode != 0:
        log.error("nmcli connection modify failed: %s", modify.stderr.strip())
        return 1

    up = subprocess.run([NMCLI, "connection", "up", profile], capture_output=True, text=True, timeout=15)
    if up.returncode != 0:
        log.error("nmcli connection up failed: %s", up.stderr.strip())
        return 1

    log.info("reapplied static IP %s to profile %r", expected, profile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
