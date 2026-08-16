/* Topbar instrument search, across three tiers.
 *
 *   1. tracked    the 35 curated names, with their live prices
 *   2. directory  ~16k cached symbols, filtered locally, no prices
 *   3. remote     one Yahoo lookup after the typing pauses, for everything else
 *
 * Tier order is the whole design. The names you track are the ones you meant,
 * so they always come first; a 16k directory must never push INTC below
 * something merely containing those letters.
 *
 * Only tier 1 has prices worth trusting. The directory holds none, and a remote
 * result's price is withheld until its currency is known — Yahoo's search
 * returns a bare number, and BARC.L's is in pence against the 5.09 the tracked
 * row shows. Two prices for one instrument on one screen is the failure this
 * whole file is arranged to avoid.
 */
import { priceNative, pctSigned, esc, clock } from "./format.js";
import * as directory from "./directory.js";

const $ = (id) => document.getElementById(id);
const DASH = "—";
const LIMIT = 8;          // the panel is 296px tall against ~37px rows
const CURATED_MAX = 5;
const DIRECTORY_MAX = 5;
const REMOTE_MAX = 4;
const BLUR_GRACE = 140;
const DEBOUNCE_MS = 350;
const REMOTE_MIN = 2;     // never ask Yahoo about one character
const THIN = 3;           // local hits below this are worth widening

let universe = null;
let quotes = {};
let owned = new Map();
let lastRefresh = null;
let query = "";
let open = false;
let active = -1;
let rows = [];
let blurTimer = null;
let debounceTimer = null;
let remoteSeq = 0;
let remote = { state: "idle", rows: [], q: "", error: null };

/* ---------------- tier 1: the curated universe ---------------- */

function kindOf(t) {
  if (String(t.sector).toLowerCase() === "etfs") return "ETF";
  if (/\bGDR\b/i.test(t.name)) return "GDR";
  return "STK";
}

function curatedRow(t) {
  const base = {
    key: t.key, symbol: t.symbol, name: t.name, kind: kindOf(t),
    venue: t.exchange_name || DASH, currency: t.currency,
    owned: t.owned, tier: "tracked",
  };
  if (t.owned) {
    const p = owned.get(t.con_id);
    return p ? { ...base, price: p.price, dayPct: p.day_change_pct, currency: p.currency } : base;
  }
  const q = quotes[t.symbol];
  return q ? { ...base, price: q.price, dayPct: q.day_change_pct, currency: q.currency } : base;
}

function matchCurated(q) {
  if (!universe?.tickers) return [];
  const all = Object.values(universe.tickers);
  // Matching the quote SYMBOL as well as the key matters more than it looks.
  // Tencent is curated as key "700" quoting "0700.HK"; without this, typing
  // "0700" misses the tracked row entirely, so its symbol never reaches the
  // dedupe set and the directory offers 0700.HK as an untracked name — a
  // position you actually hold, rendered with no Owned badge, no P&L and a
  // Watch button. Same for BARC.L and VUSA.L.
  const hits = all.filter((t) => !q
    || t.key.toLowerCase().indexOf(q) === 0
    || String(t.symbol || "").toLowerCase().indexOf(q) === 0
    || t.name.toLowerCase().indexOf(q) >= 0);
  hits.sort((a, b) => {
    if (a.owned !== b.owned) return a.owned ? -1 : 1;
    if (q) {
      const ap = a.key.toLowerCase().indexOf(q) === 0 ? 0 : 1;
      const bp = b.key.toLowerCase().indexOf(q) === 0 ? 0 : 1;
      if (ap !== bp) return ap - bp;
    }
    return a.key.localeCompare(b.key);
  });
  // Tickers, not rendered rows: prices are attached at render time by
  // curatedRow(), so caching this match cannot freeze a price on screen.
  return hits.slice(0, CURATED_MAX);
}

/* ---------------- merge ---------------- */

function results() {
  const q = query.trim().toLowerCase();
  const { curated: curatedTickers, dir: dirHits } = localFor(q);
  const curated = curatedTickers.map(curatedRow);
  if (!q) return curated.slice(0, LIMIT);

  // Dedupe on SYMBOL, not key: the registry's key is a curated short form
  // ("700") while the directory's is the quote symbol ("0700.HK"), so the same
  // company would otherwise appear twice.
  const seen = new Set(curated.map((r) => r.symbol));

  const dir = dirHits
    .filter((d) => !seen.has(d.symbol))
    .map((d) => {
      seen.add(d.symbol);
      return {
        key: d.symbol, symbol: d.symbol, name: d.name, kind: d.kind,
        venue: d.venue || DASH,
        currency: d.currency, owned: false, tier: "directory",
      };
    });

  const rem = (remote.q === q ? remote.rows : [])
    .filter((r) => !seen.has(r.symbol))
    .slice(0, REMOTE_MAX)
    .map((r) => ({
      key: r.symbol, symbol: r.symbol, name: r.name, kind: r.kind,
      venue: r.exchange_name || r.exchange || DASH,
      currency: r.currency, owned: false, tier: "remote",
      price: r.price_shown ? r.price : null,
      dayPct: r.price_shown ? r.day_change_pct : null,
    }));

  return [...curated, ...dir, ...rem].slice(0, LIMIT);
}

