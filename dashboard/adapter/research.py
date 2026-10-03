"""The Research briefing: findings written by the research co-pilot, read only.

The co-pilot (a set of scheduled agents, not built yet) writes one JSON
document, data/research_findings.json. This module reads it, checks its shape
and returns what /api/research serves. Nothing here writes. The agents own
the file and replace it whole with store.write_json (a temp file in the same
directory, fsync, then os.replace), so a reader never sees half a document.

Until the agents exist, data/research_findings.sample.json stands in. It is
served only while the real file is absent, and it says so (meta.is_sample is
true and meta.served_from is "sample") so the page can show a banner. Once
the real file exists the sample is never read, even when the real file is
broken: a broken run has to look broken, not quietly turn into made-up
findings.

Schema, version 1. Unknown fields anywhere are ignored.

    {
      "meta": {
        "generated_at": "ISO-8601",       when the run finished
        "run_id": "string",
        "status": "ok | partial | failed",
        "next_run_at": "ISO-8601 or null",
        "held_count": 15,                 holdings reviewed this run
        "watched_count": 22,              watchlist names scanned this run
        "is_sample": false,
        "errors": ["readable strings"],
        "last_finding_at": "ISO-8601"     optional; newest real finding, for the empty state
      },
      "findings": [{
        "id": "stable string",            the same story keeps the same id across runs
        "ticker": "INTC",                 universe key, as used by #stock/<key>
        "tier": "held | watchlist",
        "sector": "Semiconductors",
        "urgency": "high | medium | low | quiet",
        "headline": "one line",
        "why_it_matters": "one or two plain sentences",
        "bull": ["short points"],
        "bear": ["short points"],
        "risk_views": {"aggressive": "text", "conservative": "text", "neutral": "text"},
        "evidence": [{"label": "Form 4", "detail": "text", "url": "https://...",
                      "kind": "filing | call | 13f | news | sector | archive"}],
        "pillars_touched": [{"pillar": "Turnaround", "change": "weaker | steady | stronger"}],
        "first_seen": "ISO-8601",
        "changed_since_last_run": true
      }],
      "watchlist_triage": [{"ticker": "NVDA", "signals_flag": true, "narrative_flag": false,
                            "escalated": true, "note": "short"}],
      "runs": [{"run_id": "string", "at": "ISO-8601", "status": "ok | partial | failed",
                "agents_used": 150, "skipped": 0, "failed": 0}]
    }

What build() adds to meta on the way out:

    served_from     "live" (the real file), "sample", or "none" (neither file exists)
    error           one readable string when the document could not be used, else null;
                    the file's own messages stay in errors[]
    schema_version  1
    dropped         findings discarded for a missing id, ticker or headline, or a repeated id
    counts          totals per urgency and per tier, taken before any filter
    filters         the urgency, tier and ticker filters applied to findings

status is null only when served_from is "none". A document that cannot be
parsed, or whose top level is not an object, is served as status "failed"
with no findings.

A live file that flags itself is_sample keeps the flag, and the page labels
it as sample data: a wrong banner costs less than a missing one.

The findings describe what happened, why it may matter and what the evidence
is. They are information, never advice: no recommendations, ratings or price
targets.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RESEARCH_PATH = DATA_DIR / "research_findings.json"
SAMPLE_PATH = DATA_DIR / "research_findings.sample.json"

SCHEMA_VERSION = 1
URGENCY = ("high", "medium", "low", "quiet")    # also the ranking order
TIERS = ("held", "watchlist")
KINDS = ("filing", "call", "13f", "news", "sector", "archive")
CHANGES = ("weaker", "steady", "stronger")
STATUSES = ("ok", "partial", "failed")
RISK_VIEWS = ("aggressive", "conservative", "neutral")

# Caps keep one runaway agent from flooding the page. They are generous
# enough that real copy never meets them.
MAX_SHORT = 120      # ids, tickers, labels, sectors, pillar names
MAX_LINE = 300       # headlines, notes, evidence detail, error lines
MAX_TEXT = 1200      # why_it_matters and the risk views
MAX_URL = 2000
MAX_POINTS = 12      # bull, bear, evidence and pillars per finding
MAX_FINDINGS = 500
MAX_ROWS = 200       # triage rows and runs


# ---------------------------------------------------------------- reading

def load() -> tuple[dict | list | None, str, str | None]:
    """(document, served_from, error). Never raises.

    The real file wins whenever it exists, readable or not. The sample is
    read only when the real file is absent.
    """
    if RESEARCH_PATH.exists():
        doc, error = _read(RESEARCH_PATH)
        return doc, "live", error
    if SAMPLE_PATH.exists():
        doc, error = _read(SAMPLE_PATH)
        return doc, "sample", error
    return None, "none", None


def _read(path: Path) -> tuple[dict | list | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as exc:
        return None, (f"{path.name} is not valid JSON "
                      f"(line {exc.lineno}, column {exc.colno}: {exc.msg})")
    except UnicodeDecodeError:
        return None, f"{path.name} is not UTF-8 text"
    except OSError as exc:
        return None, f"{path.name} could not be read: {exc.strerror or exc}"


# ---------------------------------------------------------------- shape

def _text(value, cap: int) -> str:
    """A trimmed single-spaced string, or "" for anything that is not text."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def _enum(value, allowed: tuple[str, ...], default=None):
    word = value.strip().lower() if isinstance(value, str) else ""
    return word if word in allowed else default


