/* Stock detail page — #stock/<KEY>.
 *
 * Two sources, deliberately:
 *
 *   identity and price   the live payloads this page already polls
 *                        (/api/snapshot for owned, /api/watchlist for watched)
 *   everything else      /api/instrument/<KEY>, fetched on navigation
 *
 * The split matters. If the price here came from the instrument endpoint's own
 * cached quote it could differ from the same instrument's row on Overview and
 * Holdings by up to a minute, and two pages disagreeing about a price is worse
 * than a page being a second behind. So the live feed wins for the figures it
 * has, and the endpoint supplies only what it alone can — statistics, profile,
 * news, analyst view and history.
 *
 * Anything a provider did not return is an em-dash. Nothing here is invented:
 * the coverage map in the payload says which provider served each group, and a
 * group that came back empty prints nothing rather than a plausible number.
 */
import {
  priceNative, priceNativeSigned, pctSigned, money, count, compact, ratio,
  day, esc, direction, clock,
} from "./format.js";
import { priceChart } from "./charts.js";

const $ = (id) => document.getElementById(id);
const DASH = "—";

/* ---------------- state ---------------- */

let key = null;           // the ticker key in the hash
let detail = null;        // last /api/instrument payload
let loading = false;
let failed = null;        // fetch-level failure message
let range = "1M";
let rangeTouched = false; // has the user picked a range for this instrument?
// What the chart currently shows. The live polls call render() every 3s, but
// the chart's data arrives once per instrument fetch — rebuilding it per tick
// replayed the 900ms draw-on continuously. Redraw only when this changes.
let chartFp = "";
let retryTimer = null;

// live sources, fed by main.js from the pollers it already runs
let universe = null;
let quotes = {};
let owned = new Map();
let fx = {};
/* Read defensively: this runs during module evaluation, and main.js imports
 * this file, so anything thrown here blanks the entire dashboard before a
 * single view renders. A corrupt value, or a localStorage that throws at all
 * (Safari private mode), must cost the Watch marker and nothing else. */
function loadWatching() {
  try {
    const raw = JSON.parse(localStorage.getItem("watching") || "[]");
    return new Set(Array.isArray(raw) ? raw : []);
  } catch {
    return new Set();
  }
}

let watching = loadWatching();
let watchPending = false;
let watchFailed = false;   // server refused; the local marker still stands
let onWatched = null;      // set by main.js — re-poll the watchlist immediately

const RANGES = ["1D", "5D", "1M", "6M", "YTD", "1Y", "5Y", "MAX"];
const RANGE_LABEL = {
  "1D": "Today", "5D": "5 days", "1M": "1 month", "6M": "6 months",
  YTD: "Year to date", "1Y": "1 year", "5Y": "5 years", MAX: "All time",
};

/* ---------------- helpers ---------------- */

const ticker = () => universe?.tickers?.[key] || null;

/** The live figures, preferred over the endpoint's cached quote. */
function livePrice() {
  const t = ticker();
  if (!t) return null;
  if (t.owned) {
    const p = owned.get(t.con_id);
    if (p) return { last: p.price, dayPct: p.day_change_pct, currency: p.currency, live: true };
  }
  const q = quotes[t.symbol];
  if (q) return { last: q.price, dayPct: q.day_change_pct, currency: q.currency, live: true };
  return null;
}

function dirClass(v) {
  return v > 0 ? "up" : v < 0 ? "down" : "flat";
}

function paintChip(node, value) {
  if (!node) return;
  const d = dirClass(value);
  node.className = `dchip dchip--${d}`;
  node.textContent = pctSigned(value, 2);
}

/** Colour a figure by direction. The sign is always in the text too. */
function paintFigure(node, value, text) {
  if (!node) return;
  node.textContent = text;
  node.style.color = value > 0 ? "var(--gain-soft)"
    : value < 0 ? "var(--loss-soft)" : "var(--text-muted)";
}

