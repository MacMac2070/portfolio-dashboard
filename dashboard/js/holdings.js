/* The sector Holdings page — implements Holdings.dc.html.
 *
 * Two sources meet here and they are not interchangeable:
 *
 *   owned      the live IB Gateway feed — quantity, cost, P&L, weight
 *   watching   openbb quotes — price, day change, 30d range and shape, nothing else
 *
 * Ownership is decided once, in adapter/universe.py, by whether a ticker has a
 * con_id. Nothing here re-decides it per sector, so a watchlist row can never
 * sprout a fabricated £0 P&L and an owned name reads identically wherever it
 * appears.
 *
 * The design ships hardcoded July prices and a fixed NAV. None of that is used —
 * every figure below comes from the live feed.
 */

import {
  money, moneySigned, pctSigned, pct, qty,
  direction, initials, price,
} from "./format.js";
import { sparkline } from "./charts.js";

const $ = (id) => document.getElementById(id);
const DASH = "—";

let universe = null;      // { tickers, sectors } from /api/watchlist
let quotes = {};          // symbol -> openbb quote
let owned = new Map();    // con_id -> live IB position
let navTotal = 0;         // live net liquidation, for portfolio weight
let sector = null;        // selected sector key
let sortKey = "day";      // watch table sort
let sortDir = "asc";
// The set (and order) of rows currently in the DOM. While it is unchanged the
// 3s/20s polls patch figures in place instead of rewriting innerHTML — the
// same discipline ovholdings.js documents: a rebuild on every tick drops text
// selection mid-read, kills hover and restarts every logo request.
let structureKey = "";

const GAIN = "var(--gain-soft)";
const LOSS = "var(--loss-soft)";
const dirClass = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");
const dirColor = (v) => (v > 0 ? GAIN : v < 0 ? LOSS : "var(--text-muted)");

/* ---------------- merge ---------------- */

/** One row per sector member, from whichever source owns it. */
function rowFor(key) {
  const t = universe?.tickers?.[key];
  if (!t) return null;
  const tile = {
    mono: t.mono || initials(t.key), tint: t.tint, edge: t.edge, ink: t.ink,
    logo: t.logo,
  };

  if (t.owned) {
    const live = owned.get(t.con_id);
    if (!live) return { key, owned: true, symbol: t.key, name: t.name, pending: true, ...tile };
    const costLocal = live.cost_gbp;
    return {
      key, owned: true, con_id: t.con_id,
      symbol: t.key, name: t.name, currency: live.currency,
      price: live.price, dayPct: live.day_change_pct,
      quantity: live.quantity, avgCost: live.quantity ? costLocal / live.quantity : null,
      value: live.value_gbp, cost: costLocal,
      pl: live.unrealised_gbp, retPct: live.unrealised_pct,
      dayGbp: live.day_pnl_gbp,
      weight: navTotal ? (live.value_gbp / navTotal) * 100 : null,
      spark: live.spark || [], ...tile,
    };
  }

  const q = quotes[t.symbol];
  if (!q) return { key, owned: false, symbol: t.key, name: t.name, pending: true, ...tile };
  const hist = q.spark || [];
  const lo = hist.length ? Math.min(...hist) : null;
  const hi = hist.length ? Math.max(...hist) : null;
  return {
    key, owned: false, symbol: t.key, name: t.name, currency: q.currency,
    price: q.price, dayPct: q.day_change_pct,
    lo, hi,
    rangePos: lo != null && hi != null && hi > lo
      ? ((q.price - lo) / (hi - lo)) * 100 : 50,
    spark: hist, ...tile,
  };
}

const membersOf = (key) =>
  (universe?.sectors?.find((s) => s.key === key)?.members || []).map(rowFor).filter(Boolean);

/* ---------------- sector pills ---------------- */

