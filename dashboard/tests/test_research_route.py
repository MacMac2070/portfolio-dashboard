"""Route test suite for GET /api/research served by DashboardHandler in serve.py."""
from __future__ import annotations

import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

# Ensure dashboard root and adapter/ are on sys.path for importing serve and research
root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
adapter_dir = root / "adapter"
if str(adapter_dir) not in sys.path:
    sys.path.insert(0, str(adapter_dir))

import research
import serve


@pytest.fixture
def research_server(tmp_path, monkeypatch):
    """Start serve.DashboardHandler on an ephemeral port with isolated paths.

    The server runs on a background daemon thread and is cleanly shut down in
    the fixture finaliser.
    """
    monkeypatch.setattr(research, "DATA_DIR", tmp_path)
    monkeypatch.setattr(research, "RESEARCH_PATH", tmp_path / "research_findings.json")
    monkeypatch.setattr(research, "SAMPLE_PATH", tmp_path / "research_findings.sample.json")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.DashboardHandler)
    host, port = httpd.server_address
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    base_url = f"http://{host}:{port}"
    yield {"base_url": base_url, "tmp_path": tmp_path}

    httpd.shutdown()
    httpd.server_close()
    server_thread.join(timeout=2.0)


def fetch_json(url: str) -> tuple[int, dict, str]:
    """Perform a GET request and parse the JSON response body and content type."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        content_type = resp.headers.get_content_type()
        data = json.loads(resp.read().decode("utf-8"))
        return resp.status, data, content_type


def test_research_route_served_from_progression(research_server):
    """Verify served_from transitions from 'none' to 'sample' to 'live' with HTTP 200."""
    base_url = research_server["base_url"]

    # 1. Neither file exists: served_from is 'none'
    assert not research.RESEARCH_PATH.exists()
    assert not research.SAMPLE_PATH.exists()

    status, data, content_type = fetch_json(f"{base_url}/api/research")
    assert status == 200
    assert content_type == "application/json"
    assert data["meta"]["served_from"] == "none"
    assert data["meta"]["status"] is None
    assert data["meta"]["is_sample"] is False
    assert data["findings"] == []

    # 2. Only sample file exists: served_from is 'sample'
    sample_doc = {
        "meta": {"status": "ok"},
        "findings": [
            {
                "id": "sample-1",
                "ticker": "INTC",
                "tier": "held",
                "urgency": "high",
                "headline": "Sample headline briefing",
            }
        ],
    }
    research.SAMPLE_PATH.write_text(json.dumps(sample_doc), encoding="utf-8")

    status, data, content_type = fetch_json(f"{base_url}/api/research")
    assert status == 200
    assert content_type == "application/json"
    assert data["meta"]["served_from"] == "sample"
    assert data["meta"]["is_sample"] is True
    assert [f["id"] for f in data["findings"]] == ["sample-1"]

    # 3. Real file exists: served_from is 'live' (sample is ignored)
    live_doc = {
        "meta": {"status": "ok"},
        "findings": [
            {
                "id": "live-1",
                "ticker": "NVDA",
                "tier": "watchlist",
                "urgency": "medium",
                "headline": "Live briefing headline",
            }
        ],
    }
    research.RESEARCH_PATH.write_text(json.dumps(live_doc), encoding="utf-8")

    status, data, content_type = fetch_json(f"{base_url}/api/research")
    assert status == 200
    assert content_type == "application/json"
    assert data["meta"]["served_from"] == "live"
    assert data["meta"]["is_sample"] is False
    assert [f["id"] for f in data["findings"]] == ["live-1"]


def test_research_route_query_filtering(research_server):
    """Verify ?urgency=high&tier=held&ticker=intc narrows findings while preserving counts."""
    base_url = research_server["base_url"]

    findings = [
        {
            "id": "f1",
            "ticker": "INTC",
            "tier": "held",
            "urgency": "high",
            "headline": "Match one",
        },
        {
            "id": "f2",
            "ticker": "intc",
            "tier": "held",
            "urgency": "high",
            "headline": "Match two with lowercase ticker",
        },
        {
            "id": "f3",
            "ticker": "INTC",
            "tier": "watchlist",
            "urgency": "high",
            "headline": "Tier mismatch",
        },
        {
            "id": "f4",
            "ticker": "NVDA",
            "tier": "held",
            "urgency": "high",
            "headline": "Ticker mismatch",
        },
        {
            "id": "f5",
            "ticker": "INTC",
            "tier": "held",
            "urgency": "low",
            "headline": "Urgency mismatch",
        },
        {
            "id": "f6",
            "ticker": "AAPL",
            "tier": "watchlist",
            "urgency": "quiet",
            "headline": "All filter mismatch",
        },
    ]
    doc = {"meta": {"status": "ok"}, "findings": findings}
    research.RESEARCH_PATH.write_text(json.dumps(doc), encoding="utf-8")

    filter_query = "?urgency=high&tier=held&ticker=intc"
    status, data, content_type = fetch_json(f"{base_url}/api/research{filter_query}")

    assert status == 200
    assert content_type == "application/json"
    assert data["meta"]["served_from"] == "live"
    assert [f["id"] for f in data["findings"]] == ["f1", "f2"]

    # Filter metadata returned in response
    assert data["meta"]["filters"] == {
        "urgency": ["high"],
        "tier": ["held"],
        "ticker": ["intc"],
    }

    # Invariant: meta.counts reflects unfiltered universe
    counts = data["meta"]["counts"]
    assert counts["total"] == 6
    assert counts["urgency"] == {"high": 4, "medium": 0, "low": 1, "quiet": 1}
    assert counts["tier"] == {"held": 4, "watchlist": 2}
