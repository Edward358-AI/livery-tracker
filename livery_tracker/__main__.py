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
    parser.add_argument(
        "--update", action="store_true", help="check for and install the latest release"
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

    if args.update:
        from . import __version__, updater

        available = updater.check_for_update()
        if available is None:
            print(f"Already up to date (v{__version__}).")
            return
        print(f"Updating v{__version__} -> {available.tag} ...")
        if updater.apply_update(available):
            print("Update installed. Restart the tracker to run the new version.")
        else:
            print("Update failed — current version left untouched (see log output).")
            sys.exit(1)
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

    # Exit code 42 tells the supervisor (runner script / NSSM / systemd)
    # to start us again — used after a self-update installs new code.
    sys.exit(run(creds))


if __name__ == "__main__":
    main()
