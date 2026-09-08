# Portfolio Dashboard

Live GBP investment dashboard reading your real IBKR account. Implements
`Design.pdf`. Nothing in the refresh path involves Claude.

## Running it

```bash
/opt/anaconda3/bin/python3 serve.py 5174         # serves + holds the live feed
open http://localhost:5174
```

That one command is enough. `serve.py` starts a background thread that opens a
single `ib_async` connection to IB Gateway and keeps it open, so Overview and
Holdings update on their own — no page reload, no `refresh.py` run.

`serve.py --no-live` skips the feed and serves static files only, for when
Gateway is down and you just want to look at the page.

**Use `/opt/anaconda3/bin/python3`.** That is the only interpreter on this
machine with `ib_async` (2.1.0) and `openbb` (4.7.2); the default `python3`
(3.14.2) has neither. Every script hardcodes it.

IB Gateway on `127.0.0.1:4001` (running, logged in, API clients enabled) is
the *best* source, not a requirement: when it is unreachable, the page serves
from the Flex-EOD feed instead — yesterday's Open Positions off the Flex Web
Service, repriced every 60s by the same delayed yfinance quotes everything
else uses (`adapter/flexfeed.py`). The feed chip shows amber
`EOD · positions <date> · quotes delayed` in that state; the Gateway coming
back upgrades it to LIVE without a restart. The Open Positions section must
be enabled on the Activity Flex query — see `config.local.json.example`.

## How data gets in

Three independent paths, on three different clocks:

```
LIVE          IB Gateway :4001 ═══ held open ═══ adapter/feed.py
(seconds)       clientId 11                        │  in serve.py's thread
                                                   ▼
                                          GET /api/snapshot ──► page polls 3s

SNAPSHOT      IB Gateway :4001 ──┐
(on demand)     clientId 7       ├── adapter/build.py ── data/portfolio.json
              openbb ────────────┘                       (sparklines live here)

HISTORY       IBKR Flex Web Service ── adapter/backfill.py ── nav_history.jsonl
(daily)       IB Gateway clientId 7 ── adapter/refresh.py     transactions.jsonl

WORLD BOARD   openbb ─── adapter/markets.py ─── GET /api/markets ──► page polls 30s
(60s)         11 index series; exposure folded in from the live feed above

OUTLET        yfinance ── adapter/news.py ─── GET /api/news ──► page polls 5m
(20 min)      headlines for held markets/sectors, ranked by the book's own
              weights; earnings est-vs-actual rides along; cached to news.json
```

The three never block each other: the feed holds **clientId 11**, `build.py`
and `refresh.py` use **7**, `test_ibkr_connection.py` uses **1**. The daily job
runs happily while the feed is connected.

| File | Role |
|---|---|
| `adapter/feed.py` | **the live connection** — held open, recomposed every 3s |
| `adapter/derive.py` | **shared maths** — used by both the feed and the builder |
| `adapter/ibkr.py` | one-shot account, positions, executions |
| `adapter/marketdata.py` | FX, prior closes, sparkline series via openbb |
| `adapter/regions.py` | conId → region (hand-maintained; see below) |
| `adapter/build.py` | derivations → `data/portfolio.json` |
| `adapter/flex.py` | Flex Web Service client (historical NAV + trades) |
| `adapter/store.py` | append-only JSONL, merge-by-key so re-runs never duplicate |
| `adapter/refresh.py` | the scheduled job: build + record the day |
| `adapter/backfill.py` | one-time Flex history import |
| `adapter/watchlist.py` | openbb quotes for tickers you don't own (Watchlist) |
| `adapter/markets.py` | world board: market registry, sessions, index poller |
| `adapter/marks.py` | issuer marks: vendored SVG set under `assets/logos/`, slug lookup for search-added names |

## What is live, and what isn't

`GET /api/snapshot` returns the newest values plus `meta.connected` and
`meta.last_refresh`. The page polls it every 3s and drives the live/stale
indicator off those two fields — **live** means connected *and* refreshed within
15s; anything else shows the red disconnected banner and keeps the last values
on screen rather than blanking them.

| | Source | Live? |
|---|---|---|
| NAV, cash, invested, unrealised P&L | IB account values | yes |
| Positions, price, market value | IB `portfolio()` + delayed tickers | yes |
| Day change %, daily P/L, Top movers | `marketPrice` vs `ticker.close` | yes |
| Region / currency / concentration | derived locally, IBKR FX rates | yes |
| **30d sparklines** | last `build.py` run | **no** — labelled "as of HH:MM" |

Prices come from `reqMarketDataType(3)` — delayed by 15 minutes, needing no paid
subscription, which is what the header chip has always said. Account values
themselves arrive on IBKR's own push cadence (~3 min, plus immediately on a
trade); ticker prices move continuously between those pushes, and position
market values are repriced off the tick so the two never disagree on screen.

