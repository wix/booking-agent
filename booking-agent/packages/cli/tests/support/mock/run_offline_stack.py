#!/usr/bin/env python3
"""Start the multiplexed offline HTTP stack (registry + USP + UCP)."""
from __future__ import annotations

import argparse
import signal
import sys
import time

from support.mock.offline_stack import OfflineHttpStack


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline UCP demo HTTP stack")
    parser.add_argument("--port", type=int, default=0, help="Bind port (0 = ephemeral)")
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Block until SIGINT/SIGTERM (default: print port and exit)",
    )
    args = parser.parse_args()

    stack = OfflineHttpStack(port=args.port)
    port, base_url = stack.start()
    print(port, flush=True)
    print(base_url, file=sys.stderr, flush=True)

    if not args.foreground:
        stack.stop()
        return 0

    def _stop(_signum, _frame) -> None:
        stack.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    raise SystemExit(main())
