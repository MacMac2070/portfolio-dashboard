"""Categorisation: one Ollama call per new transaction, nothing else.

The model sees the raw description and the amount and returns one category
from CATEGORIES — no arithmetic, no free text. Forced-JSON output, temperature
zero, thinking off. If the Windows box is unreachable the answer is None and
the caller moves on: a pull must never fail because the categoriser is asleep,
and null categories are re-attempted on the next run (store.uncategorised).
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from . import config

log = logging.getLogger(__name__)

MODEL = "qwen3.5:9b"
TIMEOUT = 25

CATEGORIES = (
    "groceries", "transport", "dining", "subscriptions",
    "utilities", "shopping", "transfers", "other",
)

_PROMPT = (
    "Categorise one bank transaction. Reply with a single JSON object "
    '{{"category": "..."}} where category is exactly one of: {cats}. '
    "No other keys, no explanation.\n"
    "description: {description}\namount: {amount} GBP"
)


def categorise(raw_description: str, amount: float) -> str | None:
    """The category, or None when Ollama is off/unreachable/incoherent."""
    host = config.get("OLLAMA_HOST")
    if not host:
        return None
    if not host.startswith("http"):
        host = f"http://{host}"

    body = json.dumps({
        "model": MODEL,
        "messages": [{
            "role": "user",
            "content": _PROMPT.format(cats=", ".join(CATEGORIES),
                                      description=raw_description, amount=amount),
        }],
        "format": "json",
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
    }).encode()

    request = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read())
        answer = json.loads(payload["message"]["content"])
        category = str(answer.get("category", "")).strip().lower()
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        log.info("categorise: Ollama unavailable or incoherent (%s); leaving null", exc)
        return None

    # Only ever hand back a category from the list — an inventive model
    # answer is treated as no answer, not as a new category.
    return category if category in CATEGORIES else None