Day change comes from the ticker's previous close, **not** from IBKR's
per-position `dailyPnL` — see the note below on why that figure is unusable.

Sparklines need a historical series, which IB market data does not carry, so
they keep their last values and say so. Nothing else on Overview or Holdings
depends on OpenBB while the feed is running.

### Keeping it running

Optional, and separate from the daily job:

```bash
./install-live-feed.sh          # launchd keeps serve.py alive
./install-live-feed.sh --remove
```

| File | Role |
|---|---|
| `adapter/ibkr.py` | account, positions, executions over `ib_async` |
| `adapter/marketdata.py` | FX rates, prior closes, sparkline series via openbb |
| `adapter/regions.py` | conId → region (hand-maintained; see below) |
| `adapter/build.py` | derivations → `data/portfolio.json` |
| `adapter/flex.py` | Flex Web Service client (historical NAV + trades) |
| `adapter/store.py` | append-only JSONL, merge-by-key so re-runs never duplicate |
| `adapter/refresh.py` | the scheduled job: build + record the day |
| `adapter/backfill.py` | one-time Flex history import |

## Pipeline health

Since 2 Sep 2026 the pipeline says how it is doing, instead of leaving you to
read `logs/refresh.log` to find out a night was missed.

```
curl -s localhost:5173/api/health | python3 -m json.tool   # one verdict, every check
/opt/anaconda3/bin/python3 adapter/health.py                # the same, in the terminal
```

`adapter/health.py` reads the files on disk — the heartbeat the nightly job
writes to `data/last_run.json`, the NAV and positions stores, the snapshot's
own meta — plus what the server knows live: the feed's meta and the provider
breakers. Each check is `ok`, `pending`, `warn` or `fail` with a one-line
summary and, when it is not ok, a hint that says what to do. The header chip
("Data ok" / "Data · 2 warnings" / "Data · failing") is that verdict; hover it
for the list. A banner under the tape repeats the worst check the feed pill
does not already express — a missed nightly run, a stale NAV series, an
archive that lost rows.

What the nightly job now guards against, because of the night of 31 Aug 2026
when a provider call never returned and two nights went unrecorded:

- **A watchdog** ends the run at forty minutes whatever stage it is in, and
  records which. Every file write is atomic (`store.write_json`, same
  temp-and-rename as the JSONL stores), so a hard exit costs nothing.
- **The directory rebuild** runs on its own thread with a ten-minute budget and
  is abandoned rather than waited for; the previous directory keeps serving.
- **A twenty-hour gate**: a run that succeeded within twenty hours is not
  repeated, so the LaunchAgent's `RunAtLoad` (it fires at login as well as at
  23:30) is safe. `adapter/refresh.py --force` overrides it.
- **The Flex Trades section is merged nightly.** Until now only the one-time
  backfill ever wrote the ledger, so it stopped at the last hand run while
  positions moved on.
- **A desktop notification** on a failed or degraded run (`adapter/notify.py`;
  mail too, with an `alerts.smtp` block in `config.local.json`).

Provider outages are handled by one shared circuit breaker
(`adapter/resilience.py`): after three consecutive failures a provider is left
alone for two minutes, doubling to thirty, with one probe let through when
the window lapses. Every poller keeps its last good values meanwhile and says
so in `meta.error` and `meta.breaker`. Four days of Yahoo's "401 Invalid
Crumb" used to mean eight hundred retries fifteen seconds apart.

Logs rotate (`logs/serve.log`, `logs/refresh.log`, 5 MB × 5 and 2 MB × 5);
launchd's own `*.launchd.log` files only catch an interpreter that dies before
logging starts. Dependencies are pinned in `requirements.txt`.

### Does the data agree with itself?

```
/opt/anaconda3/bin/python3 adapter/reconcile.py [--live http://localhost:5173/api/snapshot]
```

`adapter/reconcile.py` is the balance-assertion layer, run by the nightly job
after the stores are refreshed and written to `data/quality.json`, which
`/api/health` folds in as `quality.*`. Every check is a pure function over
rows:

- **positions.replay** — `transactions.jsonl` replayed (with corporate
  actions) against `positions_eod.json`, to a millionth of a share.
- **nav.continuity** — duplicates, missing weekdays (IBKR reports every
  weekday, holidays included), zero rows after inception, a snapshot row Flex
  never replaced, a snapshot dated on a weekend.
- **flows.deposits** — the deposits the daily return excludes, checked per
  period against the Change in NAV's own `depositsWithdrawals`, with a
  three-day settlement window at the edges. If the Cash Transactions section
  ever stops carrying Deposits/Withdrawals this is what notices, and the
  Performance page marks the return "deposits unverified".
