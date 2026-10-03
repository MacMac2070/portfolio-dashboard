"""Mass scenario and synthetic-data tests for the research briefing adapter.

Validates schema invariants, cap enforcement, ranking, filtering subsets,
URL sanitisation, unicode preservation, and load precedence across
deterministically generated documents.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

import research

FINDING_KEYS = {
    "id", "ticker", "tier", "sector", "urgency", "headline",
    "why_it_matters", "bull", "bear", "risk_views", "evidence",
    "pillars_touched", "first_seen", "changed_since_last_run",
}

EVIDENCE_KEYS = {"label", "detail", "url", "kind"}
PILLAR_KEYS = {"pillar", "change"}
RISK_VIEW_KEYS = set(research.RISK_VIEWS)

HOSTILE_URLS = [
    "javascript:alert(1)",
    "JAVASCRIPT:alert('xss')",
    " javascript:void(0)",
    "data:text/html;base64,PHNjcmlwdD4=",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "//host/path",
    "http:/x",
    "http://example.com/with\ttab",
    "http://example.com/with\nnewline",
    "http://example.com/with\rcarriage_return",
    "http://example.com/with\0null_byte",
    "http://example.com/" + "a" * (research.MAX_URL + 1),
    "https://example.com/" + "b" * 2500,
]

VALID_URLS = [
    "http://example.com",
    "https://example.com/filing/10k",
    "HTTPS://WWW.SEC.GOV/edgar/data/123/000.txt",
    "http://127.0.0.1:8080/reports?year=2026&quarter=Q3#summary",
]


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Isolate file paths to tmp_path for every test."""
    monkeypatch.setattr(research, "DATA_DIR", tmp_path)
    monkeypatch.setattr(research, "RESEARCH_PATH", tmp_path / "research_findings.json")
    monkeypatch.setattr(research, "SAMPLE_PATH", tmp_path / "research_findings.sample.json")
    return tmp_path


def write_file(path: Path, content: str | bytes | dict | list) -> None:
    """Helper to write string, bytes, or JSON serialisable objects to disk."""
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content), encoding="utf-8")


def valid_finding(idx: int = 1, **overrides) -> dict:
    """Produce one fully valid finding document with default fields."""
    base = {
        "id": f"finding-{idx}",
        "ticker": "INTC",
        "tier": "held",
        "sector": "Semiconductors",
        "urgency": "high",
        "headline": f"Headline number {idx} regarding semiconductor business",
        "why_it_matters": "A concise explanation of why this finding matters to the book.",
        "bull": ["Strong product pipeline", "Margin expansion"],
        "bear": ["Execution risks", "Intense competition"],
        "risk_views": {
            "aggressive": "Aggressive stance on cyclical recovery.",
            "conservative": "Conservative approach pending clearer margins.",
            "neutral": "Balanced exposure across existing holdings.",
        },
        "evidence": [
            {
                "label": "Form 10-Q",
                "detail": "Quarterly regulatory filing detail.",
                "url": "https://www.sec.gov/edgar/data/50863/filing.htm",
                "kind": "filing",
            }
        ],
        "pillars_touched": [{"pillar": "Turnaround", "change": "steady"}],
        "first_seen": "2026-10-01T08:30:00+00:00",
        "changed_since_last_run": True,
    }
    base.update(overrides)
    return base


def valid_document(findings: list | None = None, **meta_overrides) -> dict:
    """Produce a standard envelope containing meta, findings, triage, and runs."""
    meta = {
        "generated_at": "2026-10-01T12:00:00+00:00",
        "run_id": "run-20261001-01",
        "status": "ok",
        "next_run_at": "2026-10-01T18:00:00+00:00",
        "held_count": 14,
        "watched_count": 28,
        "is_sample": False,
        "errors": [],
    }
    meta.update(meta_overrides)
    return {
        "meta": meta,
        "findings": [valid_finding(1)] if findings is None else findings,
        "watchlist_triage": [
            {
                "ticker": "NVDA",
                "signals_flag": True,
                "narrative_flag": False,
                "escalated": True,
                "note": "Accelerating data centre revenues noted.",
            }
        ],
        "runs": [
            {
                "run_id": "run-20261001-01",
                "at": "2026-10-01T12:00:00+00:00",
                "status": "ok",
                "agents_used": 12,
                "skipped": 0,
                "failed": 0,
            }
        ],
    }


# -----------------------------------------------------------------------------
# Invariant 1: build() never raises for arbitrary JSON or broken bytes
# -----------------------------------------------------------------------------

