/* Reads data/portfolio.json + data/nav_history.jsonl and renders the dashboard.
 *
 * Both files are produced by adapter/build.py against IB Gateway. This module
 * never talks to a broker; it only renders what the adapter wrote, so the page
 * works offline and the stale state is a genuine signal rather than a spinner.
 */

import {
  money, moneyCompact, moneySigned, pctSigned, pct, qty,
  direction, stamp, clock, initials, esc,
} from "./format.js";
import { sparkline, equityCurve, countUp } from "./charts.js";
import * as holdings from "./holdings.js";
import * as marketwatch from "./marketwatch.js";
import * as stock from "./stock.js";
import * as search from "./search.js";
import * as financials from "./financials.js";
import * as ovholdings from "./ovholdings.js";
import * as tape from "./tape.js";
import * as performance from "./performance.js";
import { allocRing } from "./alloc.js";
import * as allocationView from "./allocation.js";
import * as transactions from "./transactions.js";

const DATA_URL = "data/portfolio.json";
const NAV_URL = "data/nav_history.jsonl";
const LIVE_URL = "api/snapshot";
const WATCH_URL = "api/watchlist";
const MARKETS_URL = "api/markets";
const BENCH_URL = "api/benchmark";
/* Which index the equity curve is compared against. Remembered because it is a
 * reading preference, not state: coming back to the dashboard and finding the
 * comparison reset to the default every time would be a small, repeated
 * annoyance. Same localStorage habit stock.js uses for its watch list. */
const BENCH_KEY = "portfolio-dashboard:benchmark";
/* Watchlist quotes come from openbb on a 60s server-side cycle, so polling it
 * at the IB feed's 3s would just re-fetch the same numbers. */
const WATCH_POLL_MS = 20000;
/* Same cycle, and index levels are daily closes — there is nothing faster to
 * see. The clock strip ticks locally, so the board still feels live between
 * polls without asking the server anything. */
const MARKETS_POLL_MS = 30000;
// The outlet's cadence. Headlines age in hours; five minutes is plenty.
const NEWS_POLL_MS = 5 * 60 * 1000;
const MIN_CURVE_POINTS = 5;

/* Live cadence. The feed recomposes every 3s, so five missed polls is a
 * generous window before we stop claiming the numbers are live. */
const POLL_MS = 3000;
const LIVE_STALE_MS = 15000;
/* Only used for the no-feed fallback, where the page is showing a snapshot
 * written by build.py rather than a live connection. */
const SNAPSHOT_STALE_MS = 6 * 60 * 60 * 1000;

const $ = (id) => document.getElementById(id);
const root = document.documentElement;

let livePolling = false;   // set once /api/snapshot answers

/* The comparison series for the equity curve, as the server composed it:
 * { symbol, name, currency, ranges: { '1M': { points, count, withheld }, … } }.
 * Null until /api/benchmark answers, and null again if it fails — in both cases
 * the curve simply draws without a second line. */
let benchmark = null;

/* Write only when the value actually changed. Every one of these runs up to
 * 20 times a second across the page; blind writes cause layout thrash and drop
 * text selection mid-poll. */
function setText(node, value) {
  if (node && node.textContent !== value) node.textContent = value;
}

function setClass(node, cls) {
  if (node && node.className !== cls) node.className = cls;
}

function setStyle(node, prop, value) {
  if (node && node.style[prop] !== value) node.style[prop] = value;
}

/** Region colour comes from its fixed index, so it never shifts on filtering. */

const RANGE_DAYS = { "1D": 2, "7D": 7, "1M": 30, "3M": 90, "6M": 180, "1Y": 365, ALL: Infinity };
const RANGE_LABEL = { "1D": "1D", "7D": "7D", "1M": "1M", "3M": "3M", "6M": "6M", "1Y": "1Y", ALL: "All" };
const RANGE_NOTE = {
  "1D": "past day · daily close",
  "7D": "past week · daily close",
  "1M": "past month · daily close",
  "3M": "past three months · daily close",
  "6M": "past six months · daily close",
  "1Y": "past year · daily close",
  ALL: "since inception · daily close",
};

let portfolio = null;
let navHistory = [];
let activeRange = "1M";

/* The chart's two readings of the same account. "nav" plots what it is
 * worth; "twr" compounds the funding-aware daily returns the Performance
 * view already lives on, so a deposit can never read as a gain. */
const CHART_MODE_KEY = "portfolio-dashboard:chart-mode";
let chartMode = "nav";
let dailyReturns = null;   // Map(iso date -> daily r), from api/track

async function loadDailyReturns() {
  try {
    const res = await fetch(`api/track?t=${Date.now()}`);
    if (!res.ok) throw new Error(`track: ${res.status}`);
    const trk = await res.json();
    dailyReturns = new Map((trk.daily || []).map((d) => [d.date, d.r]));
  } catch {
    dailyReturns = null;
  }
  // Whichever mode is pressed, the chart can only get more honest now.
  renderChart();
}

/** The sliced NAV dates re-read as cumulative return, in percent. Dates the
 *  track record does not cover (pre-inception, holidays) compound as flat —
 *  the x-axis stays identical to NAV mode, so the benchmark still aligns. */
function twrSeries(points) {
  if (!dailyReturns || points.length < 2) return null;
  let acc = 1;
  return points.map((p, i) => {
    if (i > 0) acc *= 1 + (dailyReturns.get(p.date) ?? 0);
    return { date: p.date, value: (acc - 1) * 100 };
  });
}