function renderSectors() {
  const nav = $("sectorNav");
  if (!universe?.sectors?.length) { nav.innerHTML = ""; nav.dataset.key = ""; return; }

  // Structure (the pills, their order, which one is current) rebuilds only
  // when it changes; the live day % is patched into place every tick, so pill
  // hover and focus survive the poll.
  const skey = `${universe.sectors.map((s) => s.key).join(",")}|${sector}`;
  if (nav.dataset.key !== skey) {
    nav.dataset.key = skey;
    nav.innerHTML = universe.sectors.map((s) => `
      <button type="button" class="sector-pill" data-sector="${s.key}"
              ${s.key === sector ? 'aria-current="page"' : ""}>
        <span>${s.label}</span>
        <span class="sector-pill__count">${s.total_count}</span>
        <span class="sector-pill__rule" aria-hidden="true"></span>
        <!-- direction as a data attribute, not an inline style: an inline
             colour would outrank the selected-pill rule that turns it white -->
        <span class="sector-pill__day" data-dir="flat">${DASH}</span>
      </button>`).join("");

    for (const b of nav.querySelectorAll(".sector-pill")) {
      b.addEventListener("click", () => {
        if (b.dataset.sector === sector) return;
        sector = b.dataset.sector;
        location.hash = `#holdings/${encodeURIComponent(sector)}`;
        render();
      });
    }
  }

  for (const s of universe.sectors) {
    const rows = membersOf(s.key);
    const held = rows.filter((r) => r.owned && !r.pending);
    const value = held.reduce((a, r) => a + (r.value || 0), 0);
    const day = held.reduce((a, r) => a + (r.dayGbp || 0), 0);
    const open = value - day;
    const dayPct = open ? (day / open) * 100 : 0;
    const pill = nav.querySelector(`.sector-pill[data-sector="${CSS.escape(s.key)}"]`);
    const node = pill?.querySelector(".sector-pill__day");
    if (!node) continue;
    setLive(pill.querySelector(".sector-pill__count"), String(s.total_count));
    setLive(node, held.length ? pctSigned(dayPct, 1) : DASH);
    const dir = dirClass(dayPct);
    if (node.dataset.dir !== dir) node.dataset.dir = dir;
  }
}

/* ---------------- hero ---------------- */

function renderHero(rows) {
  const held = rows.filter((r) => r.owned && !r.pending);
  const value = held.reduce((a, r) => a + (r.value || 0), 0);
  const cost = held.reduce((a, r) => a + (r.cost || 0), 0);
  const unrl = held.reduce((a, r) => a + (r.pl || 0), 0);
  const day = held.reduce((a, r) => a + (r.dayGbp || 0), 0);
  const ownedN = rows.filter((r) => r.owned).length;
  const watchN = rows.length - ownedN;

  $("sectorName").textContent = sector || DASH;
  $("sectorMeta").textContent =
    `${rows.length} instruments · ${ownedN} owned · ${watchN} watching`;
  $("sectorValue").textContent = held.length ? money(value) : DASH;

  const unrlPct = cost ? (unrl / cost) * 100 : null;
  $("sectorUnrl").textContent = held.length ? moneySigned(unrl) : DASH;
  $("sectorUnrl").style.color = dirColor(unrl);
  paintChip($("sectorUnrlChip"), unrlPct);

  const open = value - day;
  const dayPct = open ? (day / open) * 100 : null;
  $("sectorDay").textContent = held.length ? moneySigned(day) : DASH;
  $("sectorDay").style.color = dirColor(day);
  paintChip($("sectorDayChip"), dayPct);

  $("sectorWeight").textContent =
    navTotal && held.length ? pct((value / navTotal) * 100, 1) : DASH;

  // Breadth counts every instrument in the sector, held or not — it describes
  // the sector's day, not the portfolio's.
  const moves = rows.map((r) => r.dayPct).filter((v) => Number.isFinite(v));
  const up = moves.filter((m) => m > 0).length;
  const down = moves.filter((m) => m < 0).length;
  const total = moves.length || 1;
  $("breadthUp").style.width = `${(up / total) * 100}%`;
  $("breadthDown").style.width = `${(down / total) * 100}%`;
  $("breadthText").textContent = `${up} up · ${down} down`;

  const avg = moves.length ? moves.reduce((a, m) => a + m, 0) / moves.length : null;
  $("avgMove").textContent = avg == null ? DASH : pctSigned(avg);
  $("avgMove").style.color = dirColor(avg);

  const ranked = rows
    .filter((r) => Number.isFinite(r.dayPct))
    .sort((a, b) => b.dayPct - a.dayPct);
  const best = ranked[0], worst = ranked[ranked.length - 1];
  $("bestTk").textContent = best ? best.symbol : DASH;
  $("bestPct").textContent = best ? pctSigned(best.dayPct) : "";
  $("worstTk").textContent = worst ? worst.symbol : DASH;
  $("worstPct").textContent = worst ? pctSigned(worst.dayPct) : "";
}

