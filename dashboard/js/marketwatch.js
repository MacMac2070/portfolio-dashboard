/* Market watch — the world board. Implements MarketWatch.dc.html.
 *
 * This page answers a different question from the rest of the dashboard. Overview
 * is your portfolio and Watchlist is your instruments; this one is markets and
 * time — what is trading right now, how its benchmark is moving, and how much of
 * your money sits there. Exposure is the last line on a card, not the first.
 *
 * Two sources meet, and only one of them knows you own anything:
 *
 *   levels     openbb/yfinance daily closes, via /api/markets
 *   exposure   the live IB feed, folded in server-side by region
 *
 * The design ships hardcoded July levels, fixed UTC offsets and a sparkline
 * synthesised from the 1-month return. None of that survives here: every figure
 * is the real series, and sessions come from the IANA database.
 */

import { money, moneySigned, pct, pctSigned, clock, esc } from "./format.js";
import { sparkline } from "./charts.js";

const $ = (id) => document.getElementById(id);
const DASH = "—";
const CLOCK_MS = 1000;

let board = null;         // last /api/markets payload
let sortMode = "region";
let perfMode = "month";
let structureKey = "";    // rebuild the grid only when this changes

const levelFmt = new Intl.NumberFormat("en-GB", {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
});
const level = (v) => (Number.isFinite(v) ? levelFmt.format(v) : DASH);

const dirClass = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");

/* ---------------- sessions ----------------
 *
 * This mirrors markets.session_of by design, not by accident. Both sides read
 * the same IANA zone and the same open/close integers — the server ships them on
 * every card precisely so this can tick between polls without inventing its own
 * schedule. Keep the two in step if either changes.
 */

const WEEKDAY = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
const formatters = new Map();

function localNow(tz) {
  let f = formatters.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-GB", {
      timeZone: tz, hour12: false,
      weekday: "short", hour: "2-digit", minute: "2-digit",
    });
    formatters.set(tz, f);
  }
  const parts = {};
  for (const p of f.formatToParts(new Date())) parts[p.type] = p.value;
  // Some engines render midnight as "24" under hour12:false.
  const hour = Number(parts.hour) % 24;
  return { dow: WEEKDAY[parts.weekday] ?? 1, minutes: hour * 60 + Number(parts.minute) };
}

function sessionOf(m) {
  const { dow, minutes } = localNow(m.tz);
  const weekday = dow >= 1 && dow <= 5;
  const isOpen = weekday && minutes >= m.open_min && minutes < m.close_min;

  let until;
  if (isOpen) {
    until = m.close_min - minutes;
  } else if (weekday && minutes < m.open_min) {
    until = m.open_min - minutes;
  } else {
    // Minutes to the next open, stepping over Saturday and Sunday so a Friday
    // evening counts down to Monday rather than to a session that never runs.
    let days = 1;
    while ([0, 6].includes((dow + days) % 7)) days += 1;
    until = (1440 - minutes) + (days - 1) * 1440 + m.open_min;
  }
  return { isOpen, until, local: hhmm(minutes) };
}

const pad = (n) => String(n).padStart(2, "0");
const hhmm = (m) => `${pad(Math.floor(m / 60))}:${pad(m % 60)}`;

function dur(m) {
  if (m >= 1440) return `${Math.floor(m / 1440)}d ${Math.floor((m % 1440) / 60)}h`;
  if (m >= 60) return `${Math.floor(m / 60)}h ${pad(m % 60)}m`;
  return `${m}m`;
}

/* ---------------- pieces ---------------- */

const chip = (v) => (Number.isFinite(v)
  ? `<span class="dchip dchip--${dirClass(v)}">${pctSigned(v)}</span>`
  : `<span class="dchip dchip--flat">${DASH}</span>`);

const perfOf = (idx) => (perfMode === "year" ? idx.year_pct : idx.month_pct);
const PERF_LABEL = { month: "1M", year: "YTD" };

/**
 * The change across the drawn window — the aria-label speaks it, so the trend
 * a screen reader hears is always the line's own. The drawn colour follows the
 * printed 1M/YTD figure instead (see render): a red line beside "+10.47%" made
 * the card contradict itself, and the printed figure is the card's story.
 */
function sparkChange(series) {
  if (!series || series.length < 2 || !series[0]) return null;
  return (series[series.length - 1] / series[0] - 1) * 100;
}

