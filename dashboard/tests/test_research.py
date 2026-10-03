"""research.py reads what the co-pilot wrote and never trusts its shape."""
import json
import re

import pytest

import research
import universe

SHIPPED_SAMPLE = research.SAMPLE_PATH       # captured before any test repoints it
ADVICE = re.compile(r"\b(buy|sell|hold|target price|you should)\b", re.IGNORECASE)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "DATA_DIR", tmp_path)
    monkeypatch.setattr(research, "RESEARCH_PATH", tmp_path / "research_findings.json")
    monkeypatch.setattr(research, "SAMPLE_PATH", tmp_path / "research_findings.sample.json")
    return tmp_path


def finding(**over):
    base = {"id": "f1", "ticker": "INTC", "tier": "held", "sector": "Semiconductors",
            "urgency": "high", "headline": "Something happened",
            "why_it_matters": "It may matter.", "bull": ["a"], "bear": ["b"],
            "risk_views": {"aggressive": "x", "conservative": "y", "neutral": "z"},
            "evidence": [{"label": "Form 4", "detail": "d", "url": "https://www.sec.gov/",
                          "kind": "filing"}],
            "pillars_touched": [{"pillar": "Turnaround", "change": "weaker"}],
            "first_seen": "2026-10-01T05:00:00+00:00", "changed_since_last_run": True}
    base.update(over)
    return base


def doc(findings=None, **meta):
    return {"meta": {"generated_at": "2026-10-02T05:40:00+00:00", "run_id": "r1",
                     "status": "ok", "next_run_at": None, "held_count": 15,
                     "watched_count": 22, "is_sample": False, "errors": [], **meta},
            "findings": [finding()] if findings is None else findings,
            "watchlist_triage": [{"ticker": "NVDA", "signals_flag": True,
                                  "narrative_flag": False, "escalated": True, "note": "n"}],
            "runs": [{"run_id": "r1", "at": "2026-10-02T05:40:00+00:00", "status": "ok",
                      "agents_used": 150, "skipped": 0, "failed": 0}]}