function paintChip(node, value) {
  if (!node) return;
  if (!Number.isFinite(value)) { node.className = "dchip"; node.textContent = ""; return; }
  node.className = `dchip dchip--${dirClass(value)}`;
  node.textContent = pctSigned(value);
}

/* ---------------- rows ----------------
 *
 * One renderer for both groups. Held and watched share a single 8-column grid,
 * so every figure lines up across the boundary between them; a watched row
 * simply leaves the owned-only columns empty and spans the 30-day range across
 * that gap instead.
 */

/** The issuer's mark, with the monogram underneath showing if it fails. */
export function tile(r) {
  return `
    <span class="ptile" style="color:${r.ink}">
      <span class="ptile__mono">${r.mono}</span>
      <!-- No loading="lazy": these sit inside a horizontally-scrollable
           container, where Chrome defers them indefinitely and they never
           start loading at all. A sector shows ~11 rows, so there is nothing
           to defer anyway. On failure the img removes itself and the monogram
           underneath becomes the identifier. -->
      ${r.logo ? `<img class="ptile__img" src="${r.logo}" alt=""
           onerror="this.remove()">` : ""}
    </span>`;
}

export function positionRow(r, isLastHeld) {
  // The boundary between held and watched is a slightly stronger hairline —
  // enough to feel the change of register without splitting the table.
  const sep = isLastHeld ? "prow--boundary" : "";

  // Rows are anchors now that there is a stock page to land on. The hover wash
  // was always here; until this it was decoration on something unclickable,
  // which the market-watch note in app.css calls a lie. Now it is an affordance.
  const cls = `prow prow--pos ${r.owned ? "prow--owned" : ""} ${sep}`.trim();
  // data-con-id lets a caller patch this row's figures in place instead of
  // rebuilding it — see ovholdings.js, which renders on the 3s live poll and
  // must not blow away text selection every tick.
  const open = `<a class="${cls}" href="#stock/${encodeURIComponent(r.key)}"`
    + `${r.con_id != null ? ` data-con-id="${r.con_id}"` : ""}`
    + ` aria-label="${r.symbol}, ${r.name}">`;

  if (r.pending) {
    return `${open}
      <span class="pid">${tile(r)}<span class="pid__text">
        <span class="pid__tk">${r.symbol}</span>
        <span class="pid__name">${r.name}</span></span></span>
      <span class="pid__name" style="grid-column:2 / -1">${
        r.owned ? "Waiting for the live feed" : "Quote unavailable"}</span>
    </a>`;
  }

  const plDir = dirColor(r.pl);
  const dayDir = dirColor(r.dayGbp);
  const ownedCells = r.owned ? `
      <span class="pdaypl num" style="grid-column:4;color:${dayDir}">${
        Number.isFinite(r.dayGbp) ? moneySigned(r.dayGbp) : DASH}</span>
      <span class="pstack pstack--wide" style="grid-column:5">
        <span class="pstack__main pstack__main--body num">${qty(r.quantity)}</span>
        <span class="pstack__sub">@ ${r.avgCost != null ? price(r.avgCost) : DASH}</span>
      </span>
      <span class="pstack pstack--wide" style="grid-column:6">
        <span class="pstack__main pstack__main--lg num">${money(r.value)}</span>
        <span class="pstack__sub">${money(r.cost)} cost</span>
      </span>
      <span class="pstack pstack--wide" style="grid-column:7">
        <span class="pstack__main pstack__main--lg num" style="color:${plDir}">${moneySigned(r.pl)}</span>
        <span class="pstack__ret" style="color:${plDir}">${pctSigned(r.retPct)}</span>
      </span>
      <span class="pweight num" style="grid-column:8">${
        r.weight != null ? pct(r.weight, 1) : DASH}</span>`
    : `
      <span class="prange">
        <span class="prange__label">30d range</span>
        <span class="prange__edge num">${r.lo != null ? price(r.lo) : DASH}</span>
        <span class="prange__track">
          <span class="prange__dot" style="left:${r.rangePos}%;background:${dirColor(r.dayPct)}"></span>
        </span>
        <span class="prange__edge num">${r.hi != null ? price(r.hi) : DASH}</span>
      </span>`;

  return `
    ${open}
      <span class="pid">${tile(r)}
        <span class="pid__text">
          <span class="pid__line">
            <span class="pid__tk">${r.symbol}</span>
            ${r.owned ? '<span class="pid__pill">Owned</span>' : ""}
          </span>
          <span class="pid__name">${r.name}</span>
        </span>
      </span>
      <span class="pstack" style="grid-column:2">
        <span class="pstack__main num">${price(r.price)}</span>
        <span class="pstack__ccy">${r.currency || ""}</span>
      </span>
      <span class="pdelta" style="grid-column:3">${chip(r.dayPct)}</span>
      ${ownedCells}
      <span class="pspark" style="grid-column:9"><svg data-spark="${r.key}" aria-hidden="true"></svg></span>
    </a>`;
}

