# Prompt for Claude Code: build the Research page

Paste everything below the line into Claude Code, started in the `dashboard/` folder.

---

You are working in my Portfolio Dashboard repo (`dashboard/`). Build the **Research page**: a new view that will display findings written by a research co-pilot (a set of AI agents that run on a schedule and write a JSON file). The agents do not exist yet. Your job is only the page, its API route, its data contract, and a clearly labelled sample fixture so the page can be developed and tested now.

## Read first
1. `CLAUDE.md` (frontend rules). Follow it exactly: invoke the `frontend-design` and `ui-ux-pro-max` skills before writing any frontend code, and the `dataviz` skill if you draw any chart or sparkline.
2. `RESEARCH_COPILOT_PLAN.md` (the design doc for the whole co-pilot).
3. `serve.py` (how `/api/desk` and `/api/news` read JSON from `data/` and respond), `js/main.js` (`showTab`, `tabFromHash`, the hashchange wiring), `index.html` (sidebar nav items and `.view` sections), `css/tokens.css`, and one existing view module such as `js/marketwatch.js` or `js/transactions.js` as the pattern for a view with a `route()` function.
4. Look at `screenshots/insp-16-overview-de-ai-pass.jpg` for the visual language to match: dark theme, purple brand, Geist Mono for figures, layered dark surfaces.

## Hard constraints (non-negotiable)
- The co-pilot is **non-autonomous and gives no trading advice**. The UI must never show buy, sell, hold, target price, or "you should" language. Findings describe what happened, why it may matter, and what the evidence is. Add a small permanent line in the detail panel: "Information only, not advice."
- No new dependencies. Stay with vanilla JS modules, hand-built SVG if any chart is needed, CSS variables from `tokens.css`, hash routing.
- Use only tokens for colour, spacing, radius, type. No hardcoded hex values. Light and dark must both work.
- Animate only `transform` and `opacity`, 150 to 300ms, with a `prefers-reduced-motion` fallback. Never `transition-all`.
- Every interactive element needs hover, `focus-visible` and active states. Meet WCAG AA contrast. Never rely on colour alone for urgency: pair it with a text label and an icon or shape.
- Every data view needs loading (skeletons, no layout shift), empty, and error/stale states.
- Data files are written atomically by the agents; the server only reads. Never write to `data/research_findings.json` from the request handler.
- British spelling in all UI copy. No em dashes anywhere in copy or code comments.

## What to build

### 1. Data contract: `data/research_findings.json`
Define and document this schema (put the documentation in a short docstring or a `data/README`-style comment block in the new Python module, and mirror it in the plan doc):

```json
{
  "meta": {
    "generated_at": "ISO-8601",
    "run_id": "string",
    "status": "ok | partial | failed",
    "next_run_at": "ISO-8601 or null",
    "held_count": 15,
    "watched_count": 35,
    "is_sample": false,
    "errors": []
  },
  "findings": [
    {
      "id": "stable string",
      "ticker": "MU",
      "tier": "held | watchlist",
      "sector": "Semiconductors",
      "urgency": "high | medium | low | quiet",
      "headline": "one line",
      "why_it_matters": "one or two plain sentences",
      "bull": ["short points"],
      "bear": ["short points"],
      "risk_views": { "aggressive": "text", "conservative": "text", "neutral": "text" },
      "evidence": [{ "label": "Form 4", "detail": "text", "url": "https://...", "kind": "filing|call|13f|news|sector|archive" }],
      "pillars_touched": [{ "pillar": "Turnaround", "change": "weaker|steady|stronger" }],
      "first_seen": "ISO-8601",
      "changed_since_last_run": true
    }
  ],
  "watchlist_triage": [
    { "ticker": "NVDA", "signals_flag": true, "narrative_flag": false, "escalated": true, "note": "short" }
  ],
  "runs": [
    { "run_id": "string", "at": "ISO-8601", "status": "ok", "agents_used": 150, "skipped": 0, "failed": 0 }
  ]
}
```

Rules: the server must tolerate a missing file (return an empty-state payload, not a 500), a malformed file (return `status: "failed"` with a readable error), and unknown extra fields (ignore them). Validate shape defensively on the server and again on the client.

### 2. Sample fixture
Create `data/research_findings.sample.json` with realistic, **made-up** findings across held names (for example INTC, HY9H, MU-style semiconductor cases, AMZN, 700, HSBA) and a few watchlist rows, with `"is_sample": true`. When the real file is absent, the API serves the sample, and the page shows a persistent banner: "Sample data. Not real findings." When the real file exists, the sample is never used. Cover every state in the sample: high, medium, low, quiet, held and watchlist, a finding with empty bull/bear, and one with a long headline to test wrapping.

