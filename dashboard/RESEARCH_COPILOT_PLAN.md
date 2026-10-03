# Research Co-Pilot — design plan

Status: design, not yet built. Written 25 Sep 2026 after a design
conversation in Cowork; revised same day once agent count, hosting, and
Vercel timing were locked in. Replaces the never-built "Nightly Digest" and
"SK Hynix thesis-drift agent" from earlier planning — this is that feature,
scoped properly, not a third system alongside them.

## Correction on architecture

Earlier planning (and an earlier Cowork conversation that produced this doc)
assumed the dashboard was heading to Next.js + Vercel + Supabase. It isn't,
today — `README.md` and `PLAN.md` in this folder show the real system: a
local Python server (`serve.py`) reading JSON documents from `data/`, fed by
`adapter/*.py` and a live IB Gateway connection, with **nothing in the
refresh path currently involving Claude**. Everything below is written
against that real architecture. The Vercel migration (see below) is planned
but not started, so treat "local disk, `serve.py`" as the current truth and
the cloud-store version as the target state once that migration happens.

## What this is

Not autonomous. It doesn't trade, and it doesn't tell you what to do. It
looks at your 15 EOD positions (per `data/positions_eod.json`) the way an
analyst covering your book would: pulls quant fundamentals, earnings,
insider and institutional activity, sector/macro trends and management
commentary from several sources, cross-references them against each other
and against a thesis you've recorded for each holding, and surfaces two
kinds of things — new evidence that changes the picture, and contradictions
between sources that are worth your attention. Everything it finds is
sourced and shown in the dashboard; nothing is a buy/sell instruction.