- **nav.composition** — the positions × FX against the statement's own
  `stock` figure, and stock + cash + accruals against `total`. Needs the
  Equity Summary's split, which the NAV rows now carry.
- **positions.unmapped / unmarked**, **actions.unbucketed**,
  **cash.unbucketed** — a contract the quote tables cannot name, a line Flex
  marks at nothing, a corporate-action or cash type the tables have not met.
- **live.positions** — the live feed against the EOD snapshot, with `--live`.

### Two costs on every row

`cost_gbp` is IBKR's cost in the instrument's currency at today's rate: what
the position would cost to buy now. `cost_gbp_tradedate` is what was actually
paid in sterling, from FIFO lots built by `adapter/lots.py` out of the ledger,
each lot carrying `fxRateToBase` from its own fill (older rows take the rate
from the ConversionRates store) and its commission and taxes. The holdings
table prints the return on the second under the first — "+£683 paid · FX
+£10" — and the Overview's unrealised tile carries the same in its title.
`fx_pnl_gbp` is the difference: the currency's own move since purchase, which
the first convention hid inside "unrealised". The trade-date figure is
withheld for any position whose ledger quantity disagrees with the broker's;
the replay check says why.

### Prices are kept

`adapter/quotes.py` is a SQLite table of daily closes (`data/quotes.sqlite3`,
gitignored, rebuilt from the providers). The world board, the sparklines and
the sterling crosses write to it and draw from it, so a provider outage
serves stored closes with their own date, the benchmark line draws before the
market feed has warmed, and the board asks for eleven indices × seven days a
minute rather than × 420. A missing day is never filled from a later one.

A non-GBP benchmark is now restated in sterling at daily rates — the return
a sterling investor would have had — with the local line alongside as
`points_local`; the legend says "USD · in GBP at daily rates". Beta and
alpha on the Performance page use the same restated series.

### Corporate actions and providers

Splits, ticker changes, spin-offs, mergers, delistings and transfers are read
from the Flex Corporate Actions and Transfers sections into
`data/corporate_actions.jsonl` as signed share quantities, which the replay
and the lot engine apply beside the fills; the NAV flow gives them their own
nodes so `drift` is only what the statement does not explain.

`adapter/providers.py` is the one place openbb is imported and the operator's
keys applied (`"providers"` in `config.local.json`, or the ones already in
`~/.openbb_platform`). FMP is offered as a second tier for US listings only —
its free plan refuses venue-suffixed symbols — and the ECB's keyless daily
reference rates are the second source for FX, ahead of the broker's own
account rates.

## The symbol directory

The search bar reaches past the 35 curated names in `universe.py` through a
cached local directory, `data/directory.sqlite3` (~15.8k symbols, WAL).

```bash
/opt/anaconda3/bin/python3 adapter/directory.py            # rebuild
/opt/anaconda3/bin/python3 adapter/directory.py --search tenc
```

Built from the only providers that can *enumerate*: `equity.search` via nasdaq
(twice — `is_etf` splits into two disjoint sets) and sec. All of them are US
only. openbb has no yfinance fetcher for `equity.search` or `etf.search`, and
FMP's ETF search needs a paid tier, so **there is no enumerable source for HKEX,
SGX, LSE or Frankfurt** — international names arrive one at a time via
`/api/lookup`, which asks `yfinance.Lookup` after the typing pauses and writes
what it learns back into the table. A rebuild deletes only rows it built, so
those survive.

The browser downloads the table once (190 KB gzipped) and keeps it in
IndexedDB, re-checking with `?have=<version>` — a match costs 78 bytes. All
filtering is local; there is no request per keystroke.

Prices in search come only from the live feed. A remote result's price stays
hidden until its currency is known, because Yahoo's search returns a bare number
and a London name's is in pence — `directory.learn_currency` records the real
one the first time the stock page opens that instrument.

"Watch" on an untracked name appends it to `data/watchlist_extra.json`, which
`universe.py` merges at import; it lands in the **Other** sector and is quoted
from the next watchlist poll. The server never rewrites `universe.py`.

## The daily job

```bash
./install-daily-job.sh          # LaunchAgent, fires 23:30 local
./install-daily-job.sh --remove
launchctl kickstart -p gui/$UID/com.portfolio-dashboard.refresh   # run now
```

launchd rather than cron — it survives reboot and logout and needs no terminal
open. Logs to `logs/refresh.log`. If the Gateway is down when it fires, the job
marks the snapshot stale and exits; re-run `backfill.py` to recover the day.

## Filling in the chart

The equity curve needs history that the TWS API cannot supply — it has no
NAV-history request at all. Until there are 5 points the chart shows its
"collecting history" state honestly.