def write(path, payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")


def ids(payload):
    return [f["id"] for f in payload["findings"]]


# ---------------------------------------------------------------- the file

def test_no_file_at_all_is_an_empty_payload_not_an_error(paths):
    out = research.build()
    meta = out["meta"]
    assert meta["served_from"] == "none"
    assert meta["status"] is None and meta["error"] is None
    assert meta["schema_version"] == 1
    assert out["findings"] == [] and out["watchlist_triage"] == [] and out["runs"] == []
    assert meta["counts"]["total"] == 0


def test_malformed_real_file_fails_readably_and_never_falls_back_to_the_sample(paths):
    write(research.RESEARCH_PATH, '{"meta": {"status": "ok"}, "findings": [')
    write(research.SAMPLE_PATH, doc(is_sample=True))
    out = research.build()
    meta = out["meta"]
    assert meta["served_from"] == "live"
    assert meta["status"] == "failed"
    assert "not valid JSON" in meta["error"] and "line 1" in meta["error"]
    assert meta["is_sample"] is False
    assert out["findings"] == []


def test_a_top_level_that_is_not_an_object_is_a_failed_run(paths):
    write(research.RESEARCH_PATH, [1, 2, 3])
    meta = research.build()["meta"]
    assert meta["status"] == "failed"
    assert "not a JSON object" in meta["error"]


def test_a_valid_file_is_served_live(paths):
    write(research.RESEARCH_PATH, doc())
    out = research.build()
    meta = out["meta"]
    assert meta["served_from"] == "live" and meta["status"] == "ok" and meta["error"] is None
    assert meta["is_sample"] is False and meta["dropped"] == 0
    assert ids(out) == ["f1"]
    assert out["findings"][0]["evidence"][0]["url"] == "https://www.sec.gov/"
    assert out["watchlist_triage"][0]["escalated"] is True
    assert out["runs"][0]["agents_used"] == 150


def test_unknown_fields_are_ignored_everywhere(paths):
    payload = doc(findings=[finding(
        surprise="x",
        risk_views={"aggressive": "a", "reckless": "b"},
        evidence=[{"label": "L", "kind": "filing", "secret": 1}])])
    payload["meta"]["surprise"] = {"nested": True}
    payload["extra_top_level"] = []
    payload["watchlist_triage"][0]["colour"] = "red"
    payload["runs"][0]["cost"] = 3.2
    write(research.RESEARCH_PATH, payload)
    out = research.build()
    f = out["findings"][0]
    assert set(f) == {"id", "ticker", "tier", "sector", "urgency", "headline",
                      "why_it_matters", "bull", "bear", "risk_views", "evidence",
                      "pillars_touched", "first_seen", "changed_since_last_run"}
    assert f["risk_views"] == {"aggressive": "a", "conservative": "", "neutral": ""}
    assert set(f["evidence"][0]) == {"label", "detail", "url", "kind"}
    assert "surprise" not in out["meta"] and "extra_top_level" not in out
    assert "colour" not in out["watchlist_triage"][0] and "cost" not in out["runs"][0]
    assert out["meta"]["status"] == "ok"


# ---------------------------------------------------------------- the sample

def test_the_sample_stands_in_only_while_the_real_file_is_absent(paths):
    write(research.SAMPLE_PATH, doc(is_sample=False))      # flagged as sample regardless
    meta = research.build()["meta"]
    assert meta["served_from"] == "sample" and meta["is_sample"] is True

    write(research.RESEARCH_PATH, doc(findings=[]))
    out = research.build()
    assert out["meta"]["served_from"] == "live" and out["meta"]["is_sample"] is False
    assert out["findings"] == []


def test_a_live_file_that_calls_itself_sample_keeps_the_label(paths):
    # By design: a wrong "sample" banner costs less than a missing one.
    write(research.RESEARCH_PATH, doc(is_sample=True))
    meta = research.build()["meta"]
    assert meta["served_from"] == "live" and meta["is_sample"] is True


def test_a_bare_null_document_is_a_failed_run_not_an_empty_one(paths):
    write(research.RESEARCH_PATH, "null")
    meta = research.build()["meta"]
    assert meta["served_from"] == "live"
    assert meta["status"] == "failed" and "not a JSON object" in meta["error"]


def test_the_failed_fallback_has_the_same_shape_as_build(paths):
    built = research.build()
    fallback = research.failed("boom")
    assert set(fallback) == set(built)
    assert set(fallback["meta"]) == set(built["meta"])
    assert fallback["meta"]["status"] == "failed" and fallback["meta"]["error"] == "boom"


def test_a_broken_sample_is_reported_not_raised(paths):
    write(research.SAMPLE_PATH, "not json")
    meta = research.build()["meta"]
    assert meta["served_from"] == "sample"
    assert meta["status"] == "failed" and "not valid JSON" in meta["error"]


# ---------------------------------------------------------------- filters

def three():
    return [finding(id="a", ticker="INTC", urgency="high", tier="held"),
            finding(id="b", ticker="NVDA", urgency="medium", tier="watchlist"),
            finding(id="c", ticker="700", urgency="quiet", tier="held")]


def test_filters_narrow_findings_and_leave_the_counts_whole(paths):
    write(research.RESEARCH_PATH, doc(findings=three()))
    assert ids(research.build(urgency="high")) == ["a"]
    assert ids(research.build(urgency="high, MEDIUM")) == ["a", "b"]
    assert ids(research.build(tier="watchlist")) == ["b"]
    assert ids(research.build(ticker="intc,700")) == ["a", "c"]
    assert ids(research.build(urgency="high", tier="watchlist")) == []

    out = research.build(urgency="quiet")
    counts = out["meta"]["counts"]
    assert counts["total"] == 3
    assert counts["urgency"] == {"high": 1, "medium": 1, "low": 0, "quiet": 1}
    assert counts["tier"] == {"held": 2, "watchlist": 1}
    assert out["meta"]["filters"] == {"urgency": ["quiet"], "tier": [], "ticker": []}
    assert len(out["watchlist_triage"]) == 1                # triage is never filtered


def test_filter_values_outside_the_schema_filter_nothing(paths):
    write(research.RESEARCH_PATH, doc(findings=three()))
    out = research.build(urgency="urgent", tier="owned")
    assert ids(out) == ["a", "b", "c"]
    assert out["meta"]["filters"]["urgency"] == [] and out["meta"]["filters"]["tier"] == []


# ---------------------------------------------------------------- shape

def test_only_absolute_http_links_survive(paths):
    links = [("js", "javascript:alert(1)"), ("data", "data:text/html,hi"),
             ("relative", "/relative/path"), ("bare", "www.example.com"),
             ("space", "https://exa mple.com"), ("ftp", "ftp://example.com"),
             ("upper", "HTTPS://example.com/x"), ("ok", "http://example.com/a?b=1"),
             ("number", 42), ("quote", 'https://example.com/"onmouseover="x'),
             ("angle", "https://example.com/<b>"), ("tick", "https://example.com/`x`")]
    evidence = [{"label": label, "url": url, "kind": "news"} for label, url in links]
    write(research.RESEARCH_PATH, doc(findings=[finding(evidence=evidence)]))
    urls = {e["label"]: e["url"] for e in research.build()["findings"][0]["evidence"]}
    assert urls == {"js": None, "data": None, "relative": None, "bare": None,
                    "space": None, "ftp": None, "upper": "HTTPS://example.com/x",
                    "ok": "http://example.com/a?b=1", "number": None,
                    "quote": None, "angle": None, "tick": None}


def test_enums_are_coerced_and_unusable_findings_are_dropped(paths):
    rows = [finding(id="a", urgency="URGENT", tier="owned", changed_since_last_run="yes",
                    pillars_touched=[{"pillar": "P", "change": "up"}, {"change": "weaker"}],
                    evidence=[{"label": "L", "kind": "tweet"}, {"url": "https://x.com"}],
                    bull="not a list", bear=["ok", 3, None, ""]),
            finding(id="", headline="no id"),
            finding(id="b", ticker=None),
            finding(id="c", headline="   "),
            finding(id="a", headline="an id already used"),
            "not an object"]
    write(research.RESEARCH_PATH, doc(findings=rows, status="weird"))
    out = research.build()
    [f] = out["findings"]
    assert f["urgency"] == "low" and f["tier"] is None
    assert f["changed_since_last_run"] is False
    assert f["pillars_touched"] == [{"pillar": "P", "change": None}]
    assert f["evidence"] == [{"label": "L", "detail": "", "url": None, "kind": None}]
    assert f["bull"] == [] and f["bear"] == ["ok", "3"]
    meta = out["meta"]
    assert meta["dropped"] == 5
    assert meta["status"] == "partial"
    assert any("5 finding(s) skipped" in line for line in meta["errors"])
    assert any("meta.status" in line for line in meta["errors"])


def test_text_is_single_spaced_and_capped(paths):
    write(research.RESEARCH_PATH, doc(findings=[finding(
        headline="  spaced\n\n  out  ", why_it_matters="word " * 1000, ticker=700)]))
    f = research.build()["findings"][0]
    assert f["headline"] == "spaced out"
    assert f["ticker"] == "700"
    assert len(f["why_it_matters"]) <= research.MAX_TEXT
    assert f["why_it_matters"].endswith("…")


def test_timestamps_gain_an_offset_and_bad_ones_become_null(paths):
    write(research.RESEARCH_PATH, doc(
        findings=[finding(id="a", first_seen="2026-10-01T05:00:00Z"),
                  finding(id="b", first_seen="yesterday"),
                  finding(id="c", first_seen="2026-10-01T06:00:00z")],
        next_run_at="2026-10-02T17:00:00"))
    out = research.build()
    seen = {f["id"]: f["first_seen"] for f in out["findings"]}
    assert seen == {"a": "2026-10-01T05:00:00+00:00", "b": None,
                    "c": "2026-10-01T06:00:00+00:00"}
    assert out["meta"]["next_run_at"] == "2026-10-02T17:00:00+00:00"


def test_findings_rank_by_urgency_then_change_then_recency(paths):
    rows = [finding(id="quiet", urgency="quiet"),
            finding(id="high-old", changed_since_last_run=True,
                    first_seen="2026-09-01T00:00:00+00:00"),
            finding(id="high-unchanged", changed_since_last_run=False,
                    first_seen="2026-10-02T00:00:00+00:00"),
            finding(id="high-new", changed_since_last_run=True,
                    first_seen="2026-10-02T00:00:00+00:00"),
            finding(id="high-undated", changed_since_last_run=True, first_seen=None),
            finding(id="medium", urgency="medium")]
    write(research.RESEARCH_PATH, doc(findings=rows))
    assert ids(research.build()) == ["high-new", "high-old", "high-undated",
                                     "high-unchanged", "medium", "quiet"]


def test_runs_come_back_newest_first(paths):
    payload = doc()
    payload["runs"] = [{"run_id": "old", "at": "2026-09-30T05:40:00+00:00", "status": "ok"},
                       {"run_id": "new", "at": "2026-10-02T05:40:00+00:00", "status": "partial"},
                       {"status": "ok"}]                     # no id, no time: dropped
    write(research.RESEARCH_PATH, payload)
    runs = research.build()["runs"]
    assert [r["run_id"] for r in runs] == ["new", "old"]
    assert runs[0]["agents_used"] is None


# ---------------------------------------------------------------- the shipped sample

def test_the_shipped_sample_validates_and_covers_every_state():
    raw = json.loads(SHIPPED_SAMPLE.read_text(encoding="utf-8"))
    out = research.normalise(raw)
    assert raw["meta"]["is_sample"] is True
    assert out["meta"]["status"] == "ok"
    assert out["meta"]["dropped"] == 0 and out["meta"]["errors"] == []
    findings = out["findings"]
    assert len(findings) == len(raw["findings"])
    assert {f["urgency"] for f in findings} == set(research.URGENCY)
    assert {f["tier"] for f in findings} == set(research.TIERS)
    assert any(not f["bull"] and not f["bear"] for f in findings)
    assert any(len(f["headline"]) > 150 for f in findings)
    assert {e["kind"] for f in findings for e in f["evidence"]} == set(research.KINDS)
    assert any(e["url"] is None for f in findings for e in f["evidence"])
    assert {p["change"] for f in findings for p in f["pillars_touched"]} == set(research.CHANGES)
    assert {r["status"] for r in out["runs"]} == set(research.STATUSES)
    escalated = {row["escalated"] for row in out["watchlist_triage"]}
    assert escalated == {True, False}


def test_the_shipped_sample_uses_real_universe_keys():
    raw = json.loads(SHIPPED_SAMPLE.read_text(encoding="utf-8"))
    for f in raw["findings"]:
        ticker = universe.TICKERS.get(f["ticker"])
        assert ticker is not None, f["ticker"]
        assert ticker.owned == (f["tier"] == "held"), f["ticker"]
        assert ticker.sector == f["sector"], f["ticker"]
    for row in raw["watchlist_triage"]:
        assert row["ticker"] in universe.TICKERS and not universe.TICKERS[row["ticker"]].owned


def test_the_shipped_sample_gives_no_advice_and_has_no_em_dashes():
    text = SHIPPED_SAMPLE.read_text(encoding="utf-8")
    assert ADVICE.findall(text) == []
    assert "\N{EM DASH}" not in text