/* ---------------- loading ---------------- */

async function loadJSON(url) {
  const res = await fetch(`${url}?t=${Date.now()}`);
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return res.json();
}

async function loadNavHistory() {
  try {
    const res = await fetch(`${NAV_URL}?t=${Date.now()}`);
    if (!res.ok) return [];
    const text = await res.text();
    return text.split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => { try { return JSON.parse(line); } catch { return null; } })
      .filter((row) => row && row.date && Number.isFinite(row.nav_gbp))
      .map((row) => ({ date: row.date, value: row.nav_gbp }))
      .sort((a, b) => a.date.localeCompare(b.date))
      .filter(sinceInception());
  } catch {
    return [];
  }
}

/**
 * Drops the zero-NAV rows Flex pads the front of the series with.
 *
 * The statement reports back to the start of its reporting year, not to the
 * account's first position, so this file opens on 2025-07-30 at £0 and does
 * not reach a real figure until 2025-10-13. Those 53 rows are true — the
 * account really did hold nothing — but drawn they cost the left quarter of
 * the plot to a flat zero line, put £0 in the All low, and pull the axis floor
 * below zero on a long-only account.
 *
 * Leading only: a zero *after* inception would mean the account was emptied,
 * which is a real event and stays on the chart. Filtered here rather than in
 * the store so the Flex import stays faithful to what IBKR sent.
 *
 * `_since_inception` in adapter/benchmark.py is the deliberate mirror of this:
 * the benchmark series is length-checked against this one before it will draw,
 * so if you change the rule here, change it there. Change one and the
 * benchmark line quietly stops appearing.
 */
function sinceInception() {
  let started = false;
  return (row) => (started ||= row.value !== 0);
}

/* ---------------- chips ---------------- */

function paintChip(node, value, { digits = 2 } = {}) {
  if (!node) return;
  const dir = direction(value);
  node.className = `chip kpi__chip chip--${dir === "up" ? "pos" : dir === "down" ? "neg" : "flat"}`;
  // The arrow is drawn by CSS from a masked lucide icon keyed on the chip's
  // direction modifier, so the span is an empty box rather than a glyph.
  node.innerHTML =
    `<i class="chip__arrow" aria-hidden="true"></i>` +
    `<span>${pctSigned(value, digits)}</span>`;
}

/* ---------------- KPI row ---------------- */

function renderKpis(data) {
  const k = data.kpis || {};

  countUp($("kpiNav"), k.net_liquidation, (v) => money(v));
  countUp($("kpiDay"), k.daily_pnl, (v) => moneySigned(v));
  countUp($("kpiUnreal"), k.unrealised_pnl, (v) => moneySigned(v));
  countUp($("kpiInvested"), k.invested, (v) => money(v));
  countUp($("kpiCash"), k.cash_available, (v) => money(v));

  const dayChip = $("kpiDayChip");
  const unrealChip = $("kpiUnrealChip");
  paintChip(dayChip, k.daily_pnl_pct);
  paintChip(unrealChip, k.unrealised_pnl_pct);

  for (const [node, value] of [[$("kpiDay"), k.daily_pnl], [$("kpiUnreal"), k.unrealised_pnl]]) {
    const dir = direction(value);
    node.classList.remove("pos", "neg", "flat");
    node.classList.add(dir === "up" ? "pos" : dir === "down" ? "neg" : "flat");
  }
}

/* ---------------- equity curve ---------------- */

function sliceRange(points, range) {
  const days = RANGE_DAYS[range] ?? 30;
  if (!Number.isFinite(days)) return points;
  return points.slice(-Math.max(2, days));
}

/* ---------------- benchmark ---------------- */

/**
 * The comparison series for one range, plus why it is absent when it is.
 *
 * Everything numeric here was computed by adapter/benchmark.py — slicing,
 * forward-filling and rebasing all happen server-side. This only decides
 * whether the array is safe to hand to the chart.
 *
 * Returns { points, reason }: `points` is the array or null; `reason` is a
 * short phrase for the strip under the chart when there is nothing to draw.
 */
function benchmarkFor(range, expected) {
  if (!benchmark) return { points: null, reason: "" };
  if (benchmark.available === false) {
    return { points: null, reason: "index data unavailable" };
  }
  const slot = benchmark.ranges?.[range];
  if (!slot) return { points: null, reason: "" };

  if (slot.withheld === "funding") {
    // The server judged this window to be dominated by deposits rather than
    // performance. Say so — an absent line with no explanation reads as a bug.
    // `funding` travels with it because the same verdict disqualifies the
    // percentage chip: see renderChart.
    return {
      points: null,
      funding: true,
      reason: "not shown over this range · deposits dominate the change",
    };
  }
  // Length parity is the server's contract, but assert it rather than trust it:
  // a mismatch would plot every point against the wrong date, and a silently
  // wrong chart is worse than no chart.
  if (slot.count !== expected) {
    return { points: null, reason: "" };
  }
  if (!slot.covered) return { points: null, reason: "no index history for this range" };
  return { points: slot.points, reason: "" };
}

/**
 * The legend and note under the chart.
 *
 * The dashed swatch is shown only while a line is actually drawn, so the key
 * never advertises a series that is not on the chart. The note carries either
 * the reason a line is missing or, for a non-GBP index, the currency caveat —
 * the comparison is of growth, so an index in its own currency shows the return
 * a local investor earned rather than a sterling one.
 */
