/* The Performance view: where the money came from, and what it cost.
 *
 * Three cards off one endpoint — the NAV flow Sankey, a monthly card that
 * toggles between dividend income and P&L, and costs by month. All of it comes
 * from IBKR Flex sections that are not enabled by default, so every chart has a
 * designed empty state that says what to switch on rather than rendering an
 * empty axis.
 *
 * Every figure here was computed by adapter/attribution.py. This module picks
 * charts, colours and copy; it does no arithmetic on money beyond summing a
 * legend, which the server has already agreed with.
 *
 * Two interactions come from inspiration/premium-tracker-marfusios:
 *
 *   Legend click to toggle a series. The reference wires Recharts' Legend
 *   onClick to an activeSeries object and sets hide={!activeSeries.x} per Bar.
 *   Here the hidden keys are filtered out before groupedBars() is called, so
 *   the axis, the stack and the tooltip total all recompute from the series
 *   that remain without the chart needing to know a series was hidden at all.
 *
 *   Income/P&L toggle. The reference's income series (options premium, stock
 *   yield) do not apply here, but the P/L half does — see attribution.py.
 */
import { money, moneySigned, esc } from "./format.js";
import { sankey, groupedBars } from "./charts.js";

const $ = (id) => document.getElementById(id);
const URL_ATTR = "api/attribution";

/* Costs draw three bands; the tooltip itemises five. See COST_BANDS in
 * adapter/attribution.py for why the stack is not five hues. */
const COST_COLOURS = {
  commissions: "var(--cost-1)",
  fees_tax: "var(--cost-2)",
  interest_paid: "var(--cost-3)",
};
const INCOME_COLOURS = {
  dividends: "var(--cost-1)",
  interest_received: "var(--cost-2)",
};

/* P&L is the one series here where green and red carry their usual meaning, so
 * it wears the P&L tokens rather than the cost ramp — and it wears them per
 * bar, because the sign is what the bar is saying. Paired with a signed label
 * in the tooltip and the axis, so it never reads by hue alone. */
const pnlColour = (v) => (v >= 0 ? "var(--pos)" : "var(--neg)");

/* Signed, except at zero: the axis crosses zero and "+£0" would put a sign on
 * a figure that has no direction. A flat month is flat, not a gain of nothing. */
const pnlFigure = (v, full) => {
  const opts = { decimals: full ? 2 : 0 };
  return v === 0 ? money(0, opts) : moneySigned(v, opts);
};

let data = null;
let loaded = false;

/* Which series the reader has switched off, per chart. Module-level so a
 * re-render on tab revisit keeps the choice; not persisted, because hiding a
 * band is a look-closer gesture rather than a setting. */
const hidden = { income: new Set(), cost: new Set() };

/* Which half of the monthly card is showing. */
let mode = "income";

/** "2026-05" -> "May 26", and a longer form for the tooltip heading. */
function monthLabel(key, _i, full) {
  const [y, m] = String(key).split("-");
  const date = new Date(Number(y), Number(m) - 1, 1);
  if (Number.isNaN(date.getTime())) return key;
  return date.toLocaleDateString("en-GB", {
    month: "short", year: full ? "numeric" : "2-digit",
  });
}

/**
 * A designed empty state, not a blank panel.
 *
 * Each of these charts is one Flex checkbox away from having data, so the copy
 * names the section and the command rather than saying "no data".
 */
function empty(host, title, body) {
  host.innerHTML = `
    <div class="collecting">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"
           stroke-linejoin="round" aria-hidden="true">
        <path d="M3 4.5h18v4H3zM5 8.5v11M19 8.5v11M9 19.5h6"/>
      </svg>
      <h3>${esc(title)}</h3>
      <p>${body}</p>
    </div>`;
}

/** The standard "switch this Flex section on" copy. */
const flexHint = (section) =>
  `Add the <b>${esc(section)}</b> section to the Flex query behind
   <code>nav_query_id</code>, then run <code>adapter/backfill.py</code>.`;

