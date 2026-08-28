"""Sandbox smoke test — build plan §8. Run this FIRST once OAuth is wired.

One pull cycle against the aggregator's sandbox, printing the raw account and
transaction payloads and what WOULD be inserted — without writing a row.
This is how the payload shape gets seen and trusted before cron_pull.py ever
touches real accounts.

    /opt/anaconda3/bin/python3 scripts/test_sandbox_pull.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api import cron_pull  # noqa: E402


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    print("— sandbox dry run: nothing will be written —")
    raise SystemExit(cron_pull.pull(dry_run=True))
