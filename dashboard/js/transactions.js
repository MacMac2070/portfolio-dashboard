/* The Transactions tab: every execution the Flex statement carries.
 *
 * Reads data/transactions.jsonl, which adapter/store.merge_transactions writes
 * from adapter/flex.parse_trades. Static file, same as nav_history.jsonl — no
 * endpoint, because there is no derivation to do server-side and the file is
 * small enough that the browser can sort it.
 *
 * The store is merge-by-key on exec_id, so the file is append-only and a
 * re-run of the backfill never duplicates a fill.
 *
 * Reading it is a filtered act, though: the FX sweeps IBKR books around real
 * orders outnumber the trades roughly two to one and drowned them, so sweeps
 * are hidden by default behind a counted toggle. The side pills and the text
 * filter narrow the view the same way — the record itself is never touched.
 */
import { price, qty, day, esc, count, initials } from "./format.js";

const TX_URL = "data/transactions.jsonl";
const WATCHLIST_URL = "/api/watchlist";

const $ = (id) => document.getElementById(id);

let rows = [];
let loaded = false;
/** symbol -> {mono,tint,ink,logo}, holdings' plate data by ticker key. */
let tiles = {};
/** Which column is sorted, and which way. Date-descending by default: the
 *  question a trade log answers first is "what did I do most recently". */
let sort = { key: "time", dir: -1 };
let showSweeps = false;
let side = "all";        // all | buy | sell
let query = "";

/**
 * BOT/SLD are IBKR's own codes and mean nothing to a reader, so they are
 * mapped rather than printed. Direction is carried by the word, not only by
 * the colour of the pill — the same rule the P&L convention follows.
 */
const SIDE = {
  BOT: { label: "Buy", cls: "is-buy" },
  BUY: { label: "Buy", cls: "is-buy" },
  SLD: { label: "Sell", cls: "is-sell" },
  SELL: { label: "Sell", cls: "is-sell" },
};
const sideOf = (raw) =>
  SIDE[String(raw || "").toUpperCase()] || { label: esc(raw || "—"), cls: "is-flat" };

const HEADS = [
  { key: "time", label: "Date", justify: "flex-start" },
  { key: "symbol", label: "Instrument", justify: "flex-start" },
  { key: "side", label: "Side", justify: "flex-start" },
  { key: "quantity", label: "Quantity", justify: "flex-end" },
  { key: "price", label: "Price", justify: "flex-end" },
  { key: "value", label: "Consideration", justify: "flex-end" },
  { key: "commission", label: "Commission", justify: "flex-end" },
];

/**
 * A cash amount, always two decimals.
 *
 * Not price(), whose precision follows magnitude so a penny share keeps four
 * places — right for a quote, wrong for a fee. A £2.41 commission came out as
 * "2.410", which reads as a quantity rather than as money.
 */
const cash = (v) => new Intl.NumberFormat("en-GB", {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
}).format(v);

/** Consideration is quantity x price in the trade's own currency, not GBP.
 *  Flex gives no FX rate per execution, and converting at today's spot would
 *  restate a year-old trade at a rate that did not apply to it. */
const considerationOf = (r) => (r.quantity || 0) * (r.price || 0);

/** Currency conversions the broker does around real orders — bookkeeping, not
 *  trading. Hidden by default behind the counted toggle; shown, they stay
 *  muted so executions still carry the page. */
const isSweep = (r) => (r.exchange || "") === "IDEALFX";

async function load() {
  try {
    const res = await fetch(`${TX_URL}?t=${Date.now()}`);
    if (!res.ok) return [];                 // 404 until the backfill writes it
    const text = await res.text();
    return text.split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => { try { return JSON.parse(line); } catch { return null; } })
      .filter((r) => r && r.time && r.symbol);
  } catch {
    return [];
  }
}

/** The same plate data the Holdings table renders from, keyed by ticker. On
 *  any failure the plates simply fall back to monograms. */
