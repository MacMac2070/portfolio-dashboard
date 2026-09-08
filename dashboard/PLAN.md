# Portfolio dashboard — roadmap

The expense tracker has carried its own plan since August; this is the
dashboard's. It records what the pipeline-hardening pass of 2–3 Sep 2026
delivered and what is knowingly still open, so the next session starts from
a list rather than from 10,000 lines of docstrings.

## Done (Sep 2026)

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

- [ ] **Flex query fields.** Tick `taxes` on the Trades section (stamp duty is
      still missing from lot cost: HSBA shows £14.26 of "FX" that is duty) and
      enable the Corporate Actions, Transfers and Conversion Rates sections.
      See `config.local.json.example`.
- [ ] **The Sunday snapshot row** (2026-07-26) in `nav_history.jsonl`: delete it
      by hand; the nightly job no longer records weekend rows.
- [ ] **Instrument service tiers.** `instrument._attempt` still hard-codes
      yfinance → default; route it through `providers.tiers()` so FMP answers
      for US names on a Yahoo outage.
- [ ] **Browser-side maths** (HHI, concentration, sector rollups, the TWR
      series, market sessions) duplicated from the server — move server-side
      and ship the numbers.
- [ ] **UK capital gains** — out of scope by decision; the lots exist so an
      export to cgt-calc (Section 104, same-day, 30-day) is a small script.
- [ ] **Stock-page range windows** anchor on today while the equity curve
      anchors on the last NAV date; align them.
- [ ] **IB Gateway** has refused connections since 30 Aug; the Flex failover
      carries everything, but live quotes and cash need it back.