To fill it immediately, configure Flex (a one-time job in IBKR Account
Management, no Claude involved):

1. Settings → Account Reporting → **Flex Web Service** → generate a token
2. Create two Flex queries and note each Query ID:
   - **Activity Statement** including the *Net Asset Value (NAV) in Base*
     section → daily NAV back to inception (13 Oct 2025)
   - **Trade Confirmation** → execution history
3. `cp config.local.json.example config.local.json`, fill in the three values
4. `/opt/anaconda3/bin/python3 adapter/backfill.py`

`config.local.json` holds a live credential — keep it out of version control.

Add the **Change in NAV** and **Cash Transactions** sections to that same query
while you are there — the NAV flow, income and cost charts all read them, and
each says which section it is waiting for until you do.

There is no monthly setting to find. Change in NAV reports **one element for
whatever range it is asked for**, and the Flex query's own *Period* option
(Last Month, Last 365 Calendar Days, Month to Date, …) picks that range rather
than breaking it down. So month-level granularity comes from asking a month at
a time: `backfill.py` runs one whole-span request for the Sankey, then one
`fd`/`td`-scoped request per calendar month since inception for the P&L bars.

That second pass is paced. IBKR allows one SendRequest per second **and** ten
per minute, so the requests go out 6.5s apart and a full year takes a few
minutes. `--months N` limits it to the last N months:

```bash
/opt/anaconda3/bin/python3 adapter/backfill.py --months 1   # one month, to check
/opt/anaconda3/bin/python3 adapter/backfill.py              # everything
```

A window that comes back wider than it was asked for is refused rather than
stored: it means `fd`/`td` were ignored, and filing a whole-span row under a
month's key would make the P&L chart count all of history as one month.

The daily job keeps it current by itself, at two requests rather than a dozen —
`refresh.py` re-pulls only the whole span and the current month, which is the
only one whose figures can still move, and skips even that if the store was
written within the day. Activity Statement data only changes once, at IBKR's
close of business.

## Performance: attribution

Three charts, all from Flex sections that are off by default, so each has an
empty state naming the section to switch on rather than an empty axis.

- **NAV flow** — a two-stage Sankey: sources feed one *Gross value* node, which
  splits into *Ending NAV* and the cost stack. FX is its own labelled node
  rather than netted into mark-to-market, because with the exposure
  concentrated in Hong Kong, China, Korea and Japan, currency is too large a
  part of the answer to hide inside another figure. Drawn from the widest
  reporting period in the store; if the flows and IBKR's own ending value
  disagree the difference is printed under the chart rather than smoothed away.
- **Dividend income / Monthly P&L** — one card with a toggle. Income is
  dividends, plus cash interest when IBKR pays any. P&L is mark-to-market plus
  realised plus the change in unrealised, read per month off the sub-periods,
  so deposits are excluded by construction rather than netted out.
- **Costs** — drawn bands over itemised figures, all read off the *Change in
  NAV* sub-periods rather than off cash transactions. IBKR has no Commissions
  cash-transaction type at all — commission is charged inside the trade record
  — so sourcing this from cash left the largest cost at zero and the card
  totalled £3.53 against £168.57 actually paid. Transaction tax, the second
  largest, was missing for the same reason. Both charts on the page now read
  the same section and cannot disagree. Bands that are zero in every month are
  dropped rather than drawn flat, because a legend entry that visibly does
  nothing when clicked reads as a broken toggle rather than as "you have paid
  no interest"; the tooltip still itemises the genuine zero. Green and red stay
  reserved for the P&L convention, and the remaining hues were checked for
  colour-vision separation (the worst rejected pair measured ΔE 0.8 for
  deuteranopia), which is why the stack draws fewer bands than it itemises.

Clicking a legend band switches it off: the axis rescales, the tooltip total
follows, and a hidden band takes its detail rows with it. The last visible band
cannot be switched off.

## Allocation: the same holdings, three ways

Region answers *where is this exposure*, sector answers *what kind of business
is this*, currency answers *what am I exposed to when sterling moves*. They are
separate axes — XDJP is Japan by region and an ETF by sector, and both are
correct — so `#allocation/region|sector|currency` switches one ring between
them rather than showing three rings at once.

One ring, not three, for a second reason: `--cat-1..7` are assigned per entity
and never cycled, so a hue means one thing for as long as that entity is in the
portfolio. Two rings on one screen would make the same hue mean "Hong Kong /
China" here and "Semiconductors" there.

Hovering or keyboard-focusing a slice swaps the ring's centre, the KPI strip
above it and the highlighted rows in the positions list underneath — one piece
of state, three readouts. The ring and its hover behaviour live in `js/alloc.js`
and are shared with Overview's donut, so there is one implementation of it.

