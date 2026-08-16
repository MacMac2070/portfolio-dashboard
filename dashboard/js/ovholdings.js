/* Overview's holdings section — what you actually hold, below the fold.
 *
 * The Watchlist tab merges held positions with the twenty names you only
 * track. This is the other question: of the things I own, what is each one
 * worth and what is it doing. Owned only, largest first, no sorting — the
 * Watchlist tab keeps the sortable headers for when you want to reorder.
 *
 * Rows come from `holdings.positionRow`, so a position looks identical wherever
 * you meet it, and the ownership rail, the tile and the click-through to
 * `#stock/<key>` all come for free.
 *
 * Why this file exists rather than a call into holdings.js
 * -------------------------------------------------------
 * `holdings.render()` rewrites its table's innerHTML on every poll. That is
 * invisible there because the tab is hidden while the poll runs. Here the table
 * is on screen: rewriting it every three seconds would drop text selection
 * mid-read, kill hover, and restart every logo request. So this module rebuilds
 * structure ONLY when the set of positions changes, and otherwise patches the
 * figures in place — the same discipline main.js applies to the mover rows and
 * marketwatch.js to its grid.
 */
import {
  money, moneySigned, pct, pctSigned, price, qty, direction,
} from "./format.js";
import { sparkline } from "./charts.js";
import { heldRows, positionRow, HEADS } from "./holdings.js";

const $ = (id) => document.getElementById(id);
const DASH = "—";

let structureKey = "";   // the set of positions currently rendered

/* ---------------- helpers ---------------- */

const dirColor = (v) => (v > 0 ? "var(--gain-soft)" : v < 0 ? "var(--loss-soft)" : "var(--text-muted)");

/** Writes only on change, and reports whether it wrote — the flash below keys
 *  off that, so a poll that returns an unchanged price stays visually silent. */
function setText(node, value) {
  if (!node || node.textContent === value) return false;
  node.textContent = value;
  return true;
}

/* ---------------- price flash ----------------
 *
 * Adapted from shadcn-fintech's holdings-table.tsx, which flashes the Current
 * Price cell on every tick with a Motion span keyed by `${id}-${price}`.
 *
 * Two differences here. There is no key to change, so the animation is
 * restarted by removing the class and forcing a reflow before re-adding it.
 * And the tint animates as the OPACITY of a pseudo-element sitting behind the
 * text, not as background-color on the cell — this codebase animates transform
 * and opacity only, and opacity is the compositor-friendly half of that rule.
 *
 * Direction needs the previous price, which the payload does not carry, so it
 * is remembered per contract. Keyed on con_id rather than symbol because that
 * is what the row is addressed by everywhere else in this file.
 */
const lastPrice = new Map();
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function flash(node, dir) {
  if (!node || reduceMotion.matches) return;
  node.classList.remove("pflash--up", "pflash--down");
  void node.offsetWidth;                 // reflow, so the animation re-fires
  node.classList.add(`pflash--${dir}`);
}

/** Static labels, not buttons: this table has no sort. */
function renderHead() {
  const host = $("ovHead");
  if (host.dataset.built) return;
  host.innerHTML = HEADS.map((h) => `
    <span class="ovhead__cell" style="grid-column:${h.col};justify-content:${h.justify}">
      ${h.label}
    </span>`).join("");
  host.dataset.built = "1";
}

/* ---------------- render ---------------- */

export function update() {
  const host = $("ovRows");
  if (!host) return;

  // Largest holding first. A portfolio reads top-down by size.
  const rows = heldRows()
    .filter((r) => !r.pending)
    .sort((a, b) => (b.value || 0) - (a.value || 0));

  const section = $("ovHoldings");
  if (!rows.length) {
    // Nothing to say yet — the universe arrives on the 20s watchlist poll, so
    // this is the normal state for the first few seconds after a cold load.
    setText($("ovCount"), DASH);
    setText($("ovFoot"), "Waiting for the position feed.");
    return;
  }
  section?.removeAttribute("hidden");

  renderHead();

  // Rebuild only when the SET changes — not when the numbers do.
  const key = rows.map((r) => r.con_id ?? r.key).join("|");
  if (key !== structureKey) {
    structureKey = key;
    host.innerHTML = rows.map((r) => positionRow(r, false)).join("");
    // Sparklines are structural: they only need redrawing when rows change.
    // Scoped to this table so it does not walk the movers' SVGs above.
    const byKey = new Map(rows.map((r) => [r.key, r]));
    for (const svg of host.querySelectorAll("[data-spark]")) {
      const r = byKey.get(svg.dataset.spark);
      if (r?.spark?.length) sparkline(svg, r.spark, { direction: direction(r.dayPct) });
    }
  } else {
    patch(host, rows);
  }

  const total = rows.reduce((sum, r) => sum + (r.value || 0), 0);
  const dayTotal = rows.reduce((sum, r) => sum + (r.dayGbp || 0), 0);
  setText($("ovCount"), String(rows.length));
  setText($("ovFoot"),
    `${rows.length} positions · ${money(total)} at market · ` +
    `${moneySigned(dayTotal)} today. Quotes are delayed 15 minutes.`);
}