const chip = (v) => Number.isFinite(v)
  ? `<span class="dchip dchip--${dirClass(v)}">${pctSigned(v)}</span>`
  : `<span class="dchip dchip--flat">${DASH}</span>`;

/* ---------------- header + sorting ---------------- */

export const HEADS = [
  { key: "tk", label: "Position", col: "1", justify: "flex-start" },
  { key: "px", label: "Last", col: "2", justify: "flex-end" },
  { key: "day", label: "Today", col: "3", justify: "flex-end" },
  // Today in pounds, beside today in percent. Owned-only: a watched name has
  // no position, so it has no daily P&L — the 30d range spans this column too.
  { key: "daypl", label: "Today P/L", col: "4", justify: "flex-end" },
  { key: "qty", label: "Qty · avg", col: "5", justify: "flex-end" },
  { key: "value", label: "Value · cost", col: "6", justify: "flex-end" },
  { key: "pl", label: "P/L · return", col: "7", justify: "flex-end" },
  { key: "weight", label: "Weight", col: "8", justify: "flex-end" },
  { key: "trend", label: "30d", col: "9", justify: "flex-end" },
];

function renderHead() {
  const head = $("tableHead");
  const html = HEADS.map((h) => {
    const on = h.key === sortKey;
    return `<button type="button" data-sort="${h.key}" data-on="${on}"
              aria-sort="${on ? (sortDir === "asc" ? "ascending" : "descending") : "none"}"
              style="grid-column:${h.col};justify-content:${h.justify}">
        <span>${h.label}</span>
        ${on ? `<span class="caret" style="transform:rotate(${sortDir === "asc" ? 180 : 0}deg)"></span>` : ""}
      </button>`;
  }).join("");
  // Only the sort state changes this markup; skip the rebuild otherwise so a
  // focused header button keeps its focus across the poll.
  if (head.dataset.html === html) return;
  head.dataset.html = html;
  head.innerHTML = html;

  for (const b of $("tableHead").querySelectorAll("button")) {
    b.addEventListener("click", () => {
      const key = b.dataset.sort;
      if (key === sortKey) sortDir = sortDir === "asc" ? "desc" : "asc";
      else { sortKey = key; sortDir = key === "tk" || key === "day" ? "asc" : "desc"; }
      render();
    });
  }
}

