"""CLI entrypoint: python -m livery_tracker [--setup | --harvest-now]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import load_credentials
from .setup_wizard import run_wizard


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="livery_tracker",
        description="Aircraft Livery Tracker — free flight watcher for special liveries.",
    )
    parser.add_argument("--setup", action="store_true", help="run the interactive setup wizard")
    parser.add_argument(
        "--harvest-now", action="store_true", help="run one schedule harvest and exit"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.setup:
        run_wizard()
        return

    creds = load_credentials()
    if creds is None:
        run_wizard()
        creds = load_credentials()
        if creds is None:
            print("Setup did not complete — exiting.")
            sys.exit(1)

    from .app import harvest_once, run  # deferred: telegram import is slow

    if args.harvest_now:
        count = asyncio.run(harvest_once(creds))
        print(f"Harvest finished: {count} new flight leg(s).")
        return

    run(creds)


if __name__ == "__main__":
    main()
