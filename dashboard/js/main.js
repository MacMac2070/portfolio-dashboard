/* Reads data/portfolio.json + data/nav_history.jsonl and renders the dashboard.
 *
 * Both files are produced by adapter/build.py against IB Gateway. This module
 * never talks to a broker; it only renders what the adapter wrote, so the page
 * works offline and the stale state is a genuine signal rather than a spinner.
 */

import {
  money, moneyCompact, moneySigned, pctSigned, pct, qty,
  direction, ARROW, stamp, clock, initials,
} from "./format.js";
import { sparkline, donut, equityCurve, countUp } from "./charts.js";
import * as holdings from "./holdings.js";
import * as marketwatch from "./marketwatch.js";
import * as stock from "./stock.js";
import * as search from "./search.js";
import * as financials from "./financials.js";
import * as ovholdings from "./ovholdings.js";

const DATA_URL = "data/portfolio.json";
const NAV_URL = "data/nav_history.jsonl";
const LIVE_URL = "api/snapshot";
const WATCH_URL = "api/watchlist";
const MARKETS_URL = "api/markets";
/* Watchlist quotes come from openbb on a 60s server-side cycle, so polling it
 * at the IB feed's 3s would just re-fetch the same numbers. */
const WATCH_POLL_MS = 20000;
/* Same cycle, and index levels are daily closes — there is nothing faster to
 * see. The clock strip ticks locally, so the board still feels live between
 * polls without asking the server anything. */
const MARKETS_POLL_MS = 30000;
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
const catColor = (index) => `var(--cat-${Math.min((index ?? 6) + 1, 7)})`;

const RANGE_DAYS = { "1D": 2, "7D": 7, "1M": 30, "1Y": 365, ALL: Infinity };
const RANGE_LABEL = { "1D": "1D", "7D": "7D", "1M": "1M", "1Y": "1Y", ALL: "All" };
const RANGE_NOTE = {
  "1D": "past day · daily close",
  "7D": "past week · daily close",
  "1M": "past month · daily close",
  "1Y": "past year · daily close",
  ALL: "since inception · daily close",
};

let portfolio = null;
let navHistory = [];
let activeRange = "1M";

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
      .sort((a, b) => a.date.localeCompare(b.date));
  } catch {
    return [];
  }
}

/* ---------------- chips ---------------- */