function renderBenchBar(bench, dirOverride = null) {
  const bar = $("benchBar");
  const key = $("benchKey");
  const note = $("benchNote");
  if (!bar || !key || !note) return;

  const drawing = Boolean(bench.points);
  key.hidden = !drawing;
  // The portfolio swatch inherits the curve's direction colour via CSS. In
  // return mode the caller knows the direction deposits can no longer fake.
  const pts = sliceRange(navHistory, activeRange);
  const dir = dirOverride
    ?? (pts.length >= 2 ? (pts.at(-1).value >= pts[0].value ? "up" : "down") : "");
  if (bar.dataset.direction !== dir) bar.dataset.direction = dir;

  setText(note, bench.reason || (drawing && benchmark?.currency && benchmark.currency !== "GBP"
    ? `${benchmark.currency} · unhedged`
    : ""));

  // With no picker, no line and nothing to explain, the strip would be a
  // legend describing a single series — which the card title already names.
  const pick = bar.querySelector(".benchbar__pick");
  const useful = drawing || Boolean(note.textContent) || !(pick?.hidden ?? true);
  bar.hidden = !useful;
}

/**
 * Fill the picker, and hide it entirely when there is nothing to pick.
 *
 * An empty <select> still renders — a 32px box with a caret and no options —
 * which is a control that looks operable and is not. If the index catalogue
 * never arrived there is no benchmark to choose, so the whole legend entry
 * goes with it.
 */
function renderBenchOptions(available, selected) {
  const sel = $("benchSelect");
  const pick = sel?.closest(".benchbar__pick");
  if (!sel) return;

  const has = Boolean(available?.length);
  if (pick) pick.hidden = !has;
  if (!has) return;

  const key = available.map((c) => c.symbol).join(",");
  if (sel.dataset.key !== key) {
    sel.dataset.key = key;
    sel.innerHTML = available
      .map((c) => `<option value="${esc(c.symbol)}">${esc(c.name)}</option>`)
      .join("");
  }
  if (selected) sel.value = selected;
}

async function loadBenchmark(symbol) {
  try {
    const url = symbol ? `${BENCH_URL}?symbol=${encodeURIComponent(symbol)}` : BENCH_URL;
    const res = await fetch(`${url}${symbol ? "&" : "?"}t=${Date.now()}`);
    if (!res.ok) throw new Error(`benchmark: ${res.status}`);
    const data = await res.json();
    benchmark = data.benchmark || null;
    renderBenchOptions(data.available, benchmark?.symbol);
    if (benchmark?.symbol) {
      try { localStorage.setItem(BENCH_KEY, benchmark.symbol); } catch { /* private mode */ }
    }
  } catch {
    // A missing comparison line is not worth breaking the page over. Clearing
    // it matters though: a failed switch must not leave the previous index's
    // line on the chart under the newly chosen name.
    benchmark = null;
  }
  renderChart();
}

function initBenchmark() {
  const sel = $("benchSelect");
  if (!sel) return;
  sel.addEventListener("change", () => loadBenchmark(sel.value));
  let stored = null;
  try { stored = localStorage.getItem(BENCH_KEY); } catch { /* private mode */ }
  loadBenchmark(stored || "");
}

