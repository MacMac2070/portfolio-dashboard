/* The Transactions tab: every execution the Flex statement carries.
 *
 * Reads data/transactions.jsonl, which adapter/store.merge_transactions writes
 * from adapter/flex.parse_trades. Static file, same as nav_history.jsonl — no
 * endpoint, because there is no derivation to do server-side and the file is
 * small enough that the browser can sort it.
 *
 * The store is merge-by-key on exec_id, so the file is append-only and a
 * re-run of the backfill never duplicates a fill.
 */
import { price, qty, day, esc, count } from "./format.js";

const TX_URL = "data/transactions.jsonl";

const $ = (id) => document.getElementById(id);

let rows = [];
let loaded = false;
/** Which column is sorted, and which way. Date-descending by default: the
 *  question a trade log answers first is "what did I do most recently". */
let sort = { key: "time", dir: -1 };

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

function sorted() {
  const key = sort.key;
  const value = (r) => {
    if (key === "value") return considerationOf(r);
    if (key === "commission") return Math.abs(r.commission ?? 0);
    if (key === "side") return sideOf(r.side).label;
    return r[key];
  };
  return [...rows].sort((a, b) => {
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
    <div class="txrow" role="row">
      <span class="txrow__date num">${esc(day(r.time))}</span>
      <span class="txrow__sym">
        ${esc(r.symbol)}
        <span class="txrow__venue">${esc(r.exchange || "")}</span>
      </span>
      <span class="txrow__side"><span class="txpill ${side.cls}">${side.label}</span></span>
      <span class="txrow__n num">${qty(r.quantity)}</span>
      <span class="txrow__n num">${price(r.price)}</span>
      <span class="txrow__n num">${cash(considerationOf(r))}
        <span class="txrow__ccy">${esc(r.currency || "")}</span></span>
      <span class="txrow__n num txrow__fee">${fee === null ? "—" : cash(fee)}</span>
    </div>`;
}

function render() {
  const host = $("txBody");
  const headHost = $("txHead");
  if (!host) return;

  if (!rows.length) {
    headHost.innerHTML = "";
    $("txMeta").textContent = "";
    host.innerHTML = `
      <div class="collecting">
        <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.4"
             aria-hidden="true">
          <rect x="6" y="9" width="36" height="30" rx="4"/><path d="M6 18h36M14 26h12M14 32h8"/>
        </svg>
        <h3>No transactions yet</h3>
        <p>Add the <b>Trades</b> section to the Flex query behind
           <code>nav_query_id</code>, or set <code>trades_query_id</code> to a
           Trade Confirmation query, then run <code>adapter/backfill.py</code>.</p>
      </div>`;
    return;
  }

  const list = sorted();
  headHost.innerHTML = head();
  host.innerHTML = list.map(row).join("");

  // The span is the record's own, not the selected range: this is a log, and
  // saying how far back it reaches is what stops a short file reading as a
  // complete history.
  const dates = rows.map((r) => r.time).sort();
  $("txMeta").textContent =
    `${count(rows.length)} executions · ${day(dates[0])} – ${day(dates.at(-1))}`;
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
}

export async function route() {
  if (!loaded) {
    loaded = true;
    rows = await load();
  }
  render();
}