/** Axis and tooltip labels. Intraday ranges read as times, daily as dates. */
function labelFor(rangeKey) {
  const intraday = rangeKey === "1D" || rangeKey === "5D";
  return (stamp, _i, long = false) => {
    if (!stamp) return "";
    const d = new Date(String(stamp).replace(" ", "T"));
    if (Number.isNaN(d.getTime())) return String(stamp).slice(0, 10);
    if (intraday) {
      const time = d.toLocaleTimeString("en-GB",
        { hour: "2-digit", minute: "2-digit", hour12: false });
      if (rangeKey === "1D" && !long) return time;
      return `${d.toLocaleDateString("en-GB", { weekday: "short" })} ${time}`;
    }
    if (long) return d.toLocaleDateString("en-GB",
      { day: "numeric", month: "short", year: "numeric" });
    const wide = rangeKey === "5Y" || rangeKey === "MAX";
    return d.toLocaleDateString("en-GB",
      wide ? { month: "short", year: "2-digit" } : { day: "numeric", month: "short" });
  };
}

/* ---------------- fetch ---------------- */

async function load(force = false) {
  if (!key) return;
  if (loading && !force) return;
  loading = true;
  failed = null;
  try {
    const res = await fetch(`api/instrument/${encodeURIComponent(key)}?t=${Date.now()}`,
      { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    // A different instrument was opened while this was in flight.
    if (payload?.instrument?.key && payload.instrument.key !== key) return;
    detail = payload;

    // The service warms openbb on its own thread, and long groups can miss the
    // request budget. Both say retry, so retry once rather than leaving the
    // page half-filled.
    const retry = payload?.meta?.retry_after;
    clearTimeout(retryTimer);
    if (retry) retryTimer = setTimeout(() => load(true), retry * 1000);
  } catch (err) {
    failed = String(err.message || err);
    detail = null;
  } finally {
    loading = false;
    render();
  }
}

/* ---------------- render ---------------- */

/* ---------------- distributions ----------------
 * Annual DPS from the desk file (daily yfinance pull), yield-on-cost against
 * the live position. Fetched once per session — dividend history does not
 * move intraday. */
let deskCache = null;
let deskWanted = false;

function loadDesk() {
  if (deskWanted) return;
  deskWanted = true;
  fetch("api/desk").then((r) => (r.ok ? r.json() : null))
    .then((d) => { deskCache = d; renderDistributions(); renderEarningsSoon(); })
    .catch(() => {});
}

function renderDistributions() {
  const card = $("stockDivCard");
  if (!card || !key) return;
  const model = deskCache?.income?.holdings?.[key];
  const note = $("stockDivNote");

  if (!deskCache?.meta?.ready) {
    card.hidden = true;
    return;
  }
  card.hidden = false;

  const annual = model?.annual || [];
  if (!annual.length) {
    note.textContent = "";
    $("stockDivBars").innerHTML = "";
    $("stockDivStats").innerHTML = `
      <p class="sdivs__none">No distributions on record — this line has not
      paid one in the history Yahoo carries.</p>`;
    return;
  }

  const ccy = model.currency || "";
  const years = annual.slice(-10);
  const currentYear = new Date().getFullYear();
  const peak = Math.max(...years.map((y) => y.dps));
  $("stockDivBars").innerHTML = years.map((y, i) => {
    const prev = years[i - 1];
    const cut = prev && y.dps < prev.dps * 0.98 && y.year !== currentYear;
    return `
      <div class="sdivs__col" title="${y.year}: ${y.dps.toFixed(3)} ${esc(ccy)}${
        y.year === currentYear ? " (year to date)" : ""}${cut ? " — cut" : ""}">
        <span class="sdivs__val num">${y.dps >= 10 ? y.dps.toFixed(0) : y.dps.toFixed(2)}</span>
        <i class="sdivs__bar${y.year === currentYear ? " is-partial" : ""}"
           style="height:${Math.max((y.dps / peak) * 100, 3)}%"></i>
        <span class="sdivs__yr">${cut ? "▾ " : ""}${String(y.year).slice(2)}</span>
      </div>`;
  }).join("");

  const stats = [];
  stats.push(["TTM per share", `${model.ttm_dps.toFixed(model.ttm_dps >= 10 ? 1 : 3)} ${ccy}`]);
  if (model.cadence) stats.push(["Cadence", model.cadence]);
  if (model.streak_years > 0) stats.push(["Raised or held", `${model.streak_years}y running`]);
  if (model.dps_cagr5 != null) stats.push(["5y growth", `${(model.dps_cagr5 * 100).toFixed(1)}%/yr`]);
  if (model.yield_on_cost != null) stats.push(["Yield on cost", `${(model.yield_on_cost * 100).toFixed(2)}%`]);
  $("stockDivStats").innerHTML = stats.map(([k, v]) => `
    <div class="sdivs__stat"><span>${k}</span><b class="num">${esc(String(v))}</b></div>`).join("");

  note.textContent = `per share, ${esc(ccy)} · current year is partial`;
}

/** A quiet pill when a report is inside the week — the desk file's date. */
function renderEarningsSoon() {
  const pill = $("stockEarningsSoon");
  if (!pill || !key) return;
  const dates = deskCache?.holdings?.[key]?.next_earnings || [];
  const today = new Date().toISOString().slice(0, 10);
  const next = dates.find((d) => d >= today);
  const days = next ? Math.ceil((new Date(next) - Date.now()) / 86400000) : null;
  if (days != null && days <= 7) {
    pill.hidden = false;
    pill.textContent = days <= 0 ? "Earnings today" : `Earnings in ${days}d`;
  } else {
    pill.hidden = true;
  }
}

function render() {
  const view = $("view-stock");
  if (!view || view.hidden) return;
  loadDesk();
  renderDistributions();
  renderEarningsSoon();

  const t = ticker();
  const d = detail || {};
  const ident = d.instrument || {};
  const name = t?.name || ident.name;
  const currency = ident.display_currency || t?.currency || "";

  /* ---- breadcrumb ---- */
  const sector = t?.sector || ident.sector || DASH;
  const tracked = Boolean(t) || ident.tracked;
  const crumb = $("stockCrumbSector");
  if (tracked) {
    crumb.textContent = sector;
    crumb.href = `#holdings/${encodeURIComponent(sector)}`;
  } else {
    // Found through search but not tracked. Linking to a sector page that does
    // not hold it would be a promise the page cannot keep.
    crumb.textContent = "Not tracked";
    crumb.removeAttribute("href");
  }
  $("stockCrumbKey").textContent = key || DASH;

  /* ---- banner ---- */
  const banner = $("stockBanner");
  const msg = bannerMessage(d);
  banner.hidden = !msg;
  if (msg) $("stockBannerText").textContent = msg;

  /* ---- identity ---- */
  // Painted unconditionally from whichever source has the instrument. Guarding
  // this on `t` left the previous instrument's colours and logo in place when
  // the next one was not in the live universe — opening AAPL then 0700.HK put
  // Apple's mark beside Tencent's name. The endpoint now ships the same tile
  // colours to_dict() does, from the one blend in universe.tile_colours().
  const tile = $("stockTile");
  const skin = t || ident;
  const logo = skin.logo || "";
  $("stockMono").textContent = skin.mono || "";
  tile.style.background = skin.tint || "#EDF0F3";
  $("stockMono").style.color = skin.ink || "#2E2452";

  let img = tile.querySelector("img");
  if (!logo) {
    // No mark for this instrument — drop any left from the last one, so the
    // monogram underneath is what shows.
    if (img) img.remove();
  } else {
    if (!img) {
      img = document.createElement("img");
      img.className = "ptile__img";
      img.alt = "";
      img.onerror = () => img.remove();
      tile.append(img);
    }
    if (img.dataset.src !== logo) { img.dataset.src = logo; img.src = logo; }
  }
  $("stockName").textContent = name || (key ? `Loading ${key}…` : DASH);
  $("stockTicker").textContent = key || DASH;
  $("stockOwned").hidden = !(t?.owned ?? ident.owned);

  // Delayed, not real-time: IBKR is 15-minute delayed on this account and the
  // openbb quotes are too. The topbar already says so; this must not contradict it.
  // `currency` here is the DISPLAY currency, not the registry's quote currency:
  // a London name quotes in GBp but every price on this page has already been
  // divided to pounds, so a sub-line reading "GBp" beside "£15.75" would be
  // telling you the figures are in pence when they are not.
  $("stockSub").textContent = [
    t?.exchange_name || ident.exchange_name,
    "Delayed price",
    currency,
    sector,
  ].filter(Boolean).join(" · ") || DASH;

  // Tracked means it is in the registry (curated or already watched). The
  // button only has work to do for a name that is not.
  const watchBtn = $("stockWatch");
  const isWatched = tracked || (key ? watching.has(key) : false);
  watchBtn.setAttribute("aria-pressed", String(isWatched));
  watchBtn.disabled = watchPending;
  watchBtn.querySelector("span").textContent =
    watchPending ? "Adding…"
    : watchFailed ? "Couldn't add — retry"
    : isWatched ? "Watching"
    : "Watch";

  // The Financials pill. A fund files no company statements, so it reads as
  // unavailable with a reason rather than leading somewhere empty — the same
  // idiom as a chart range the provider has no data for. The kind check is a
  // heuristic, so the financials page's own empty state is the real backstop.
  const pill = $("stockFinancials");
  const kind = String(
    ident.kind || ((t?.sector || "").toLowerCase() === "etfs" ? "ETF" : "")
  ).toUpperCase();
  const why = kind === "ETF" ? "Funds do not file company financial statements" : "";
  if (key && !why) {
    pill.href = `#financials/${encodeURIComponent(key)}`;
    pill.removeAttribute("aria-disabled");
    pill.removeAttribute("title");
    $("stockFinancialsWhy").textContent = "";
  } else {
    pill.removeAttribute("href");
    pill.setAttribute("aria-disabled", "true");
    pill.title = why || "Loading…";
    $("stockFinancialsWhy").textContent = why;
  }

  /* ---- price ---- */
  const live = livePrice();
  const px = d.price || {};
  const last = live?.last ?? px.last;
  const dayPct = live?.dayPct ?? px.change_pct;
  const prev = px.prev_close;
  const dayAbs = Number.isFinite(last) && Number.isFinite(prev) ? last - prev
    : Number.isFinite(px.change) ? px.change : null;

  $("stockPrice").textContent = priceNative(last, currency);
  // The move borrows the price's precision, so the two line up decimally.
  paintFigure($("stockDayAbs"), dayAbs, priceNativeSigned(dayAbs, currency, { ref: last }));
  paintChip($("stockDayChip"), dayPct);
  $("stockAsOf").textContent = live
    ? `Live · delayed 15 min · ${clock(new Date().toISOString())}`
    : px.as_of ? `At close · ${day(px.as_of)}` : DASH;

  const ah = px.after_hours;
  $("stockAh").hidden = !ah;
  if (ah) {
    $("stockAhPx").textContent = priceNative(ah.price, currency);
    paintFigure($("stockAhDelta"), ah.change_pct, pctSigned(ah.change_pct, 2));
    $("stockAhNote").textContent = "After hours · quotes delayed 15 minutes";
  }

  // The "In GBP" tile exists to convert a foreign quote. On a name that already
  // trades in pounds it would just repeat the headline price, so it steps out
  // rather than restating it.
  const nativeGbp = String(currency).toUpperCase() === "GBP";
  const rate = nativeGbp ? 1 : fx[String(t?.currency || ident.currency || "").toUpperCase()];
  const gbpTile = $("stockGbp").closest(".sstat");
  if (gbpTile) gbpTile.hidden = nativeGbp;
  $("stockGbp").textContent = Number.isFinite(last) && Number.isFinite(rate)
    ? money(last * rate, { decimals: 2 }) : DASH;

  const ks = d.key_stats || {};
  $("stockDayRange").textContent = rangeText(ks.day_low, ks.day_high, currency);
  $("stockYearRange").textContent = rangeText(ks.year_low, ks.year_high, currency);

  /* ---- chart ---- */
  renderRanges(d.ranges || {});
  renderChart(d, currency);

  /* ---- statistics ---- */
  renderStats(ks, currency, d.instrument?.currency);

  /* ---- profile + news ---- */
  renderProfile(d.profile || {});
  renderNews(d.news || []);

  /* ---- analyst ---- */
  renderAnalyst(d.analyst || {}, last, currency);
}

function bannerMessage(d) {
  if (failed) return `Couldn't reach the instrument service — ${failed}. Figures below are from the live feed only.`;
  const meta = d.meta;
  if (!meta) return loading ? null : null;
  if (meta.state === "warming") return "Market data service is starting. Statistics and history will appear shortly.";
  if (meta.state === "busy") return "Market data service is busy. Retrying…";
  if (meta.error) return `Market data unavailable — ${meta.error}`;
  if (meta.pending?.length) return `Still loading: ${meta.pending.join(", ")}.`;
  if (meta.stale?.length) return `Showing last known values for ${meta.stale.join(", ")} — a refresh failed.`;
  return null;
}

/** Decimals an axis needs so its ticks stay distinct. */
function axisDigits(info) {
  const values = (info?.series || []).filter(Number.isFinite);
  if (values.length < 2) return 2;
  const span = Math.max(...values) - Math.min(...values);
  return span >= 40 ? 0 : span >= 4 ? 1 : span >= 0.4 ? 2 : 3;
}

function rangeText(lo, hi, currency) {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return DASH;
  return `${priceNative(lo, currency)} – ${priceNative(hi, currency)}`;
}

function renderRanges(ranges) {
  const host = $("stockRanges");
  const html = RANGES.map((r) => {
    const info = ranges[r] || {};
    const available = info.available !== false;
    const on = r === range;
    return `<button class="srange" type="button" data-range="${r}"
      aria-pressed="${on}" ${available ? "" : "disabled"}
      ${available ? "" : `title="No ${r} data from the provider for this instrument"`}
      >${r}</button>`;
  }).join("");
  if (host.dataset.html !== html) { host.innerHTML = html; host.dataset.html = html; }
}

function renderChart(d, currency) {
  const info = (d.ranges || {})[range];
  const svg = $("stockSvg");
  const label = $("stockRangeLabel");
  label.textContent = RANGE_LABEL[range] || range;

  if (!info || !info.available) {
    svg.replaceChildren();
    chartFp = "";
    $("stockRangePct").textContent = DASH;
    $("stockRangePct").style.color = "var(--text-muted)";
    $("stockRangeAbs").textContent = loading ? "Loading…" : DASH;
    $("stockBenchLegend").hidden = true;
    $("stockLegendName").textContent = key || DASH;
    return;
  }

  // Same instrument, range, series and benchmark as the drawing on screen —
  // this is a price/KPI tick, not new chart data. Everything below would
  // write identical values and replay the draw-on animation; skip it.
  const fp = `${key}|${range}|${currency}|${info.series?.length ?? 0}`
    + `|${info.series?.[info.series.length - 1] ?? ""}`
    + `|${Array.isArray(info.benchmark) ? info.benchmark.length : 0}`;
  if (fp === chartFp) return;
  chartFp = fp;

  const dir = direction(info.change_pct);
  paintFigure($("stockRangePct"), info.change_pct, pctSigned(info.change_pct, 2));
  $("stockRangeAbs").textContent =
    priceNativeSigned(info.change, currency, { ref: info.end });

  $("stockLegendName").textContent = key || DASH;
  $("stockLegendSwatch").style.background =
    dir === "up" ? "var(--gain-soft)" : dir === "down" ? "var(--loss-soft)" : "var(--flat)";

  const bench = d.benchmark || {};
  const hasBench = Array.isArray(info.benchmark) && info.benchmark.some(Number.isFinite);
  $("stockBenchLegend").hidden = !hasBench;
  if (hasBench) {
    $("stockBenchName").textContent =
      `${bench.name || "Benchmark"}, rebased${bench.fallback ? " (nearest market)" : ""}`;
  }

  priceChart(svg, {
    series: info.series,
    labels: info.labels,
    benchmark: hasBench ? info.benchmark : null,
    direction: dir,
    // Axis ticks round, per the design ($132, not $132.51) — five gridlines at
    // full precision would compete with the figures in the header. Precision
    // comes from the axis SPAN, not the value: rounding to whole units would
    // print five identical "S$6" labels on an ETF that trades 5.75–5.80.
    formatValue: (v, long = false) =>
      priceNative(v, currency, long ? undefined : { digits: axisDigits(info) }),
    formatLabel: labelFor(range),
    tooltip: $("tip"),
    seriesName: key,
    benchmarkName: bench.name || "Benchmark",
  });
}

function renderStats(ks, currency, nativeCurrency) {
  const rows = [
    ["Previous close", priceNative(ks.previous_close, currency)],
    ["Open", priceNative(ks.open, currency)],
    ["Bid", priceNative(ks.bid, currency)],
    ["Ask", priceNative(ks.ask, currency)],
    ["Volume", count(ks.volume)],
    ["Average volume", count(ks.average_volume)],
    ["Market cap", compact(ks.market_cap, currency)],
    ["Enterprise value", compact(ks.enterprise_value, currency)],
    ["P/E (trailing)", ratio(ks.pe_trailing)],
    ["P/E (forward)", ratio(ks.pe_forward)],
    ["EPS (TTM)", priceNative(ks.eps_ttm, currency, { digits: 2 })],
    ["Beta (5Y monthly)", ratio(ks.beta_5y_monthly)],
    ["Dividend yield", ks.dividend_yield_pct == null ? DASH : `${ks.dividend_yield_pct.toFixed(2)}%`],
    ["Ex-dividend date", day(ks.ex_dividend_date)],
    ["Next earnings", day(ks.next_earnings_date)],
    ["Shares outstanding", compact(ks.shares_outstanding)],
  ];
  $("stockStats").innerHTML = rows.map(([k, v]) => `
    <span class="srow">
      <span class="srow__k">${esc(k)}</span>
      <span class="srow__v num">${esc(v)}</span>
    </span>`).join("");
}

function renderProfile(p) {
  const text = p.description
    || "No company description available from the data provider for this instrument.";
  $("stockProfileText").textContent = text;
  // The paragraph is line-clamped to the design's depth, so the full filing text
  // stays reachable rather than being silently cut.
  if (p.description) $("stockProfileText").title = text; else $("stockProfileText").removeAttribute("title");
  const cells = [
    ["Sector", p.sector], ["Industry", p.industry],
    ["Headquarters", p.headquarters],
    ["Employees", p.employees == null ? null : count(p.employees)],
  ];
  $("stockProfileGrid").innerHTML = cells.map(([k, v]) => `
    <span class="scell">
      <span class="scell__k">${esc(k)}</span>
      <span class="scell__v">${esc(v || DASH)}</span>
    </span>`).join("");
}

function renderNews(items) {
  const host = $("stockNews");
  if (!items.length) {
    host.innerHTML = `<div class="hempty">
      <p class="hempty__title">No headlines</p>
      <p class="hempty__msg">The data provider publishes no news for this instrument.
        ETFs and index funds rarely have any.</p>
    </div>`;
    return;
  }
  host.innerHTML = items.slice(0, 4).map((n) => {
    const meta = [n.source, n.relative].filter(Boolean)
      .map(esc).join('</span><i aria-hidden="true">·</i><span>');
    const inner = `
      <span class="snewsitem__title">${esc(n.title)}</span>
      <span class="snewsitem__meta"><span>${meta || DASH}</span></span>`;
    return n.url
      ? `<a class="snewsitem" href="${esc(n.url)}" target="_blank" rel="noopener noreferrer">${inner}</a>`
      : `<div class="snewsitem">${inner}</div>`;
  }).join("");
}

function renderAnalyst(a, last, currency) {
  const counts = a.ratings;
  const total = counts ? Object.values(counts).reduce((s, n) => s + n, 0) : 0;

  $("stockAnalystMeta").textContent = a.broker_count
    ? `${a.broker_count} brokers covering${a.consensus ? ` · consensus ${a.consensus}` : ""}`
    : "No analyst coverage from the data provider";

  const bars = counts ? [
    ["Buy", counts.strong_buy + counts.buy, "var(--gain-soft)"],
    ["Hold", counts.hold, "var(--hold-soft)"],
    ["Sell", counts.sell + counts.strong_sell, "var(--loss-soft)"],
  ] : [];
  $("stockRatings").innerHTML = bars.length
    ? bars.map(([k, n, color]) => `
      <span class="srating">
        <span class="srating__k">${k}</span>
        <span class="srating__track">
          <span class="srating__fill" style="width:${total ? (n / total) * 100 : 0}%;background:${color}"></span>
        </span>
        <span class="srating__n num">${n}</span>
      </span>`).join("")
    : `<span class="scard__note">${DASH}</span>`;

  const t = a.target || {};
  $("stockTargetMean").textContent = priceNative(t.mean, currency);
  paintFigure($("stockTargetUpside"), t.upside_pct, pctSigned(t.upside_pct, 2));

  const lo = t.low, hi = t.high, mean = t.mean;
  const span = Number.isFinite(lo) && Number.isFinite(hi) && hi > lo ? hi - lo : null;
  const band = $("stockTargetBand");
  const mark = $("stockTargetMark");
  if (span && Number.isFinite(mean)) {
    // A narrow band around the consensus, so the marker's distance from it is
    // legible without implying a precision the estimates do not have.
    const halfWidth = span * 0.08;
    band.style.left = `${Math.max(0, ((mean - halfWidth - lo) / span) * 100)}%`;
    band.style.width = `${Math.min(100, (halfWidth * 2 / span) * 100)}%`;
    band.hidden = false;
  } else {
    band.hidden = true;
  }
  if (span && Number.isFinite(last)) {
    mark.style.left = `${Math.min(100, Math.max(0, ((last - lo) / span) * 100))}%`;
    mark.hidden = false;
  } else {
    mark.hidden = true;
  }
  $("stockTargetLow").textContent = Number.isFinite(lo) ? `${priceNative(lo, currency)} low` : DASH;
  $("stockTargetHigh").textContent = Number.isFinite(hi) ? `${priceNative(hi, currency)} high` : DASH;
  $("stockTargetLast").textContent = Number.isFinite(last)
    ? `Last ${priceNative(last, currency)}` : DASH;

  const e = a.estimates || {};
  const trend = a.rating_trend_90d || [];
  const up = trend.filter((r) => /upgrade|buy|outperform/i.test(r.rating || "")).length;
  const down = trend.filter((r) => /downgrade|sell|underperform/i.test(r.rating || "")).length;
  const cells = [
    ["Revenue, FY est.", e.revenue_fy == null ? DASH : compact(e.revenue_fy, currency)],
    ["EPS, FY est.", e.eps_fy == null ? DASH : priceNative(e.eps_fy, currency)],
    ["Revenue growth", e.revenue_growth_pct == null ? DASH : pctSigned(e.revenue_growth_pct, 1)],
    ["Rating trend, 90d", trend.length ? `${up} up · ${down} down` : DASH],
  ];
  $("stockEstimates").innerHTML = cells.map(([k, v]) => `
    <span class="sestcell">
      <span class="sestcell__k">${esc(k)}</span>
      <span class="sestcell__v num">${esc(v)}</span>
    </span>`).join("");
}

/* ---------------- routing ---------------- */

function fromHash() {
  const m = /^#stock\/(.+)$/.exec(location.hash);
  return m ? decodeURIComponent(m[1]) : null;
}

export function route() {
  const next = fromHash();
  if (!next) return;
  if (next !== key) {
    key = next;
    detail = null;
    failed = null;
    // A fresh instrument starts on the default range unless the user has
    // deliberately chosen one — keeping a 5Y selection across a click into a
    // different stock is more surprising than helpful.
    if (!rangeTouched) range = "1M";
    clearTimeout(retryTimer);
    render();
    load(true);
    return;
  }
  render();
}

/** main.js hands us a way to refresh the watchlist the moment a name is added,
 *  rather than leaving the page stale until the next 20s poll. */
export function onWatchAdded(fn) { onWatched = fn; }

export function init() {
  $("stockRanges").addEventListener("click", (event) => {
    const btn = event.target.closest(".srange");
    if (!btn || btn.disabled) return;
    range = btn.dataset.range;
    rangeTouched = true;
    render();
  });

  // Watch persists the instrument into the tracked universe. It is not an order
  // — this dashboard stays read-only against IBKR — it adds the name to the
  // watchlist overlay the server merges at startup, so it survives a restart
  // and gets quoted from the next poll.
  $("stockWatch").addEventListener("click", async () => {
    if (!key || watchPending) return;
    const t = ticker();
    if (t || detail?.instrument?.tracked) return;   // already tracked, nothing to do

    watchPending = true;
    watchFailed = false;
    render();

    const ident = detail?.instrument || {};
    try {
      const res = await fetch("api/watch", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          // serve.py requires this; a cross-origin form POST cannot set it.
          "X-Requested-With": "portfolio-dashboard",
        },
        body: JSON.stringify({
          key, symbol: ident.symbol || key, name: ident.name || "",
          exchange: ident.exchange || "", currency: ident.currency || "",
        }),
      });
      const payload = await res.json();
      if (!payload.ok) throw new Error(payload.error || `HTTP ${res.status}`);
      // Only mark it locally once the server has actually stored it. Marking
      // optimistically left the button reading "Watching" on the next load for
      // a name that was tracked nowhere.
      watching.add(key);
      try {
        localStorage.setItem("watching", JSON.stringify([...watching]));
      } catch { /* quota or private mode — the server is the real record */ }
      watchFailed = false;
      // The watchlist poll will carry the new name within ~20s; nudge the page
      // so the "Not tracked" breadcrumb resolves without waiting for it.
      onWatched?.();
    } catch (err) {
      watchFailed = true;
      console.warn("watch failed — not tracked:", err);
    } finally {
      watchPending = false;
      render();
    }
  });

  window.addEventListener("hashchange", () => {
    if (!$("view-stock").hidden) route();
  });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!$("view-stock").hidden && detail) renderChart(detail, currentCurrency());
    }, 140);
  });
}

const currentCurrency = () =>
  detail?.instrument?.display_currency || ticker()?.currency || "";

/* ---------------- live feeds ---------------- */

export function updateOwned(portfolio) {
  owned = new Map((portfolio?.positions || []).map((p) => [p.con_id, p]));
  if (portfolio?.fx) fx = portfolio.fx;
  if (!$("view-stock")?.hidden) render();
}

export function updateQuotes(payload) {
  quotes = payload?.quotes || {};
  if (payload?.universe?.tickers) universe = payload.universe;
  if (!$("view-stock")?.hidden) render();
}