/** A figure with no sign-independent meaning: coloured *and* signed, never hue alone. */
function figure(v, { cls = "" } = {}) {
  if (!Number.isFinite(v)) return `<span class="mfig ${cls}">${DASH}</span>`;
  return `<span class="mfig mfig--${dirClass(v)} ${cls}">${pctSigned(v)}</span>`;
}

/**
 * A stale close is never printed as if it were today's. The board says when it
 * last traded and drops the day figure entirely — the same contract the rest of
 * the dashboard uses for a disconnected feed.
 */
function benchDelta(b) {
  // No usable day figure — stale close, or a close the provider repeated so
  // "0.00%" would be an artefact, not a reading. Either way: say when.
  if (b.stale || !Number.isFinite(b.day_pct)) {
    return `<span class="dchip dchip--flat" title="Last close ${esc(b.as_of || "unknown")}">
      ${b.as_of ? `as of ${esc(b.as_of.slice(5))}` : b.stale ? "stale" : DASH}</span>`;
  }
  return chip(b.day_pct);
}

function othersRows(m) {
  if (!m.others.length) return "";
  return `<div class="mcard__others">${m.others.map((o) => `
    <div class="mrow">
      <span class="mrow__name">${esc(o.name)}</span>
      <span class="mrow__level num">${level(o.level)}</span>
      <span class="mrow__day num">${o.stale ? DASH : figure(o.day_pct)}</span>
      <span class="mrow__perf num">${figure(perfOf(o))}</span>
    </div>`).join("")}</div>`;
}

/**
 * Three states, not two. Before the IB feed has composed its first set of
 * positions every region reads £0, and printing that as "nothing held" would
 * be a confident lie about markets you are in fact invested in.
 */
function exposureBand(m) {
  if (!m.exposure_known) {
    // Said once, in the board bar — eight cards repeating "waiting for the
    // live feed" turns one condition into a wall of apology.
    return `
      <div class="mexpo">
        <span class="mexpo__label">Your exposure</span>
        <span class="mexpo__figures"><span class="mexpo__value num" data-held="false">${DASH}</span></span>
      </div>`;
  }
  const held = m.exposure_gbp > 0;
  return `
    <div class="mexpo">
      <span class="mexpo__label">Your exposure</span>
      ${held
        ? `<span class="mexpo__track"><span class="mexpo__fill" style="width:${m.exposure_bar.toFixed(1)}%"></span></span>`
        : '<span class="mexpo__none">Tracked only · nothing held</span>'}
      <span class="mexpo__figures">
        <span class="mexpo__value num" data-held="${held}">${held ? money(m.exposure_gbp) : DASH}</span>
        <span class="mexpo__weight num">${
          held && Number.isFinite(m.exposure_pct) ? pct(m.exposure_pct, 1) : ""}</span>
      </span>
    </div>`;
}

function card(m) {
  const s = sessionOf(m);
  const b = m.benchmark;
  const solo = m.others.length === 0;

  // Six of the eight markets carry a benchmark only, so that is the primary
  // card, not an exception: it gives the space the other-index rows would have
  // taken back to the benchmark rather than padding it out.
  return `
    <article class="mcard ${solo ? "mcard--solo" : ""}" data-open="${s.isOpen}" data-code="${m.code}">
      <header class="mcard__head">
        <span class="mcode">${esc(m.code)}</span>
        <span class="mcard__name">${esc(m.name)}</span>
        <span class="msess">
          <i class="livedot" data-on="${s.isOpen}" aria-hidden="true"></i>
          <span class="msess__state">${s.isOpen ? "Open" : "Closed"}</span>
          <span class="msess__time num" data-clock="${m.code}">${s.local}</span>
        </span>
      </header>

      <div class="mbench">
        <div class="mbench__id">
          <span class="mbench__name">${esc(b.name)} · ${esc(m.currency)}</span>
          <span class="mbench__row">
            <span class="mbench__level num">${level(b.level)}</span>
            ${benchDelta(b)}
          </span>
          <span class="mbench__perf">
            <span class="mbench__perfkey">${PERF_LABEL[perfMode]}</span>
            ${figure(perfOf(b))}
          </span>
        </div>
        <span class="mspark">
          <svg data-spark="${m.code}" role="img"
               aria-label="${esc(b.name)}, ${b.spark.length}-day trend: ${
                 Number.isFinite(sparkChange(b.spark))
                   ? pctSigned(sparkChange(b.spark)) : "unavailable"}"></svg>
        </span>
      </div>

      ${othersRows(m)}
      ${exposureBand(m)}
    </article>`;
}

