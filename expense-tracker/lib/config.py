"""Config for the expense tracker — its own env file, not the dashboard's.

Reads KEY=VALUE lines from `.env.expense-tracker` at the module root (copy
`.env.expense-tracker.example` and fill it in; the real file is gitignored).
Process environment wins over the file, so a one-off override needs no edit.
stdlib only, like everything in this module.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env.expense-tracker"


def _parse(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split("#", 1)[0].strip().strip("'\"")
        if value:
            out[key.strip()] = value
    return out


_FILE = _parse(ENV_PATH)


def get(key: str, default: str | None = None) -> str | None:
    """Environment first, then the env file, then the default."""
    return os.environ.get(key) or _FILE.get(key) or default


def require(key: str) -> str:
    value = get(key)
    if not value:
        raise RuntimeError(
            f"{key} is not set — copy .env.expense-tracker.example to "
            f"{ENV_PATH.name} and fill it in")
    return value