/**
 * Sort one group. Held and watched are sorted separately and concatenated, so
 * the held group stays on top whichever column is clicked. A column a watched
 * row has no value for falls back to its day move, which keeps that group's
 * order stable rather than arbitrary.
 */
function sortGroup(rows) {
  const trend = (r) => (r.spark?.length >= 2 ? (r.spark[r.spark.length - 1] / r.spark[0] - 1) * 100 : 0);
  const pick = {
    tk: (r) => r.symbol,
    px: (r) => r.price ?? 0,
    day: (r) => r.dayPct ?? 0,
    trend,
    daypl: (r) => r.dayGbp ?? r.dayPct ?? 0,
    qty: (r) => r.quantity ?? r.dayPct ?? 0,
    value: (r) => r.value ?? r.dayPct ?? 0,
    pl: (r) => r.pl ?? r.dayPct ?? 0,
    weight: (r) => r.weight ?? r.dayPct ?? 0,
  }[sortKey] || ((r) => r.dayPct ?? 0);

  return rows.slice().sort((a, b) => {
    const av = pick(a), bv = pick(b);
    const c = typeof av === "string" ? av.localeCompare(bv) : av - bv;
    return sortDir === "asc" ? c : -c;
  });
}

/* ---------------- render ---------------- */

function render() {
  if (!universe?.sectors?.length) return;
  if (!sector || !universe.sectors.some((s) => s.key === sector)) {
    sector = universe.sectors[0].key;
  }

  const rows = membersOf(sector);
  renderSectors();
  renderHero(rows);

  // One list, held first. Each group is sorted on its own so the held block
  // stays on top whichever column is clicked.
  const held = sortGroup(rows.filter((r) => r.owned));
  const watching = sortGroup(rows.filter((r) => !r.owned));
  const all = [...held, ...watching];

  $("rowsCount").textContent = all.length;
  $("listNote").textContent =
    `${held.length} held, listed first · ${watching.length} watched`;

  renderHead();

  // Rebuild only when the STRUCTURE changes — sector, sort, the row set or a
  // row's shape (pending/owned). A tick that only moves figures patches them.
  const key = `${sector}|${sortKey}|${sortDir}|`
    + all.map((r) => `${r.key}${r.pending ? "!" : ""}${r.owned ? "+" : ""}`).join(",");
  if (key === structureKey && all.length) {
    patchRows(all);
    return;
  }
  structureKey = key;

  $("tableRows").innerHTML = all.length
    ? all.map((r, i) => positionRow(r, r.owned && !all[i + 1]?.owned && i < all.length - 1)).join("")
    : `<div class="hempty"><span class="hempty__title">Nothing in this sector yet</span>
       <span class="hempty__msg">Star an asset to track its price here.</span></div>`;

  const byKey = new Map(all.map((r) => [r.key, r]));
  // Scoped to this table. Overview now renders its own positions table, and a
  // document-wide [data-spark] query would have the two walking each other's
  // SVGs — it fails safe (the keys never match) but it is still wrong.
  for (const svg of $("tableRows").querySelectorAll("[data-spark]")) {
    const r = byKey.get(svg.dataset.spark);
    if (r?.spark?.length) sparkline(svg, r.spark, { direction: direction(r.dayPct) });
  }
}

/** Writes only on change — an unchanged value never touches the DOM, which is
 *  what keeps a text selection alive across a poll. */
function setLive(node, value) {
  if (node && node.textContent !== value) node.textContent = value;
}

/**
 * Update figures without touching structure. Rows are addressed by index —
 * safe because the structure key pins the exact order this DOM was built in.
 * Mirrors ovholdings.js's patch(); sparklines and colours included.
 */
