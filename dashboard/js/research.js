/* Research briefing: what the co-pilot found, read only.
 *
 * Reads /api/research (adapter/research.py). Until the agents write
 * data/research_findings.json the server answers with a labelled sample, and
 * the page says so in a banner that stays up for as long as it is showing.
 *
 * State lives in three places, each with one job:
 *   - the hash owns what you are looking at, #research/<id>?urgency=high&...,
 *     so a view is linkable and survives reload and back/forward;
 *   - localStorage owns what you have read and when you last looked. It is
 *     per viewer and the page works without it;
 *   - the payload owns what the co-pilot said. It is re-fetched every minute
 *     while this view is on screen, and never otherwise.
 *
 * Everything from the JSON reaches the DOM through esc() or textContent, and
 * an evidence link becomes an href only after new URL() says http or https.
 * The server checks all of this first; this file checks it again.
 */

import { esc, initials, clock, count, relative, moment, day } from "./format.js";
import { countUp } from "./charts.js";

const API_URL = "api/research";
const WATCH_URL = "api/watchlist";
const POLL_MS = 60_000;
const FETCH_TIMEOUT_MS = 15_000;
const STALE_HOURS = 36;
const READ_DWELL_MS = 1000;
const SEARCH_DEBOUNCE_MS = 120;

const URGENCY = ["high", "medium", "low", "quiet"];
const TIERS = ["held", "watchlist"];
const KINDS = ["filing", "call", "13f", "news", "sector", "archive"];
const CHANGES = ["weaker", "steady", "stronger"];
const STATUSES = ["ok", "partial", "failed"];
const RISK_VIEWS = ["aggressive", "conservative", "neutral"];
const SECTOR_ORDER = ["Semiconductors", "Big tech", "ETFs", "Airlines", "Financials", "Other"];

const URGENCY_LABEL = { high: "High", medium: "Medium", low: "Low", quiet: "Quiet" };
const TIER_LABEL = { held: "Held", watchlist: "Watchlist" };
const KIND_LABEL = { filing: "Filing", call: "Call", "13f": "13F", news: "News",
  sector: "Sector", archive: "Archive" };
const CHANGE_LABEL = { weaker: "Weaker", steady: "Steady", stronger: "Stronger" };
const RISK_LABEL = { aggressive: "Aggressive", conservative: "Conservative", neutral: "Neutral" };
const RUN_STATUS = { ok: ["ok", "Run complete", "Complete"],
  partial: ["warn", "Partial run", "Partial"],
  failed: ["fail", "Run failed", "Failed"] };

const GROUPS = [
  { key: "attention", title: "Needs attention", has: (f) => f.urgency === "high" || f.urgency === "medium" },
  { key: "changed", title: "Changed", has: (f) => f.urgency === "low" },
  { key: "quiet", title: "Quiet", has: (f) => f.urgency === "quiet" },
];

const TRIAGE_COLUMNS = [
  { key: "ticker", label: "Ticker", sortable: true, first: "asc" },
  { key: "signals", label: "Signals", sortable: true, first: "desc" },
  { key: "narrative", label: "Narrative", sortable: true, first: "desc" },
  { key: "escalated", label: "Escalated", sortable: true, first: "desc" },
  { key: "note", label: "Note", sortable: false },
];

const EMPTY_VIEW = Object.freeze({ id: null, urgency: [], tier: "", source: "", sector: "",
  unread: false, q: "" });

const $ = (id) => document.getElementById(id);

const state = {
  payload: null,          // last good payload, sanitised; kept when a refresh fails
  error: null,            // why the last fetch failed, or null
  inflight: null,         // the one request in flight, shared by init() and route()
  polling: false,
  pollGen: 0,
  timer: null,
  view: { ...EMPTY_VIEW },
  focusId: null,          // a row to give focus back to after the next feed render
  focusBack: null,        // the row a closing sheet hands focus back to
  openedFor: null,        // the selection whose group was last opened for it
  scrollToDetail: false,
  tiles: new Map(),       // universe key -> { logo, mono, ink, name, owned }
  tilesAsked: false,
  open: { attention: true, changed: true, quiet: false },
  triageSort: { key: "escalated", dir: "desc" },
  readTimer: null,
  searchTimer: null,
  baseline: null,
  baselineFor: null,
  sheetOpen: false,
  inerted: [],
  wired: false,
};

let lastFeedHtml = "";
let lastDetailHtml = "";

/* ---------------- shape ---------------- */

const str = (value, cap = 2000) => {
  const text = typeof value === "string" ? value
    : typeof value === "number" && Number.isFinite(value) ? String(value) : "";
  return text.trim().slice(0, cap);
};
const oneOf = (value, allowed, fallback = null) => {
  const word = typeof value === "string" ? value.trim().toLowerCase() : "";
  return allowed.includes(word) ? word : fallback;
};
const strs = (value) => (Array.isArray(value) ? value : [])
  .slice(0, 12).map((item) => str(item, 600)).filter(Boolean);
const when = (value) => (typeof value === "string" && !Number.isNaN(Date.parse(value)) ? value : null);
const whole = (value) => (Number.isInteger(value) && value >= 0 ? value : null);
const objects = (value, cap) => (Array.isArray(value) ? value : [])
  .slice(0, cap).filter((item) => item && typeof item === "object" && !Array.isArray(item));

