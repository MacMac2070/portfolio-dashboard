/* Company financials — #financials/<KEY>.
 *
 * One source: /api/financials/<KEY>. Deliberately no live wiring — this page
 * shows no price, so there is nothing here that could disagree with Overview
 * or the stock page, and the endpoint already ships the identity block.
 *
 * Two directions on one screen, both on purpose:
 *   the tables run newest-first, as filings and the provider do
 *   the trend chart runs oldest-first, because a time axis reads left to right
 * The latest column is marked and the chart's periods are labelled, so this
 * reads as two conventions rather than as a bug.
 *
 * Every figure is as the company reported it, in the company's own reporting
 * currency, which is not always what the share quotes in — HSBC reports USD
 * and quotes pence. Nothing here is converted or scaled; see adapter/units.py.
 */
import {
  statementScale, statementValue, priceNative, compact, pct, esc, day,
} from "./format.js";
import { groupedBars } from "./charts.js";

const $ = (id) => document.getElementById(id);
const DASH = "—";

const TABS = [
  ["income", "Income statement"],
  ["balance", "Balance sheet"],
  ["cash", "Cash flow"],
];
const SERIES_COLOURS = ["var(--fin-1)", "var(--fin-2)", "var(--fin-3)"];

let key = null;
let detail = null;
let loading = false;
let failed = null;
let retryTimer = null;
let period = "annual";
let tab = "income";
let tabTouched = false;
/** key -> logo url from the watchlist universe — the financials payload
 *  carries identity colours but no mark. Fetched once, lazily. */
let logos = null;

/* ---------------- fetch ---------------- */