/* ---------------- remote ---------------- */

/* Every tier is derived from the same two scans, computed once per keystroke.
 * Without this, one input event ran matchCurated() and directory.find() three
 * times over — six linear passes of ~16k rows for a value already in hand, and
 * again on every 3s snapshot tick while the panel is open. */
let localCache = { q: null, curated: [], dir: [] };

function localFor(q) {
  if (localCache.q !== q) {
    localCache = { q, curated: matchCurated(q), dir: directory.find(q, DIRECTORY_MAX) };
  }
  return localCache;
}

function invalidateLocal() { localCache = { q: null, curated: [], dir: [] }; }

const localHitCount = (q) => {
  const { curated, dir } = localFor(q);
  return curated.length + dir.length;
};

/* remote.q is always lowercased, because results() and render() compare it
 * against a lowercased query. Storing the raw case here silently dropped every
 * wider-market result for a query containing a capital — i.e. for "TSCO" or
 * "Roche", which is how tickers are actually typed. */
function scheduleRemote() {
  clearTimeout(debounceTimer);
  const q = query.trim().toLowerCase();
  if (q.length < REMOTE_MIN || localHitCount(q) >= THIN) {
    remote = { state: "idle", rows: [], q, error: null };
    return;
  }
  debounceTimer = setTimeout(() => runRemote(q), DEBOUNCE_MS);
}