/** An http(s) link, or null. The server already checked; this is the second lock. */
export function safeUrl(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const url = new URL(value.trim());
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

/** The payload reduced to what this page draws. Never throws. */
function sanitise(raw) {
  const doc = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const meta = doc.meta && typeof doc.meta === "object" ? doc.meta : {};
  const ids = new Set();
  const findings = [];
  for (const f of objects(doc.findings, 500)) {
    const id = str(f.id, 120);
    const ticker = str(f.ticker, 120);
    const headline = str(f.headline, 300);
    if (!id || !ticker || !headline || ids.has(id)) continue;
    ids.add(id);
    const views = f.risk_views && typeof f.risk_views === "object" ? f.risk_views : {};
    findings.push({
      id, ticker, headline,
      tier: oneOf(f.tier, TIERS),
      sector: str(f.sector, 120),
      urgency: oneOf(f.urgency, URGENCY, "low"),
      why: str(f.why_it_matters, 1200),
      bull: strs(f.bull),
      bear: strs(f.bear),
      risk: Object.fromEntries(RISK_VIEWS.map((k) => [k, str(views[k], 1200)])),
      evidence: objects(f.evidence, 12)
        .map((e) => ({ label: str(e.label, 120), detail: str(e.detail, 300),
          url: safeUrl(e.url), kind: oneOf(e.kind, KINDS) }))
        .filter((e) => e.label || e.detail),
      pillars: objects(f.pillars_touched, 12)
        .map((p) => ({ pillar: str(p.pillar, 120), change: oneOf(p.change, CHANGES) }))
        .filter((p) => p.pillar),
      firstSeen: when(f.first_seen),
      changed: f.changed_since_last_run === true,
    });
  }
  const triage = objects(doc.watchlist_triage, 200)
    .map((r) => ({ ticker: str(r.ticker, 120), signals: r.signals_flag === true,
      narrative: r.narrative_flag === true, escalated: r.escalated === true,
      note: str(r.note, 300) }))
    .filter((r) => r.ticker);
  const runs = objects(doc.runs, 200)
    .map((r) => ({ runId: str(r.run_id, 120) || null, at: when(r.at),
      status: oneOf(r.status, STATUSES), agents: whole(r.agents_used),
      skipped: whole(r.skipped), failed: whole(r.failed) }))
    .filter((r) => r.runId || r.at);
  const servedFrom = oneOf(meta.served_from, ["live", "sample", "none"], "none");
  return {
    meta: {
      generatedAt: when(meta.generated_at),
      runId: str(meta.run_id, 120) || null,
      status: oneOf(meta.status, STATUSES),
      nextRunAt: when(meta.next_run_at),
      watchedCount: whole(meta.watched_count),
      isSample: meta.is_sample === true || servedFrom === "sample",
      servedFrom,
      error: str(meta.error, 400) || null,
      lastFindingAt: when(meta.last_finding_at),
    },
    findings, triage, runs,
  };
}

/* ---------------- the hash ---------------- */

/** #research[/<id>][?urgency=high,medium&tier=held&source=filing&sector=…&unread=1&q=…] */
export function parseHash(hash = location.hash) {
  const raw = String(hash || "").replace(/^#/, "");
  const cut = raw.indexOf("?");
  const [tab, ...rest] = (cut < 0 ? raw : raw.slice(0, cut)).split("/");
  if (tab !== "research") return null;
  const params = new URLSearchParams(cut < 0 ? "" : raw.slice(cut + 1));
  const list = (key, allowed) => (params.get(key) || "").toLowerCase().split(",")
    .map((word) => word.trim()).filter((word) => allowed.includes(word));
  let id = null;
  if (rest.length) {
    try { id = decodeURIComponent(rest.join("/")) || null; } catch { id = null; }
  }
  const urgency = list("urgency", URGENCY);
  return {
    id,
    urgency: URGENCY.filter((u) => urgency.includes(u)),
    tier: list("tier", TIERS)[0] || "",
    source: list("source", KINDS)[0] || "",
    sector: (params.get("sector") || "").slice(0, 120),
    unread: params.get("unread") === "1",
    q: (params.get("q") || "").slice(0, 80),
  };
}

/** The inverse of parseHash, in one canonical order so equal views compare equal. */
export function buildHash(view) {
  const params = new URLSearchParams();
  if (view.urgency.length) params.set("urgency", URGENCY.filter((u) => view.urgency.includes(u)).join(","));
  if (view.tier) params.set("tier", view.tier);
  if (view.source) params.set("source", view.source);
  if (view.sector) params.set("sector", view.sector);
  if (view.unread) params.set("unread", "1");
  if (view.q) params.set("q", view.q);
  const query = params.toString().replace(/%2C/gi, ",");
  return `#research${view.id ? `/${encodeURIComponent(view.id)}` : ""}${query ? `?${query}` : ""}`;
}

const filtersActive = (v) => Boolean(v.urgency.length || v.tier || v.source || v.sector
  || v.unread || v.q);

/* ---------------- per-viewer memory ---------------- */

// Storage can be missing or throwing (private windows, blocked site data).
// Every read and write goes through these two, and a Map keeps the value for
// this tab so the page behaves the same either way.
const memory = new Map();

function getItem(area, key) {
  try {
    const value = (area === "session" ? sessionStorage : localStorage).getItem(key);
    if (value !== null) return value;
  } catch { /* storage blocked: fall through to memory */ }
  return memory.get(`${area}:${key}`) ?? null;
}

function setItem(area, key, value) {
  memory.set(`${area}:${key}`, value);
  try {
    (area === "session" ? sessionStorage : localStorage).setItem(key, value);
  } catch { /* storage blocked: memory holds it for this tab */ }
}

// Sample reading state never touches the real keys, so the day the agents
// start writing, nothing reads as already seen.
const prefix = () => (state.payload?.meta.servedFrom === "sample"
  ? "pd.research.sample." : "pd.research.");

function readMap() {
  try {
    const map = JSON.parse(getItem("local", `${prefix()}read`) || "{}");
    return map && typeof map === "object" && !Array.isArray(map) ? map : {};
  } catch {
    return {};
  }
}

/** Unread until opened, and unread again when a later run changes it. */
function isUnread(f, map) {
  if (!Object.hasOwn(map, f.id)) return true;
  return f.changed && map[f.id] !== (state.payload?.meta.runId ?? "");
}

function markRead(id, { paint = true } = {}) {
  const p = state.payload;
  if (!p || !p.findings.some((f) => f.id === id)) return;
  const map = readMap();
  const run = p.meta.runId ?? "";
  if (map[id] === run) return;
  const live = new Set(p.findings.map((f) => f.id));
  const next = {};
  for (const [key, value] of Object.entries(map)) if (live.has(key)) next[key] = value;
  next[id] = run;
  setItem("local", `${prefix()}read`, JSON.stringify(next));
  updateBadge();
  if (paint) renderFeed(p);
}

/** When this viewer last looked, frozen once per tab so a reload keeps the count. */
function baseline() {
  const key = prefix();
  if (state.baselineFor === key) return state.baseline;
  let base = getItem("session", `${key}baseline`);
  if (base === null) {
    base = getItem("local", `${key}seen`) || "none";
    setItem("session", `${key}baseline`, base);
  }
  state.baselineFor = key;
  state.baseline = base === "none" ? null : base;
  return state.baseline;
}

/** Remember the newest run this viewer has had on screen. The run's time, not
 *  the clock's, so a run that lands while they are away still counts as new. */
function stampSeen() {
  const at = state.payload?.meta.generatedAt;
  if (!at) return;
  const key = `${prefix()}seen`;
  const seen = getItem("local", key);
  if (!seen || Date.parse(seen) < Date.parse(at)) setItem("local", key, at);
}

function changedSinceVisit(p) {
  baseline();
  if (!state.baseline) {
    return { n: p.findings.filter((f) => f.changed).length, caption: "Since the last run" };
  }
  const base = Date.parse(state.baseline);
  const ran = Date.parse(p.meta.generatedAt || "");
  const n = p.findings.filter((f) => (f.firstSeen && Date.parse(f.firstSeen) > base)
    || (f.changed && ran > base)).length;
  return { n, caption: "Since your last visit" };
}

/* ---------------- fetching ---------------- */

const viewEl = () => $("view-research");
const onScreen = () => Boolean(viewEl() && !viewEl().hidden);
const sheetQuery = window.matchMedia("(max-width: 760px)");
const stackQuery = window.matchMedia("(max-width: 1100px)");
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function load() {
  if (state.inflight) return state.inflight;
  state.inflight = (async () => {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), FETCH_TIMEOUT_MS);
    try {
      const res = await fetch(`${API_URL}?t=${Date.now()}`, { cache: "no-store", signal: abort.signal });
      if (!res.ok) throw new Error(`the server answered ${res.status}`);
      state.payload = sanitise(await res.json());
      state.error = null;
    } catch (error) {
      state.error = error?.name === "AbortError" ? "the request timed out"
        : (error?.message || "the request failed");
      console.warn("research: could not load /api/research", error);
    } finally {
      clearTimeout(timer);
      state.inflight = null;
    }
  })();
  return state.inflight;
}

