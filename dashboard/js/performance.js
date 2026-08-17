/* The Performance view: where the money came from, and what it cost.
 *
 * Three charts off one endpoint — the NAV flow Sankey, dividend income by
 * month, and costs by month. All three come from IBKR Flex sections that are
 * not enabled by default, so every one of them has a designed empty state
 * that says what to switch on rather than rendering an empty axis.
 *
 * Every figure here was computed by adapter/attribution.py. This module picks
 * charts, colours and copy; it does no arithmetic on money beyond summing a
 * legend, which the server has already agreed with.
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

let data = null;
let loaded = false;

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
function empty(host, title, section) {
  host.innerHTML = `
    <div class="collecting">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"
           stroke-linejoin="round" aria-hidden="true">
        <path d="M3 4.5h18v4H3zM5 8.5v11M19 8.5v11M9 19.5h6"/>
      </svg>
      <h3>${esc(title)}</h3>
      <p>Add the <b>${esc(section)}</b> section to the Flex query behind
         <code>nav_query_id</code>, then run <code>adapter/backfill.py</code>.</p>
    </div>`;
}

function renderFlow() {
  const host = $("flowPlot");
  const flow = data?.flow;
  if (!flow) {
    empty(host, "No NAV attribution yet", "Change in NAV");
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

/** A legend row per drawn band. Identity is never colour alone. */
function legend(host, series, colours) {
  host.innerHTML = series.map((s) => `
    <span class="perf-legend__item">
      <i style="background:${colours[s.key] || "var(--text-3)"}"></i>
      <span>${esc(s.label)}</span>
    </span>`).join("");
}

function renderBars(which) {
  const monthly = data?.monthly;
  const isCost = which === "cost";
  const host = $(isCost ? "costPlot" : "incomePlot");
  const series = (isCost ? monthly?.costs : monthly?.income) || [];
  const hasData = monthly?.months?.length && series.some((s) => s.values.some((v) => v));

  if (!hasData) {
    empty(host, isCost ? "No costs recorded yet" : "No dividends recorded yet",
          "Cash Transactions");
    $(isCost ? "costTotal" : "incomeTotal").textContent = "";
    $(isCost ? "costLegend" : "incomeLegend").innerHTML = "";
    return;
  }

  const colours = isCost ? COST_COLOURS : INCOME_COLOURS;
  const withColour = series.map((s) => ({ ...s, color: colours[s.key] || "var(--text-3)" }));

  const id = isCost ? "costSvg" : "incomeSvg";
  host.innerHTML = `<svg id="${id}" role="img" aria-label="${
    isCost ? "Costs by month" : "Dividend income by month"}"></svg>`;

  groupedBars($(id), {
    periods: monthly.months,
    series: withColour,
    stacked: true,
    // The cost tooltip itemises all five underlying figures even though the
    // stack draws three; income has nothing further to break down.
    detail: isCost ? monthly.cost_detail : null,
    totalLabel: isCost ? "Total costs" : "Total",
    formatValue: (v, full) => money(v, { decimals: full ? 2 : 0 }),
    formatPeriod: monthLabel,
    tooltip: $("tip"),
    chartLabel: isCost ? "Costs by month" : "Dividend income by month",
  });

  $(isCost ? "costTotal" : "incomeTotal").textContent =
    money(isCost ? monthly.cost_total : monthly.income_total, { decimals: 2 });
  legend($(isCost ? "costLegend" : "incomeLegend"), series, colours);
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
  // Nothing to wire up front; route() does the work on first show.
}
