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
import { sankey, groupedBars, underwater, sparkline } from "./charts.js";

const $ = (id) => document.getElementById(id);
const URL_ATTR = "api/attribution";
const URL_TRACK = "api/track";
const URL_DESK = "api/desk";
// The grid's benchmark row follows the equity curve's picker — one selection,
// two readouts. Same literal main.js writes.
const BENCH_KEY = "portfolio-dashboard:benchmark";

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

/** The standard "switch this Flex section on" runbook, as a terminal block. */
const flexHint = (section) => `
  <span class="setup">query    nav_query_id
section  <b>${esc(section)}</b>   ← enable in Account Management
then     /opt/anaconda3/bin/python3 adapter/backfill.py</span>`;

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
    // Rounded data-ends and a baseline fade — the ink is strongest where the
    // figure is, thinner where the bar is merely reaching from zero.
    rx: 2,
    gradient: true,
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
  renderTrack();
  settleCharts();
}

/**
 * The spread's charts size themselves from their box, but the box is not
 * final until every pane has content — the heatmap's auto row claims its
 * height last and shrinks the rows above it. One frame later, any SVG whose
 * viewBox no longer matches its box is drawn again at the settled size.
 * Redraw is conditional, so the load animation only replays when the first
 * pass genuinely drew at the wrong geometry.
 */
function settleCharts() {
  // setTimeout, not requestAnimationFrame: Chrome parks rAF entirely while
  // the window is occluded, which would leave a wrongly-sized chart frozen
  // until the next repaint for no reason a timer doesn't have.
  setTimeout(() => {
    for (const [id, redraw] of [
      ["incomeSvg", () => renderBars("income")],
      ["ddSvg", () => renderDrawdown()],
      ["costSvg", () => renderBars("cost")],
      ["flowSvg", () => renderFlow()],
    ]) {
      const svg = $(id);
      if (!svg) continue;
      const vb = (svg.getAttribute("viewBox") || "").split(" ").map(Number);
      const box = svg.getBoundingClientRect();
      if (vb.length === 4 && box.width
          && (Math.abs(vb[2] - box.width) > 8 || Math.abs(vb[3] - box.height) > 8)) {
        redraw();
      }
    }
  }, 50);
}

/* ---------------- track record ---------------- */

let track = null;
let desk = null;

const MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
// monthLabel already exists above ("2026-05" -> "May 26") — reuse it.
const pctCell = (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v * 100).toFixed(2)}%`;

/** Wash strength from magnitude — the printed figure is the encoding, the
 *  wash is the glance layer. Alpha steps, never hue steps. */
function washStyle(v) {
  if (v == null || v === 0) return "";
  const alpha = Math.min(0.06 + Math.abs(v) * 2.6, 0.42).toFixed(2);
  const base = v > 0 ? "52, 211, 153" : "251, 113, 133";
  return `background: rgba(${base}, ${alpha})`;
}

function renderMonthlyGrid() {
  const host = $("monthlyGrid");
  if (!host) return;
  const grid = track?.monthly;
  const months = grid ? Object.keys(grid.months).sort() : [];
  if (!months.length) {
    host.innerHTML = `<div class="collecting"><h3>No monthly record yet</h3>
      <p>Cells appear as the Flex Change in NAV sub-periods land.</p></div>`;
    $("gridNote").textContent = "";
    return;
  }

  const benchName = document.querySelector(
    `#benchSelect option[value="${CSS.escape(grid.benchmark_symbol || "")}"]`)
    ?.textContent || (grid.benchmark_symbol || "").replace(/^\^/, "");
  const hasBench = Object.keys(grid.benchmark || {}).length > 0;

  const cell = (v) => v == null
    ? '<td class="mgrid__cell mgrid__cell--none">—</td>'
    : `<td class="mgrid__cell num" style="${washStyle(v)}">${pctCell(v)}</td>`;

  host.innerHTML = `<table class="mgrid__table">
    <thead><tr><th></th>${months.map((m) => `<th>${monthLabel(m)}</th>`).join("")}</tr></thead>
    <tbody>
      <tr><th>Portfolio</th>${months.map((m) => cell(grid.months[m])).join("")}</tr>
      ${hasBench ? `<tr class="mgrid__bench"><th>${esc(benchName)}</th>
        ${months.map((m) => cell(grid.benchmark[m] ?? null)).join("")}</tr>` : ""}
    </tbody></table>`;

  $("gridNote").textContent = "from IBKR's own monthly figures";
  // When the spread squeezes the grid into a scroll, recent months win.
  host.scrollLeft = host.scrollWidth;
}