## Transactions

`data/transactions.jsonl`, written by `store.merge_transactions` from
`flex.parse_trades` and read straight off disk by `js/transactions.js` — a
static file like `nav_history.jsonl`, because there is nothing to derive
server-side and the browser can sort a few hundred fills itself.

Seven sortable columns; date descending by default, since the first question a
trade log answers is what happened most recently. Consideration stays in the
trade's **own** currency: Flex carries no FX rate per execution, and converting
at today's spot would restate a year-old trade at a rate that never applied to
it. Commission is formatted as cash at two decimals rather than through
`price()`, whose magnitude rule gives a £2.41 fee three places.

Buy and sell read as words, with the tint as the second cue — the same rule the
P&L colours follow. IBKR's own `BOT`/`SLD` never reach the screen.

Empty until the **Trades** section is added to the Flex query behind
`nav_query_id`, or `trades_query_id` is pointed at a Trade Confirmation query;
the empty state names both routes.

## Holdings: the sector page

Implements `Holdings.dc.html` from the Claude Design project *Portfolio overview
July 2026*. Five sector pills, a sector hero with a breadth strip, and one
aligned table with owned rows banded above watchlist rows. 35 tickers across
Semiconductors, Big tech, ETFs, Airlines and Financials, defined in
`adapter/universe.py`.

**Sector is a separate axis from region.** `regions.py` drives Overview's
allocation donut and answers "where is this exposure"; sector answers "what kind
of business is this". XDJP is Japan by region and an ETF by sector, and both are
correct — which is why adding sectors left Overview untouched.

**Ownership is a property of the ticker, not of the sector.** A ticker carries a
`con_id`; having one *is* ownership. Sectors hold nothing but references, so a
name reads identically wherever it appears and nothing re-decides ownership per
sector. Store it per sector and the first divergence shows a position you hold
as a watchlist row.

**One table, not two bands.** Held rows sort above watched under a single
8-column header, so every figure lines up across the boundary between the two
groups — which is the whole point of merging them. Each group sorts
*independently*, so the held block stays on top whichever column is clicked; a
column a watched row has no value for falls back to its day move, keeping that
group's order stable rather than arbitrary. The boundary is a slightly stronger
violet hairline on the last held row.

Held rows carry both the 2px violet rail and an **"Owned" pill** beside the
ticker, so ownership never rests on colour alone.

**Logos.** Each row shows the issuer's own mark from a public symbol-logo
service, on a light plate because marks are drawn for light ground, with the
brand-coloured monogram underneath showing if a mark fails. 33 of 35 symbols
resolve directly; `3115` and `ES3` carry explicit overrides, and `HY9H`'s mark
is filed under its Korean primary listing `000660.KS`. The footer carries the
identification-only attribution.

Do **not** add `loading="lazy"` to those images. They sit inside a
horizontally-scrollable container, where Chrome defers them indefinitely and
they never begin loading — the tiles silently stay monograms. A sector shows
about a dozen rows, so there is nothing to defer.

**Deviations for WCAG AA.** The design's monogram ink, `mix(brand, #0A0D12,
0.18)`, fails on the light plate for 8 of 35 issuers — C6L's amber at 2.52:1,
Apple's grey at 2.91:1. Darkened to **0.45**, where the lowest is 4.77:1 and all
35 pass with the hue still plainly the issuer's. `--loss-soft` keeps its lift to
`#c76d75` (4.18:1 inside its own chip otherwise), and `--text-disabled` its lift
to `#788391` — though the design's latest revision moved the column headers and
row sublabels off that token to `--text-muted` on its own, so it now carries
only the footer note.

The design ships hardcoded July prices and a fixed `NAV = 50331.8759`. None of
it is used — owned rows come from the live IB feed, watchlist rows from openbb,
and portfolio weight from live NAV. Sector values reconcile: the five totals sum
to `invested` to the pound.

| | owned rows | watchlist rows |
|---|---|---|
| source | IB Gateway live feed | openbb / yfinance |
| shows | qty, cost, P&L, return, weight | price, day change, 30d only |
| marked by | purple rail + `Owned` pill | hollow-ring `Watching` pill |

Watchlist rows never get a zeroed quantity or a £0 P&L — the columns show an
em-dash, and the ragged column *is* the signal, reinforced by rail and pill so
it never rests on colour alone.

`adapter/watchlist.py` polls openbb every 60s for the 15 symbols IB does not
cover, plus a daily batched 30d history for sparklines. It is a separate thread
on a separate endpoint (`/api/watchlist`) and imports nothing from `feed.py`.