/* ---------------- skeleton / empty / error ---------------- */

const SKELETON = Array.from({ length: 8 }, (_, i) => `
  <article class="mcard mcard--skeleton" style="animation-delay:${i * 40}ms" aria-hidden="true">
    <header class="mcard__head"><span class="sk sk--chip"></span><span class="sk sk--name"></span></header>
    <div class="mbench"><div class="mbench__id">
      <span class="sk sk--label"></span><span class="sk sk--level"></span>
    </div><span class="sk sk--spark"></span></div>
    <div class="mexpo"><span class="sk sk--bar"></span></div>
  </article>`).join("");

const EMPTY = `
  <div class="card empty board-empty">
    <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round">
      <circle cx="24" cy="24" r="17"/><path d="M7 24h34M24 7c4.5 5 6.7 10.7 6.7 17S28.5 41 24 41s-6.7-5.7-6.7-12S19.5 7 24 7Z"/>
    </svg>
    <h2>No index data</h2>
    <p>The market feed is running but hasn't returned a usable series yet. Levels appear
       here on its next cycle — your positions and P/L are unaffected.</p>
  </div>`;

/* ---------------- render ---------------- */

function ordered(markets) {
  const rows = markets.slice();
  if (sortMode === "day") {
    // Nulls last, so a market with no fresh close never outranks one that moved.
    return rows.sort((a, b) => {
      const av = a.benchmark.day_pct, bv = b.benchmark.day_pct;
      if (!Number.isFinite(av)) return 1;
      if (!Number.isFinite(bv)) return -1;
      return bv - av;
    });
  }
  if (sortMode === "expo") return rows.sort((a, b) => (b.exposure_gbp || 0) - (a.exposure_gbp || 0));
  return rows;   // registry order — grouped by region
}

const ORDER_NOTE = {
  region: "grouped by region",
  day: "ordered by today's benchmark move",
  expo: "ordered by what you hold there",
};

function render() {
  const grid = $("boardGrid");
  if (!board) { grid.innerHTML = SKELETON; return; }

  const markets = ordered(board.markets || []);
  $("mktCount").textContent = markets.length || DASH;
  $("orderNote").textContent = ORDER_NOTE[sortMode];
  // The feed-down condition, stated once for the whole board — the cards
  // themselves just show an em-dash (see exposureBand).
  $("expoNote").textContent = markets.length && markets.every((m) => !m.exposure_known)
    ? "· exposure waiting for the live feed" : "";

  if (!markets.length) { grid.innerHTML = EMPTY; structureKey = ""; return; }

  // Rebuilding eight cards a second would restart every sparkline and drop the
  // hover state, so structure is only redrawn when the order or the figures
  // genuinely change. The clock ticks patch text in place instead.
  // Exposure arrives from the IB feed on its own cadence, so it has to be part
  // of the fingerprint — otherwise the band stays on "Waiting for the live
  // feed" until the *index* feed happens to tick.
  const key = [sortMode, perfMode, board.meta?.last_refresh,
               markets.map((m) => `${m.code}:${m.exposure_gbp ?? "?"}`).join(",")].join("|");
  if (key !== structureKey) {
    structureKey = key;
    grid.innerHTML = markets.map(card).join("");

    const byCode = new Map(markets.map((m) => [m.code, m]));
    for (const svg of grid.querySelectorAll("[data-spark]")) {
      const m = byCode.get(svg.dataset.spark);
      const series = m?.benchmark?.spark || [];
      if (series.length >= 2) {
        // Coloured by the figure printed beside it, so one card tells one
        // story; the spark's own change stays in the aria-label. perfMode is
        // part of structureKey, so toggling 1M/YTD recolours the lines.
        const shown = perfOf(m.benchmark);
        sparkline(svg, series, {
          direction: dirClass(Number.isFinite(shown) ? shown : sparkChange(series)),
        });
      }
    }
  }
  tickClocks();
}