function renderChart() {
  const plot = $("chartPlot");
  const points = sliceRange(navHistory, activeRange);

  $("chartNote").textContent = RANGE_NOTE[activeRange];
  for (const id of ["chartChangeLabel", "chartHighLabel", "chartLowLabel"]) {
    const suffix = id.includes("Change") ? "change" : id.includes("High") ? "high" : "low";
    $(id).textContent = `${RANGE_LABEL[activeRange]} ${suffix}`;
  }

  // Gated on how much history exists, not on how many points this range
  // happens to slice to. Those are different questions, and conflating them
  // made 1D — two daily closes, by definition — fall into the empty state and
  // announce "221 of 5 daily points recorded", which is both wrong and
  // nonsense. With enough history every range draws; a short one is simply a
  // short line.
  if (navHistory.length < MIN_CURVE_POINTS) {
    // No chart at all here, so the key must not keep advertising a benchmark
    // from whichever range was shown before this one.
    renderBenchBar({ points: null, reason: "" });
    $("chartChip").className = "chip chip--flat";
    $("chartChip").textContent = navHistory.length ? `${navHistory.length} of ${MIN_CURVE_POINTS}` : "no history";
    plot.innerHTML = `
      <div class="collecting">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round">
          <path d="M3 17 9 11l3.5 3.2L21 6"/><path d="M15 6h6v6"/>
        </svg>
        <h3>Collecting history</h3>
        <p>${navHistory.length || "No"} of ${MIN_CURVE_POINTS} daily points recorded.
        Run <code>backfill.py</code> with Flex configured to fill this in from October 2025.</p>
      </div>`;
    $("chartChange").textContent = "—";
    $("chartHigh").textContent = "—";
    $("chartLow").textContent = "—";
    return;
  }

  const bench = benchmarkFor(activeRange, points.length);

  // Return mode: same dates, but the y-axis is performance rather than worth.
  // Falls back to NAV silently while the track record is still loading.
  const twr = chartMode === "twr" ? twrSeries(points) : null;
  if (twr) {
    $("chartNote").textContent = `${RANGE_NOTE[activeRange]} · deposits excluded`;
    // The server withholds the benchmark where deposits dominate the NAV
    // change — a verdict about the £ chart. Here deposits are already out,
    // but the withheld points are gone with it, so only the wording changes.
    if (bench.funding) bench.reason = "index line unavailable over this range";
  }
  renderBenchBar(bench, twr ? (twr.at(-1).value >= 0 ? "up" : "down") : null);

  plot.innerHTML = `<svg id="chartSvg" role="img" aria-label="${
    twr ? "Portfolio return over time, deposits excluded" : "Portfolio value over time"}"></svg>`;
  // In return mode the benchmark sheds its £ rebase and speaks percent too —
  // the rebase preserved the index's growth, so dividing by its first point
  // recovers it exactly.
  const benchPoints = !twr ? bench.points
    : bench.points?.length && bench.points[0]
      ? bench.points.map((v) => (v / bench.points[0] - 1) * 100)
      : null;
  equityCurve($("chartSvg"), twr ?? points, {
    tooltip: $("tip"),
    formatValue: twr ? (v, full) => pctSigned(v, full ? 2 : 1)
                     : (v, full) => (full ? money(v) : moneyCompact(v)),
    formatDate: (d) => new Date(d).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }),
    benchmark: benchPoints,
    benchmarkName: benchmark?.name ?? "Benchmark",
  });

  const changeNode = $("chartChange");
  if (twr) {
    // Every figure below is a return, so the deposit gate has nothing to gate.
    const vals = twr.map((p) => p.value);
    const total = vals[vals.length - 1];
    paintChip($("chartChip"), total);
    $("chartChip").className = $("chartChip").className.replace("kpi__chip ", "");
    changeNode.textContent = pctSigned(total, 2);
    changeNode.className = `num ${total > 0 ? "pos" : total < 0 ? "neg" : "flat"}`;
    $("chartHigh").textContent = pctSigned(Math.max(...vals));
    $("chartLow").textContent = pctSigned(Math.min(...vals));
    return;
  }

  const first = points[0].value;
  const last = points[points.length - 1].value;
  const values = points.map((p) => p.value);
  // A simple return is only a return when the money in the account did the
  // moving. Over the whole span this account went from a £10 opening balance
  // to £51k almost entirely on deposits, which reads as "+513,314%" — true,
  // and worthless. The server already makes that call for the benchmark line;
  // the chip stands down on the same verdict rather than inventing a second
  // rule, so the chip and the note under the chart never disagree. (The
  // Return toggle is the escape hatch: there the figure is real.)
  const changePct = bench.funding || !first ? null : ((last - first) / first) * 100;

  paintChip($("chartChip"), changePct);
  $("chartChip").className = $("chartChip").className.replace("kpi__chip ", "");

  changeNode.textContent = moneySigned(last - first);
  changeNode.className = `num ${direction(last - first) === "up" ? "pos" : direction(last - first) === "down" ? "neg" : "flat"}`;
  $("chartHigh").textContent = money(Math.max(...values));
  $("chartLow").textContent = money(Math.min(...values));
}

/* ---------------- allocation ---------------- */

/* The donut, its legend and the hover swap all live in js/alloc.js, because
 * the Allocation view draws the same thing on three axes and drives a KPI
 * strip off the same active state. Built lazily so the DOM exists by then. */
let overviewRing = null;

/* Built on first use rather than at a fixed point in boot: the first render
 * happens before initAllocation() is reached, and an ordering dependency
 * between the two is the kind that fails silently — the card just draws
 * nothing. */
function ring() {
  if (!overviewRing) {
    const svg = $("donutSvg");
    const legend = $("allocLegend");
    if (!svg || !legend) return null;
    overviewRing = allocRing({ svg, legend, caption: "Invested" });
  }
  return overviewRing;
}

function renderAllocation(data) {
  ring()?.render(data.regions || [], data.kpis?.invested ?? 0);
}

function initAllocation() { ring(); }

/* ---------------- movers ---------------- */

function moverRow(p) {
  const dir = direction(p.day_change_pct);
  const chipClass = dir === "up" ? "chip--pos" : dir === "down" ? "chip--neg" : "chip--flat";
  const tint = dir === "up" ? "var(--pos-bg)" : dir === "down" ? "var(--neg-bg)" : "rgba(122,132,144,.12)";
  return `
    <div class="mover" data-con-id="${p.con_id}">
      <span class="avatar" style="background:${tint}">${esc(initials(p.symbol))}</span>
      <span class="mover__ticker">${esc(p.symbol)}</span>
      <svg class="mover__spark" data-spark="${p.con_id}" aria-hidden="true"></svg>
      <span class="chip ${chipClass} mover__chip">
        <i class="chip__arrow" aria-hidden="true"></i>${pctSigned(p.day_change_pct)}
      </span>
    </div>`;
}

/** Identity of a movers list, so it is only rebuilt when the tickers change. */
function moversKey(data) {
  const { gainers = [], losers = [] } = data.movers || {};
  return [...gainers, ...losers].map((p) => p.con_id).join(",");
}