**A swallowed NameError cost hours.** Extracting `_resolve_contracts` out of
`_session` left `from ib_async import Contract` behind in `_session`, so the new
method raised `NameError` on every call. `details_for`'s broad
`except Exception` caught it and `log.debug` hid it, so the symptom was
`resolved 0/15 contracts` logged in the same millisecond as connect — which is
indistinguishable from IB Gateway being unreachable, and sent the diagnosis
down the wrong path repeatedly. A total failure now logs `ERROR` with the first
real exception, and the import lives in the method that uses it.

Two things learned building it:

- **openbb's import is CPU-heavy enough to starve the IB feed.** `from openbb
  import obb` spends several seconds building its extension registry, and the
  GIL blocks the feed's asyncio loop meanwhile. `WatchlistFeed` therefore waits
  for the live feed's first successful refresh before its first call.
- **`:not([hidden])` on every `#view-*` and `.hview` rule is load-bearing.** An
  id selector outranks `.view[hidden] { display: none }`, so a bare
  `#view-overview { display: grid }` wins over the hide rule and the tabs render
  stacked on top of each other.

## Market watch: the world board

`MarketWatch.dc.html`, wired to `adapter/markets.py` + `js/marketwatch.js`. It
answers a different question from the other two pages — not *what do I own* but
*what is trading right now, and how is it moving* — so exposure is the last line
on a card rather than the first.

**Sessions are real.** Each market carries an IANA zone and its local open/close
minutes; `zoneinfo` does the rest. The design hardcodes `tz: -5` for New York,
which is wrong for eight months of the year. Weekends count as closed and the
countdown runs to Monday, not to a session that will not happen.

The browser re-derives the same session state every second from the same two
integers, shipped on each card, so the clocks tick between 30s polls. `session_of`
in `markets.py` and `sessionOf` in `marketwatch.js` are deliberate mirrors — if
you change one, change the other.

**Coverage is thinner than the design assumes.** Probed 1 Aug 2026:

| Wanted | Reality |
|---|---|
| 19 indices | **11**. KOSDAQ, TOPIX, HSCEI, TPEx, SET50 and FTSE ST Mid Cap return nothing from yfinance under any ticker variant |
| 9 markets | **8**. CSI 300 and SET were both 15 days stale and dropped; SET was Thailand's only index, so Thailand went with it |

So **6 of the 8 cards carry a benchmark only** — that is the primary card, not an
exception, and it spends the freed height on a full-width sparkline instead of
leaving a gap. Anything whose last bar is older than 3 days prints its date and
no day-change rather than passing a stale close off as today's.

**Exposure comes from the live IB feed**, folded in by `serve.py` through
`REGION_TO_MARKET` — `regions.py` already resolves by underlying exposure, which
is why XDJP counts as Japan and HY9H as Korea. It reconciles to `invested` to the
pound. Three states, not two: a market you hold, `Tracked only · nothing held`
(CN and TW), and `Waiting for the live feed` before the feed has composed —
because £0 and *not yet known* must not look alike.

**The sparkline is coloured by its own 30-day series**, never by the selected
1M/YTD figure. Colouring a visibly falling line green because the year was up is
the one thing a trend line must not do.

## Maintaining the region table

`adapter/regions.py` is hand-maintained because IBKR does not tag positions
with an investment region, and neither the listing venue nor the trading
currency implies one:

| Holding | Lists on | Trades in | Actually |
|---|---|---|---|
| `XDJP` | LSE | GBP | Japan (Nikkei 225) |
| `IUCS` | LSE | USD | United States (S&P 500) |
| `HY9H` | Frankfurt | EUR | South Korea (SK hynix) |
| `SMSN` | LSE IOB | USD | South Korea (Samsung) |

A position not in the table falls through to **Unclassified** and stays visible
in the UI rather than vanishing from the totals — that is the signal to add it.

## Things worth knowing

**Daily P/L is summed from positions, not taken from IBKR.** IBKR's
account-level `dailyPnL` silently reports a *partial* total — only the
positions its market-data farms managed to value. Measured 26 Jul 2026: IBKR
said −£552.47, which is exactly HSBA + HY9H + IUCS + SMSN + XDJP; the other ten
returned warning 2150 or never streamed. Summing all fifteen gives −£935.60.
`build.py` prefers the complete set and records which source it used in
`meta.daily_pnl_source`.

**Ticker gotchas.** `SMSN.IL` not `SMSN.L` (the latter is stale and prints
0.00%). `HY9H.F` not `HY9H.DE` (the latter returns nothing). LSE quotes come
back in pence — `marketdata.py` divides `GBp` by 100, without which every LSE
day-change is 100× wrong.