/**
 * Update the figures without touching the structure.
 *
 * Addressed by data-con-id, and every write goes through setText so an
 * unchanged value never touches the DOM — that is what keeps a text selection
 * alive across a poll.
 */
function patch(host, rows) {
  for (const r of rows) {
    const row = host.querySelector(`[data-con-id="${r.con_id}"]`);
    if (!row) continue;

    const cells = row.querySelectorAll(".pstack");
    // 1: last price + currency, 2: qty + avg cost, 3: value + cost, 4: P/L + return
    const priceCell = cells[0]?.querySelector(".pstack__main");
    const prev = lastPrice.get(r.con_id);
    if (Number.isFinite(r.price)) lastPrice.set(r.con_id, r.price);
    // Flash only on a real move. `wrote` alone is not enough: price() rounds,
    // so a sub-precision tick can leave the text identical, and the first
    // patch after a rebuild has no previous price to compare against.
    if (setText(priceCell, price(r.price))
        && Number.isFinite(prev) && Number.isFinite(r.price) && r.price !== prev) {
      flash(priceCell, r.price > prev ? "up" : "down");
    }

    const chip = row.querySelector(".dchip");
    if (chip) {
      const d = direction(r.dayPct);
      const cls = `dchip dchip--${d}`;
      if (chip.className !== cls) chip.className = cls;
      setText(chip, Number.isFinite(r.dayPct) ? pctSigned(r.dayPct) : DASH);
    }

    const dayPl = row.querySelector(".pdaypl");
    if (dayPl) {
      setText(dayPl, Number.isFinite(r.dayGbp) ? moneySigned(r.dayGbp) : DASH);
      const c = dirColor(r.dayGbp);
      if (dayPl.style.color !== c) dayPl.style.color = c;
    }

    setText(cells[1]?.querySelector(".pstack__main"), qty(r.quantity));
    setText(cells[1]?.querySelector(".pstack__sub"),
      `@ ${r.avgCost != null ? price(r.avgCost) : DASH}`);

    setText(cells[2]?.querySelector(".pstack__main"), money(r.value));
    setText(cells[2]?.querySelector(".pstack__sub"), `${money(r.cost)} cost`);

    const plMain = cells[3]?.querySelector(".pstack__main");
    const plRet = cells[3]?.querySelector(".pstack__ret");
    const colour = dirColor(r.pl);
    if (plMain) {
      setText(plMain, moneySigned(r.pl));
      if (plMain.style.color !== colour) plMain.style.color = colour;
    }
    if (plRet) {
      setText(plRet, pctSigned(r.retPct));
      if (plRet.style.color !== colour) plRet.style.color = colour;
    }

    setText(row.querySelector(".pweight"),
      r.weight != null ? pct(r.weight, 1) : DASH);
  }
}

/* ---------------- the scroll cue ---------------- */

export function init() {
  const view = $("view-overview");
  const cue = $("ovCue");
  if (!view || !cue) return;

  // Overview has never scrolled, so nothing signals there is more below.
  // The cue fades once you have moved — opacity only, so it reflows nothing.
  const onScroll = () => {
    cue.dataset.gone = String(view.scrollTop > 24);
  };
  view.addEventListener("scroll", onScroll, { passive: true });

  cue.addEventListener("click", () => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    $("ovHoldings")?.scrollIntoView({
      behavior: reduce ? "auto" : "smooth", block: "start",
    });
  });

  onScroll();
}