def test_build_never_raises_on_arbitrary_top_level_json_values(paths):
    """build() must never raise for any valid JSON value placed at the top level."""
    test_values = [
        None,
        0,
        -180,
        3.14159,
        "",
        "just a text string",
        True,
        False,
        [],
        [1, 2, 3],
        [None, True, False, "nested"],
        {"random_key": "random_value"},
        {"meta": None, "findings": 42},
        {"meta": {"status": "ok"}, "findings": "not a list"},
        [[[[1, 2, [3, 4]]]]],
        {"a": {"b": {"c": {"d": {"e": [1, 2, 3]}}}}},
    ]
    for val in test_values:
        write_file(research.RESEARCH_PATH, val)
        payload = research.build()
        assert isinstance(payload, dict)
        assert "meta" in payload and "findings" in payload
        assert payload["meta"]["schema_version"] == research.SCHEMA_VERSION
        assert payload["meta"]["served_from"] == "live"


def test_build_never_raises_on_random_bytes_and_invalid_utf8(paths):
    """build() must never raise when reading random bytes or non-UTF8 sequences."""
    rng = random.Random(42)
    # Include known invalid UTF-8 sequences alongside random byte arrays
    byte_samples = [
        b"",
        b"\xff",
        b"\xfe\xff",
        b"\xc0\xaf",
        b"\xed\xa0\x80",
        b"\x80\x81\x82\x83",
        b"{\x80: 1}",
        b"{\"meta\": \xff\xfe}",
    ]
    for _ in range(50):
        length = rng.randint(1, 256)
        byte_samples.append(bytes(rng.randint(0, 255) for _ in range(length)))

    for raw in byte_samples:
        write_file(research.RESEARCH_PATH, raw)
        payload = research.build()
        assert isinstance(payload, dict)
        meta = payload["meta"]
        assert meta["served_from"] == "live"
        assert meta["status"] == "failed"
        assert isinstance(meta["error"], str)
        assert payload["findings"] == []


# -----------------------------------------------------------------------------
# Invariant 2: Findings schema structure, enums, URLs and types
# -----------------------------------------------------------------------------

def test_returned_findings_conform_strictly_to_schema_invariants(paths):
    """Every returned finding must have exactly 14 keys, validated enums and sanitised URLs."""
    rng = random.Random(101)
    findings = []
    urgency_choices = list(research.URGENCY) + ["URGENT", "invalid", "", None, 123]
    tier_choices = list(research.TIERS) + ["owned", "watchlist_tier", "", None]
    kind_choices = list(research.KINDS) + ["tweet", "blog", "", None]
    change_choices = list(research.CHANGES) + ["improving", "deteriorating", "", None]
    url_pool = VALID_URLS + HOSTILE_URLS + [None, 42, "not a url", "http:/bad"]

    for i in range(120):
        f = valid_finding(
            i,
            urgency=rng.choice(urgency_choices),
            tier=rng.choice(tier_choices),
            evidence=[
                {
                    "label": f"Label {i}",
                    "detail": f"Detail {i}",
                    "url": rng.choice(url_pool),
                    "kind": rng.choice(kind_choices),
                }
            ],
            pillars_touched=[
                {"pillar": f"Pillar {i}", "change": rng.choice(change_choices)}
            ],
        )
        findings.append(f)

    doc = valid_document(findings=findings)
    write_file(research.RESEARCH_PATH, doc)
    payload = research.build()

    assert len(payload["findings"]) > 0
    for finding in payload["findings"]:
        # Exactly the 14 schema keys
        assert set(finding.keys()) == FINDING_KEYS

        # Urgency must be in URGENCY enum
        assert finding["urgency"] in research.URGENCY

        # Tier must be in TIERS or None
        assert finding["tier"] is None or finding["tier"] in research.TIERS

        # Risk views must contain exactly the three named keys
        assert set(finding["risk_views"].keys()) == RISK_VIEW_KEYS
        for v in finding["risk_views"].values():
            assert isinstance(v, str)

        # Evidence validation
        for ev in finding["evidence"]:
            assert set(ev.keys()) == EVIDENCE_KEYS
            assert ev["kind"] is None or ev["kind"] in research.KINDS
            url = ev["url"]
            if url is not None:
                assert url.lower().startswith("http://") or url.lower().startswith("https://")
                assert not any(ch <= " " or ch == "\x7f" for ch in url)

        # Pillars validation
        for p in finding["pillars_touched"]:
            assert set(p.keys()) == PILLAR_KEYS
            assert p["change"] is None or p["change"] in research.CHANGES