def _flag(value) -> bool:
    return value is True


def _count(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _when(value) -> str | None:
    """An ISO-8601 timestamp with an offset (UTC assumed when absent), or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw[-1] in "Zz":                     # fromisoformat takes "Z" but not "z"
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()


def _url(value) -> str | None:
    """An absolute http(s) link, or None. Anything else never reaches an href.

    Quotes, angle brackets and backticks are not legal in a URL unencoded, so a
    link carrying one is refused outright rather than trusted to the page's
    escaping.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > MAX_URL or any(ch <= " " or ch in '\x7f"<>`' for ch in raw):
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return raw


def _points(value, cap: int = MAX_LINE) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [_text(item, cap) for item in value[:MAX_POINTS]]
    return [item for item in out if item]


def _evidence(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for raw in value[:MAX_POINTS]:
        if not isinstance(raw, dict):
            continue
        item = {"label": _text(raw.get("label"), MAX_SHORT),
                "detail": _text(raw.get("detail"), MAX_LINE),
                "url": _url(raw.get("url")),
                "kind": _enum(raw.get("kind"), KINDS)}
        if item["label"] or item["detail"]:
            out.append(item)
    return out


def _pillars(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for raw in value[:MAX_POINTS]:
        if not isinstance(raw, dict):
            continue
        pillar = _text(raw.get("pillar"), MAX_SHORT)
        if pillar:
            out.append({"pillar": pillar, "change": _enum(raw.get("change"), CHANGES)})
    return out


def _finding(raw) -> dict | None:
    """One finding reduced to the schema, or None when it cannot be shown."""
    if not isinstance(raw, dict):
        return None
    fid = _text(raw.get("id"), MAX_SHORT)
    ticker = _text(raw.get("ticker"), MAX_SHORT)
    headline = _text(raw.get("headline"), MAX_LINE)
    if not (fid and ticker and headline):
        return None
    views = raw.get("risk_views") if isinstance(raw.get("risk_views"), dict) else {}
    return {
        "id": fid,
        "ticker": ticker,
        "tier": _enum(raw.get("tier"), TIERS),
        "sector": _text(raw.get("sector"), MAX_SHORT),
        "urgency": _enum(raw.get("urgency"), URGENCY, "low"),
        "headline": headline,
        "why_it_matters": _text(raw.get("why_it_matters"), MAX_TEXT),
        "bull": _points(raw.get("bull")),
        "bear": _points(raw.get("bear")),
        "risk_views": {name: _text(views.get(name), MAX_TEXT) for name in RISK_VIEWS},
        "evidence": _evidence(raw.get("evidence")),
        "pillars_touched": _pillars(raw.get("pillars_touched")),
        "first_seen": _when(raw.get("first_seen")),
        "changed_since_last_run": _flag(raw.get("changed_since_last_run")),
    }


def _triage(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    ticker = _text(raw.get("ticker"), MAX_SHORT)
    if not ticker:
        return None
    return {"ticker": ticker,
            "signals_flag": _flag(raw.get("signals_flag")),
            "narrative_flag": _flag(raw.get("narrative_flag")),
            "escalated": _flag(raw.get("escalated")),
            "note": _text(raw.get("note"), MAX_LINE)}


def _run(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    run = {"run_id": _text(raw.get("run_id"), MAX_SHORT) or None,
           "at": _when(raw.get("at")),
           "status": _enum(raw.get("status"), STATUSES),
           "agents_used": _count(raw.get("agents_used")),
           "skipped": _count(raw.get("skipped")),
           "failed": _count(raw.get("failed"))}
    return run if run["run_id"] or run["at"] else None


def _epoch(stamp: str | None) -> float | None:
    return datetime.fromisoformat(stamp).timestamp() if stamp else None


def _rank(finding: dict) -> tuple:
    """Most urgent first, then what changed, then the newest."""
    seen = _epoch(finding["first_seen"])
    return (URGENCY.index(finding["urgency"]),
            not finding["changed_since_last_run"],
            seen is None, -(seen or 0.0))


def _rows(value, shape) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [row for row in map(shape, value[:MAX_ROWS]) if row is not None]


def empty() -> dict:
    """The payload when there is no document to show."""
    return {"meta": {"generated_at": None, "run_id": None, "status": None,
                     "next_run_at": None, "held_count": None, "watched_count": None,
                     "is_sample": False, "errors": [], "last_finding_at": None,
                     "dropped": 0},
            "findings": [], "watchlist_triage": [], "runs": []}


def failed(error: str, served_from: str = "none") -> dict:
    """A complete failed payload, for a caller that could not even run build()."""
    payload = empty()
    payload["meta"].update(status="failed", served_from=served_from, error=error,
                           schema_version=SCHEMA_VERSION, counts=_counts([]),
                           filters={"urgency": [], "tier": [], "ticker": []})
    return payload


def normalise(doc) -> dict:
    """The document reduced to the schema: known fields only, right types.

    Raises ValueError when the top level is not an object, which build()
    serves as a failed run.
    """
    if not isinstance(doc, dict):
        raise ValueError("the top level is not a JSON object")
    raw_meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    errors = _points(raw_meta.get("errors"))
    status = _enum(raw_meta.get("status"), STATUSES)
    if status is None:
        status = "partial"
        errors.append("meta.status is missing or not one of ok, partial, failed")
    meta = {"generated_at": _when(raw_meta.get("generated_at")),
            "run_id": _text(raw_meta.get("run_id"), MAX_SHORT) or None,
            "status": status,
            "next_run_at": _when(raw_meta.get("next_run_at")),
            "held_count": _count(raw_meta.get("held_count")),
            "watched_count": _count(raw_meta.get("watched_count")),
            "is_sample": _flag(raw_meta.get("is_sample")),
            "errors": errors,
            "last_finding_at": _when(raw_meta.get("last_finding_at"))}

    raw_findings = doc.get("findings") if isinstance(doc.get("findings"), list) else []
    findings, ids, dropped = [], set(), 0
    for raw in raw_findings[:MAX_FINDINGS]:
        finding = _finding(raw)
        if finding is None or finding["id"] in ids:
            dropped += 1
            continue
        ids.add(finding["id"])
        findings.append(finding)
    if dropped:
        errors.append(f"{dropped} finding(s) skipped: missing id, ticker or headline, "
                      "or an id already used")
    if len(raw_findings) > MAX_FINDINGS:
        errors.append(f"only the first {MAX_FINDINGS} findings are shown")
    meta["dropped"] = dropped
    findings.sort(key=_rank)

    runs = _rows(doc.get("runs"), _run)
    runs.sort(key=lambda run: -(_epoch(run["at"]) or 0.0))
    return {"meta": meta, "findings": findings,
            "watchlist_triage": _rows(doc.get("watchlist_triage"), _triage),
            "runs": runs}


# ---------------------------------------------------------------- serving

def _wanted(value, allowed: tuple[str, ...] | None = None) -> list[str]:
    """A comma list from the query string, lower-cased, known values only."""
    words = [word.strip().lower() for word in (value or "").split(",")]
    words = [word for word in words if word]
    if allowed is not None:
        words = [word for word in words if word in allowed]
    return list(dict.fromkeys(words))


def _counts(findings: list[dict]) -> dict:
    return {"total": len(findings),
            "urgency": {level: sum(f["urgency"] == level for f in findings)
                        for level in URGENCY},
            "tier": {tier: sum(f["tier"] == tier for f in findings) for tier in TIERS}}


def build(urgency: str = "", tier: str = "", ticker: str = "") -> dict:
    """The /api/research payload. Never raises for a bad or missing file.

    Filters take comma lists (urgency=high,medium) and apply to findings
    only; the ticker match ignores case. Values outside the schema are
    ignored, so a filter made only of unknown values filters nothing.
    """
    doc, served_from, error = load()
    payload = empty()
    # Any file that parsed goes through normalise(), even one holding a bare
    # null: that is a document with the wrong shape, not the absence of one.
    if served_from != "none" and error is None:
        try:
            payload = normalise(doc)
        except ValueError as exc:
            error = f"{(RESEARCH_PATH if served_from == 'live' else SAMPLE_PATH).name}: {exc}"
    meta = payload["meta"]
    if error:
        meta["status"] = "failed"
    if served_from == "sample":
        meta["is_sample"] = True
    meta.update(served_from=served_from, error=error, schema_version=SCHEMA_VERSION)
    meta["counts"] = _counts(payload["findings"])

    filters = {"urgency": _wanted(urgency, URGENCY), "tier": _wanted(tier, TIERS),
               "ticker": _wanted(ticker)}
    meta["filters"] = filters
    payload["findings"] = [
        f for f in payload["findings"]
        if (not filters["urgency"] or f["urgency"] in filters["urgency"])
        and (not filters["tier"] or f["tier"] in filters["tier"])
        and (not filters["ticker"] or f["ticker"].lower() in filters["ticker"])
    ]
    return payload