async function loadTiles() {
  try {
    const res = await fetch(WATCHLIST_URL);
    if (!res.ok) return;
    const payload = await res.json();
    for (const t of Object.values(payload?.universe?.tickers || {})) {
      tiles[t.key] = {
        mono: t.mono || initials(t.key),
        tint: t.tint, ink: t.ink, logo: t.logo,
      };
    }
  } catch { /* monogram fallback */ }
}

/** The issuer's mark at row scale — the Holdings plate idiom, one size down. */
function plate(symbol) {
  const t = tiles[symbol] || { mono: initials(symbol) };
  return `
    <span class="ptile ptile--sm"${t.ink ? ` style="color:${esc(t.ink)}"` : ""}>
      <span class="ptile__mono">${esc(t.mono)}</span>
      ${t.logo ? `<img class="ptile__img" src="${esc(t.logo)}" alt=""
           onerror="this.remove()">` : ""}
    </span>`;
}

function filtered() {
  const q = query.trim().toUpperCase();
  return rows.filter((r) => {
    if (!showSweeps && isSweep(r)) return false;
    if (side !== "all" && sideOf(r.side).label.toLowerCase() !== side) return false;
    if (q && !`${r.symbol} ${r.exchange || ""}`.toUpperCase().includes(q)) return false;
    return true;
  });
}

function sorted(list) {
  const key = sort.key;
  const value = (r) => {
    if (key === "value") return considerationOf(r);
    if (key === "commission") return Math.abs(r.commission ?? 0);
    if (key === "side") return sideOf(r.side).label;
    return r[key];
  };
  return [...list].sort((a, b) => {
    const av = value(a), bv = value(b);
    if (typeof av === "number" && typeof bv === "number") return (av - bv) * sort.dir;
    return String(av).localeCompare(String(bv)) * sort.dir;
  });
}

function head() {
  return HEADS.map((h) => {
    const on = sort.key === h.key;
    const arrow = on ? (sort.dir === 1 ? "↑" : "↓") : "";
    return `
      <button class="txhead__cell" type="button" data-sort="${h.key}"
              style="justify-content:${h.justify}"
              aria-sort="${on ? (sort.dir === 1 ? "ascending" : "descending") : "none"}">
        ${esc(h.label)}<span class="txhead__arrow" aria-hidden="true">${arrow}</span>
      </button>`;
  }).join("");
}

function row(r) {
  const side = sideOf(r.side);
  const fee = Number.isFinite(r.commission) ? Math.abs(r.commission) : null;
  return `
    <div class="txrow${isSweep(r) ? " txrow--sweep" : ""}" role="row">
      <span class="txrow__date num">${esc(day(r.time))}</span>
      <span class="txrow__sym">${plate(r.symbol)}<span class="txrow__symtext">
        ${esc(r.symbol)}
        <span class="txrow__venue">${esc(r.exchange || "")}</span></span>
      </span>
      <span class="txrow__side"><span class="txpill ${side.cls}">${side.label}</span></span>
      <span class="txrow__n num">${qty(r.quantity)}</span>
      <span class="txrow__n num">${price(r.price)}</span>
      <span class="txrow__n num">${cash(considerationOf(r))}
        <span class="txrow__ccy">${esc(r.currency || "")}</span></span>
      <span class="txrow__n num txrow__fee">${fee === null ? "—" : cash(fee)}</span>
    </div>`;
}

/** Per-currency totals over the trades on screen. Sweeps never count — moving
 *  cash between currencies is not buying anything — and staying in the trade
 *  currency follows the consideration column's own rule. */