# -----------------------------------------------------------------------------
# Invariant 3: String caps and list length limits
# -----------------------------------------------------------------------------

def test_caps_and_limits_strictly_respected(paths):
    """Strings and lists must never exceed their documented caps."""
    rng = random.Random(202)
    # Generate 650 findings to verify MAX_FINDINGS cap (500)
    findings = []
    long_short = "A" * (research.MAX_SHORT + 80)
    long_line = "B" * (research.MAX_LINE + 150)
    long_text = "C" * (research.MAX_TEXT + 300)

    for i in range(650):
        f = valid_finding(
            i,
            id=f"id-{i}-" + "x" * 150,
            ticker="TICK-" + "t" * 150,
            sector="Sector-" + "s" * 150,
            headline=f"Headline {i} " + long_line,
            why_it_matters=f"Why {i} " + long_text,
            bull=[f"Bull {j} " + long_line for j in range(20)],
            bear=[f"Bear {j} " + long_line for j in range(20)],
            risk_views={
                "aggressive": "Aggressive " + long_text,
                "conservative": "Conservative " + long_text,
                "neutral": "Neutral " + long_text,
            },
            evidence=[
                {
                    "label": f"L{j} " + long_short,
                    "detail": f"D{j} " + long_line,
                    "url": "https://example.com/filing",
                    "kind": "filing",
                }
                for j in range(20)
            ],
            pillars_touched=[
                {"pillar": f"P{j} " + long_short, "change": "steady"}
                for j in range(20)
            ],
        )
        findings.append(f)

    # Generate 250 triage rows and 250 runs to verify MAX_ROWS cap (200)
    triage = [
        {
            "ticker": f"TR{i}-" + long_short,
            "signals_flag": True,
            "narrative_flag": False,
            "escalated": False,
            "note": f"Note {i} " + long_line,
        }
        for i in range(250)
    ]
    runs = [
        {
            "run_id": f"run-{i}-" + long_short,
            "at": "2026-10-01T12:00:00+00:00",
            "status": "ok",
            "agents_used": 10,
        }
        for i in range(250)
    ]

    doc = valid_document(findings=findings)
    doc["watchlist_triage"] = triage
    doc["runs"] = runs

    write_file(research.RESEARCH_PATH, doc)
    payload = research.build()

    # List caps
    assert len(payload["findings"]) == research.MAX_FINDINGS
    assert len(payload["watchlist_triage"]) == research.MAX_ROWS
    assert len(payload["runs"]) == research.MAX_ROWS

    # Cap verification per item
    for f in payload["findings"]:
        assert len(f["id"]) <= research.MAX_SHORT
        assert len(f["ticker"]) <= research.MAX_SHORT
        assert len(f["sector"]) <= research.MAX_SHORT
        assert len(f["headline"]) <= research.MAX_LINE
        assert len(f["why_it_matters"]) <= research.MAX_TEXT
        assert len(f["bull"]) <= research.MAX_POINTS
        assert len(f["bear"]) <= research.MAX_POINTS
        assert len(f["evidence"]) <= research.MAX_POINTS
        assert len(f["pillars_touched"]) <= research.MAX_POINTS

        for pt in f["bull"]:
            assert len(pt) <= research.MAX_LINE
        for pt in f["bear"]:
            assert len(pt) <= research.MAX_LINE
        for rv in f["risk_views"].values():
            assert len(rv) <= research.MAX_TEXT
        for ev in f["evidence"]:
            assert len(ev["label"]) <= research.MAX_SHORT
            assert len(ev["detail"]) <= research.MAX_LINE
        for pil in f["pillars_touched"]:
            assert len(pil["pillar"]) <= research.MAX_SHORT

    for tr in payload["watchlist_triage"]:
        assert len(tr["ticker"]) <= research.MAX_SHORT
        assert len(tr["note"]) <= research.MAX_LINE

    for r in payload["runs"]:
        assert len(r["run_id"]) <= research.MAX_SHORT


# -----------------------------------------------------------------------------
# Invariant 4: Finding IDs uniqueness and exact dropped count
# -----------------------------------------------------------------------------