function renderMovers(data) {
  const { gainers = [], losers = [] } = data.movers || {};
  $("gainers").innerHTML = gainers.map(moverRow).join("") ||
    '<p class="chart-card__note">No risers today.</p>';
  $("losers").innerHTML = losers.map(moverRow).join("") ||
    '<p class="chart-card__note">No fallers today.</p>';

  // Scoped to the two mover columns. The holdings table below the fold also
  // uses [data-spark], and a document-wide query would have each render walking
  // the other's SVGs.
  const byId = new Map((data.positions || []).map((p) => [p.con_id, p]));
  for (const host of [$("gainers"), $("losers")]) {
    for (const svg of host?.querySelectorAll("[data-spark]") || []) {
      const p = byId.get(Number(svg.dataset.spark));
      if (p) sparkline(svg, p.spark, { direction: direction(p.day_change_pct) });
    }
  }
}

/* ---------------- currency + concentration ---------------- */

function renderCurrencies(data) {
  const rows = data.currencies || [];
  const max = Math.max(...rows.map((r) => r.weight_pct || 0), 1);
  $("ccyList").innerHTML = rows.map((r, i) => `
    <div class="ccy__row" data-ccy="${r.code}">
      <div class="ccy__top">
        <span class="ccy__code">${r.code}</span>
        <span class="ccy__pct num" data-f="pct">${pct(r.weight_pct)}</span>
        <span class="ccy__val num" data-f="val">${money(r.value_gbp)}</span>
      </div>
      <div class="bar">
        <div class="bar__fill" style="width:${(r.weight_pct / max) * 100}%;animation-delay:${i * 40}ms"></div>
      </div>
    </div>`).join("");
}

/* ---------------- the outlet ----------------
 * Ranked headlines for the markets and sectors the book holds, with earnings
 * as figures on top. The server ranks; this only draws. Concentration moved
 * wholesale to the Allocation view — the payload field lives on there. */