/** Issuer marks and names from the universe, once. The monogram holds the tile until then. */
async function loadTiles() {
  if (state.tilesAsked) return;
  state.tilesAsked = true;
  try {
    const res = await fetch(WATCH_URL, { cache: "no-store" });
    if (!res.ok) return;
    const payload = await res.json();
    // The same second lock as the findings: a mark is a same-origin path or
    // an http(s) URL, and an ink is a hex colour, before either reaches an
    // attribute.
    const logo = (v) => (typeof v !== "string" ? ""
      : v.startsWith("/") && !v.startsWith("//") ? v : safeUrl(v) || "");
    const ink = (v) => (typeof v === "string" && /^#[0-9a-f]{3,8}$/i.test(v) ? v : "");
    for (const t of Object.values(payload?.universe?.tickers || {})) {
      if (!t || typeof t.key !== "string") continue;
      state.tiles.set(t.key, { logo: logo(t.logo), ink: ink(t.ink),
        mono: typeof t.mono === "string" ? t.mono : "",
        name: typeof t.name === "string" ? t.name : "", owned: t.owned === true });
    }
    if (onScreen()) render();
  } catch {
    state.tilesAsked = false;     // try again on the next visit
  }
}

// Polls only while the view is on screen. A generation number retires a
// cycle the moment it is stopped, so a tick that was mid-request when the
// view hid cannot schedule itself again.
function startPolling() {
  if (state.polling) return;
  state.polling = true;
  const gen = ++state.pollGen;
  const tick = async () => {
    if (gen !== state.pollGen) return;
    if (!onScreen()) { stopPolling(); return; }
    if (!document.hidden) {
      await load();
      if (gen !== state.pollGen) return;
      render();
    }
    state.timer = setTimeout(tick, POLL_MS);
  };
  state.timer = setTimeout(tick, POLL_MS);
}

function stopPolling() {
  state.polling = false;
  state.pollGen += 1;
  clearTimeout(state.timer);
  state.timer = null;
}

/* ---------------- entry points ---------------- */

/** Boot: wire the view and fetch once, so the nav badge is right before anyone opens it. */
export function init() {
  wire();
  load().then(() => {
    updateBadge();
    if (onScreen()) render();
  });
}

/** Called by showTab on every #research… hash, including selection changes. */
export async function route() {
  wire();
  loadTiles();
  state.view = parseHash() || { ...EMPTY_VIEW };
  if (!state.polling) {
    if (!state.payload) render();           // first show: skeletons
    await load();
    if (!onScreen()) return;
    startPolling();
  }
  if (state.view.id) markRead(state.view.id, { paint: false });
  render();
  if (state.scrollToDetail) {
    state.scrollToDetail = false;
    if (stackQuery.matches && !sheetQuery.matches) {
      $("rsDetail")?.scrollIntoView({ block: "start", behavior: reduceMotion.matches ? "auto" : "smooth" });
    }
  }
}

/* ---------------- rendering ---------------- */

function setText(node, text) {
  if (node && node.textContent !== text) node.textContent = text;
}

/** relative() for things that have already happened. A run or finding stamped
 *  a little ahead of this machine's clock reads "just now", never "in 9h". */
function ago(iso) {
  return relative(iso && Date.parse(iso) > Date.now() ? new Date().toISOString() : iso);
}

function render() {
  const root = $("rsRoot");
  if (!root) return;
  const p = state.payload;
  root.dataset.phase = p ? "ready" : state.error ? "error" : "loading";
  measure();
  renderHeader(p);
  renderGlance(p);
  renderSectors(p);
  syncControls();
  renderFeed(p);
  renderDetail(p);
  renderTriage(p);
  renderRuns(p);
  renderSheet();
  updateBadge();
  if (p && onScreen()) stampSeen();
}

function isStale(meta) {
  if (meta.servedFrom !== "live") return false;
  if (!meta.generatedAt) return true;
  return Date.now() - Date.parse(meta.generatedAt) > STALE_HOURS * 3_600_000;
}

function renderHeader(p) {
  const m = p?.meta;
  const waiting = state.error ? "Unavailable" : "Loading";
  setText($("rsLastRun"), !m ? waiting
    : m.generatedAt ? `${clock(m.generatedAt)} · ${ago(m.generatedAt)}` : "No run yet");
  let next = waiting;
  if (m) {
    if (!m.nextRunAt) next = "Not scheduled";
    else if (Date.parse(m.nextRunAt) > Date.now()) next = `${clock(m.nextRunAt)} · ${relative(m.nextRunAt)}`;
    else next = `${clock(m.nextRunAt)} · overdue`;
  }
  setText($("rsNextRun"), next);

  const runChip = $("rsRunChip");
  const status = m && m.servedFrom !== "none" ? RUN_STATUS[m.status] : null;
  runChip.hidden = !status;
  if (status) {
    runChip.dataset.state = status[0];
    setText($("rsRunLabel"), status[1]);
    runChip.title = m.runId ? `Run ${m.runId}` : "";
  }

  let feed;
  if (!p) feed = state.error ? ["fail", "Unreachable", state.error] : ["pending", "Loading", ""];
  else if (state.error) feed = ["fail", "Unreachable", `Showing the last results received. ${state.error}`];
  else if (m.servedFrom === "none") feed = ["pending", "No data yet", "No findings file has been written yet"];
  else if (m.servedFrom === "sample") feed = ["warn", "Sample data", "Served from the sample file, not a real run"];
  else if (isStale(m)) feed = ["warn", "Stale", `No run for over ${STALE_HOURS} hours`];
  else feed = ["ok", "Live", `Written ${moment(m.generatedAt)}`];
  const feedChip = $("rsFeedChip");
  feedChip.dataset.state = feed[0];
  setText($("rsFeedLabel"), feed[1]);
  feedChip.title = feed[2];

  $("rsSample").hidden = !(m && m.isSample);

  let stale = "";
  if (m && m.status === "failed") {
    const lastOk = p.runs.find((r) => r.status === "ok")?.at || (p.findings.length ? m.generatedAt : null);
    stale = lastOk ? `Last run failed. Showing results from ${moment(lastOk)}.`
      : `Last run failed. ${m.error ? `${m.error}. ` : ""}There are no earlier results to show.`;
  } else if (m && isStale(m)) {
    stale = m.generatedAt
      ? `No run for over ${STALE_HOURS} hours. Showing results from ${moment(m.generatedAt)}.`
      : "This run has no time recorded, so its age is unknown.";
  } else if (m && state.error) {
    stale = `Could not refresh the research feed. Showing results from ${moment(m.generatedAt)}.`;
  }
  $("rsStale").hidden = !stale;
  $("rsStale").dataset.state = m && m.status === "failed" ? "fail" : "warn";
  setText($("rsStaleCopy"), stale);
}

function skeletonValue(node) {
  if (!node) return;
  delete node.dataset.value;
  // A skeleton promises data is coming. Once the fetch has failed it is not,
  // so the tile says so instead of shimmering forever.
  const html = state.error
    ? '<span class="rs-na">Not available</span>'
    : '<span class="rs-sk rs-sk--kpi" aria-hidden="true"></span><span class="u-sr">Loading</span>';
  if (node.innerHTML !== html) node.innerHTML = html;
}

function tileValue(node, n) {
  if (!node) return;
  const placeholder = node.querySelector(".rs-sk, .rs-na");
  if (node.dataset.value === String(n) && !placeholder) return;
  if (placeholder) node.textContent = "0";
  countUp(node, n, (v) => count(Math.round(v)));
}

function renderGlance(p) {
  const ids = ["rsKpiAttention", "rsKpiChanged", "rsKpiQuiet"];
  if (!p) {
    for (const id of ids) skeletonValue($(id));
    return;
  }
  const f = p.findings;
  const high = f.filter((x) => x.urgency === "high").length;
  const medium = f.filter((x) => x.urgency === "medium").length;
  const quiet = f.filter((x) => x.urgency === "quiet").length;
  const changed = changedSinceVisit(p);
  tileValue($("rsKpiAttention"), high + medium);
  tileValue($("rsKpiChanged"), changed.n);
  tileValue($("rsKpiQuiet"), quiet);
  setText($("rsKpiAttentionSub"), `${count(high)} high, ${count(medium)} medium`);
  setText($("rsKpiChangedSub"), changed.caption);
  setText($("rsKpiQuietSub"), quiet === 1 ? "1 name with no material change"
    : `${count(quiet)} names with no material change`);
}

function renderSectors(p) {
  const select = $("rsSector");
  if (!select) return;
  const found = new Set((p?.findings || []).map((f) => f.sector).filter(Boolean));
  if (state.view.sector) found.add(state.view.sector);
  const sectors = [...found].sort((a, b) => {
    const ia = SECTOR_ORDER.indexOf(a), ib = SECTOR_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
  const signature = sectors.join("|");
  if (select.dataset.sectors === signature) return;
  select.dataset.sectors = signature;
  select.innerHTML = `<option value="">All sectors</option>${sectors
    .map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("")}`;
}

/** Controls follow the hash, never the other way round, so back/forward restores them. */
function syncControls() {
  const v = state.view;
  for (const btn of document.querySelectorAll("#rsFilters [data-rs-urgency]")) {
    btn.setAttribute("aria-pressed", String(v.urgency.includes(btn.dataset.rsUrgency)));
  }
  for (const btn of document.querySelectorAll("#rsFilters [data-rs-tier]")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.rsTier === v.tier));
  }
  if ($("rsSource").value !== v.source) $("rsSource").value = v.source;
  if ($("rsSector").value !== v.sector) $("rsSector").value = v.sector;
  $("rsUnread").setAttribute("aria-pressed", String(v.unread));
  const search = $("rsSearch");
  if (search.value !== v.q && document.activeElement !== search) search.value = v.q;
  $("rsClear").hidden = !filtersActive(v);
}

function glyph(urgency) {
  return `<i class="rs-glyph" data-urgency="${urgency}" aria-hidden="true"></i>`;
}

function urgencyTag(urgency) {
  return `<span class="rs-urg" data-urgency="${urgency}">${glyph(urgency)}${URGENCY_LABEL[urgency]}<span class="u-sr"> urgency</span></span>`;
}

function tileHtml(key) {
  const t = state.tiles.get(key) || {};
  return `<span class="ptile ptile--sm rs-tile"${t.ink ? ` style="color:${esc(t.ink)}"` : ""} aria-hidden="true">
      <span class="ptile__mono">${esc(t.mono || initials(key))}</span>
      ${t.logo ? `<img class="ptile__img" src="${esc(t.logo)}" alt="" loading="lazy" onerror="this.remove()">` : ""}
    </span>`;
}

function applyFilters(findings, v, map) {
  const q = v.q.trim().toLowerCase();
  return findings.filter((f) => (!v.urgency.length || v.urgency.includes(f.urgency))
    && (!v.tier || f.tier === v.tier)
    && (!v.source || f.evidence.some((e) => e.kind === v.source))
    && (!v.sector || f.sector === v.sector)
    && (!v.unread || isUnread(f, map))
    && (!q || f.ticker.toLowerCase().includes(q) || f.headline.toLowerCase().includes(q)));
}

function row(f, map) {
  const unread = isUnread(f, map);
  const kinds = [...new Set(f.evidence.map((e) => e.kind).filter(Boolean))];
  return `<a class="rs-row" href="${esc(buildHash({ ...state.view, id: f.id }))}" data-id="${esc(f.id)}"${
    f.id === state.view.id ? ' aria-current="true"' : ""}${unread ? ' data-unread="true"' : ""}>
    ${tileHtml(f.ticker)}
    <span class="rs-row__body">
      <span class="rs-row__top">
        ${unread ? '<i class="rs-dot" aria-hidden="true"></i><span class="u-sr">Unread. </span>' : ""}
        <span class="rs-row__ticker">${esc(f.ticker)}</span>
        ${f.tier ? `<span class="rs-tag">${TIER_LABEL[f.tier]}</span>` : ""}
      </span>
      <span class="rs-row__headline">${esc(f.headline)}</span>
      ${f.why ? `<span class="rs-row__why">${esc(f.why)}</span>` : ""}
      ${kinds.length ? `<span class="rs-row__sources">${kinds
    .map((k) => `<span class="rs-src">${KIND_LABEL[k]}</span>`).join("")}</span>` : ""}
    </span>
    <span class="rs-row__meta">
      ${urgencyTag(f.urgency)}
      <span class="rs-row__time num">${esc(f.firstSeen ? ago(f.firstSeen) : "Undated")}</span>
    </span>
  </a>`;
}

const CARET = '<svg class="rs-caret" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg>';

function group(g, rows, map) {
  if (!rows.length) return "";
  return `<details class="rs-group" data-group="${g.key}"${state.open[g.key] ? " open" : ""}>
    <summary class="rs-group__head">
      <span class="rs-group__title">${g.title}</span>
      <span class="rs-group__count num">${count(rows.length)}</span>${CARET}
    </summary>
    <ul class="rs-group__list">${rows.map((f) => `<li>${row(f, map)}</li>`).join("")}</ul>
  </details>`;
}

const SKELETON_ROWS = `<p class="u-sr">Loading findings</p>${Array.from({ length: 5 }, () => `
  <div class="rs-row rs-row--sk" aria-hidden="true">
    <span class="rs-sk rs-sk--tile"></span>
    <span class="rs-row__body"><span class="rs-sk rs-sk--line"></span><span class="rs-sk rs-sk--line rs-sk--short"></span></span>
    <span class="rs-sk rs-sk--tag"></span>
  </div>`).join("")}`;

function stateCard({ title, body = "", action = "", icon = "info", role = "" }) {
  const icons = {
    info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 8v.1"/>',
    alert: '<path d="M12 3.6 21 19.4H3Z"/><path d="M12 10v4M12 16.6v.1"/>',
    empty: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M4 13h4l1.5 2h5L16 13h4"/>',
    filter: '<path d="M4 6h16M7 12h10M10 18h4"/>',
  };
  return `<div class="rs-state"${role ? ` role="${role}"` : ""}>
    <svg class="rs-state__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[icon]}</svg>
    <p class="rs-state__title">${esc(title)}</p>
    ${body ? `<p class="rs-state__body">${esc(body)}</p>` : ""}
    ${action}
  </div>`;
}

function feedHtml(p) {
  if (!p) {
    if (!state.error) return SKELETON_ROWS;
    return stateCard({ title: "Could not reach the research feed", icon: "alert", role: "alert",
      body: `The dashboard server did not answer /api/research (${state.error}). Check that serve.py is running, then try again.`,
      action: '<button class="rs-btn" type="button" data-rs-action="retry">Try again</button>' });
  }
  if (p.meta.servedFrom === "none") {
    return stateCard({ title: "No research yet", icon: "empty",
      body: "The co-pilot writes its findings to data/research_findings.json. The first run will appear here; this page checks for it every minute while it is open." });
  }
  if (!p.findings.length) {
    if (p.meta.status === "failed") {
      return stateCard({ title: "No findings to show", icon: "alert",
        body: "The last run did not produce a readable set of findings. The banner above says why." });
    }
    const last = p.meta.lastFindingAt ? `The last finding was on ${day(p.meta.lastFindingAt)}.`
      : p.meta.generatedAt ? `The last run, ${moment(p.meta.generatedAt)}, found nothing to report.`
      : "The last run found nothing to report.";
    return stateCard({ title: "Nothing new today", icon: "empty", body: last });
  }
  const map = readMap();
  const shown = applyFilters(p.findings, state.view, map);
  if (!shown.length) {
    return stateCard({ title: "No findings match these filters", icon: "filter",
      body: "Clear them to see everything from the latest run.",
      action: '<button class="rs-btn" type="button" data-rs-action="clear">Clear filters</button>' });
  }
  // A deep link into a collapsed group opens it, once per selection, so a
  // group the reader then closes stays closed through the next poll.
  const selected = state.view.id && shown.find((f) => f.id === state.view.id);
  if (selected && state.openedFor !== selected.id) {
    state.open[GROUPS.find((g) => g.has(selected)).key] = true;
    state.openedFor = selected.id;
  }
  return GROUPS.map((g) => group(g, shown.filter(g.has), map)).join("");
}

function renderFeed(p) {
  const host = $("rsFeedBody");
  if (!host) return;
  const active = document.activeElement;
  const focusId = state.focusId
    || (host.contains(active) && active.classList?.contains("rs-row") ? active.dataset.id : null);
  state.focusId = null;

  if (p && p.findings.length && p.meta.servedFrom !== "none") {
    const n = applyFilters(p.findings, state.view, readMap()).length;
    setText($("rsFeedCount"), n === p.findings.length ? count(n) : `${count(n)} of ${count(p.findings.length)}`);
  } else {
    setText($("rsFeedCount"), "");
  }

  const html = feedHtml(p);
  if (html === lastFeedHtml) return;
  host.innerHTML = html;
  lastFeedHtml = html;
  if (focusId) {
    const target = [...host.querySelectorAll(".rs-row")].find((r) => r.dataset.id === focusId);
    target?.focus({ preventScroll: true });
  }
}

/** Selection without a re-render, so arrow keys keep their focus. */
function paintSelection() {
  for (const node of document.querySelectorAll("#rsFeedBody .rs-row")) {
    if (node.dataset.id === state.view.id) node.setAttribute("aria-current", "true");
    else node.removeAttribute("aria-current");
  }
}

function points(list) {
  return list.length
    ? `<ul class="rs-points">${list.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`
    : '<p class="rs-none">No points recorded.</p>';
}

const EXTERNAL = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.5 2.8h3.7v3.7M13.2 2.8 7.6 8.4"/><path d="M11.6 9.6v2.6a1 1 0 0 1-1 1H3.8a1 1 0 0 1-1-1V5.4a1 1 0 0 1 1-1h2.6"/></svg>';
const ARROW_PATH = { weaker: "M8 3v10M4 9l4 4 4-4", steady: "M3 8h10M9 4l4 4-4 4", stronger: "M8 13V3M4 7l4-4 4 4" };

function evidence(list) {
  if (!list.length) return '<p class="rs-none">No evidence recorded.</p>';
  return `<ul class="rs-evidence">${list.map((e) => `<li class="rs-ev">
      <span class="rs-src">${e.kind ? KIND_LABEL[e.kind] : "Source"}</span>
      <span class="rs-ev__text">
        ${e.label ? `<span class="rs-ev__label">${esc(e.label)}</span>` : ""}
        ${e.detail ? `<span class="rs-ev__detail">${esc(e.detail)}</span>` : ""}
      </span>
      ${e.url ? `<a class="rs-ev__link" href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">Open${
    EXTERNAL}<span class="u-sr"> ${esc(e.label || "source")} (opens in a new tab)</span></a>`
    : '<span class="rs-ev__nolink">No link</span>'}
    </li>`).join("")}</ul>`;
}

function pillars(f) {
  if (!f.pillars.length) {
    return `<p class="rs-none">${f.tier === "watchlist"
      ? "Watchlist names carry no thesis pillars." : "No thesis pillars touched."}</p>`;
  }
  return `<ul class="rs-pillars">${f.pillars.map((p) => `<li class="rs-pillar">
      <span class="rs-pillar__name">${esc(p.pillar)}</span>
      ${p.change ? `<span class="rs-change" data-change="${p.change}"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ARROW_PATH[p.change]}"/></svg>${CHANGE_LABEL[p.change]}</span>`
    : '<span class="rs-change">Not stated</span>'}
    </li>`).join("")}</ul>`;
}

const DISCLAIMER = `<p class="rs-disclaimer">
  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6.2"/><path d="M8 7.4v3.6M8 5v.1"/></svg>
  Information only, not advice.</p>`;

function detailHtml(p) {
  if (!p && !state.error) {
    return `<div class="rs-prompt" aria-hidden="true"><span class="rs-sk rs-sk--line"></span><span class="rs-sk rs-sk--line rs-sk--short"></span></div>${DISCLAIMER}`;
  }
  if (!p || !p.findings.length) {
    return `<div class="rs-prompt">
      <h2 class="rs-prompt__title" id="rsDetailTitle" tabindex="-1">Nothing to open yet</h2>
      <p class="rs-prompt__body">When the co-pilot reports a finding, its reasoning and sources appear here.</p>
    </div>${DISCLAIMER}`;
  }
  const f = state.view.id ? p.findings.find((x) => x.id === state.view.id) : null;
  if (state.view.id && !f) {
    return `<div class="rs-prompt">
      <h2 class="rs-prompt__title" id="rsDetailTitle" tabindex="-1">This finding is no longer in the latest run</h2>
      <p class="rs-prompt__body">The co-pilot has run since this link was made. Pick a finding from the feed instead.</p>
      <button class="rs-btn" type="button" data-rs-action="deselect">Back to findings</button>
    </div>${DISCLAIMER}`;
  }
  if (!f) {
    return `<div class="rs-prompt">
      <svg class="rs-prompt__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.5" y="4" width="17" height="16" rx="2.5"/><path d="M9.5 4v16M12.5 9h5M12.5 12.5h5M12.5 16h3"/></svg>
      <h2 class="rs-prompt__title" id="rsDetailTitle" tabindex="-1">Select a finding</h2>
      <p class="rs-prompt__body">Its reasoning, both readings, the three risk views and every source appear here.</p>
      <p class="rs-prompt__keys"><kbd>↑</kbd><kbd>↓</kbd> move through the feed</p>
    </div>${DISCLAIMER}`;
  }
  const t = state.tiles.get(f.ticker);
  const ticker = t
    ? `<a class="rs-detail__ticker" href="#stock/${encodeURIComponent(f.ticker)}">${esc(f.ticker)}<span class="u-sr">, open the stock page</span><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5"/></svg></a>`
    : `<span class="rs-detail__ticker">${esc(f.ticker)}</span>`;
  return `<div class="rs-detail__id">
      ${tileHtml(f.ticker)}
      <div class="rs-detail__who">
        <p class="rs-detail__line">${ticker}${t?.name ? `<span class="rs-detail__name">${esc(t.name)}</span>` : ""}</p>
        <p class="rs-detail__tags">
          ${urgencyTag(f.urgency)}
          ${f.tier ? `<span class="rs-tag">${TIER_LABEL[f.tier]}</span>` : ""}
          ${f.sector ? `<span class="rs-tag">${esc(f.sector)}</span>` : ""}
          ${f.changed ? '<span class="rs-tag rs-tag--changed">Changed this run</span>' : ""}
        </p>
      </div>
    </div>
    <h2 class="rs-detail__headline" id="rsDetailTitle" tabindex="-1">${esc(f.headline)}</h2>
    <p class="rs-detail__seen num">First seen ${esc(moment(f.firstSeen))}</p>
    <section class="rs-block">
      <h3 class="rs-block__title">Why it may matter</h3>
      <p class="rs-prose">${f.why ? esc(f.why) : '<span class="rs-none">No summary recorded.</span>'}</p>
    </section>
    <section class="rs-block rs-readings" aria-label="Bull and bear readings">
      <div class="rs-reading"><h3 class="rs-block__title">Bull reading</h3>${points(f.bull)}</div>
      <div class="rs-reading"><h3 class="rs-block__title">Bear reading</h3>${points(f.bear)}</div>
    </section>
    <section class="rs-block">
      <h3 class="rs-block__title">Three risk views</h3>
      <dl class="rs-views">${RISK_VIEWS.map((k) => `<div class="rs-view"><dt>${RISK_LABEL[k]}</dt><dd>${
    f.risk[k] ? esc(f.risk[k]) : '<span class="rs-none">No view recorded.</span>'}</dd></div>`).join("")}</dl>
    </section>
    <section class="rs-block">
      <h3 class="rs-block__title">Evidence</h3>
      ${evidence(f.evidence)}
    </section>
    <section class="rs-block">
      <h3 class="rs-block__title">Thesis pillars touched</h3>
      ${pillars(f)}
    </section>
    ${DISCLAIMER}`;
}

function renderDetail(p) {
  const host = $("rsDetailBody");
  if (!host) return;
  const html = detailHtml(p);
  if (html === lastDetailHtml) return;
  host.innerHTML = html;
  lastDetailHtml = html;
  host.parentElement.scrollTop = 0;
}

/* ---- narrow screens: the detail becomes a full-screen sheet ---- */

function setOutsideInert(on) {
  if (!on) {
    for (const node of state.inerted) node.inert = false;
    state.inerted = [];
    return;
  }
  if (state.inerted.length) return;
  let node = $("rsDetail");
  while (node && node.parentElement && node !== document.body) {
    for (const sibling of node.parentElement.children) {
      if (sibling !== node && !sibling.inert && sibling.tagName !== "SCRIPT") {
        sibling.inert = true;
        state.inerted.push(sibling);
      }
    }
    node = node.parentElement;
  }
}

function renderSheet() {
  const root = $("rsRoot");
  const detail = $("rsDetail");
  if (!root || !detail) return;
  const sheet = sheetQuery.matches;
  const selected = Boolean(state.view.id && state.payload);
  const open = sheet && selected && onScreen();
  const wasOpen = state.sheetOpen;
  state.sheetOpen = open;
  root.dataset.sheet = open ? "open" : "closed";
  root.dataset.selected = String(selected);
  if (sheet) {
    detail.setAttribute("role", "dialog");
    detail.setAttribute("aria-modal", "true");
  } else {
    detail.removeAttribute("role");
    detail.removeAttribute("aria-modal");
  }
  detail.inert = sheet && !open;
  setOutsideInert(open);
  if (open && !wasOpen) $("rsDetailTitle")?.focus({ preventScroll: true });
  if (!open && wasOpen && state.focusBack) {
    const row = [...document.querySelectorAll("#rsFeedBody .rs-row")]
      .find((r) => r.dataset.id === state.focusBack);
    row?.focus({ preventScroll: true });
    state.focusBack = null;
  }
}

function closeDetail() {
  const id = state.view.id;
  if (!id) return;
  state.focusBack = id;
  clearTimeout(state.readTimer);
  if (history.state?.rsOpened) {
    history.back();                 // hashchange re-routes and re-renders
    return;
  }
  state.view = { ...state.view, id: null };
  history.replaceState(history.state, "", buildHash(state.view));
  render();
}

/* ---- watchlist triage and run history ---- */

function flag(on) {
  const icon = on
    ? '<path d="m3.5 8.4 2.9 2.9 6.1-6.6"/>'
    : '<path d="M4.5 8h7"/>';
  return `<span class="rs-flag" data-on="${on}"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icon}</svg>${on ? "Yes" : "No"}</span>`;
}

function sortTriage(rows) {
  const { key, dir } = state.triageSort;
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const primary = key === "ticker" ? a.ticker.localeCompare(b.ticker) : Number(a[key]) - Number(b[key]);
    return sign * primary || a.ticker.localeCompare(b.ticker);
  });
}