def test_finding_ids_are_unique_and_meta_dropped_matches_exactly(paths):
    """Returned IDs must be distinct and meta.dropped must match unusable + duplicate count."""
    rng = random.Random(303)
    for iteration in range(15):
        usable_count = rng.randint(20, 50)
        usable_findings = [valid_finding(i) for i in range(usable_count)]

        # Generate unusable findings
        missing_id = [valid_finding(1000 + i, id="") for i in range(rng.randint(3, 7))]
        missing_ticker = [valid_finding(2000 + i, ticker="   ") for i in range(rng.randint(3, 7))]
        missing_headline = [valid_finding(3000 + i, headline=None) for i in range(rng.randint(3, 7))]
        non_dict_rows = [None, 42, "not a dict", [1, 2]] * rng.randint(1, 3)

        # Generate duplicate IDs from the usable set
        dup_count = rng.randint(4, 10)
        duplicates = [
            valid_finding(9999 + i, id=usable_findings[i % usable_count]["id"])
            for i in range(dup_count)
        ]

        expected_dropped = (
            len(missing_id)
            + len(missing_ticker)
            + len(missing_headline)
            + len(non_dict_rows)
            + len(duplicates)
        )

        all_findings = (
            usable_findings
            + missing_id
            + missing_ticker
            + missing_headline
            + non_dict_rows
            + duplicates
        )
        rng.shuffle(all_findings)

        doc = valid_document(findings=all_findings)
        write_file(research.RESEARCH_PATH, doc)
        payload = research.build()

        returned_ids = [f["id"] for f in payload["findings"]]
        assert len(returned_ids) == len(set(returned_ids)), "All finding IDs must be unique"
        assert len(returned_ids) == usable_count
        assert payload["meta"]["dropped"] == expected_dropped


# -----------------------------------------------------------------------------
# Invariant 5: meta.counts totals and per-urgency sums
# -----------------------------------------------------------------------------

def test_meta_counts_totals_and_urgency_sums(paths):
    """meta.counts totals must equal the unfiltered count and per-urgency counts sum to total."""
    rng = random.Random(404)
    for _ in range(25):
        findings = []
        expected_urgency_counts = {u: 0 for u in research.URGENCY}
        expected_tier_counts = {t: 0 for t in research.TIERS}

        for i in range(rng.randint(15, 60)):
            urgency = rng.choice(research.URGENCY)
            # Tier can be valid or None
            tier = rng.choice(list(research.TIERS) + [None])
            expected_urgency_counts[urgency] += 1
            if tier in expected_tier_counts:
                expected_tier_counts[tier] += 1

            findings.append(valid_finding(i, urgency=urgency, tier=tier))

        doc = valid_document(findings=findings)
        write_file(research.RESEARCH_PATH, doc)
        payload = research.build()

        counts = payload["meta"]["counts"]
        assert counts["total"] == len(payload["findings"])
        assert sum(counts["urgency"].values()) == counts["total"]
        assert counts["urgency"] == expected_urgency_counts
        assert counts["tier"] == expected_tier_counts


# -----------------------------------------------------------------------------
# Invariant 6: Filtering is a consistent subset preserving order and counts
# -----------------------------------------------------------------------------

def test_filtering_produces_consistent_ordered_subset_and_preserves_counts(paths):
    """Filtered findings must be a strictly ordered subset of unfiltered findings."""
    rng = random.Random(505)
    tickers = ["INTC", "NVDA", "700", "AAPL", "MSFT", "TSM", "BABA"]
    findings = []

    for i in range(80):
        findings.append(
            valid_finding(
                i,
                ticker=rng.choice(tickers),
                tier=rng.choice(list(research.TIERS) + [None]),
                urgency=rng.choice(research.URGENCY),
            )
        )

    doc = valid_document(findings=findings)
    write_file(research.RESEARCH_PATH, doc)

    unfiltered = research.build()
    unfiltered_ids = [f["id"] for f in unfiltered["findings"]]
    unfiltered_counts = unfiltered["meta"]["counts"]

    # Fuzz filter arguments with varied formatting, cases, spaces and unknown tokens
    urgency_filter_samples = [
        "",
        "high",
        "HIGH, quiet",
        "  medium , HIGH  ",
        "unknown_urgency, quiet",
        ",,,",
        "high, medium, low, quiet",
        "hiGh, LOW, unknown",
    ]
    tier_filter_samples = [
        "",
        "held",
        "WATCHLIST",
        "held, watchlist",
        "  held , unknown_tier ",
        ",,",
        "held, held",
    ]
    ticker_filter_samples = [
        "",
        "intc",
        "INTC, nvda",
        "  700 , aapl  ",
        "unknown_ticker, msft",
        ",,",
        "intc, INTC, 700",
        "阿里巴巴, tsm",
    ]

    for _ in range(60):
        u_filter = rng.choice(urgency_filter_samples)
        t_filter = rng.choice(tier_filter_samples)
        k_filter = rng.choice(ticker_filter_samples)

        filtered = research.build(urgency=u_filter, tier=t_filter, ticker=k_filter)
        filtered_ids = [f["id"] for f in filtered["findings"]]

        # Invariant: meta.counts remains unchanged by filtering
        assert filtered["meta"]["counts"] == unfiltered_counts

        # Invariant: filtered IDs form an ordered subset of unfiltered IDs
        assert set(filtered_ids).issubset(set(unfiltered_ids))
        unfiltered_iterator = iter(unfiltered_ids)
        assert all(fid in unfiltered_iterator for fid in filtered_ids)

        # Invariant: returned findings satisfy the active filter constraints
        active_urgencies = filtered["meta"]["filters"]["urgency"]
        active_tiers = filtered["meta"]["filters"]["tier"]
        active_tickers = filtered["meta"]["filters"]["ticker"]

        for f in filtered["findings"]:
            if active_urgencies:
                assert f["urgency"] in active_urgencies
            if active_tiers:
                assert f["tier"] in active_tiers
            if active_tickers:
                assert f["ticker"].lower() in active_tickers