const shortAge = (iso) => {
  if (!iso) return "";
  const mins = Math.max(0, (Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 60) return `${Math.round(mins)}m`;
  if (mins < 60 * 36) return `${Math.round(mins / 60)}h`;
  return `${Math.round(mins / 1440)}d`;
};

const fmtEps = (v) => (v == null ? "—"
  : Math.abs(v) >= 100 ? Math.round(v).toLocaleString("en-GB") : v.toFixed(2));

function renderOutlet(data) {
  const host = $("outletBody");
  if (!host) return;
  const items = (data?.items || []).slice(0, 12);
  const earn = data?.earnings || {};
  const reported = (earn.reported || []).slice(0, 2);
  const upcoming = (earn.upcoming || []).slice(0, 2);
  if (!items.length && !reported.length && !upcoming.length) return;

  const fmtD = (d) => new Date(d).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  // The surprise chip is signed as well as tinted — never hue alone.
  const chip = (r) => (r.surprise_pct == null ? "" : `
    <span class="outlet__chip ${r.surprise_pct >= 0 ? "pos" : "neg"} num">${
      r.surprise_pct >= 0 ? "+" : "−"}${Math.abs(r.surprise_pct).toFixed(1)}%</span>`);

  const erows = [
    ...reported.map((r) => `
      <div class="outlet__earn">
        <span class="outlet__esym num">${esc(r.key)}</span>
        <span class="outlet__etext">EPS ${fmtEps(r.eps_reported)}${
          r.eps_estimate != null ? ` vs ${fmtEps(r.eps_estimate)} est` : ""} · ${fmtD(r.date)}</span>
        ${chip(r)}
      </div>`),
    ...upcoming.map((r) => `
      <div class="outlet__earn">
        <span class="outlet__esym num">${esc(r.key)}</span>
        <span class="outlet__etext">reports ${fmtD(r.date)}</span>
      </div>`),
  ].join("");

  host.innerHTML = `${erows ? `<div class="outlet__earnings">${erows}</div>` : ""}
    <div class="outlet__list">${items.map((n) => `
      <a class="outlet__row" href="${esc(n.url || "#")}" target="_blank" rel="noopener">
        <span class="outlet__tag outlet__tag--${esc(n.kind)}">${esc(n.tag)}</span>
        <span class="outlet__title" title="${esc(n.title)}">${esc(n.title)}</span>
        <span class="outlet__meta">${esc(n.publisher || "")}${n.publisher && n.at ? " · " : ""}${shortAge(n.at)}</span>
      </a>`).join("")}</div>`;
}

/* ---------------- holdings ---------------- */

/* Holdings is its own module now: a grouped browser rather than one table.
 * It owns its rendering and routing; this file just hands it the live IB data
 * as it arrives. */
function renderHoldings(data) {
  holdings.updateOwned(data);
}

/* ---------------- live updates ----------------
 *
 * Everything below writes into DOM that already exists, addressed by
 * data-con-id / data-region / data-ccy. Rebuilding innerHTML every 3s would
 * reset the Holdings scroll position, drop hover and selection, and flicker.
 * Structure is only rebuilt when the set of rows genuinely changes.
 */

const cls = (d) => (d === "up" ? "pos" : d === "down" ? "neg" : "flat");

function applyLive(data) {
  const positions = data.positions || [];
  if (!positions.length) return;

  const invested = data.kpis?.invested || 1;

  renderKpis(data);

  // Patches its figures in place unless the set of holdings changed; a rebuild
  // here would restart the scroll animation on every poll.
  tape.update(data);

  // --- allocation: donut arcs + legend figures ---
  // render() rebuilds the legend only when the membership changes and patches
  // its figures in place otherwise, so hover and focus survive the tick.
  renderAllocation(data);
  allocationView.update(data);

  // --- movers: rebuild only if the tickers changed, else retint the chips ---
  const key = moversKey(data);
  if (key !== applyLive._moversKey) {
    applyLive._moversKey = key;
    renderMovers(data);
  } else {
    const byId = new Map(positions.map((p) => [p.con_id, p]));
    for (const row of document.querySelectorAll(".mover")) {
      const p = byId.get(Number(row.dataset.conId));
      if (!p) continue;
      const dir = direction(p.day_change_pct);
      const chip = row.querySelector(".mover__chip");
      setClass(chip, `chip chip--${cls(dir)} mover__chip`);
      chip.innerHTML =
        `<i class="chip__arrow" aria-hidden="true"></i>${pctSigned(p.day_change_pct)}`;
    }
  }

  // --- currency exposure ---
  const currencies = data.currencies || [];
  const maxWeight = Math.max(...currencies.map((c) => c.weight_pct || 0), 1);
  for (const c of currencies) {
    const row = document.querySelector(`.ccy__row[data-ccy="${CSS.escape(c.code)}"]`);
    if (!row) continue;
    setText(row.querySelector('[data-f="pct"]'), pct(c.weight_pct));
    setText(row.querySelector('[data-f="val"]'), money(c.value_gbp));
    setStyle(row.querySelector(".bar__fill"), "width", `${(c.weight_pct / maxWeight) * 100}%`);
  }

  // Holdings, the stock page and search all read the same payload, so every
  // view shows one instrument at one price.
  holdings.updateOwned(data);
  stock.updateOwned(data);
  search.updateOwned(data);
  // Overview's own holdings table. It patches figures in place rather than
  // rebuilding, so running it on the 3s tick is safe.
  ovholdings.update();
}

/* ---------------- freshness ---------------- */

function renderFreshness(data) {
  const meta = data.meta || {};

  let stale;
  let label;
  let eod = false;
  if (livePolling) {
    // Live: the feed reports whether it holds a connection, and last_refresh
    // proves the loop is still running. Either failing means stale. The
    // window follows the feed's own cadence — the Flex-EOD feed composes
    // every 60s, and holding it to the gateway's 15s would call a healthy
    // feed dead.
    const at = meta.last_refresh ? new Date(meta.last_refresh) : null;
    const age = at ? Date.now() - at.getTime() : Infinity;
    const window = Math.max(LIVE_STALE_MS, (meta.poll_seconds || 0) * 2500);
    stale = !meta.connected || age > window;
    eod = !stale && meta.source === "flex-eod";
    // Branch on the timestamp rather than printing a dash into the sentence:
    // "last live — —" and "received at — —" both shipped as broken copy.
    const asof = meta.positions_asof
      ? new Date(meta.positions_asof).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })
      : null;
    label = stale
      ? (at ? `Disconnected · last live ${clock(meta.last_refresh)}` : "Disconnected")
      : eod
        ? `EOD${asof ? ` · positions ${asof}` : ""} · quotes delayed`
        : `Live · ${clock(meta.last_refresh)} · delayed 15 min`;
    setText($("staleCopy"), at
      ? `Feed disconnected. These are the last values received at ${clock(meta.last_refresh)} — don't trade on them.`
      : "Feed disconnected — don't trade on these values.");
  } else {
    // No feed: we are showing a snapshot written by build.py.
    const generated = meta.generated_at ? new Date(meta.generated_at) : null;
    const age = generated ? Date.now() - generated.getTime() : Infinity;
    stale = meta.gateway !== "ok" || age > SNAPSHOT_STALE_MS;
    label = stale
      ? (generated ? `Stale · last ${clock(meta.generated_at)}` : "Stale")
      : `${stamp(meta.generated_at)} · delayed 15 min`;
    setText($("staleCopy"), generated
      ? `Snapshot from ${stamp(meta.generated_at)} — the live feed is not running.`
      : "Feed disconnected — don't trade on these values.");
  }

  root.dataset.state = stale ? "stale" : "ready";
  $("feed").dataset.state = stale ? "stale" : eod ? "eod" : "live";
  setText($("feedLabel"), label);
  // One feed indicator, not two: the topbar pill owns the live state. The
  // sidebar caption only speaks in snapshot mode, where there is no pill row
  // saying when the figures are from.
  setText($("footMeta"), livePolling
    ? ""
    : (meta.generated_at ? `Snapshot ${stamp(meta.generated_at)}` : "No snapshot"));

}

/* ---------------- shell interactions ---------------- */

function showTab(name) {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.setAttribute("aria-selected", String(tab.dataset.tab === name));
  }
  for (const view of document.querySelectorAll(".view")) {
    view.hidden = view.id !== `view-${name}`;
  }
  // Every other view re-renders when it is shown; Overview never did, and its
  // charts size their viewBox from a real pixel box. Boot on any other route —
  // #performance, #allocation, a #stock deep link — and the equity curve is
  // drawn at zero size, falling back to a 780x260 viewBox that then sits
  // letterboxed in a 526x90 box for the rest of the session. Redraw on show.
  if (name === "overview") renderChart();
  // Holdings owns a sub-route; re-enter it so a deep link survives tab changes.
  if (name === "holdings") holdings.route();
  if (name === "market") marketwatch.route();
  if (name === "stock") stock.route();
  if (name === "financials") financials.route();
  if (name === "performance") performance.route();
  if (name === "allocation") allocationView.route();
  if (name === "transactions") transactions.route();
  for (const item of document.querySelectorAll(".nav-item")) {
    if (item.dataset.nav) {
      item.toggleAttribute("aria-current", item.dataset.nav === name);
      if (item.dataset.nav === name) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    }
  }
}

