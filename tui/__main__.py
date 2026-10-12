"""
Switching Circuit V2 - TUI Entry Point.

Usage:
    python -m tui                          # auto-discover; Connect dialog lists
                                           # visible pi_SW# APs or takes an IP
    python -m tui --host 192.168.1.100     # connect directly
    python -m tui --host 192.168.1.100 --port 5555
    python -m tui --log-level DEBUG        # verbose logging (default WARNING)
    python -m tui --sim                    # simulation: no Pi needed — starts a
                                           # local simulated server (runs under a
                                           # Python >= 3.10 found on PATH; log in
                                           # ~/SwitchingCircuitV2_logs/sim_server.log)
    python -m tui --coincell               # coin-cell mode: AVG=128, uA plot,
                                           # mA-scale auto-follow thresholds
                                           # (combines with --sim; u toggles it
                                           # at runtime too)

Press ? in the app for the full key reference.
"""

import argparse
import logging
import sys
import time

from tui.app import SwitchingCircuitApp


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Switching Circuit V2 - TUI Client"
    )
    parser.add_argument(
        "--host",
        default="",
        help="Raspberry Pi IP address (prompts if omitted)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5555,
        help="Server port (default: 5555)",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: WARNING)",
    )
    parser.add_argument(
        "--sim",
        action="store_true",
        help="Simulation mode: run against a local simulated server with "
             "synthetic data (no Raspberry Pi / hardware)",
    )
    parser.add_argument(
        "--coincell",
        action="store_true",
        help="Coin-cell mode: heavy INA226 averaging, currents shown in uA, "
             "mA-scale auto-follow thresholds, z key zeroes current offsets",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.sim:
        _run_sim(coincell=args.coincell)
        return

    app = SwitchingCircuitApp(host=args.host, port=args.port, coincell=args.coincell)
    try:
        app.run()
    finally:
        app.leave_coincell()


def _run_sim(coincell: bool = False) -> None:
    from tui.sim_launcher import SimServer, SimLaunchError

    print("Starting simulated server (no hardware)...", flush=True)
    try:
        sim = SimServer.start()
    except SimLaunchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    app = None
    try:
        app = SwitchingCircuitApp(host=sim.host, port=sim.port, sim=True,
                                  coincell=coincell)
        app.run()
    finally:
        if app is not None and app.leave_coincell():
            # The sim server debounces its state-file write (0.5 s); let it
            # land so the next --sim launch starts from normal settings.
            time.sleep(0.7)
        # Drop the client first so its receive thread doesn't report the
        # server going away on the now-restored terminal.
        if app is not None and app._client is not None:
            app._client.disconnect()
        sim.stop()


if __name__ == "__main__":
    main()
