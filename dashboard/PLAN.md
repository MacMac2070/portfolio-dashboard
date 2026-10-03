# Portfolio dashboard — roadmap

The expense tracker has carried its own plan since August; this is the
dashboard's. It records what the pipeline-hardening pass of 2–3 Sep 2026
delivered and what is knowingly still open, so the next session starts from
a list rather than from 10,000 lines of docstrings.

## Done (Sep 2026)

- [x] Overview layout pass (22 Sep 2026, from Cursor's layout review): positions
      below the fold; news + upcoming-dividends brief row on the first screen;
      one news surface on Overview (Outlet card removed, earnings line in the
      brief head, full digest on Market watch); movers as one strip under the
      book; sidebar is the only navigation (topbar tab strip removed, sidebar
      clicks write the hash).
- [x] Trust/UI pass (20 Sep 2026): `stores.asof` lag check, health ledger/cash
      lag, Overview YTD + positions above the fold, unified Holdings nav,
      equity-curve tone from period P&L, loading skeletons, stock/financials
      `hidden`.

- [x] Atomic writes for every JSON document (`store.write_json`).
- [x] Nightly job: watchdog (40 min), directory budget (10 min), heartbeat
      (`data/last_run.json`), content-aware gate, Flex Trades / ConversionRates /
      Corporate Actions merged nightly, desktop + mail alerts, opt-in auto-commit.
- [x] `/api/health`, the header chip and data banner, `adapter/health.py` CLI.
- [x] One shared circuit breaker (`adapter/resilience.py`) on every Yahoo call;
      negative caches in the click-driven services; rotated logs.
- [x] Reconciliation (`adapter/reconcile.py`, `data/quality.json`): replay,
      NAV continuity, deposits, composition, live-vs-Flex, unmapped/unmarked.
- [x] Trade-date FX cost basis (`adapter/lots.py`), both conventions on every
      row, FX P&L, a missing rate is a failure not an assumption.
- [x] Quote store (`adapter/quotes.py`); benchmark restated in sterling.
- [x] Corporate actions and transfers in the replay, the lots and the flow.
- [x] Provider layer with an ECB FX tier and FMP for US listings.
- [x] `requirements.txt`, README "Pipeline health".

## Open

- [ ] **Flex query fields (operator).** In IBKR Flex Query Manager, tick `taxes`
      on Trades and enable Corporate Actions, Transfers and Conversion Rates.
      The example config now lists them; the portal still needs the ticks.
      Without `taxes`, stamp duty still lands in FX P&L (e.g. HSBA).
- [x] **Sunday snapshot row** (2026-07-26) removed from `nav_history.jsonl`.
- [ ] **Instrument service tiers.** `instrument._attempt` still hard-codes
      yfinance → default; route it through `providers.tiers()` so FMP answers
      for US names on a Yahoo outage.
- [x] **Server-side HHI / YTD TWR** on the snapshot (`derive` +
      `attach_headline_returns`); Allocation prefers payload HHI.
      Browser still duplicates some TWR chart series and market sessions.
- [ ] **UK capital gains** — out of scope by decision; the lots exist so an
      export to cgt-calc (Section 104, same-day, 30-day) is a small script.
- [ ] **Stock-page range windows** anchor on today while the equity curve
      anchors on the last NAV date; align them.
- [ ] **IB Gateway** has refused connections since 30 Aug; the Flex failover
      carries everything, but live quotes and cash need it back.
- [ ] **Flex failover NAV ≠ broker NLV** (Cursor audit, 20 Sep). Failover NAV
      is cash + repriced invested, so the KPI can step between the day and
      night figures without a hard fail; reconcile against the statement NLV
      and warn on a gap.
- [x] **"Live" wording while marks are delayed** (Cursor audit). Done 22 Sep:
      the pill reads "Repriced HH:MM · quotes up to 15 min late · book as of
      <date>", the page polls every 15s on the Flex feed, FX moves intraday,
      units slips and large moves are named in `meta.repricing`, and the quote
      breaker is separate from the news one.
- [ ] **Cost convention flips silently** (`flex-fifo@spot` vs
      `ibkr-average@spot`) and unpriced rows drop from totals rather than
      being flagged (Cursor audit).
