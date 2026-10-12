"""
Switching Circuit V2 - TUI Entry Point.

Usage:
    python -m tui                          # auto-discover; Connect dialog lists
                                           # visible pi_SW# APs or takes an IP
    python -m tui --host 192.168.1.100     # connect directly
    python -m tui --host 192.168.1.100 --port 5555
    python -m tui --log-level DEBUG        # verbose logging (default WARNING)

Press ? in the app for the full key reference.
"""

import argparse
import logging

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
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    app = SwitchingCircuitApp(host=args.host, port=args.port)
    app.run()


if __name__ == "__main__":
    main()