/** The one-second job: local times, session badges and the countdown strip. */
function tickClocks() {
  if (!board?.markets?.length) return;

  const openNames = [];
  const strip = [];

  for (const m of board.markets) {
    const s = sessionOf(m);
    if (s.isOpen) openNames.push(m.name);

    const cardEl = $("boardGrid")?.querySelector(`.mcard[data-code="${CSS.escape(m.code)}"]`);
    if (cardEl) {
      if (cardEl.dataset.open !== String(s.isOpen)) cardEl.dataset.open = String(s.isOpen);
      const dot = cardEl.querySelector(".livedot");
      if (dot && dot.dataset.on !== String(s.isOpen)) dot.dataset.on = String(s.isOpen);
      const state = cardEl.querySelector(".msess__state");
      const wanted = s.isOpen ? "Open" : "Closed";
      if (state && state.textContent !== wanted) state.textContent = wanted;
      const time = cardEl.querySelector(".msess__time");
      if (time && time.textContent !== s.local) time.textContent = s.local;
    }

    strip.push(`
      <span class="clockstrip__item" data-open="${s.isOpen}">
        <i class="livedot" data-on="${s.isOpen}" aria-hidden="true"></i>
        <span class="clockstrip__code">${esc(m.code)}</span>
        <span class="clockstrip__time num">${s.local}</span>
        <span class="clockstrip__note">${s.isOpen ? "closes in" : "opens in"} ${dur(s.until)}</span>
      </span>`);
  }

  const stripHtml = strip.join("");
  const stripEl = $("clockStrip");
  if (stripEl && stripEl.dataset.html !== stripHtml) {
    stripEl.dataset.html = stripHtml;
    stripEl.innerHTML = `${stripHtml}
      <span class="clockstrip__delay">Quotes are delayed 15 minutes.</span>`;
  }

  const trading = openNames.length
    ? `${openNames.join(" · ")} trading now`
    : "All markets closed";
  const now = $("tradingNow");
  if (now && now.textContent !== trading) now.textContent = trading;
  const dot = $("tradingDot");
  if (dot && dot.dataset.on !== String(openNames.length > 0)) {
    dot.dataset.on = String(openNames.length > 0);
  }
}

/** The date line, in the reader's own locale rather than any market's. */
function renderDate(meta) {
  const el = $("boardDate");
  const text = new Date().toLocaleDateString("en-GB", {
    weekday: "long", day: "numeric", month: "long",
  });
  if (el.textContent !== text) el.textContent = text;

  // The board carries its own freshness: the IB feed can be perfectly healthy
  // while yfinance is failing, and the topbar indicator speaks only for IB.
  const hero = document.querySelector(".board-hero");
  if (!hero) return;
  const bad = meta && meta.connected === false;
  hero.dataset.stale = String(Boolean(bad));
  hero.title = bad
    ? `Index feed disconnected${meta.last_refresh ? ` — last update ${clock(meta.last_refresh)}` : ""}`
    : "";
}

/* ---------------- wiring ---------------- */

export function update(payload) {
  board = payload;
  renderDate(payload?.meta);
  if (!$("view-market")?.hidden) render();
}

/** Called by main.js when the tab opens, so a deep link renders immediately. */
/* ---------------- desk context ----------------
 * Overnight diff, earnings rail and the news digest — fetched once per view
 * entry (the file behind it changes daily) and rendered beside the session
 * clocks they belong with. */
let deskData = null;
let deskFetched = false;

const REL_DAY = 86400000;
function relTime(iso) {
  const at = new Date(iso).getTime();
  if (!Number.isFinite(at)) return "";
  const d = Date.now() - at;
  if (d < 3600000) return `${Math.max(1, Math.round(d / 60000))}m`;
  if (d < REL_DAY) return `${Math.round(d / 3600000)}h`;
  return `${Math.round(d / REL_DAY)}d`;
}

function loadDesk() {
  if (deskFetched) { renderDesk(); return; }
  deskFetched = true;
  fetch("api/desk").then((r) => (r.ok ? r.json() : null))
    .then((d) => { deskData = d; renderDesk(); })
    .catch(() => renderDesk());
}

function renderOvernight() {
  const host = $("onightBody");
  if (!host) return;
  const on = deskData?.overnight;
  if (!on) {
    host.innerHTML = `<p class="onight__none">No baseline yet — the diff
      appears after tonight's refresh run writes one.</p>`;
    $("onNote").textContent = "";
    return;
  }
  const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "flat");
  const arrow = (a, b) => (b > a ? "▲" : b < a ? "▼" : "·");
  host.innerHTML = `
    <div class="onight__split">
      <span>NAV <b class="num ${cls(on.nav_delta)}">${moneySigned(on.nav_delta)}</b></span>
      <span>market <b class="num ${cls(on.market_delta)}">${moneySigned(on.market_delta)}</b></span>
      <span>flows <b class="num">${on.flows ? moneySigned(on.flows) : "—"}</b></span>
    </div>
    ${on.shifts.slice(0, 5).map((x) => `
      <div class="onight__row">
        <span class="onight__sym">${esc(x.symbol)}</span>
        <span class="onight__w num">${x.w_from}% → ${x.w_to}%
          <i class="${cls(x.w_to - x.w_from)}">${arrow(x.w_from, x.w_to)}</i></span>
        <span class="onight__gbp num ${cls(x.delta_gbp)}">${moneySigned(x.delta_gbp)}</span>
      </div>`).join("")}`;
  $("onNote").textContent = `since ${new Date(on.since).toLocaleDateString("en-GB",
    { weekday: "short", day: "numeric", month: "short" })} close`;
}