function renderFlow() {
  const host = $("flowPlot");
  const flow = data?.flow;
  if (!flow) {
    empty(host, "No NAV attribution yet", flexHint("Change in NAV"));
    $("flowPeriod").textContent = "";
    $("flowFoot").textContent = "";
    return;
  }

  host.innerHTML = '<svg id="flowSvg" role="img" aria-label="Where the account\'s value came from and went"></svg>';
  sankey($("flowSvg"), flow, {
    // Precision follows magnitude, as price() does elsewhere in this codebase.
    // A £0.10 broker fee is a real cost and printing it as "£0" beside a
    // £43,500 deposit would be a lie of rounding; £43,500.00 would just be
    // noise. Whole pounds above £1, pennies below it.
    formatValue: (v) => money(v, { decimals: Math.abs(v) < 1 ? 2 : 0 }),
    tooltip: $("tip"),
  });

  $("flowPeriod").textContent =
    flow.from_date && flow.to_date ? `${flow.from_date} → ${flow.to_date}` : "";

  // Drift means the flows and IBKR's own ending value disagree — almost always
  // a field this build does not map yet. Say so; a chart that does not add up
  // should not look like one that does.
  const notes = [];
  if (Math.abs(flow.drift) >= 0.01) {
    notes.push(`Flows and the reported ending value differ by ${moneySigned(flow.drift, { decimals: 2 })}`);
  }
  if (flow.unmapped?.length) {
    notes.push(`unmapped Flex fields: ${flow.unmapped.join(", ")}`);
  }
  $("flowFoot").textContent = notes.join(" · ");
}

/**
 * A legend row per drawn band, as a button that switches the band off.
 *
 * Buttons rather than styled spans: hover, focus-visible and active then come
 * from one place, and the toggle is reachable by keyboard without any extra
 * wiring. `aria-pressed` carries the state, and the off look changes the
 * swatch and the label together so it does not read by colour alone.
 *
 * The last visible band cannot be switched off — an axis with nothing on it is
 * not a state worth being able to reach — so its button says why instead.
 */
function legend(host, series, colours, which) {
  const off = hidden[which];
  const visible = series.filter((s) => !off.has(s.key));
  host.innerHTML = series.map((s) => {
    const on = !off.has(s.key);
    const last = on && visible.length === 1;
    return `
      <button class="perf-legend__item" type="button" data-key="${esc(s.key)}"
              aria-pressed="${on}"${last ? ' disabled title="The only band still showing"' : ""}>
        <i style="--swatch:${colours[s.key] || "var(--text-3)"}"></i>
        <span>${esc(s.label)}</span>
      </button>`;
  }).join("");
}

/**
 * One monthly chart. `which` is the card — "income" or "cost" — and for the
 * income card `mode` decides whether it is drawing dividends or P&L.
 */