/** The hash owns which tab is showing, so #holdings/mag7 survives a reload. */
function tabFromHash() {
  const name = (location.hash || "").replace(/^#/, "").split("/")[0];
  return document.getElementById(`view-${name}`) ? name : null;
}

/* The theme toggle the tokens file always promised ("a toggle is a one-line
 * change rather than a redesign"). Auto follows the OS; the explicit states
 * stamp data-theme, which is what every token block keys on. */
const THEME_KEY = "pd.theme";
const THEME_ORDER = ["auto", "dark", "light"];

function applyTheme(mode) {
  if (mode === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = mode;
  const label = $("themeToggle");
  if (label) label.textContent = `Theme · ${mode[0].toUpperCase()}${mode.slice(1)}`;
}

function initTheme() {
  let mode = null;
  try { mode = localStorage.getItem(THEME_KEY); } catch { /* private mode */ }
  if (!THEME_ORDER.includes(mode)) mode = "auto";
  applyTheme(mode);
  $("themeToggle")?.addEventListener("click", () => {
    mode = THEME_ORDER[(THEME_ORDER.indexOf(mode) + 1) % THEME_ORDER.length];
    applyTheme(mode);
    try { localStorage.setItem(THEME_KEY, mode); } catch { /* private mode */ }
  });
}

function wireShell() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      // Writing the hash drives showTab via hashchange, so a tab click is
      // linkable and the back button steps through tabs.
      if (tabFromHash() === tab.dataset.tab) showTab(tab.dataset.tab);
      else location.hash = `#${tab.dataset.tab}`;
    });
  }

  window.addEventListener("hashchange", () => {
    const name = tabFromHash();
    if (name) showTab(name);
  });
  for (const item of document.querySelectorAll(".nav-item[data-nav]")) {
    item.addEventListener("click", (event) => {
      const name = item.dataset.nav;
      if (document.getElementById(`view-${name}`)) {
        event.preventDefault();
        showTab(name);
      }
    });
  }
  // Scoped to [data-range]. `.range` is the shared pressed-button look and is
  // worn by other groups too — Market watch's sort order, Performance's income
  // toggle — and an unscoped selector bound this handler to those as well,
  // blanking the time range whenever one of them was clicked.
  for (const button of document.querySelectorAll(".range[data-range]")) {
    button.addEventListener("click", () => {
      activeRange = button.dataset.range;
      for (const other of document.querySelectorAll(".range[data-range]")) {
        other.setAttribute("aria-pressed", String(other === button));
      }
      renderChart();
    });
  }

  // NAV / Return toggle, remembered — someone who reads the account in
  // performance terms reads it that way tomorrow too.
  try {
    const stored = localStorage.getItem(CHART_MODE_KEY);
    if (stored === "twr" || stored === "nav") chartMode = stored;
  } catch { /* private mode */ }
  for (const button of document.querySelectorAll("#chartMode [data-chart-mode]")) {
    button.setAttribute("aria-pressed", String(button.dataset.chartMode === chartMode));
    button.addEventListener("click", () => {
      if (button.dataset.chartMode === chartMode) return;
      chartMode = button.dataset.chartMode;
      for (const other of document.querySelectorAll("#chartMode [data-chart-mode]")) {
        other.setAttribute("aria-pressed", String(other === button));
      }
      try { localStorage.setItem(CHART_MODE_KEY, chartMode); } catch { /* private mode */ }
      renderChart();
    });
  }

  // The curve sizes its viewBox from the element's real pixel box, so a resize
  // has to redraw it — otherwise the geometry keeps the old window's shape.
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (portfolio) renderChart(); }, 150);
  });
}

/* ---------------- boot ---------------- */