function renderDrawdown() {
  const dd = track?.drawdown;
  const plot = $("ddPlot");
  if (!plot) return;
  if (!dd) {
    plot.innerHTML = `<div class="collecting"><h3>Collecting history</h3>
      <p>The drawdown needs a few daily NAV points.</p></div>`;
    $("ddEpisodes").innerHTML = "";
    $("ddCurrent").textContent = "";
    return;
  }
  plot.innerHTML = '<svg id="ddSvg" role="img" aria-label="Drawdown from peak over time"></svg>';
  underwater($("ddSvg"), dd.curve, {
    tooltip: $("tip"),
    formatDate: (d) => new Date(d).toLocaleDateString("en-GB", { month: "short", year: "2-digit" }),
  });
  const cur = dd.current;
  $("ddCurrent").textContent = cur < -0.0005
    ? `${(cur * 100).toFixed(1)}% from peak` : "at peak";
  $("ddCurrent").className = `perf-card__total num ${cur < -0.0005 ? "neg" : ""}`;

  const fmt = (d) => new Date(d).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "2-digit" });
  // Each row's wash is as wide as the episode is deep, scaled to the worst
  // on the list — the table doubles as its own bar chart.
  const worstDepth = Math.max(...dd.episodes.slice(0, 4).map((ep) => Math.abs(ep.depth)), 1e-9);
  $("ddEpisodes").innerHTML = dd.episodes.slice(0, 4).map((ep) => `
    <div class="ddeps__row" style="--depth:${((Math.abs(ep.depth) / worstDepth) * 100).toFixed(0)}%">
      <span class="ddeps__depth num neg">−${Math.abs(ep.depth * 100).toFixed(1)}%</span>
      <span class="ddeps__span">${fmt(ep.peak_date)} → ${fmt(ep.trough_date)}</span>
      <span class="ddeps__days num">${ep.days_down}d down</span>
      <span class="ddeps__state">${ep.recovered
        ? `recovered ${fmt(ep.recovered)}` : "<b>ongoing</b>"}</span>
    </div>`).join("");
}

/** Alpha-stepped day cells; the sign glyph rides strong cells so the grid
 *  never reads by hue alone, and the tooltip always carries the figure. */