**The LSE quotes in pence, and IBKR will tell you so.** The same trap exists on
the live path, and it is worse there because the tick is multiplied by quantity:
an unscaled pence tick once put XDJP at £565,250 and invested at £759k against a
£50k NAV. Do not infer the unit — `reqContractDetails` returns
**`priceMagnifier`** for exactly this (100 for HSBA and XDJP, 1 for the other
thirteen). `feed.py` divides market-data prices by it. Account values
(`item.marketPrice`, `item.marketValue`) are already in major units and must
**not** be scaled.

It hid for a whole session because the LSE was shut during every test: the tick
was IBKR's −1 sentinel, so the code fell back to the account price, which is in
pounds. It surfaced the moment London opened. Anything that depends on a market
being open is worth testing while one is.

**The invested cross-check.** `_compose` compares its total against IBKR's own
`GrossPositionValue` every cycle and, if they diverge by more than 2%, drops the
tick repricing and rebuilds from account values, logging loudly and setting
`meta.invested_check = "fallback"`. Verified by reintroducing the bug: the guard
fires at 9900% and still returns the correct figure. Repricing from ticks is
what makes the page move between IBKR's ~3-minute pushes, so it is worth
keeping — but only behind that check.

**Everything degrades.** openbb FX falls back to IBKR's own `ExchangeRate`
rows; day-change falls back from openbb → IBKR price + openbb prior close →
IBKR `dailyPnL`; a missing Flex config skips the backfill with an explanation.
The snapshot completes even when the network does not.

## The four flagships (24 Aug 2026)

Chosen from a six-lane research survey (56 proposals, deduped, adversarially
critiqued), scoped to features the existing data can feed honestly:

- **Track record** (Performance) — monthly returns grid anchored to IBKR's own
  Change-in-NAV sub-periods with a benchmark row following the equity curve's
  picker; underwater drawdown with ranked episodes; a daily P/L calendar in
  the contributions-grid idiom; best/worst days. All share one funding-aware
  daily return series (`adapter/track.py`) — deposits are excluded by
  construction, so a deposit day is a flat day, not a good one.
- **Income rail** (Performance + stock pages) — trailing 12 months of paid
  dividends (solid; Flex fact) against the next 12 estimated (outline; a
  cadence model that looks like one), the broker's declared-but-unpaid accrual
  as its own tier, and per-stock DPS history with cut markers and
  yield-on-cost. `adapter/desk.py` + `adapter/income.py`; the daily job keeps
  the desk file fresh.
- **Desk context** (Market watch) — an Overnight panel splitting the NAV move
  market-vs-flows against the last 23:30 close snapshot; an earnings countdown
  rail in the session-clock idiom; a deduped news digest across the book.
- **Allocation intent** (Allocation) — operator targets in
  `config.local.json` drawn as diverging violet drift bars with computed
  rebalance £; a squarified treemap as a second display mode sharing the
  ring's hover state; a rules panel (largest position, cash floor, currency
  band, effective-N/HHI) with defaults until config overrides.

The stores are now guarded archives: Flex reaches back 365 days, so from
~Oct 2026 the oldest rows exist nowhere else — `store._write` refuses to
shrink a file and the daily job logs a row census. The real ledgers never
enter this repository: `dashboard/data/` is gitignored and backed up outside
git. What the repository ships instead is a mock dataset, CSVs under
`dashboard/data/mock/` and the generator in `dashboard/scripts/` that turns
them into the files the app reads, so the dashboard runs end to end on
invented figures; see `dashboard/data/mock/README.md`.

## The de-AI pass (20 Aug 2026)

A six-critic design audit hunted everything that read as "made by AI" —
template defaults rather than choices — and the fixes landed as departures
from the pinned references, each signed off by Mac:

- **Brand re-toned and flattened.** The soft-lavender `#7c55e8` becomes a
  deeper, higher-chroma `#6d2ef5` (`#5314d6` light); every gradient and glow
  is gone. The accent's one device is the thin rule + wash the hero plates
  already used. Elevated surfaces, hero shadows, scrollbars and hairlines
  lose their violet cast — violet now means selection, ownership and focus,
  nothing else.
- **One sans.** Inter (body) and Manrope (UI) are retired; Archivo — already
  the display face — takes every sans role, with Geist Mono keeping every
  figure. 27 letter-spacing literals collapsed to five tokens; nothing is set
  below 9.5px; no 800 weight below display sizes.
- **The categorical ramp is re-stepped** (the invitation at the bottom of
  this file, taken): green and rose leave the ramp — both reserved for P&L —
  with Korea marine, the UK plum, the US a deeper in-band gold. Every dataviz
  validator check now passes in both themes, including the two failures
  documented below, which are gone.
- **Charts lose their defaults**: no gradient washes (the world board's eight
  red/green mountains are line-only), axis ticks come from `niceTicks`,
  square bar ends, solid gridlines (the benchmark's dash is the page's only
  dashed vocabulary), the Sankey reads as tinted glass, and teal leads the
  attribution series with violet held back. Dense contexts (tape, tables,
  market rows) show deltas as bare signed mono — sign plus hue, never hue
  alone — with the full chip kept for KPI tiles and heroes.