function patchRows(all) {
  const host = $("tableRows");
  all.forEach((r, i) => {
    const row = host.children[i];
    if (!row || r.pending) return;

    setLive(row.querySelector(".pstack__main"), price(r.price));

    const dchip = row.querySelector(".dchip");
    if (dchip) {
      const cls = `dchip dchip--${dirClass(r.dayPct)}`;
      if (dchip.className !== cls) dchip.className = cls;
      setLive(dchip, Number.isFinite(r.dayPct) ? pctSigned(r.dayPct) : DASH);
    }

    if (r.owned) {
      const dayPl = row.querySelector(".pdaypl");
      if (dayPl) {
        setLive(dayPl, Number.isFinite(r.dayGbp) ? moneySigned(r.dayGbp) : DASH);
        const c = dirColor(r.dayGbp);
        if (dayPl.style.color !== c) dayPl.style.color = c;
      }
      const stacks = row.querySelectorAll(".pstack--wide");
      // 0: qty @ avg, 1: value · cost, 2: P/L · return — see positionRow.
      setLive(stacks[0]?.querySelector(".pstack__main"), qty(r.quantity));
      setLive(stacks[0]?.querySelector(".pstack__sub"),
        `@ ${r.avgCost != null ? price(r.avgCost) : DASH}`);
      setLive(stacks[1]?.querySelector(".pstack__main"), money(r.value));
      setLive(stacks[1]?.querySelector(".pstack__sub"), `${money(r.cost)} cost`);
      const plMain = stacks[2]?.querySelector(".pstack__main");
      const plRet = stacks[2]?.querySelector(".pstack__ret");
      const colour = dirColor(r.pl);
      if (plMain) {
        setLive(plMain, moneySigned(r.pl));
        if (plMain.style.color !== colour) plMain.style.color = colour;
      }
      if (plRet) {
        setLive(plRet, pctSigned(r.retPct));
        if (plRet.style.color !== colour) plRet.style.color = colour;
      }
      setLive(row.querySelector(".pweight"), r.weight != null ? pct(r.weight, 1) : DASH);
    } else {
      const dot = row.querySelector(".prange__dot");
      if (dot) {
        const left = `${r.rangePos}%`;
        if (dot.style.left !== left) dot.style.left = left;
        const bg = dirColor(r.dayPct);
        if (dot.style.background !== bg) dot.style.background = bg;
      }
      const edges = row.querySelectorAll(".prange__edge");
      setLive(edges[0], r.lo != null ? price(r.lo) : DASH);
      setLive(edges[1], r.hi != null ? price(r.hi) : DASH);
    }

    const svg = row.querySelector("[data-spark]");
    if (svg && r.spark?.length) {
      // Sparklines redraw only when their series actually changed — the 30d
      // history moves once a day, not per tick.
      const sig = r.spark.join(",");
      if (svg.dataset.sig !== sig) {
        svg.dataset.sig = sig;
        sparkline(svg, r.spark, { direction: direction(r.dayPct) });
      }
    }
  });
}

/**
 * Every position you hold, as fully-merged rows.
 *
 * The join lives here because `rowFor` needs `universe` (logo, tile colours,
 * the ticker key that makes `#stock/<key>` work) and `navTotal` (the weight
 * column), and both are module state fed by two different polls. A caller
 * working from `/api/snapshot`'s `positions` alone would have neither.
 */
export function heldRows() {
  if (!universe?.tickers) return [];
  return Object.values(universe.tickers)
    .filter((t) => t.owned)
    .map((t) => rowFor(t.key))
    .filter(Boolean);
}

/* ---------------- wiring ---------------- */

export function updateOwned(portfolio) {
  owned = new Map((portfolio?.positions || []).map((p) => [p.con_id, p]));
  navTotal = portfolio?.kpis?.net_liquidation || 0;
  if (!$("view-holdings")?.hidden) render();
}

export function updateQuotes(payload) {
  quotes = payload?.quotes || {};
  if (payload?.universe?.sectors?.length) universe = payload.universe;
  if (!$("view-holdings")?.hidden) render();
}

function fromHash() {
  const m = (location.hash || "").match(/^#holdings\/(.+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}

/** Called by main.js when the Holdings tab opens, and on hashchange. */
export function route() {
  const wanted = fromHash();
  if (wanted) sector = wanted;
  render();
}

export function init() {
  window.addEventListener("hashchange", () => {
    if (!$("view-holdings")?.hidden) route();
  });
  const wanted = fromHash();
  if (wanted) sector = wanted;
}