async function boot() {
  wireShell();
  try {
    [portfolio, navHistory] = await Promise.all([loadJSON(DATA_URL), loadNavHistory()]);
  } catch (error) {
    root.dataset.state = "stale";
    $("feed").dataset.state = "stale";
    $("feedLabel").textContent = "No data";
    setText($("staleCopy"), "Feed disconnected — don't trade on these values.");
    console.error("could not load portfolio data", error);
    return;
  }

  if (!portfolio.positions || portfolio.positions.length === 0) {
    root.dataset.state = "empty";
    renderFreshness(portfolio);
    // Replace the screenful, not the view: the view now also holds the
    // holdings section and the scroll cue, and blowing those away would leave
    // the page unable to render them again without a reload.
    const screen = document.querySelector(".ov-screen") || $("view-overview");
    screen.innerHTML = `
      <div class="card empty">
        <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.4">
          <rect x="5" y="12" width="38" height="26" rx="4"/><path d="M5 20h38M13 29h8"/>
        </svg>
        <h2>No positions yet</h2>
        <p>The account connected, but it holds nothing right now. Positions appear here as soon as you open one.</p>
      </div>`;
    // Nothing held means nothing to list below the fold either.
    $("ovHoldings")?.setAttribute("hidden", "");
    $("ovCue")?.setAttribute("hidden", "");
    // The empty state used to be a dead end: boot returned before any poller
    // started, so a position opened later never appeared without a manual
    // reload. Keep asking quietly and reload into the full page when one does.
    const recheck = async () => {
      try {
        const res = await fetch(`${LIVE_URL}?t=${Date.now()}`, { cache: "no-store" });
        if (res.ok) {
          const snap = await res.json();
          if ((snap.positions || []).length) { location.reload(); return; }
        }
      } catch { /* transient; keep waiting */ }
      setTimeout(recheck, 30000);
    };
    setTimeout(recheck, 30000);
    return;
  }

  renderKpis(portfolio);
  renderChart();
  renderAllocation(portfolio);
  allocationView.update(portfolio);
  renderMovers(portfolio);
  renderCurrencies(portfolio);
  renderHoldings(portfolio);
  tape.update(portfolio);
  renderFreshness(portfolio);

  applyLive._moversKey = moversKey(portfolio);

  holdings.init();
  marketwatch.init();
  stock.init();
  search.init();
  financials.init();
  ovholdings.init();
  // Fires its own fetch and re-renders the curve when it lands, so the chart
  // paints immediately from nav_history and gains its comparison line a moment
  // later rather than waiting on a second request before showing anything.
  initBenchmark();
  // Same pattern for the Return mode's daily-return series: fetched behind
  // the first paint, re-rendering the curve when it lands.
  loadDailyReturns();
  initAllocation();
  initTheme();
  performance.init();
  allocationView.init();
  transactions.init();
  // A name added from the stock page appears without waiting out the 20s poll.
  stock.onWatchAdded(() => pollWatchlist());
  const initial = tabFromHash();
  if (initial) showTab(initial);

  startPolling();
  startWatchlistPolling();
  startMarketsPolling();
  startNewsPolling();
}

/* Watchlist quotes are fetched on their own cadence and are entirely optional:
 * if the endpoint is missing or failing, owned rows still arrive from the IB
 * feed and Holdings simply shows watchlist entries as unavailable. */
async function pollWatchlist() {
  try {
    const res = await fetch(`${WATCH_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) return false;          // older serve.py — stop asking
    const payload = await res.json();
    holdings.updateQuotes(payload);
    stock.updateQuotes(payload);
    search.updateQuotes(payload);
    // The universe arrives on THIS poll, not the snapshot one — logos, tile
    // colours and the ticker key that makes each row a link all come from here.
    ovholdings.update();
    return true;
  } catch {
    return true;                         // transient; keep trying
  }
}

function startWatchlistPolling() {
  const tick = async () => {
    const again = await pollWatchlist();
    if (again) setTimeout(tick, WATCH_POLL_MS);
  };
  tick();
}

/* The world board is the third independent source. Like the watchlist it is
 * entirely optional: if it never answers, Market watch keeps its skeletons and
 * every owned figure elsewhere carries on from the IB feed. */
async function pollMarkets() {
  try {
    const res = await fetch(`${MARKETS_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) return false;          // older serve.py — stop asking
    marketwatch.update(await res.json());
    return true;
  } catch {
    return true;                         // transient; keep trying
  }
}

function startMarketsPolling() {
  const tick = async () => {
    const again = await pollMarkets();
    if (again) setTimeout(tick, MARKETS_POLL_MS);
  };
  tick();
}

/* The outlet re-asks every five minutes — the server itself only sweeps its
 * sources every twenty, so most polls are a cheap cache read. */
async function pollNews() {
  try {
    const res = await fetch(`api/news?t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) return false;          // older serve.py — stop asking
    renderOutlet(await res.json());
    return true;
  } catch {
    return true;                         // transient; keep trying
  }
}

function startNewsPolling() {
  const tick = async () => {
    const again = await pollNews();
    if (again) setTimeout(tick, NEWS_POLL_MS);
  };
  tick();
}

/* ---------------- live polling ---------------- */

async function pollOnce() {
  let live;
  try {
    const res = await fetch(`${LIVE_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (res.status === 404) {
      // Older serve.py with no endpoint. Stop asking and keep the snapshot.
      livePolling = false;
      return false;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    live = await res.json();
  } catch {
    // Server unreachable — the values on screen are no longer moving, and
    // renderFreshness will say so on the next tick.
    if (livePolling && portfolio) {
      portfolio.meta = { ...portfolio.meta, connected: false };
      renderFreshness(portfolio);
    }
    return true;
  }

  const meta = live.meta || {};
  if (meta.source === "none") {   // started with --no-live
    livePolling = false;
    return false;
  }
  livePolling = true;

  if (meta.connected && (live.positions || []).length) {
    // Keep the sparklines from the initial snapshot; the feed cannot supply a
    // historical series and sends an empty array.
    const sparks = new Map((portfolio?.positions || []).map((p) => [p.con_id, p.spark]));
    for (const p of live.positions) {
      if (!p.spark?.length) p.spark = sparks.get(p.con_id) || [];
    }
    portfolio = { ...portfolio, ...live, meta: { ...portfolio?.meta, ...meta } };
    applyLive(portfolio);
  } else if (portfolio) {
    portfolio.meta = { ...portfolio.meta, ...meta };
  }

  renderFreshness(portfolio || live);
  return true;
}

function startPolling() {
  let timer = null;
  const tick = async () => {
    const again = await pollOnce();
    if (again) timer = setTimeout(tick, POLL_MS);
  };
  tick();

  // Don't poll a tab nobody is looking at; catch up immediately on return.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && livePolling) {
      clearTimeout(timer);
      tick();
    }
  });
}

boot();