function renderEarnings() {
  const host = $("earnBody");
  if (!host) return;
  const holdings = deskData?.holdings || {};
  const today = new Date().toISOString().slice(0, 10);
  const events = Object.entries(holdings)
    .flatMap(([key, h]) => (h.next_earnings || [])
      .filter((d) => d >= today).slice(0, 1)
      .map((d) => ({ key, date: d, est: (h.next_earnings || []).length > 1 })))
    .sort((a, b) => a.date.localeCompare(b.date))
    .slice(0, 5);
  if (!events.length) {
    host.innerHTML = `<p class="onight__none">No dates on the calendar for
      the fifteen names.</p>`;
    $("erNote").textContent = "";
    return;
  }
  const days = (d) => Math.ceil((new Date(d) - Date.now()) / REL_DAY);
  host.innerHTML = events.map((e, i) => `
    <div class="earn__row${i === 0 ? " is-next" : ""}">
      <span class="earn__sym">${esc(e.key)}</span>
      <span class="earn__date">${new Date(e.date).toLocaleDateString("en-GB",
        { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" })}</span>
      <span class="earn__in num">${days(e.date)}d</span>
      <span class="earn__flag">${e.est ? "est." : "sched."}</span>
    </div>`).join("");
  $("erNote").textContent = "reports across the book";
}

function renderDigest() {
  const host = $("digestBody");
  if (!host) return;
  const holdings = deskData?.holdings || {};
  const seen = new Set();
  const items = [];
  for (const [key, h] of Object.entries(holdings)) {
    for (const n of h.news || []) {
      const sig = n.title.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
      if (seen.has(sig)) continue;
      seen.add(sig);
      items.push({ ...n, key });
    }
  }
  items.sort((a, b) => (b.at || "").localeCompare(a.at || ""));
  if (!items.length) {
    host.innerHTML = `<p class="onight__none">Nothing in the desk file yet —
      it fills on the daily run.</p>`;
    $("digestNote").textContent = "";
    return;
  }
  host.innerHTML = items.slice(0, 12).map((n) => `
    <a class="digest__row" href="${esc(n.url || "#")}" target="_blank" rel="noopener">
      <span class="digest__sym">${esc(n.key)}</span>
      <span class="digest__title">${esc(n.title)}</span>
      <span class="digest__meta">${esc(n.publisher || "")} · ${relTime(n.at)}</span>
    </a>`).join("");
  $("digestNote").textContent =
    `deduped across holdings · fetched ${relTime(deskData?.meta?.fetched_at)} ago`;
}

function renderDesk() {
  renderOvernight();
  renderEarnings();
  renderDigest();
}

export function route() {
  render();
  loadDesk();
}

export function init() {
  for (const b of document.querySelectorAll("[data-sort]")) {
    b.addEventListener("click", () => {
      if (sortMode === b.dataset.sort) return;
      sortMode = b.dataset.sort;
      for (const o of document.querySelectorAll("[data-sort]")) {
        o.setAttribute("aria-pressed", String(o === b));
      }
      render();
    });
  }
  for (const b of document.querySelectorAll("[data-perf]")) {
    b.addEventListener("click", () => {
      if (perfMode === b.dataset.perf) return;
      perfMode = b.dataset.perf;
      for (const o of document.querySelectorAll("[data-perf]")) {
        o.setAttribute("aria-pressed", String(o === b));
      }
      render();
    });
  }

  // Sessions flip on the minute; a one-second tick keeps the countdown honest
  // without touching the network. Skipped while the tab is hidden.
  setInterval(() => {
    if (document.visibilityState === "visible" && !$("view-market")?.hidden) tickClocks();
  }, CLOCK_MS);

  render();   // skeletons until the first payload lands
}