const TALLY_SKELETON = Array.from({ length: 4 }, () =>
  '<div class="rs-tally__item" aria-hidden="true"><span class="rs-sk rs-sk--line rs-sk--short"></span><span class="rs-sk rs-sk--num"></span></div>').join("");

function renderTriage(p) {
  const tally = $("rsTally");
  const host = $("rsTriageBody");
  if (!tally || !host) return;
  if (!p) {
    if (state.error) {
      tally.innerHTML = "";
      host.innerHTML = '<p class="rs-line">Triage is unavailable until the research feed answers.</p>';
    } else if (!tally.querySelector(".rs-sk")) {
      tally.innerHTML = TALLY_SKELETON;
      host.innerHTML = "";
    }
    return;
  }
  const rows = p.triage;
  const scanned = p.meta.watchedCount ?? rows.length;
  tally.innerHTML = [
    ["Names scanned", scanned],
    ["Flagged for a deep look", rows.filter((r) => r.escalated).length],
    ["Signals (filed)", rows.filter((r) => r.signals).length],
    ["Narrative (said)", rows.filter((r) => r.narrative).length],
  ].map(([label, value]) => `<div class="rs-tally__item"><dt>${label}</dt><dd class="num">${count(value)}</dd></div>`).join("");

  if (!rows.length) {
    host.innerHTML = '<p class="rs-line">No watchlist names were triaged in this run.</p>';
    return;
  }
  const { key, dir } = state.triageSort;
  const head = TRIAGE_COLUMNS.map((c) => {
    const sort = c.key === key ? (dir === "asc" ? "ascending" : "descending") : "none";
    return `<th scope="col"${c.sortable ? ` aria-sort="${sort}"` : ""}${c.key === "note" ? ' class="rs-table__note"' : ""}>${
      c.sortable ? `<button class="rs-sort" type="button" data-rs-sort="${c.key}">${c.label}${CARET}</button>` : c.label}</th>`;
  }).join("");
  const body = sortTriage(rows).map((r) => `<tr>
      <th scope="row"><span class="rs-cell-ticker">${tileHtml(r.ticker)}${esc(r.ticker)}</span></th>
      <td>${flag(r.signals)}</td><td>${flag(r.narrative)}</td><td>${flag(r.escalated)}</td>
      <td class="rs-table__note">${r.note ? esc(r.note) : '<span class="rs-none">No note</span>'}</td>
    </tr>`).join("");
  host.innerHTML = `<table class="rs-table rs-table--triage">
      <caption class="u-sr">Watchlist triage. Column headers sort the table.</caption>
      <thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function renderRuns(p) {
  const host = $("rsRunsBody");
  if (!host) return;
  if (!p) {
    host.innerHTML = "";
    setText($("rsRunsCount"), "");
    return;
  }
  const runs = p.runs;
  setText($("rsRunsCount"), runs.length === 1 ? "1 run" : `${count(runs.length)} runs`);
  if (!runs.length) {
    host.innerHTML = '<p class="rs-line">No runs recorded yet.</p>';
    return;
  }
  const n = (v) => (v == null ? '<span class="rs-none">Not recorded</span>' : count(v));
  host.innerHTML = `<table class="rs-table rs-table--runs">
    <caption class="u-sr">Recent co-pilot runs, newest first</caption>
    <thead><tr><th scope="col">Time</th><th scope="col">Run</th><th scope="col">Status</th>
      <th scope="col" class="rs-num">Agents used</th><th scope="col" class="rs-num">Skipped</th>
      <th scope="col" class="rs-num">Failed</th></tr></thead>
    <tbody>${runs.map((r) => `<tr>
      <td class="num">${esc(moment(r.at))}</td>
      <td class="rs-mono">${r.runId ? esc(r.runId) : '<span class="rs-none">Not recorded</span>'}</td>
      <td>${r.status ? `<span class="rs-status" data-status="${r.status}"><i aria-hidden="true"></i>${RUN_STATUS[r.status][2]}</span>`
    : '<span class="rs-none">Unknown</span>'}</td>
      <td class="rs-num num">${n(r.agents)}</td><td class="rs-num num">${n(r.skipped)}</td>
      <td class="rs-num num">${n(r.failed)}</td>
    </tr>`).join("")}</tbody></table>`;
}

/* ---- nav badge ---- */

function updateBadge() {
  const badge = $("rsBadge");
  if (!badge) return;
  const p = state.payload;
  let n = 0;
  // Live data only: a sample count would put a made-up number on every tab,
  // with no banner beside it to say so.
  if (p && p.meta.servedFrom === "live") {
    const map = readMap();
    n = p.findings.filter((f) => f.urgency !== "quiet" && isUnread(f, map)).length;
  }
  badge.hidden = n === 0;
  setText($("rsBadgeCount"), n > 99 ? "99+" : String(n));
}

/* ---- sizing ---- */

// The sticky detail panel caps its height at whatever is scrolling it: the
// view while the shell is viewport-locked, the window once it is released.
function measure() {
  const view = viewEl();
  if (!view || view.hidden) return;
  const locked = getComputedStyle(view).overflowY !== "visible";
  view.style.setProperty("--rs-viewport", `${locked ? view.clientHeight : window.innerHeight}px`);
}

/* ---------------- wiring ---------------- */

// replaceState keeps history.state, so an entry opened from the feed stays
// marked as one through filter changes and arrow-key moves.
function commit() {
  history.replaceState(history.state, "", buildHash(state.view));
  syncControls();
  renderFeed(state.payload);
}

function select(id) {
  state.view = { ...state.view, id };
  history.replaceState(history.state, "", buildHash(state.view));
  clearTimeout(state.readTimer);
  state.readTimer = setTimeout(() => {
    if (state.view.id === id && onScreen()) markRead(id);
  }, READ_DWELL_MS);
  paintSelection();
  renderDetail(state.payload);
}

function onFeedKey(event) {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  if (event.target.closest("input, select, textarea")) return;
  const rows = [...$("rsFeedBody").querySelectorAll("details[open] .rs-row")];
  if (!rows.length) return;
  const at = rows.indexOf(event.target.closest(".rs-row"));
  let next;
  if (event.key === "Home") next = 0;
  else if (event.key === "End") next = rows.length - 1;
  else if (at < 0) next = event.key === "ArrowDown" ? 0 : rows.length - 1;
  else next = Math.max(0, Math.min(rows.length - 1, at + (event.key === "ArrowDown" ? 1 : -1)));
  event.preventDefault();
  const target = rows[next];
  target.focus({ preventScroll: true });
  target.scrollIntoView({ block: "nearest" });
  // On a phone the detail is a sheet; arrows only move focus there, Enter opens.
  if (!sheetQuery.matches) select(target.dataset.id);
}

function wire() {
  if (state.wired || !$("rsRoot")) return;
  state.wired = true;

  const filters = $("rsFilters");
  filters.addEventListener("click", (event) => {
    const urgency = event.target.closest("[data-rs-urgency]");
    if (urgency) {
      const u = urgency.dataset.rsUrgency;
      const on = state.view.urgency.includes(u);
      state.view = { ...state.view,
        urgency: on ? state.view.urgency.filter((x) => x !== u) : [...state.view.urgency, u] };
      commit();
      return;
    }
    const tier = event.target.closest("[data-rs-tier]");
    if (tier) {
      state.view = { ...state.view, tier: tier.dataset.rsTier };
      commit();
      return;
    }
    if (event.target.closest("#rsUnread")) {
      state.view = { ...state.view, unread: !state.view.unread };
      commit();
      return;
    }
    if (event.target.closest("#rsClear")) {
      state.view = { ...EMPTY_VIEW, id: state.view.id };
      commit();
      $("rsSearch").value = "";
    }
  });
  $("rsSource").addEventListener("change", (event) => {
    state.view = { ...state.view, source: event.target.value };
    commit();
  });
  $("rsSector").addEventListener("change", (event) => {
    state.view = { ...state.view, sector: event.target.value };
    commit();
  });
  $("rsSearch").addEventListener("input", (event) => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => {
      state.view = { ...state.view, q: event.target.value.slice(0, 80) };
      commit();
    }, SEARCH_DEBOUNCE_MS);
  });

  const feed = $("rsFeedBody");
  feed.addEventListener("click", (event) => {
    const action = event.target.closest("[data-rs-action]");
    if (action) {
      if (action.dataset.rsAction === "retry") {
        state.error = null;
        render();
        load().then(render);
      } else if (action.dataset.rsAction === "clear") {
        state.view = { ...EMPTY_VIEW, id: state.view.id };
        $("rsSearch").value = "";
        commit();
      }
      return;
    }
    const row = event.target.closest(".rs-row");
    if (!row || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (row.dataset.id === state.view.id) return;   // already open; must not stack history
    // The finding gets a history entry of its own, marked on the entry itself
    // (history.state) so "Back to findings" knows it may step back rather than
    // rewrite. The mark travels with the entry through back and forward, which
    // a module-level flag could not. pushState fires no hashchange, so this
    // routes directly; modified clicks fall through to the plain link above.
    state.focusId = sheetQuery.matches ? null : row.dataset.id;
    state.scrollToDetail = true;
    history.pushState({ rsOpened: true }, "", row.getAttribute("href"));
    route();
  });
  feed.addEventListener("keydown", onFeedKey);
  feed.addEventListener("toggle", (event) => {
    const node = event.target;
    if (node.matches?.(".rs-group")) state.open[node.dataset.group] = node.open;
  }, true);

  $("rsDetail").addEventListener("click", (event) => {
    if (event.target.closest("#rsBack") || event.target.closest('[data-rs-action="deselect"]')) closeDetail();
  });
  // Escape closes an open finding at every width. Outside the sheet it leaves
  // text fields alone, where Escape already means "clear this".
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || event.defaultPrevented || !onScreen() || !state.view.id) return;
    if (!state.sheetOpen && event.target.closest?.("input, select, textarea")) return;
    event.preventDefault();
    closeDetail();
  });

  $("rsTriageBody").addEventListener("click", (event) => {
    const btn = event.target.closest("[data-rs-sort]");
    if (!btn) return;
    const key = btn.dataset.rsSort;
    const col = TRIAGE_COLUMNS.find((c) => c.key === key);
    const same = state.triageSort.key === key;
    state.triageSort = { key, dir: same ? (state.triageSort.dir === "asc" ? "desc" : "asc") : col.first };
    renderTriage(state.payload);
    $("rsTriageBody").querySelector(`[data-rs-sort="${key}"]`)?.focus();
  });

  // showTab only tells the view it is being shown; this is how it learns it
  // was hidden, which is when polling stops.
  new MutationObserver(() => {
    if (!viewEl().hidden) return;
    stopPolling();
    clearTimeout(state.readTimer);
    state.sheetOpen = false;
    setOutsideInert(false);
    $("rsRoot").dataset.sheet = "closed";
  }).observe(viewEl(), { attributes: true, attributeFilter: ["hidden"] });

  sheetQuery.addEventListener("change", () => { if (onScreen()) renderSheet(); });
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(measure, 150);
  });
}