- **The light theme is finished.** The Holdings ink family is aliased onto
  the themed system, so the three views that were dark-only render in both
  themes and the regression cannot be reintroduced.
- **Chrome honesty**: the gradient "P" badge is a mono wordmark; Settings,
  Log out and the collapse button — none of which did anything — are gone;
  the sidebar footer carries the feed truth and the theme toggle; titles are
  sentence case with the eyebrow demoted back to labelling figures; links
  say where they go; the feed-down condition is said once per view.

## Two deliberate departures from `Design.pdf`

1. **The equity curve is real, so it looks different.** The reference chart is
   illustrative — it shows −0.16% in a tight £50.0k–£51.5k band. The actual 1M
   TWR is −6.29% (£53,709 → £50,332). Note a ~£10.7k deposit on 25 Jun makes
   the raw NAV delta misleading; TWR is the honest figure.

2. **Top movers rows do not overlap.** In the PDF the avatar chips sit on top
   of the ticker text, the `+3`/`+1` badges are half-covered, and INTC's chip
   runs off the card edge. Four elements were being squeezed into a ~200px
   column. Every part now has a hard minimum and only the sparkline flexes;
   spacing, colour and type are otherwise unchanged.

## The Overview is viewport-locked

The whole dashboard reads on one screen, as it does on the reference sheet. That
needed more than tidying: `Design.pdf` reproduced at its natural proportions
wants **1349 px** of height at a 1470 px width, against a real browser viewport
of **801 px**. A PDF page has no viewport, which is why everything appears to
fit there.

So `#view-overview` is a grid whose rows carry the reference's measured
proportions — **19.6 / 46.9 / 33.5** — inside `100vh`, and the display type
scales with `vh` (`--t-hero` is `clamp(34px, 5.2vh, 64px)`, reaching its 64 px
reference size at ~1230 px of viewport height). Below `700px` tall, or under the
existing width breakpoints, the lock releases and the page scrolls rather than
crushing the content.

Two things that are easy to get wrong here:

- **`min-height: 0` is load-bearing.** Grid and flex children default to
  `min-height: auto` and refuse to shrink below their content, which makes the
  whole fit silently fail. It appears at every level of the chain.
- **`--leading-body: 1.7` is for prose, not data rows.** Left on the currency
  and concentration rows it added ~14 px each and pushed the last two entries off
  their cards. Those rows are explicitly `line-height: 1.2`.

The allocation donut's grid row has a hard `minmax(104px, 1fr)` floor — with
`minmax(0, 1fr)` the legend's natural height claims the whole card and the ring
disappears entirely.

`equityCurve()` sizes its `viewBox` from the element's real pixel box rather
than a fixed `780×260`. With a fixed box the SVG is stretched to fit, and a
non-uniform stretch squashes the axis **text** — `vector-effect` protects stroke
width, not glyphs. At the reduced plot height (~133 px) that distortion would be
obvious.

## Accessibility

- Gain/loss never relies on hue: every value carries a sign, every chip an arrow.
- WCAG AA verified in **both** themes, including P&L colours against their own
  tinted chip backgrounds. Three tokens were adjusted to reach it: dark loss
  `#ea3943 → #eb4650` (+3% lightness, same hue — the reference red clears AA on
  the card at 4.62:1 but only 4.31:1 inside its chip), light gain
  `#0a8f5c → #087c50`, and `--text-3` in both themes (`#59616b → #737e8b`,
  `#8b95a1 → #6c7785`) — it was failing at 2.99:1 and drives the chart's axis
  labels. Everything now clears the 4.5:1 normal-text bar; the compressed layout
  shrinks several values below the 18.66px "large text" exemption, so that is the
  bar that applies.
- `prefers-reduced-motion` disables count-up, draw-on and entrance animations.
- `prefers-color-scheme` honoured; every token has a light and a dark value.

### Known: the reference donut palette is hard to read for some viewers

Validated with the dataviz palette checker under all-pairs comparison, which is
the right test for a donut since any segment may be compared to any other:

- Japan `#9b72e8` vs Hong Kong/China `#4c8df6` — ΔE **2.7** for deuteranopes
- Singapore `#3fb6c4` vs South Korea `#2fb88a` — ΔE **8.9** with *full* colour
  vision (below the 15 floor)

The labelled legend means identity is never carried by colour alone, so the
chart stays usable — but those pairs are genuinely hard to tell apart in the
ring. Resolved in the de-AI pass above: the word was said, and the re-stepped ramp
passes every validator check in both themes.