async function load(force = false) {
  if (!key) return;
  if (loading && !force) return;
  loading = true;
  failed = null;
  render();
  try {
    const res = await fetch(
      `api/financials/${encodeURIComponent(key)}?period=${period}&t=${Date.now()}`,
      { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    if (payload?.instrument?.key && payload.instrument.key !== key) return;
    detail = payload;

    // The service warms openbb on its own thread, and a slow statement can miss
    // the request budget. Both say retry.
    const retry = payload?.meta?.retry_after;
    clearTimeout(retryTimer);
    if (retry) retryTimer = setTimeout(() => load(true), retry * 1000);

    // Land on a statement that actually has something, unless the user chose.
    if (!tabTouched) {
      const first = TABS.find(([id]) => payload.statements?.[id]?.available);
      if (first) tab = first[0];
    }
  } catch (err) {
    failed = String(err.message || err);
    detail = null;
  } finally {
    loading = false;
    render();
  }
}

/* ---------------- render ---------------- */

function render() {
  const view = $("view-financials");
  if (!view || view.hidden) return;

  const d = detail || {};
  const ident = d.instrument || {};
  const rep = d.reporting || {};
  const statements = d.statements || {};
  const active = pruneDeadPeriods(statements[tab] || {});

  /* identity */
  $("finCrumbStock").textContent = key || DASH;
  $("finCrumbStock").href = `#stock/${encodeURIComponent(key || "")}`;
  const tile = $("finTile");
  $("finMono").textContent = ident.mono || "";
  tile.style.background = ident.tint || "#EDF0F3";
  $("finMono").style.color = ident.ink || "#2E2452";
  // The issuer's mark, as the stock hero one click earlier draws it. The
  // financials payload carries no logo, so it comes from the watchlist
  // universe; until (or unless) that lands, the monogram holds the tile.
  const img = $("finLogo");
  const logo = (logos && key && logos[key]) || "";
  if (img) {
    if (logo) {
      if (img.dataset.src !== logo) { img.dataset.src = logo; img.src = logo; }
      img.hidden = false;
    } else {
      img.hidden = true;
    }
  }
  $("finName").textContent = ident.name || (key ? `Loading ${key}…` : DASH);
  $("finTicker").textContent = key || DASH;
  $("finSub").textContent = [ident.exchange_name, ident.sector]
    .filter(Boolean).join(" · ") || DASH;

  $("finCurrency").textContent = rep.currency || DASH;
  const mismatch = $("finMismatch");
  mismatch.hidden = !rep.differs_from_quote;
  if (rep.differs_from_quote) {
    mismatch.textContent = `Share quotes in ${rep.quote_currency}`;
  }

  /* banner */
  const msg = bannerMessage(d, statements);
  $("finBanner").hidden = !msg;
  if (msg) $("finBannerText").textContent = msg;

  /* controls */
  renderTabs(statements);
  for (const b of $("finPeriods").querySelectorAll("[data-period]")) {
    b.setAttribute("aria-pressed", String(b.dataset.period === period));
  }

  /* the two cards */
  const anything = TABS.some(([id]) => statements[id]?.available);
  $("finChartCard").hidden = !anything;
  if (anything) {
    renderChart(active, rep);
    renderTable(active, rep, d.coverage || {});
  } else {
    renderEmpty(ident, d);
  }
}

/** Drop statement periods that are effectively dead. yfinance files four
 *  full annual periods; a fifth arrives carrying three stray interest lines
 *  against twenty-nine real rows, and drew as an empty year in both the
 *  chart and the table. A period stays only with at least a quarter of the
 *  best period's populated rows, so sparse-but-real filers are untouched. */
function pruneDeadPeriods(active) {
  const periods = active.periods || [];
  if (!periods.length) return active;
  const rows = [
    ...(active.chart || []),
    ...(active.sections || []).flatMap((s) => s.rows || []),
  ];
  const counts = periods.map((_, i) =>
    rows.reduce((n, r) => n + (Number.isFinite(r.values?.[i]) ? 1 : 0), 0));
  const floor = Math.max(1, Math.max(...counts) * 0.25);
  const alive = counts.map((n) => n >= floor);
  if (alive.every(Boolean)) return active;
  const cut = (values) => (values || []).filter((_, i) => alive[i]);
  return {
    ...active,
    periods: periods.filter((_, i) => alive[i]),
    chart: (active.chart || []).map((s) => ({ ...s, values: cut(s.values) })),
    sections: (active.sections || []).map((sec) => ({
      ...sec, rows: (sec.rows || []).map((r) => ({ ...r, values: cut(r.values) })),
    })),
  };
}

function bannerMessage(d, statements) {
  if (failed) return `Couldn't reach the financials service — ${failed}.`;
  const meta = d.meta;
  if (!meta) return null;
  if (meta.state === "warming") return "Market data service is starting. Statements will appear shortly.";
  if (meta.state === "busy") return "Market data service is busy. Retrying…";
  if (meta.error) return `Statements unavailable — ${meta.error}`;
  if (meta.pending?.length) return `Still loading: ${meta.pending.join(", ")}.`;
  if (meta.stale?.length) return `Showing last known figures for ${meta.stale.join(", ")} — a refresh failed.`;
  const missing = TABS.filter(([id]) => statements[id] && !statements[id].available);
  if (missing.length && missing.length < TABS.length) {
    return `${missing.map(([, label]) => label).join(" and ")} unavailable for this instrument.`;
  }
  return null;
}

function renderTabs(statements) {
  const html = TABS.map(([id, label]) => {
    const s = statements[id] || {};
    const ok = s.available;
    return `<button class="srange" type="button" data-tab="${id}"
      aria-pressed="${id === tab}" ${ok ? "" : "disabled"}
      ${ok ? "" : `title="${esc(s.reason || "not published for this instrument")}"`}
      >${label}</button>`;
  }).join("");
  const host = $("finTabs");
  if (host.dataset.html !== html) { host.innerHTML = html; host.dataset.html = html; }
}

/* ---------------- chart ---------------- */

function renderChart(active, rep) {
  const svg = $("finSvg");
  const label = TABS.find(([id]) => id === tab)?.[1] || "Trend";
  $("finChartTitle").textContent = label;

  const chart = active.chart || [];
  if (!active.available || !chart.length) {
    svg.replaceChildren();
    $("finLegend").innerHTML = "";
    $("finChartNote").textContent = loading ? "Loading…" : DASH;
    return;
  }

  // The chart reverses: the tables are newest-first but a time axis reads left
  // to right, so oldest is leftmost here.
  const periods = [...(active.periods || [])].reverse();
  const series = chart.map((s, i) => ({
    key: s.key, label: s.label, color: SERIES_COLOURS[i % SERIES_COLOURS.length],
    values: [...s.values].reverse(),
  }));

  const all = series.flatMap((s) => s.values).filter(Number.isFinite);
  const scale = statementScale(all);
  const ccy = rep.currency || "";

  $("finChartNote").textContent = marginNote(active, periods);
  $("finLegend").innerHTML = series.map((s) => `
    <span class="slegend"><i class="slegend__line slegend__sq" style="background:${s.color}"></i>
    <span>${esc(s.label)}</span></span>`).join("");

  groupedBars(svg, {
    periods,
    series,
    formatValue: (v, long = false) => long
      ? `${ccy ? ccy + " " : ""}${statementValue(v, { divisor: 1 })}`
      : statementValue(v, scale),
    formatPeriod: (p, _i, long = false) => (long ? day(p) : String(p).slice(0, 4)),
    tooltip: $("tip"),
    chartLabel: label,
  });
  $("finUnits").dataset.scale = scale.unit;
}

/** The readout a second y-axis would have carried, as text instead. */
function marginNote(active, periods) {
  const find = (name) => {
    for (const sec of active.sections || []) {
      const hit = (sec.rows || []).find((r) => r.label === name);
      if (hit) return hit;
    }
    return null;
  };
  const margin = find("Net margin");
  if (margin) {
    const vals = [...margin.values].reverse().filter(Number.isFinite);
    if (vals.length >= 2) {
      return `Net margin ${pct(vals[0], 1)} → ${pct(vals[vals.length - 1], 1)}`;
    }
  }
  return periods.length ? `${periods[0]} to ${periods[periods.length - 1]}` : DASH;
}

/* ---------------- table ---------------- */

function renderTable(active, rep, coverage) {
  const table = $("finTable");
  const label = TABS.find(([id]) => id === tab)?.[1] || "";
  $("finTableTitle").textContent = label;

  if (!active.available) {
    table.innerHTML = "";
    $("finUnits").textContent = DASH;
    $("finNote").textContent = active.reason || "";
    return;
  }

  const periods = active.periods || [];
  const money = (active.sections || []).flatMap((s) => s.rows)
    .filter((r) => r.kind === "money").flatMap((r) => r.values);
  const scale = statementScale(money);
  const ccy = rep.currency || "";
  $("finUnits").textContent =
    [ccy, scale.unit].filter(Boolean).join(" ") || "as reported";

  const cell = (row, v) => {
    switch (row.kind) {
      case "per_share": return priceNative(v, ccy, { digits: 2 });
      case "shares": return compact(v);
      case "pct": return pct(v, 1);
      default: return statementValue(v, scale);
    }
  };

  const head = `<thead><tr><th scope="col">Line item</th>${
    periods.map((p, i) => `<th scope="col" class="${i === 0 ? "is-latest" : ""}">${
      esc(day(p))}</th>`).join("")}</tr></thead>`;

  const body = (active.sections || []).map((sec) => {
    const header = `<tr class="finsection${sec.derived ? " finsection--derived" : ""}">
      <th scope="colgroup" colspan="${periods.length + 1}">${esc(sec.title)}</th></tr>`;
    const rows = sec.rows.map((r) => `
      <tr data-emphasis="${r.indent}">
        <th scope="row" data-indent="${r.indent}">${esc(r.label)}</th>
        ${r.values.map((v, i) => `<td class="${i === 0 ? "is-latest" : ""}"${
          Number.isFinite(v) ? ` title="${esc(String(v))}"` : ""
        }>${esc(cell(r, v))}</td>`).join("")}
      </tr>`).join("");
    return header + rows;
  }).join("");

  table.innerHTML = head + `<tbody>${body}</tbody>`;

  const provider = coverage[tab]?.provider || "the data provider";
  $("finNote").textContent =
    `${label} served by ${provider}. Figures are as reported by the company` +
    `${ccy ? ` in ${ccy}` : ""}, not adjusted and not converted` +
    `${rep.differs_from_quote ? `; the share quotes in ${rep.quote_currency}` : ""}.` +
    ` Latest period first.`;
}

function renderEmpty(ident, d) {
  const isFund = String(ident.sector || "").toLowerCase() === "etfs";
  $("finTableTitle").textContent = "Financial statements";
  $("finUnits").textContent = "";
  $("finNote").textContent = "";
  $("finTable").innerHTML = `
    <tbody><tr><td>
      <div class="hempty">
        <p class="hempty__title">${isFund ? "Funds don't file company statements" : "No statements published"}</p>
        <p class="hempty__msg">${isFund
          ? `${esc(ident.name || key || "This instrument")} is a fund. Funds publish holdings and a factsheet rather than company income, balance-sheet and cash-flow statements.`
          : `The data provider publishes no financial statements for ${esc(ident.name || key || "this instrument")}.`}</p>
        <p class="hempty__msg"><a class="crumb__link" href="#stock/${encodeURIComponent(key || "")}">Back to ${esc(key || "the stock page")} →</a></p>
      </div>
    </td></tr></tbody>`;
}

async function loadLogos() {
  if (logos) return;
  logos = {};
  try {
    const res = await fetch("/api/watchlist");
    if (!res.ok) return;
    const payload = await res.json();
    for (const t of Object.values(payload?.universe?.tickers || {})) {
      logos[t.key] = t.logo || "";
    }
    if (!$("view-financials").hidden) render();
  } catch { /* the monogram holds the tile */ }
}

/* ---------------- routing ---------------- */

function fromHash() {
  const m = /^#financials\/(.+)$/.exec(location.hash);
  return m ? decodeURIComponent(m[1]) : null;
}

export function route() {
  const next = fromHash();
  if (!next) return;
  loadLogos();
  if (next !== key) {
    key = next;
    detail = null;
    failed = null;
    tabTouched = false;
    clearTimeout(retryTimer);
    render();
    load(true);
    return;
  }
  render();
}

export function init() {
  $("finTabs").addEventListener("click", (event) => {
    const btn = event.target.closest(".srange");
    if (!btn || btn.disabled) return;
    tab = btn.dataset.tab;
    tabTouched = true;
    render();
  });

  $("finPeriods").addEventListener("click", (event) => {
    const btn = event.target.closest(".srange");
    if (!btn || btn.disabled || btn.dataset.period === period) return;
    period = btn.dataset.period;
    load(true);
  });

  window.addEventListener("hashchange", () => {
    if (!$("view-financials").hidden) route();
  });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!$("view-financials").hidden && detail) {
        renderChart(detail.statements?.[tab] || {}, detail.reporting || {});
      }
    }, 140);
  });
}