# -----------------------------------------------------------------------------
# Invariant 7: Urgency ranking is non-decreasing down the list
# -----------------------------------------------------------------------------

def test_ranking_urgency_is_non_decreasing(paths):
    """Urgency rank must be non-decreasing down the findings list."""
    rng = random.Random(606)
    timestamps = [
        "2026-10-01T00:00:00+00:00",
        "2026-10-02T12:00:00+00:00",
        "2026-09-15T08:00:00+00:00",
        None,
    ]

    for _ in range(15):
        findings = []
        for i in range(50):
            findings.append(
                valid_finding(
                    i,
                    urgency=rng.choice(research.URGENCY),
                    changed_since_last_run=rng.choice([True, False]),
                    first_seen=rng.choice(timestamps),
                )
            )

        doc = valid_document(findings=findings)
        write_file(research.RESEARCH_PATH, doc)
        payload = research.build()

        urgency_ranks = [research.URGENCY.index(f["urgency"]) for f in payload["findings"]]
        # Assert non-decreasing order: ranks[i] <= ranks[i+1]
        for i in range(len(urgency_ranks) - 1):
            assert urgency_ranks[i] <= urgency_ranks[i + 1]


# -----------------------------------------------------------------------------
# Invariant 8: Hostile URLs are always nulled
# -----------------------------------------------------------------------------

def test_hostile_urls_are_always_nulled(paths):
    """Hostile schemes, whitespace, control characters and oversized URLs must be nulled."""
    findings = []
    for idx, url in enumerate(HOSTILE_URLS):
        findings.append(
            valid_finding(
                idx,
                evidence=[
                    {"label": f"Ev-{idx}", "detail": "Detail", "url": url, "kind": "filing"}
                ],
            )
        )

    doc = valid_document(findings=findings)
    write_file(research.RESEARCH_PATH, doc)
    payload = research.build()

    for idx, f in enumerate(payload["findings"]):
        assert len(f["evidence"]) == 1
        assert f["evidence"][0]["url"] is None, f"Expected hostile URL to be nulled: {HOSTILE_URLS[idx]}"


# -----------------------------------------------------------------------------
# Invariant 9: Unicode text survives intact under the cap
# -----------------------------------------------------------------------------