function renderBars(which) {
  const monthly = data?.monthly;
  const isCost = which === "cost";
  const showPnl = !isCost && mode === "pnl";
  const host = $(isCost ? "costPlot" : "incomePlot");
  const totalEl = $(isCost ? "costTotal" : "incomeTotal");
  const legendEl = $(isCost ? "costLegend" : "incomeLegend");

  if (!isCost) {
    // The card is one card in two states, so its heading and the chart's
    // accessible name have to follow the toggle rather than stay put.
    $("incomeTitle").textContent = showPnl ? "Monthly P&L" : "Dividend income";
  }

  const series = (isCost ? monthly?.costs
                 : showPnl ? monthly?.pnl
                 : monthly?.income) || [];
  const hasData = monthly?.months?.length && series.some((s) => s.values.some((v) => v));

  if (!hasData) {
    // Three different reasons to be empty, three different things to do about
    // it. P&L needs a query *setting*, not another section, so saying "enable
    // Cash Transactions" there would send the reader to the wrong switch.
    if (showPnl && !monthly?.pnl_available) {
      empty(host, "No monthly P&L yet",
        `The <b>Change in NAV</b> section is reporting one period, not months.
         Set the Flex query behind <code>nav_query_id</code> to monthly periods,
         then run <code>adapter/backfill.py</code>.`);
    } else {
      empty(host, isCost ? "No costs recorded yet"
                         : showPnl ? "No monthly P&L yet" : "No dividends recorded yet",
        flexHint(showPnl ? "Change in NAV" : "Cash Transactions"));
    }
    totalEl.textContent = "";
    legendEl.innerHTML = "";
    return;
  }

  const colours = isCost ? COST_COLOURS : INCOME_COLOURS;
  const off = isCost ? hidden.cost : hidden.income;

  // Hidden bands are filtered out here rather than inside the chart: with them
  // gone, groupedBars() rescales the axis and totals the tooltip over what is
  // left without needing to know anything was switched off.
  const drawn = showPnl
    ? series.map((s) => ({ ...s, color: pnlColour }))
    : series.filter((s) => !off.has(s.key))
            .map((s) => ({ ...s, color: colours[s.key] || "var(--text-3)" }));

  // The cost tooltip itemises five figures behind three drawn bands, so a
  // hidden band takes its detail rows with it. `detail_keys` comes from
  // attribution.py; the mapping stays server-side.
  let detail = null;
  if (isCost && monthly.cost_detail) {
    const keep = new Set(drawn.flatMap((s) => s.detail_keys || [s.key]));
    detail = monthly.cost_detail.filter((d) => keep.has(d.key));
  }

  const id = isCost ? "costSvg" : "incomeSvg";
  const label = isCost ? "Costs by month"
                       : showPnl ? "Profit and loss by month" : "Dividend income by month";
  host.innerHTML = `<svg id="${id}" role="img" aria-label="${label}"></svg>`;

  groupedBars($(id), {
    periods: monthly.months,
    series: drawn,
    // P&L is one signed figure per month, so it is a plain column crossing the
    // zero line rather than something to stack.
    stacked: !showPnl,
    detail,
    totalLabel: isCost ? "Total costs" : showPnl ? "Net" : "Total",
    // A signed figure needs its sign: +£1,250 and −£770 are different months,
    // and on P&L the sign is the whole reading.
    formatValue: showPnl ? pnlFigure : (v, full) => money(v, { decimals: full ? 2 : 0 }),
    formatPeriod: monthLabel,
    tooltip: $("tip"),
    chartLabel: label,
  });

  // Totalled over the bands actually drawn, so switching one off moves the
  // headline figure with it. Left as the server's figure and it would disagree
  // with the tooltip, which totals what is on screen — two totals, one chart.
  // With nothing hidden this sums to the server's own cost_total/income_total,
  // because the drawn bands cover every underlying bucket between them.
  const drawnTotal = drawn.reduce(
    (acc, s) => acc + s.values.reduce((a, v) => a + (Number.isFinite(v) ? v : 0), 0), 0);
  totalEl.textContent = showPnl
    ? pnlFigure(monthly.pnl_total, true)
    : money(drawnTotal, { decimals: 2 });
  // Colour the running total to match its sign, but only when it has one.
  totalEl.className = "perf-card__total"
    + (showPnl && monthly.pnl_total ? (monthly.pnl_total > 0 ? " pos" : " neg") : "");

  // One series needs no legend — the card title names it. Two or more always
  // get one, so identity is never colour alone.
  if (showPnl || drawn.length + off.size < 2) legendEl.innerHTML = "";
  else legend(legendEl, series, colours, isCost ? "cost" : "income");
}

function renderAll() {
  renderFlow();
  renderBars("income");
  renderBars("cost");
}

async function load() {
  try {
    const res = await fetch(`${URL_ATTR}?t=${Date.now()}`);
    if (!res.ok) throw new Error(`attribution: ${res.status}`);
    data = await res.json();
  } catch {
    data = null;
  }
  loaded = true;
  renderAll();
}

/**
 * Called when the view is shown.
 *
 * Fetched on first visit rather than at boot: this is a click-driven page
 * reading two JSONL stores that only change when the daily job runs, so
 * loading it behind Overview would cost a request nobody asked for. Re-render
 * on every visit so a resize while the tab was hidden is picked up — the SVGs
 * size themselves from their box.
 */
export function route() {
  if (!loaded) { load(); return; }
  renderAll();
}

export function init() {
  // Legend clicks are delegated: renderBars() rewrites the legend's innerHTML
  // on every draw, and a handler per button would not survive that.
  for (const [host, which] of [["incomeLegend", "income"], ["costLegend", "cost"]]) {
    $(host)?.addEventListener("click", (event) => {
      const button = event.target.closest(".perf-legend__item");
      if (!button || button.disabled) return;
      const key = button.dataset.key;
      const off = hidden[which];
      if (off.has(key)) off.delete(key); else off.add(key);
      renderBars(which);
    });
  }

  $("incomeMode")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-mode]");
    if (!button || button.dataset.mode === mode) return;
    mode = button.dataset.mode;
    for (const other of $("incomeMode").querySelectorAll("[data-mode]")) {
      other.setAttribute("aria-pressed", String(other === button));
    }
    renderBars("income");
  });
}