function foot(list) {
  const per = new Map();
  for (const r of list) {
    if (isSweep(r)) continue;
    const ccy = r.currency || "—";
    const t = per.get(ccy) || { bought: 0, sold: 0, fees: 0 };
    if (sideOf(r.side).label === "Sell") t.sold += considerationOf(r);
    else t.bought += considerationOf(r);
    if (Number.isFinite(r.commission)) t.fees += Math.abs(r.commission);
    per.set(ccy, t);
  }
  if (!per.size) return "";
  const groups = [...per.entries()]
    .sort((a, b) => (b[1].bought + b[1].sold) - (a[1].bought + a[1].sold))
    .map(([ccy, t]) => `
      <span class="txfoot__group"><b>${esc(ccy)}</b>
        bought <span class="num">${cash(t.bought)}</span>
        · sold <span class="num">${cash(t.sold)}</span>
        · fees <span class="num">${cash(t.fees)}</span></span>`);
  return `<span class="txfoot__note">shown trades</span>${groups.join("")}`;
}

function render() {
  const host = $("txBody");
  const headHost = $("txHead");
  if (!host) return;

  if (!rows.length) {
    headHost.innerHTML = "";
    $("txMeta").textContent = "";
    $("txControls")?.setAttribute("hidden", "");
    const footHost = $("txFoot");
    if (footHost) footHost.innerHTML = "";
    host.innerHTML = `
      <div class="collecting">
        <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.4"
             aria-hidden="true">
          <rect x="6" y="9" width="36" height="30" rx="4"/><path d="M6 18h36M14 26h12M14 32h8"/>
        </svg>
        <h3>No transactions yet</h3>
        <p><span class="setup">query    nav_query_id
section  <b>Trades</b>   ← enable, or point trades_query_id at a
         Trade Confirmation query
then     /opt/anaconda3/bin/python3 adapter/backfill.py</span></p>
      </div>`;
    return;
  }

  $("txControls")?.removeAttribute("hidden");
  const list = sorted(filtered());
  headHost.innerHTML = head();
  host.innerHTML = list.length ? list.map(row).join("") : `
    <div class="txnone">No trades match — clear the side or symbol filter.</div>`;
  const footHost = $("txFoot");
  if (footHost) footHost.innerHTML = foot(list);

  // The span is the record's own, not the selected filter: this is a log, and
  // saying how far back it reaches is what stops a short file reading as a
  // complete history. A narrowed view says so with a shown-count up front.
  const dates = rows.map((r) => r.time).sort();
  const sweeps = rows.filter(isSweep).length;
  const trades = rows.length - sweeps;
  const narrowed = side !== "all" || query.trim() !== "" || showSweeps;
  $("txMeta").textContent =
    `${narrowed ? `${count(list.length)} shown · ` : ""}`
    + `${count(trades)} trades · ${count(sweeps)} FX sweeps · `
    + `${day(dates[0])} – ${day(dates.at(-1))}`;
  const sweepBtn = $("txSweeps");
  if (sweepBtn) sweepBtn.textContent = `FX sweeps · ${count(sweeps)}`;
}

export function init() {
  const headHost = $("txHead");
  if (!headHost) return;
  // Delegated: the header is rewritten on every sort, which would discard
  // handlers bound to its buttons.
  headHost.addEventListener("click", (event) => {
    const button = event.target.closest("[data-sort]");
    if (!button) return;
    const key = button.dataset.sort;
    // Re-clicking a column flips it; a new column starts descending, which is
    // the useful direction for every column here — newest, largest, dearest.
    sort = sort.key === key ? { key, dir: -sort.dir } : { key, dir: -1 };
    render();
  });

  for (const button of document.querySelectorAll("#txControls [data-side]")) {
    button.addEventListener("click", () => {
      side = button.dataset.side;
      for (const other of document.querySelectorAll("#txControls [data-side]")) {
        other.setAttribute("aria-pressed", String(other === button));
      }
      render();
    });
  }
  $("txSweeps")?.addEventListener("click", () => {
    showSweeps = !showSweeps;
    $("txSweeps").setAttribute("aria-pressed", String(showSweeps));
    render();
  });
  $("txFilter")?.addEventListener("input", () => {
    query = $("txFilter").value;
    render();
  });
}

export async function route() {
  if (!loaded) {
    loaded = true;
    [rows] = await Promise.all([load(), loadTiles()]);
  }
  render();
}