def test_unicode_text_survives_normalisation_intact(paths):
    """CJK, emoji, RTL scripts, and combining marks must survive normalisation intact."""
    cjk_headline = "台积电发布第三季度财报 净利润创历史新高"
    cjk_why = "先进制程芯片需求强劲，推动营业利润率显著提升。"
    cjk_sector = "半导体制造"

    emoji_bull = ["🔥 营业收入同比增长25%", "🚀 新一代制程量产进度超预期", "📈 毛利率持续修复"]
    rtl_bear = ["تباطؤ الطلب في قطاع السيارات والأجهزة الذكية", "تصاعد حدة المنافسة في الأسواق الآسيوية"]

    combining_views = {
        "aggressive": "Na\u0308ive estimation of re\u0301sume\u0301 growth potential.",
        "conservative": "Prudent me\u0301lange of fa\u00e7ade and credit risks.",
        "neutral": "E\u0301quilibre\u0301 perspective on long-term cash flow.",
    }
    unicode_pillar = "Re\u0301volution"
    unicode_note = "متابعة دقيقة لمؤشرات السيولة والربحية 📊"

    finding = valid_finding(
        1,
        ticker="700",
        sector=cjk_sector,
        headline=cjk_headline,
        why_it_matters=cjk_why,
        bull=emoji_bull,
        bear=rtl_bear,
        risk_views=combining_views,
        pillars_touched=[{"pillar": unicode_pillar, "change": "stronger"}],
    )
    doc = valid_document(findings=[finding])
    doc["watchlist_triage"] = [
        {
            "ticker": "9988",
            "signals_flag": True,
            "narrative_flag": False,
            "escalated": False,
            "note": unicode_note,
        }
    ]

    write_file(research.RESEARCH_PATH, doc)
    payload = research.build()

    [f] = payload["findings"]
    assert f["sector"] == cjk_sector
    assert f["headline"] == cjk_headline
    assert f["why_it_matters"] == cjk_why
    assert f["bull"] == emoji_bull
    assert f["bear"] == rtl_bear
    assert f["risk_views"] == combining_views
    assert f["pillars_touched"][0]["pillar"] == unicode_pillar
    assert payload["watchlist_triage"][0]["note"] == unicode_note


# -----------------------------------------------------------------------------
# Invariant 10: load() precedence: real file presence suppresses sample
# -----------------------------------------------------------------------------

def test_load_precedence_real_file_never_reads_sample(paths):
    """The real file wins whenever present; sample is never read when live file exists."""
    sample_finding = valid_finding(999, headline="Sample briefing headline")
    sample_doc = valid_document(findings=[sample_finding], is_sample=True)
    write_file(research.SAMPLE_PATH, sample_doc)

    # 1. Valid real file present
    live_finding = valid_finding(1, headline="Live briefing headline")
    write_file(research.RESEARCH_PATH, valid_document(findings=[live_finding]))
    payload = research.build()
    assert payload["meta"]["served_from"] == "live"
    assert payload["meta"]["is_sample"] is False
    assert payload["meta"]["status"] == "ok"
    assert [f["id"] for f in payload["findings"]] == ["finding-1"]

    # 2. Malformed JSON real file present
    write_file(research.RESEARCH_PATH, '{"meta": {"status": "ok"}, "findings": [broken')
    payload = research.build()
    assert payload["meta"]["served_from"] == "live"
    assert payload["meta"]["is_sample"] is False
    assert payload["meta"]["status"] == "failed"
    assert "not valid JSON" in payload["meta"]["error"]
    assert payload["findings"] == []

    # 3. Empty real file present
    write_file(research.RESEARCH_PATH, "")
    payload = research.build()
    assert payload["meta"]["served_from"] == "live"
    assert payload["meta"]["is_sample"] is False
    assert payload["meta"]["status"] == "failed"
    assert "not valid JSON" in payload["meta"]["error"]
    assert payload["findings"] == []

    # 4. Directory occupying the real file path
    research.RESEARCH_PATH.unlink()
    research.RESEARCH_PATH.mkdir()
    payload = research.build()
    assert payload["meta"]["served_from"] == "live"
    assert payload["meta"]["is_sample"] is False
    assert payload["meta"]["status"] == "failed"
    assert "could not be read" in payload["meta"]["error"]
    assert payload["findings"] == []


# -----------------------------------------------------------------------------
# Regression tests: a file holding a bare JSON null once bypassed normalise()
# and came back with status None. Found by this suite on 2 Oct 2026, fixed in
# build(), so these now run as ordinary tests.
# -----------------------------------------------------------------------------

def test_top_level_null_sets_failed_status_when_live_file_present(paths):
    """JSON literal 'null' in the live file must result in status 'failed', not None."""
    write_file(research.RESEARCH_PATH, "null")
    payload = research.build()
    assert payload["meta"]["served_from"] == "live"
    # The docstring states: status is null only when served_from is 'none'.
    # A document that cannot be parsed, or whose top level is not an object,
    # is served as status 'failed' with no findings.
    assert payload["meta"]["status"] == "failed"
    assert payload["meta"]["error"] is not None


def test_top_level_null_sets_failed_status_when_sample_file_present(paths):
    """JSON literal 'null' in the sample file must result in status 'failed', not None."""
    write_file(research.SAMPLE_PATH, "null")
    payload = research.build()
    assert payload["meta"]["served_from"] == "sample"
    assert payload["meta"]["status"] == "failed"
    assert payload["meta"]["error"] is not None