Separately, ~35 additional tickers (the rest of the Mag 7 you don't hold,
plus other names you're watching and might buy) get a much lighter,
cheaper scan rather than the full pipeline — see "Watchlist tier" below.

## Data sources

All already connected, no new subscriptions needed:

- **IBKR** — your actual positions, cost basis, live P&L (same feed
  `serve.py` already uses).
- **Yahoo Finance** — fundamentals, estimates, US insider trades (Form 4).
- **OpenBB** — broader cross-market fundamentals and screening.
- **Web search** — SEC EDGAR filings (US names), DART (Korea), EDINET
  (Japan), HKEX disclosures (HK names — Cathay Pacific, Tencent), earnings
  call transcripts, CEO commentary, sector news.
- **FMP** — left on the free tier. Checked pricing: even the $49/mo Premium
  tier only adds UK/Canada coverage, not Asia, and 13F/insider-trade data is
  inherently US-only (SEC filing types) regardless of tier, so it doesn't
  help the Korea/HK/Japan-heavy part of the book. Not worth paying for on
  this project specifically.

Existing per-holding earnings archives already sit in
`../Stock research/<Company> — Earnings Archive/` — the pipeline should read
these as prior context, not just start from zero each run.

## Subagent pipeline — the 15 holdings (10 agents per run)

Built as a Workflow (Cowork's scripted multi-agent orchestration tool:
`agent()` / `parallel()` / `pipeline()` / `phase()`), run per holding. Sized
against TradingAgents' actual structure (the closest reference repo),
dropping the parts that don't apply here — no Trader or Fund Manager agent,
since this never acts autonomously or executes anything. 10 agents total,
inside the Workflow tool's default ~10-agent-per-run guideline:

1. **Analyst stage — 4 agents, parallel:**
   - Fundamentals/quant: earnings, net profit, margins, valuation vs peers
   - Insider & institutional: Form 4s, 13F changes (US names), local
     regulator filings (Asia names)
   - Sector/macro: what's moving the sector, comparable companies, macro
     trends
   - Management commentary: what CEOs/CFOs are actually saying, on calls
     and in interviews
2. **Debate stage — 2 agents:** a bull-case researcher and a bear-case
   researcher, each a separate agent (not a merged "debate stage"), forced
   to argue from the analyst stage's findings — the TradingAgents pattern.
   This is what makes contradictions surface instead of getting smoothed
   over into one summary.
3. **Risk read — 3 agents:** aggressive, conservative, and neutral takes on
   what the debate actually means for the position, run separately rather
   than folded into one risk paragraph.
4. **Synthesizer — 1 agent:** reconciles the debate and the three risk
   reads against the existing thesis record, and produces the updated
   pillar status, a list of dated findings with sources, and a flag for
   whether anything crosses the "you should know this" bar.

4 + 2 + 3 + 1 = 10 agents per holding, per run.

## Watchlist tier — the other ~35 tickers

Running the full 10-agent pipeline on 50 names every cycle is 500 agent
calls per run — far past sensible cost and runtime for a single scheduled
job. The ~35 watchlist names (Mag 7 names not held, other stocks you're
interested in and might buy) get a cheap, frequent scan instead of the deep
pipeline, and only escalate to the full 10-agent run for a specific name
when that scan flags something worth a proper look.

Two agents per scan, not one — these are genuinely different source types,
not two ways of doing the same search:

- **Signals agent** — the quiet, technical, filed-not-reported stuff: an
  unusually large insider buy, an institutional position change, a 13F
  filing, an options-flow-style signal. Comes from structured filings and
  data feeds (SEC EDGAR, Form 4s, 13F changes), not headlines, and needs
  that different search approach.
- **Narrative agent** — the public, said-out-loud stuff: an analyst rating
  change, a CEO saying something notable on a call or in an interview, a
  sector-wide story that would move a watched name even without
  company-specific news. Closer to what people mean by "news" day to day.

Kept separate because a name can have a large insider buy with zero news
coverage, or a lot of news noise with no insider activity at all —
collapsing the two into one agent would let whichever source is more
talkative in a given week crowd out the other.

## Thesis memory (per holding)

Modelled on HunterCode's investment-thesis-memory pattern: record why you
bought, keep re-testing it.

- Cost basis and entry thesis (free text, written once)
- Five key pillars (the claims the thesis depends on)
- Per pillar: status (intact / weakening / broken), last-checked date,
  evidence
- Full evidence log: dated, sourced, links back to the analyst/debate
  findings that produced each status change

## Where the data lands

Today: no Supabase, no cloud store. Two new local JSON documents in
`data/`, written with the same atomic-write convention (`store.write_json`)
and `schema_version` / `generated_at` header shape already used by
`data/quality.json`:

- `data/research_findings.json` — dated findings per holding, with sources
- `data/thesis.json` — the five-pillar record per holding

`serve.py` gets a new read-only endpoint (`/api/research`, following the
existing `/api/snapshot` / `/api/health` pattern) that serves these to the
page. The frontend gets one new panel that queries it — same shape as every
other panel, nothing structurally new for the frontend to learn.

### Data contract for `research_findings.json` (built 2 Oct 2026)

The page, the endpoint and a labelled sample now exist; the agents do not.
`adapter/research.py` holds the authoritative copy of this schema in its
docstring, and `tests/test_research.py` pins it.

```json
{
  "meta": {
    "generated_at": "ISO-8601", "run_id": "string",
    "status": "ok | partial | failed", "next_run_at": "ISO-8601 or null",
    "held_count": 15, "watched_count": 22, "is_sample": false, "errors": [],
    "last_finding_at": "ISO-8601, optional"
  },
  "findings": [{
    "id": "stable across runs", "ticker": "universe key, as #stock/<key>",
    "tier": "held | watchlist", "sector": "Semiconductors",
    "urgency": "high | medium | low | quiet",
    "headline": "one line", "why_it_matters": "one or two plain sentences",
    "bull": ["..."], "bear": ["..."],
    "risk_views": {"aggressive": "", "conservative": "", "neutral": ""},
    "evidence": [{"label": "Form 4", "detail": "", "url": "https://...",
                  "kind": "filing | call | 13f | news | sector | archive"}],
    "pillars_touched": [{"pillar": "Turnaround", "change": "weaker | steady | stronger"}],
    "first_seen": "ISO-8601", "changed_since_last_run": true
  }],
  "watchlist_triage": [{"ticker": "NVDA", "signals_flag": true,
                        "narrative_flag": false, "escalated": true, "note": ""}],
  "runs": [{"run_id": "", "at": "ISO-8601", "status": "ok",
            "agents_used": 214, "skipped": 0, "failed": 0}]
}
```

- **Writing it.** Agents replace the whole file with `store.write_json`
  (temp file in `data/`, fsync, `os.replace`). The server never writes it.
- **What `/api/research` adds to `meta`.** `served_from` (`live`, `sample`,
  or `none` when neither file exists), `error` (one string or null, beside
  the file's own `errors[]`), `schema_version` (1), `dropped`, `counts` (per
  urgency and tier, before filters) and `filters`. Query params `urgency`,
  `tier` and `ticker` take comma lists and narrow `findings` only.
- **Tolerance.** Missing file: empty payload. Unparseable file, or a top
  level that is not an object: `status: "failed"` with a readable `error`.
  Unknown fields anywhere are dropped; findings without an id, ticker or
  headline are skipped and counted; evidence links that are not absolute
  http(s) are nulled.
- **The sample.** `data/research_findings.sample.json` is served only while
  the real file is absent, and the page shows "Sample data. Not real
  findings." Once the real file exists the sample is never read, even if
  the real file is broken.
- **Pillars.** `pillars_touched[].change` is a movement this run (weaker,
  steady, stronger). It is not the pillar status in Thesis memory above
  (intact, weakening, broken), which belongs to `thesis.json`.

This changes once the Vercel migration happens (see below) — the write
target becomes whatever cloud store the deployed app reads from, not this
machine's disk.

## Dashboard hosting: Vercel migration — planned, not yet started

Decision, stated plainly for the record: the dashboard will move to Vercel.
Timing — **once most of the features and the design are done**, not before.
Until that migration happens, the dashboard stays on localhost, served by
`serve.py` off local JSON files, exactly as described above. Don't let this
doc drift out of sync with the code: everything under "Where the data
lands" is the *current* state; this section is the *planned* one.

The move is a bigger step than "add a hosting step": `serve.py` currently
holds the live IB Gateway connection in a background thread on the Mac.
Vercel's serverless functions can't hold that kind of persistent local
connection or persistent local disk, so the migration means standing up a
real cloud data store for the backend too, not just redeploying the
frontend. That rearchitecture is out of scope for this doc until it's
actually underway — noted here so the pipeline's "where it runs" decision
below is understood as downstream of this decision, not independent of it.

## Where it runs — Cowork scheduled task (confirmed)

Locked in: the subagent pipeline runs as a **Cowork scheduled task**, not
Claude Code on the Mac Mini. This reverses an earlier in-conversation
recommendation (Mac Mini, made when the write target was believed to be
local disk with no near-term hosting change); the Mac Mini isn't needed as
the always-on worker.

The reasoning, now that the Vercel migration above is confirmed as the
plan: once the dashboard is genuinely cloud-hosted, a Cowork scheduled task
writing from the cloud straight to whatever cloud store the Vercel app
reads from is the cleaner architecture — no dependency on the Mac being on,
no dependency on the device bridge being live at write time. This holds
regardless of exactly when the Vercel move happens, because Cowork's
scheduling and Workflow tool are the pipeline's home either way; only the
write target changes (local JSON via the device bridge, today; a cloud
store, post-migration).

Practically, until the Vercel migration lands: a Cowork scheduled task
still runs the 10-agent Workflow, but writing `data/research_findings.json`
and `data/thesis.json` back to this machine needs the device bridge live
(the Mac online, desktop app running) at the moment the schedule fires.
That dependency goes away once the write target is a cloud store instead of
this disk.

Phone alerts stay a separate branch regardless of hosting: a Cowork
scheduled task's own push notification, firing after the findings file
changes and something crosses the alert threshold — independent of whether
you ever open the dashboard.

## Open questions

- Exact alert threshold for a phone push vs. dashboard-only (needs a first
  real run to calibrate — too sensitive and it's noise, too quiet and it
  defeats the point)
- Whether the 15-holding pipeline runs on a rotation or all 15 positions
  every cycle
- What cadence the watchlist tier's signals/narrative scan runs on across
  the ~35 names, and what specifically triggers escalation to the full
  10-agent pipeline for a flagged name
- Cadence and trigger for the Vercel migration itself ("most features and
  design done" — needs a concrete checklist once the dashboard is closer to
  that point)
- Whether `.claude/agents/` in this repo (currently `ui-designer`,
  `architect-reviewer`, `frontend-developer`) should also host the new
  analyst/debate/risk/synthesizer subagent definitions, or whether those
  stay Cowork Workflow-only
- SKILL directory at `github.com/agentpit-io/hunter-community/tree/main/skills`
  worth reading in full for the thesis-memory and truth-verification prompts
  before writing this pipeline's own prompts