function renderDayCal() {
  const host = $("dayCal");
  if (!host) return;
  const days = track?.daily || [];
  if (days.length < 10) {
    host.innerHTML = "";
    $("calNote").textContent = "";
    return;
  }

  // Month blocks that read like a wall calendar: Mon-Fri across, weeks
  // stacking down. Weekend columns would be permanently empty cells. Days the
  // account hasn't lived through are invisible pads; days it sat out
  // (holidays) keep the outlined blank so the gap is honest.
  const byDate = new Map(days.map((d) => [d.date, d]));
  const first = new Date(days[0].date);
  const last = new Date(days[days.length - 1].date);

  const level = (r) => Math.abs(r) >= 0.02 ? 3 : Math.abs(r) >= 0.008 ? 2 : Math.abs(r) > 0.0005 ? 1 : 0;
  const PAD = '<i class="dcal__cell dcal__cell--pad"></i>';
  let months = "";
  for (let m = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth(), 1));
       m <= last; m.setUTCMonth(m.getUTCMonth() + 1)) {
    const dow1 = (m.getUTCDay() + 6) % 7;
    let cells = PAD.repeat(dow1 >= 5 ? 0 : dow1);
    for (const day = new Date(m); day.getUTCMonth() === m.getUTCMonth();
         day.setUTCDate(day.getUTCDate() + 1)) {
      if ((day.getUTCDay() + 6) % 7 >= 5) continue;
      const iso = day.toISOString().slice(0, 10);
      const row = byDate.get(iso);
      if (!row) {
        cells += day < first || day > last ? PAD : '<i class="dcal__cell dcal__cell--none"></i>';
        continue;
      }
      const lv = level(row.r);
      const dir = row.r > 0 ? "pos" : row.r < 0 ? "neg" : "flat";
      cells += `<i class="dcal__cell dcal__cell--${dir} dcal__cell--l${lv}"
        data-date="${iso}" data-r="${(row.r * 100).toFixed(2)}" data-pnl="${row.pnl}"
        >${lv >= 2 ? (row.r > 0 ? "+" : "−") : ""}</i>`;
    }
    months += `<div class="dcal__month"><span class="dcal__mlabel">${
      MONTH_SHORT[m.getUTCMonth()]}</span><div class="dcal__days">${cells}</div></div>`;
  }
  host.innerHTML = `<div class="dcal__grid">${months}</div>`;
  $("calNote").textContent = `${days.length} trading days since ${new Date(track.inception)
    .toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}`;

  // One delegated tooltip for the whole grid.
  const tip = $("tip");
  host.onpointerover = (event) => {
    const cell = event.target.closest(".dcal__cell[data-date]");
    if (!cell || !tip) return;
    tip.dataset.open = "true";
    const when = new Date(cell.dataset.date).toLocaleDateString("en-GB",
      { weekday: "short", day: "numeric", month: "short", year: "numeric" });
    tip.innerHTML = `<div class="tip__date">${when}</div>
      <div class="tip__val">${moneySigned(Number(cell.dataset.pnl))} · ${
        Number(cell.dataset.r) > 0 ? "+" : ""}${cell.dataset.r}%</div>`;
    const at = cell.getBoundingClientRect();
    tip.style.left = `${Math.min(at.left + 10, window.innerWidth - tip.offsetWidth - 8)}px`;
    tip.style.top = `${at.top - 8}px`;
  };
  host.onpointerout = () => { if (tip) tip.dataset.open = "false"; };
}

function renderDays() {
  const host = $("bwDays");
  if (!host) return;
  const d = track?.days;
  if (!d) { host.innerHTML = ""; return; }

  const fmt = (x) => new Date(x).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "2-digit" });
  const row = (r) => `
    <div class="bwdays__row">
      <span class="bwdays__date">${fmt(r.date)}</span>
      <span class="bwdays__r num ${r.r > 0 ? "pos" : "neg"}">${pctCell(r.r)}</span>
      <span class="bwdays__pnl num">${moneySigned(r.pnl)}</span>
    </div>`;
  host.innerHTML = `
    <div class="bwdays__col"><p class="eyebrow">Best</p>${d.best.map(row).join("")}</div>
    <div class="bwdays__col"><p class="eyebrow">Worst</p>${d.worst.map(row).join("")}</div>`;
  // The win-rate line rides the heatmap caption now — one home for day stats.
  const cal = $("calNote");
  if (cal && !cal.textContent.includes("up days")) {
    cal.textContent = `${cal.textContent}${cal.textContent ? " · " : ""}`
      + `${(d.win_rate * 100).toFixed(0)}% up days · runs +${d.best_streak}/−${Math.abs(d.worst_streak)}`;
  }
}

/* ---------------- verdict masthead ----------------
 * The spread opens with the answer. Every figure is signed; ratios print
 * em-dashes until 60 aligned observations exist — the server guards that. */