async function runRemote(q) {
  const seq = ++remoteSeq;
  remote = { state: "loading", rows: remote.q === q ? remote.rows : [], q, error: null };
  if (open) render();
  try {
    const res = await fetch(`api/lookup?q=${encodeURIComponent(q)}`, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    if (seq !== remoteSeq) return;          // a newer keystroke already won
    if (payload?.meta?.error && !(payload.results || []).length) {
      throw new Error(payload.meta.error);
    }
    remote = { state: "ready", rows: payload.results || [], q, error: null };
  } catch (err) {
    if (seq !== remoteSeq) return;
    remote = { state: "failed", rows: [], q, error: String(err.message || err) };
  }
  if (open) render();
}

/* ---------------- render ---------------- */

const SKELETON = Array.from({ length: 4 }, () => `
  <div class="sresult" aria-hidden="true">
    <span class="sk sk--skind"></span>
    <span class="sresult__id"><span class="sk sk--stk"></span><span class="sk sk--sco"></span></span>
    <span class="sresult__figs"><span class="sk sk--spx2"></span></span>
  </div>`).join("");

function footer() {
  const bits = [];
  if (lastRefresh) bits.push(`Prices as of ${clock(lastRefresh)}`);
  const dstate = directory.state();
  if (dstate === "loading") bits.push("directory loading");
  else if (dstate === "ready") bits.push(`${directory.info().count.toLocaleString("en-GB")} symbols`);
  if (!bits.length) return "";
  return `<p class="search__foot">${bits.join(" · ")}</p>`;
}

function render() {
  const panel = $("searchResults");
  const input = $("searchInput");
  $("search").dataset.open = String(open);
  input.setAttribute("aria-expanded", String(open));
  panel.hidden = !open;
  if (!open) { input.removeAttribute("aria-activedescendant"); return; }

  if (!universe?.tickers && directory.state() !== "ready") {
    panel.innerHTML = SKELETON;
    $("searchStatus").textContent = "Loading instruments";
    return;
  }

  rows = results();
  if (active >= rows.length) active = rows.length - 1;

  const q = query.trim();
  const searching = remote.state === "loading"
    || (q.length >= REMOTE_MIN && remote.q !== q && localHitCount(q.toLowerCase()) < THIN);

  if (!rows.length) {
    // While a wider search is still pending, saying "no matches" would be
    // wrong for about a third of a second and then contradict itself.
    panel.innerHTML = searching
      ? `<div class="sresult sresult--note">Searching all markets…</div>`
      : remote.state === "failed"
        ? `<p class="search__empty">No instrument matches “${esc(q)}” in your watchlist.<br>
             Couldn't reach the wider market — ${esc(remote.error || "lookup unavailable")}</p>`
        : `<p class="search__empty">No instrument matches “${esc(q)}”</p>`;
    $("searchStatus").textContent = searching
      ? "Searching wider markets" : `No matches for ${q}`;
    input.removeAttribute("aria-activedescendant");
    return;
  }

  const body = rows.map((r, i) => `
    <a class="sresult" role="option" id="sr-${i}"
       aria-selected="${i === active}" data-active="${i === active}"
       data-key="${esc(r.key)}" href="#stock/${encodeURIComponent(r.key)}">
      <span class="sresult__kind" data-kind="${esc(r.kind)}">${esc(r.kind || DASH)}</span>
      <span class="sresult__id">
        <span class="sresult__tk">${esc(r.key)}</span>
        <span class="sresult__name">${esc(r.name)} · ${esc(r.venue)}</span>
      </span>
      <span class="sresult__figs">
        <span class="sresult__px num">${esc(priceNative(r.price, r.currency))}</span>
        <span class="sresult__day num" style="color:${
          r.dayPct > 0 ? "var(--gain-soft)" : r.dayPct < 0 ? "var(--loss-soft)" : "var(--text-muted)"
        }">${esc(r.dayPct == null ? "" : pctSigned(r.dayPct, 2))}</span>
      </span>
    </a>`).join("");

  // The note and footer sit AFTER the mapped options, so they never enter
  // rows[] and the sr-N indices stay aligned with arrow-key navigation.
  const note = searching
    ? `<div class="sresult sresult--note">Searching all markets…</div>`
    : remote.state === "failed"
      ? `<p class="search__foot search__foot--warn">Couldn't reach the wider market — ${esc(remote.error)}</p>`
      : "";

  panel.innerHTML = body + note + footer();

  if (active >= 0) input.setAttribute("aria-activedescendant", `sr-${active}`);
  else input.removeAttribute("aria-activedescendant");
  $("searchStatus").textContent =
    `${rows.length} instrument${rows.length === 1 ? "" : "s"}` +
    (searching ? ", searching wider markets" : "");
}

/* ---------------- behaviour ---------------- */

function show() { open = true; render(); }

function hide() {
  open = false;
  active = -1;
  clearTimeout(debounceTimer);
  render();
}

function go(index) {
  const r = rows[index];
  if (!r) return;
  location.hash = `#stock/${encodeURIComponent(r.key)}`;
  $("searchInput").blur();
  query = "";
  $("searchInput").value = "";
  remote = { state: "idle", rows: [], q: "", error: null };
  hide();
}

export function init() {
  const input = $("searchInput");
  const panel = $("searchResults");

  const mac = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent);
  $("searchKbd").textContent = mac ? "⌘K" : "Ctrl K";

  input.addEventListener("input", () => {
    query = input.value;
    active = -1;
    scheduleRemote();
    show();
  });
  input.addEventListener("focus", show);
  input.addEventListener("blur", () => {
    clearTimeout(blurTimer);
    blurTimer = setTimeout(hide, BLUR_GRACE);
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { hide(); input.blur(); return; }
    if (!open && (event.key === "ArrowDown" || event.key === "ArrowUp")) { show(); return; }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      active = rows.length ? (active + 1) % rows.length : -1;
      render();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      active = rows.length ? (active - 1 + rows.length) % rows.length : -1;
      render();
    } else if (event.key === "Enter") {
      if (active >= 0) { event.preventDefault(); go(active); }
      else if (rows.length === 1) { event.preventDefault(); go(0); }
    }
  });

  panel.addEventListener("mousedown", (event) => {
    if (event.target.closest(".sresult:not(.sresult--note)")) event.preventDefault();
  });
  panel.addEventListener("click", (event) => {
    const hit = event.target.closest(".sresult:not(.sresult--note)");
    if (!hit) return;
    event.preventDefault();
    go(rows.findIndex((r) => r.key === hit.dataset.key));
  });

  window.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();          // otherwise Chrome opens its own search
      input.focus();
      input.select();
      show();
    }
  });

  // Fire and forget: the curated names are searchable immediately, and the
  // directory folds in whenever it lands.
  directory.load().then(() => { invalidateLocal(); if (open) render(); });

  render();
}

/* ---------------- live feeds ---------------- */

export function updateOwned(portfolio) {
  owned = new Map((portfolio?.positions || []).map((p) => [p.con_id, p]));
  if (open) render();
}

export function updateQuotes(payload) {
  quotes = payload?.quotes || {};
  // The ticker set itself can change under us — Watch adds one — so the cached
  // match is only valid for a given universe.
  if (payload?.universe?.tickers) { universe = payload.universe; invalidateLocal(); }
  lastRefresh = payload?.meta?.last_refresh || lastRefresh;
  if (open) render();
}