### 3. API: `GET /api/research`
Add the route in `serve.py` following the existing handler pattern (`_research` method, `route == "/api/research"` in `do_GET`). Optional query params, all applied server-side: `urgency`, `tier`, `ticker`. Return the whole payload with `meta.served_from` set to `"live"` or `"sample"`. Add a pytest file `tests/test_research.py` covering: missing file, malformed file, valid file, unknown fields, sample fallback flag, filtering.

### 4. The page (`#research`)
Add a sidebar nav item "Research" below Analysis (with an unread-count badge fed from the payload, hidden at zero), a `<section class="view view--scroll" id="view-research">`, a new `js/research.js` exporting `route()` like the other views, a branch in `showTab`, and styles in a new `css/research.css` (link it, do not bloat `app.css`). Layout, top to bottom:

1. **Header.** Title "Research briefing", last run time (with relative time), next run time, a run-status chip (ok, partial, failed), and a feed-health indicator reusing the existing health chip pattern. If `meta.status` is `failed` or the data is older than 36 hours, show a stale banner like the existing feed-disconnected banner: "Last run failed. Showing results from <time>."
2. **Today at a glance.** Three KPI tiles in the style of the Overview KPI row: Needs attention (high plus medium), Changed since last visit, Quiet. "Last visit" is stored per viewer in `localStorage` (wrap in try/catch and render correctly without it).
3. **Filter bar.** Urgency chips, Held/Watchlist toggle, source-type select, sector select, unread-only toggle, and a text search over ticker and headline. Filters live in the URL hash query (for example `#research?urgency=high&tier=held`) so views are linkable and survive reload.
4. **Findings feed (left, wider column).** Grouped under three collapsible headings: Needs attention, Changed, Quiet (collapsed by default). Each row: ticker avatar chip, headline, one-line why-it-matters, evidence source chips, urgency label, held/watchlist label, relative time. Selecting a row (click or Enter) selects it and updates `#research/<finding id>`. Arrow keys move selection. Unread findings carry a visible marker and become read when opened (stored in `localStorage`).
5. **Detail panel (right column, sticky).** Headline and ticker with a link to `#stock/<ticker>` if that holding exists; plain-English why-it-matters; bull and bear columns side by side; the three risk views; evidence list with external links (`rel="noopener noreferrer"`, open in new tab); pillars touched with a change arrow and a text label; the "Information only, not advice" line. Empty selection shows a designed prompt, not a blank box. On narrow screens the panel becomes a full-screen sheet opened from the feed, with a back button.
6. **Watchlist triage.** A compact table: ticker, signals flag, narrative flag, escalated yes/no, note. Tabular figures, right-aligned numeric columns, sortable by clicking headers (with `aria-sort`).
7. **Run history.** A collapsed disclosure showing recent runs (time, status, agents used, skipped, failed).

Responsive: desktop first (two columns), tablet stacks the detail below, phone is one column with the sheet pattern. Check at 1440, 1024, 768 and 390 widths.

### 5. Wiring details
- `tabFromHash` already finds `view-<name>`, so `#research` works once the section exists. Make sure deep links `#research/<id>` and the filter query survive reload and back/forward.
- Refresh on show, and poll `/api/research` every 60 seconds only while the Research view is visible. Stop polling on other tabs.
- Escape all text from the JSON before inserting into the DOM (use `textContent` or a safe helper). Evidence URLs must be validated as `https:` or `http:` before being used as `href`.
- Keep numbers formatted via `js/format.js`.

## Process
1. Start by writing a short plan and listing the files you will touch. Do not touch unrelated views.
2. Build the API and fixture first, with tests passing (`/opt/anaconda3/bin/python3 -m pytest tests/test_research.py`).
3. Then the page. Serve on localhost using the existing server (port 5174, `/opt/anaconda3/bin/python3 serve.py`). Do not start a second server if one is running.
4. Screenshot from localhost into `screenshots/` with auto-incremented names (never overwrite), read them back, and compare against `research_page_wireframe.html` (in the repo root or given to you) for structure. Do at least two rounds, covering: loaded, loading skeleton, empty, stale/failed, sample banner, detail panel open, dark and light, and the 390px layout.
5. Run the existing test suite to confirm nothing else broke.
6. Finish with a summary: files added and changed, the final schema, how to feed real data (agents write `data/research_findings.json` atomically), and anything you chose not to do.

## Out of scope
Do not build the agents or the scheduled task, any Supabase or Drive storage, push notifications, the Overview strip, the per-stock Research tab, or the inbox drawer. Leave clean hooks for them (the unread count in the nav badge and a documented payload) but do not implement them.