function renderMasthead() {
  const st = track?.stats;
  const put = (id, text, signed = null) => {
    const node = $(id);
    if (!node) return;
    node.textContent = text;
    if (signed !== null) {
      node.classList.toggle("pos", signed > 0);
      node.classList.toggle("neg", signed < 0);
    }
  };
  const fmtPct = (v, dp = 2) => (v == null ? "—"
    : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v * 100).toFixed(dp)}%`);

  if (!st) {
    for (const id of ["vSi", "vMtd", "vYtd", "vBench", "vAlpha", "vBeta", "vVol", "vSharpe"]) put(id, "—");
    $("vSpark")?.replaceChildren();
    const race0 = $("vRace");
    if (race0) race0.hidden = true;
    return;
  }

  // The spark: the compounded daily series — the same arithmetic stats()
  // ran server-side, replayed only to draw the line.
  const spark = $("vSpark");
  if (spark) {
    let acc = 1;
    const curve = (track?.daily || []).map((day) => (acc *= 1 + day.r));
    if (curve.length >= 2) {
      sparkline(spark, curve, {
        direction: st.si > 0 ? "up" : st.si < 0 ? "down" : "flat",
      });
    } else spark.replaceChildren();
  }
  const benchName = document.querySelector(
    `#benchSelect option[value="${CSS.escape(st.benchmark_symbol || "")}"]`)
    ?.textContent || (st.benchmark_symbol || "").replace(/^\^/, "") || "benchmark";

  const from = track.inception ? new Date(track.inception) : null;
  const to = st.asof ? new Date(st.asof) : null;
  const d = (x) => x.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "2-digit" }).toUpperCase();
  // The benchmark is named two rows down; repeating it here just ellipsizes.
  put("vRange", `PERFORMANCE${from && to ? ` · ${d(from)} → ${d(to)}` : ""}`);

  put("vSi", fmtPct(st.si, 1), st.si);
  put("vMtd", fmtPct(st.mtd), st.mtd);
  put("vYtd", fmtPct(st.ytd), st.ytd);
  const delta = (st.bench?.si != null && st.si != null) ? st.si - st.bench.si : null;
  $("vBenchLabel").textContent = `vs ${benchName} (SI)`;
  put("vBench", delta == null ? "—"
    : `${delta > 0 ? "+" : delta < 0 ? "−" : ""}${Math.abs(delta * 100).toFixed(1)}pp`, delta);

  // The race: both runners on one scale, the +pp edge made visible. A
  // negative return is a zero-length bar — the printed figure still says so.
  const race = $("vRace");
  if (race) {
    if (st.bench?.si != null && st.si != null) {
      race.hidden = false;
      const span = Math.max(Math.abs(st.si), Math.abs(st.bench.si), 1e-9);
      $("vRacePort").style.width = `${(Math.max(st.si, 0) / span) * 100}%`;
      $("vRaceBench").style.width = `${(Math.max(st.bench.si, 0) / span) * 100}%`;
      $("vRacePortVal").textContent = fmtPct(st.si, 1);
      $("vRaceBenchVal").textContent = fmtPct(st.bench.si, 1);
      $("vRaceBenchLabel").textContent = benchName;
    } else race.hidden = true;
  }

  const ra = st.ratios;
  // Alpha is the one ratio with a sign worth colouring; beta, vol and sharpe
  // are magnitudes, not verdicts.
  put("vAlpha", ra?.alpha_ann == null ? "—" : fmtPct(ra.alpha_ann), ra?.alpha_ann ?? null);
  put("vBeta", ra?.beta == null ? "—" : ra.beta.toFixed(2));
  put("vVol", ra?.vol_ann == null ? "—" : `${(ra.vol_ann * 100).toFixed(1)}%`);
  put("vSharpe", ra?.sharpe == null ? "—" : ra.sharpe.toFixed(2));

  // The one income echo, so the appendix never hides a due date.
  const ev = desk?.income?.next_events?.[0];
  $("vIncome").innerHTML = ev
    ? `Next payment · ${esc(ev.key)} ${new Date(ev.date).toLocaleDateString("en-GB",
        { day: "numeric", month: "short" })}${ev.gbp != null ? ` · <b>~${money(ev.gbp)}</b>` : ""}`
    : "";
}

/* The flow headline: the Sankey's answer in one readable line. */
function renderFlowLine() {
  const node = $("flowLine");
  if (!node) return;
  const flow = data?.flow;
  if (!flow) { node.textContent = ""; return; }
  const find = (name) => flow.links.find((l) => l.source === name || l.target === name)?.value;
  const deposits = find("Deposits");
  const costs = flow.links.filter((l) => l.kind === "cost" && l.target !== "Ending NAV")
    .reduce((s2, l) => s2 + l.value, 0);
  node.innerHTML = [
    deposits != null ? `<b>${money(deposits)}</b> in` : null,
    `<b>${money(flow.ending)}</b> ending NAV`,
    costs ? `<b>${money(costs)}</b> costs` : null,
  ].filter(Boolean).join(" · ");
}

