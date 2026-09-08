"""One logging setup for the two long-lived processes.

serve.py and refresh.py used to log to stdout and let launchd append that to a
file forever: serve.log reached 2.4 MB in four days, most of it one provider
refusing a connection every thirty seconds. Each process now owns a rotating
file under logs/, and launchd's StandardOutPath is left for what only it can
catch — an interpreter that dies before logging starts.

The stream handler is added only at a terminal, so a hand run still prints
and a LaunchAgent run does not write every line twice.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Loggers that say the same thing many times a minute while a dependency is
# down. The dashboard's own modules already log one line per failed attempt.
QUIET = {
    "ib_async.client": logging.CRITICAL,
    "ib_async.wrapper": logging.ERROR,
    "yfinance": logging.CRITICAL,       # its ERROR lines are re-raised to callers anyway
    "peewee": logging.WARNING,
    "urllib3": logging.WARNING,
}


def configure(name: str, *, max_bytes: int = 5 * 1024 * 1024, backups: int = 5,
              level: int = logging.INFO) -> Path:
    """Route the root logger to logs/<name>.log, rotated. Returns the path."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{name}.log"
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter(FORMAT)
    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    if sys.stderr.isatty():
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)
    for logger_name, quiet_level in QUIET.items():
        logging.getLogger(logger_name).setLevel(quiet_level)
    return path