function paintChip(node, value, { digits = 2 } = {}) {
  if (!node) return;
  const dir = direction(value);
  node.className = `chip kpi__chip chip--${dir === "up" ? "pos" : dir === "down" ? "neg" : "flat"}`;
  node.innerHTML =
    `<span class="chip__arrow" aria-hidden="true">${ARROW[dir]}</span>` +
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

function renderChart() {
  const plot = $("chartPlot");
  const points = sliceRange(navHistory, activeRange);

  $("chartNote").textContent = RANGE_NOTE[activeRange];
  for (const id of ["chartChangeLabel", "chartHighLabel", "chartLowLabel"]) {
    const suffix = id.includes("Change") ? "change" : id.includes("High") ? "high" : "low";
    $(id).textContent = `${RANGE_LABEL[activeRange]} ${suffix}`;
  }

  if (points.length < MIN_CURVE_POINTS) {
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

  plot.innerHTML = '<svg id="chartSvg" role="img" aria-label="Portfolio value over time"></svg>';
  equityCurve($("chartSvg"), points, {
    tooltip: $("tip"),
    formatValue: (v, full) => (full ? money(v) : moneyCompact(v)),
    formatDate: (d) => new Date(d).toLocaleDateString("en-GB", { day: "numeric", month: "short" }),
  });

  const first = points[0].value;
  const last = points[points.length - 1].value;
  const values = points.map((p) => p.value);
  const changePct = first ? ((last - first) / first) * 100 : null;

  paintChip($("chartChip"), changePct);
  $("chartChip").className = $("chartChip").className.replace("kpi__chip ", "");

  const changeNode = $("chartChange");
  changeNode.textContent = moneySigned(last - first);
  changeNode.className = `num ${direction(last - first) === "up" ? "pos" : direction(last - first) === "down" ? "neg" : "flat"}`;
  $("chartHigh").textContent = money(Math.max(...values));
  $("chartLow").textContent = money(Math.min(...values));
}

/* ---------------- allocation ---------------- */

function renderAllocation(data) {
  const rows = data.regions || [];
  const invested = data.kpis?.invested ?? 0;

  donut($("donutSvg"), rows.map((r) => ({
    label: r.name,
    value: r.value_gbp,
    color: catColor(r.color_index),
    display: `${pct(r.weight_pct, 0)} · ${money(r.value_gbp)}`,
  })), {
    centreValue: money(invested),
    centreCaption: "Invested",
  });

  $("allocLegend").innerHTML = rows.map((r) => `
    <div class="legend__row" data-region="${r.name}">
      <span class="legend__dot" style="background:${catColor(r.color_index)}"></span>
      <span class="legend__name">${r.name}</span>
      <span class="legend__pct num" data-f="pct">${pct(r.weight_pct, 0)}</span>
      <span class="legend__val num" data-f="val">${money(r.value_gbp)}</span>
    </div>`).join("");
}

/* ---------------- movers ---------------- */

function moverRow(p) {
  const dir = direction(p.day_change_pct);
  const chipClass = dir === "up" ? "chip--pos" : dir === "down" ? "chip--neg" : "chip--flat";
  const tint = dir === "up" ? "var(--pos-bg)" : dir === "down" ? "var(--neg-bg)" : "rgba(122,132,144,.12)";
  return `
    <div class="mover" data-con-id="${p.con_id}">
      <span class="avatar" style="background:${tint}">${initials(p.symbol)}</span>
      <span class="mover__ticker">${p.symbol}</span>
      <svg class="mover__spark" data-spark="${p.con_id}" aria-hidden="true"></svg>
      <span class="chip ${chipClass} mover__chip">
        <span class="chip__arrow" aria-hidden="true">${ARROW[dir]}</span>${pctSigned(p.day_change_pct)}
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

function renderConcentration(data) {
  const c = data.concentration || {};
  const rows = [
    {
      label: "Largest position",
      html: `<span class="conc__badge num">${c.positions ?? "—"}</span>` +
            `<span class="conc__value conc__value--brand num">${c.largest_symbol ?? "—"} · ${pct(c.largest_weight_pct)}</span>`,
    },
    { label: "Top 3 weight", html: `<span class="conc__value num">${pct(c.top3_weight_pct)}</span>` },
    { label: "Positions",    html: `<span class="conc__value num">${c.positions ?? "—"}</span>` },
    { label: "Markets",      html: `<span class="conc__value num">${c.markets ?? "—"}</span>` },
    { label: "Cash weight",  html: `<span class="conc__value num">${pct(c.cash_weight_pct)}</span>` },
  ];
  $("concList").innerHTML = rows.map((r) => `
    <div class="conc__row" data-conc="${r.label}">
      <span class="conc__label">${r.label}</span>
      <span style="display:flex;align-items:center;justify-content:flex-end">${r.html}</span>
    </div>`).join("");
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

  // --- allocation: donut arcs + legend figures ---
  const regions = data.regions || [];
  const legendKeys = [...document.querySelectorAll(".legend__row")]
    .map((r) => r.dataset.region).join(",");
  if (legendKeys !== regions.map((r) => r.name).join(",")) {
    renderAllocation(data);                       // composition changed
  } else {
    donut($("donutSvg"), regions.map((r) => ({
      label: r.name, value: r.value_gbp, color: catColor(r.color_index),
      display: `${pct(r.weight_pct, 0)} · ${money(r.value_gbp)}`,
    })), { centreValue: money(invested), centreCaption: "Invested" });
    for (const r of regions) {
      const row = document.querySelector(`.legend__row[data-region="${CSS.escape(r.name)}"]`);
      if (!row) continue;
      setText(row.querySelector('[data-f="pct"]'), pct(r.weight_pct, 0));
      setText(row.querySelector('[data-f="val"]'), money(r.value_gbp));
    }
  }

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
        `<span class="chip__arrow" aria-hidden="true">${ARROW[dir]}</span>${pctSigned(p.day_change_pct)}`;
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

  // --- concentration ---
  const conc = data.concentration || {};
  const concValues = {
    "Largest position": `${conc.largest_symbol ?? "—"} · ${pct(conc.largest_weight_pct)}`,
    "Top 3 weight": pct(conc.top3_weight_pct),
    "Positions": String(conc.positions ?? "—"),
    "Markets": String(conc.markets ?? "—"),
    "Cash weight": pct(conc.cash_weight_pct),
  };
  for (const [label, value] of Object.entries(concValues)) {
    const row = document.querySelector(`.conc__row[data-conc="${CSS.escape(label)}"]`);
    setText(row?.querySelector(".conc__value"), value);
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
  if (livePolling) {
    // Live: the feed reports whether it holds a connection, and last_refresh
    // proves the loop is still running. Either failing means stale.
    const at = meta.last_refresh ? new Date(meta.last_refresh) : null;
    const age = at ? Date.now() - at.getTime() : Infinity;
    stale = !meta.connected || age > LIVE_STALE_MS;
    label = stale
      ? `Disconnected · last live ${at ? clock(meta.last_refresh) : "—"}`
      : `Live · ${clock(meta.last_refresh)} · delayed 15 min`;
    $("staleTime").textContent = at ? clock(meta.last_refresh) : "—";
  } else {
    // No feed: we are showing a snapshot written by build.py.
    const generated = meta.generated_at ? new Date(meta.generated_at) : null;
    const age = generated ? Date.now() - generated.getTime() : Infinity;
    stale = meta.gateway !== "ok" || age > SNAPSHOT_STALE_MS;
    label = stale
      ? `Stale · last ${clock(meta.generated_at)}`
      : `${stamp(meta.generated_at)} · delayed 15 min`;
    $("staleTime").textContent = clock(meta.generated_at);
  }

  root.dataset.state = stale ? "stale" : "ready";
  $("feed").dataset.state = stale ? "stale" : "live";
  setText($("feedLabel"), label);

  const tx = (data.fills || []).length;
  setText($("txCopy"), tx
    ? `${tx} execution${tx === 1 ? "" : "s"} recorded today. Older trades appear once the Flex import is configured.`
    : "Trade history is collected from the daily job and, once configured, backfilled from your IBKR Flex statement.");
}

/* ---------------- shell interactions ---------------- */

function showTab(name) {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.setAttribute("aria-selected", String(tab.dataset.tab === name));
  }
  for (const view of document.querySelectorAll(".view")) {
    view.hidden = view.id !== `view-${name}`;
  }
  // Holdings owns a sub-route; re-enter it so a deep link survives tab changes.
  if (name === "holdings") holdings.route();
  if (name === "market") marketwatch.route();
  if (name === "stock") stock.route();
  if (name === "financials") financials.route();
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
  for (const button of document.querySelectorAll(".range")) {
    button.addEventListener("click", () => {
      activeRange = button.dataset.range;
      for (const other of document.querySelectorAll(".range")) {
        other.setAttribute("aria-pressed", String(other === button));
      }
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
    $("staleTime").textContent = "—";
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
    return;
  }

  renderKpis(portfolio);
  renderChart();
  renderAllocation(portfolio);
  renderMovers(portfolio);
  renderCurrencies(portfolio);
  renderConcentration(portfolio);
  renderHoldings(portfolio);
  renderFreshness(portfolio);

  applyLive._moversKey = moversKey(portfolio);

  holdings.init();
  marketwatch.init();
  stock.init();
  search.init();
  financials.init();
  ovholdings.init();
  // A name added from the stock page appears without waiting out the 20s poll.
  stock.onWatchAdded(() => pollWatchlist());
  const initial = tabFromHash();
  if (initial) showTab(initial);

  startPolling();
  startWatchlistPolling();
  startMarketsPolling();
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