function renderTrack() {
  renderMasthead();
  renderMonthlyGrid();
  renderDrawdown();
  renderDayCal();
  renderDays();
  renderIncomeRail();
  renderFlowLine();
}

/* ---------------- income rail ----------------
 * Trailing 12 months of paid dividends (solid — broker fact) against the
 * next 12 of cadence-projected estimates (outline — a model, and it says
 * so). Composed against live share counts and FX server-side, so the £
 * figures move with the day. */
function renderIncomeRail() {
  const barsHost = $("incRailBars");
  if (!barsHost) return;
  const inc = desk?.income;
  if (!desk?.meta?.ready || !inc) {
    barsHost.innerHTML = `
      <div class="collecting"><h3>No desk data yet</h3>
      <p><span class="setup">run   /opt/anaconda3/bin/python3 adapter/desk.py
then  reload — the daily job keeps it fresh from there</span></p></div>`;
    $("incRailEvents").innerHTML = "";
    $("incRailTotal").textContent = "";
    $("incRailNote").textContent = "";
    return;
  }

  const months = inc.months || [];
  const peak = Math.max(...months.map((m) => Math.max(m.confirmed, m.estimated)), 1);
  const now = new Date().toISOString().slice(0, 7);
  barsHost.innerHTML = months.map((m, idx) => {
    const conf = (m.confirmed / peak) * 100;
    const est = (m.estimated / peak) * 100;
    const label = monthLabel(m.month);
    const amount = m.confirmed || m.estimated;
    return `
      <div class="irail__col${m.month === now ? " is-now" : ""}"
           title="${label}: ${m.confirmed ? `£${m.confirmed.toFixed(0)} paid` : ""}${
             m.confirmed && m.estimated ? " · " : ""}${
             m.estimated ? `~£${m.estimated.toFixed(0)} est.` : ""}">
        <span class="irail__amt num">${amount >= 5 ? `£${Math.round(amount)}` : ""}</span>
        <span class="irail__stack">
          ${m.confirmed ? `<i class="irail__seg irail__seg--paid" style="height:${Math.max(conf, 2)}%"></i>` : ""}
          ${m.estimated ? `<i class="irail__seg irail__seg--est" style="height:${Math.max(est, 2)}%"></i>` : ""}
        </span>
        <span class="irail__m">${idx % 3 === 0 ? label : ""}</span>
      </div>`;
  }).join("");

  const fmt = (d) => new Date(d).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  $("incRailEvents").innerHTML = `
    <p class="eyebrow">Next payments</p>
    ${(inc.next_events || []).slice(0, 7).map((e) => `
      <div class="irail__ev">
        <span class="irail__evdate num">${fmt(e.date)}</span>
        <span class="irail__evkey">${esc(e.key)}</span>
        <span class="irail__evamt num">${e.gbp != null
          ? `~${money(e.gbp)}`
          : `${Number(e.amount.toFixed(4))} ${esc(e.currency)}`}</span>
        <span class="irail__evtier">${e.tier === "declared_date" ? "declared" : "est."}</span>
      </div>`).join("")}
    ${inc.declared_gbp ? `<p class="irail__declared">Declared, unpaid: <b class="num">${money(inc.declared_gbp)}</b> (broker accrual)</p>` : ""}`;

  $("incRailTotal").textContent = inc.forward_12m_gbp != null
    ? `~${money(inc.forward_12m_gbp)} est. next 12m` : "";
  $("incRailNote").textContent = inc.ready_gbp
    ? "solid = paid · outline = estimated"
    : "feed down — per-share schedule only";
}

async function load() {
  let bench = "";
  try { bench = localStorage.getItem(BENCH_KEY) || ""; } catch { /* private mode */ }
  const [attr, trk, dsk] = await Promise.all([
    fetch(`${URL_ATTR}?t=${Date.now()}`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
    fetch(`${URL_TRACK}?symbol=${encodeURIComponent(bench)}&t=${Date.now()}`)
      .then((r) => (r.ok ? r.json() : null)).catch(() => null),
    fetch(`${URL_DESK}?t=${Date.now()}`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
  ]);
  data = attr;
  track = trk && trk.ready !== undefined ? trk : null;
  desk = dsk;
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

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    if (!loaded) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(settleCharts, 150);
  });

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
