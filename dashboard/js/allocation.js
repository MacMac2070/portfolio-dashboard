/* The Allocation view: the same holdings cut three ways.
 *
 * Region answers "where is this exposure", sector answers "what kind of
 * business is this", currency answers "what am I actually exposed to when
 * sterling moves". They are separate axes — XDJP is Japan by region and an ETF
 * by sector and both are correct — so this is one ring on a switch rather than
 * three rings side by side.
 *
 * One ring, not three, for a second reason: --cat-1..7 are assigned per entity
 * and never cycled, so two rings on one screen would make the same hue mean
 * "Hong Kong / China" here and "Semiconductors" there. Showing one axis at a
 * time keeps a colour meaning one thing at a time.
 *
 * The KPI strip above the ring swaps to the hovered slice — the extension
 * inspiration/shadcn-fintech-abderrahimghazali's notes propose, driven off the
 * same active state the ring already holds rather than a second mechanism.
 */
import { money, moneySigned, pct, pctSigned, esc, count } from "./format.js";
import { allocRing, catColor } from "./alloc.js";
import { squarify } from "./charts.js";

const $ = (id) => document.getElementById(id);
const setText = (node, text) => {
  if (node && node.textContent !== text) node.textContent = text;
};

/**
 * The three axes.
 *
 * `rows` picks the server's rollup; `field` is the matching key on a position,
 * so the KPI strip and the positions list can group on any axis with one code
 * path. Currency rows are keyed `code` rather than `name` — normalised here
 * because that is presentation, not arithmetic.
 */
const AXES = {
  region: {
    label: "Region",
    field: "region",
    rows: (d) => d.regions || [],
  },
  sector: {
    label: "Sector",
    field: "sector",
    rows: (d) => d.sectors || [],
  },
  currency: {
    label: "Currency",
    field: "currency",
    rows: (d) => (d.currencies || []).map((c) => ({ ...c, name: c.code })),
  },
};

let axis = "region";
let chartMode = "ring";
let ring = null;
let data = null;
let intent = null;    // /api/intent: operator targets + rule thresholds

/** Positions in a slice of the current axis, or all of them for null. */
function positionsIn(row) {
  const all = data?.positions || [];
  if (!row) return all;
  return all.filter((p) => p[AXES[axis].field] === row.name);
}

/**
 * The KPI strip.
 *
 * Idle it reads the whole portfolio; on a hovered slice it reads that slice.
 * Only the first cell's label moves — the other two keep their headings so the
 * strip reads as the same three measures narrowed, not three different ones.
 */
function paintKpi(row, total) {
  const held = positionsIn(row);
  const pnl = held.reduce((acc, p) => acc + (p.unrealised_gbp || 0), 0);
  const write = (key, text) => {
    const node = $("allocKpi").querySelector(`[data-k="${key}"]`);
    if (node && node.textContent !== text) node.textContent = text;
  };

  write("label", row ? row.name : "Invested");
  write("value", money(row ? row.value_gbp : total));
  write("count", count(held.length));
  write("pnl", moneySigned(pnl));

  const pnlNode = $("allocKpi").querySelector('[data-k="pnl"]');
  // Signed already; the class is the second cue, never the only one.
  pnlNode.className = `allockpi__v num ${pnl > 0 ? "pos" : pnl < 0 ? "neg" : "flat"}`;

  paintPositions(row);
}

/** The list currently on screen: the axis plus the order it is ranked in. */
let posKey = null;

/**
 * Every holding, largest first, with the slice it belongs to on the current
 * axis. Hovering a slice dims the rows outside it, so the ring and the list
 * are answering the same question at the same time.
 *
 * Called on every hover *and* every 3s live tick, so like everything else that
 * repaints on the tick it separates a rebuild from a patch. Rewriting this
 * list's innerHTML three times a second would drop text selection and the
 * row's own hover; on a hover it would do it on every pointer move.
 */
function paintPositions(activeRow) {
  const host = $("allocPositions");
  const all = data?.positions || [];
  if (!all.length) {
    host.innerHTML = "";
    posKey = null;
    return;
  }

  const field = AXES[axis].field;
  const rows = AXES[axis].rows(data);
  // Colour follows the slice, so a row's dot matches its arc.
  const hue = new Map(rows.map((r) => [r.name, catColor(r.color_index)]));
  const invested = data.kpis?.invested || 1;
  const ranked = [...all].sort((a, b) => b.value_gbp - a.value_gbp);
  const max = Math.max(...ranked.map((p) => p.value_gbp), 1);

  const key = `${axis}|${ranked.map((p) => p.con_id).join(",")}`;
  if (key !== posKey) {
    posKey = key;
    host.innerHTML = ranked.map((p) => {
      const slice = p[field] ?? "—";
      return `
        <div class="allocpos__row" data-con-id="${p.con_id}" data-slice="${esc(slice)}">
          <span class="allocpos__sym">${esc(p.symbol)}</span>
          <span class="allocpos__name">${esc(p.name)}</span>
          <span class="allocpos__slice">
            <i style="background:${hue.get(slice) || "var(--text-3)"}"></i>${esc(slice)}
          </span>
          <span class="allocpos__pct num" data-f="pct"></span>
          <span class="allocpos__val num" data-f="val"></span>
          <span class="allocpos__bar"><i></i></span>
        </div>`;
    }).join("");
  }

  // Figures and the dim state are patched either way — the figures move on
  // every tick, and the dim state moves on every hover.
  ranked.forEach((p) => {
    const row = host.querySelector(`.allocpos__row[data-con-id="${p.con_id}"]`);
    if (!row) return;
    const dim = Boolean(activeRow) && row.dataset.slice !== activeRow.name;
    if (row.classList.contains("is-dim") !== dim) row.classList.toggle("is-dim", dim);
    setText(row.querySelector('[data-f="pct"]'), pct((p.value_gbp / invested) * 100));
    setText(row.querySelector('[data-f="val"]'), money(p.value_gbp));
    const fill = row.querySelector(".allocpos__bar i");
    const width = `${(p.value_gbp / max) * 100}%`;
    if (fill && fill.style.width !== width) fill.style.width = width;
  });

  setText($("allocPosNote"),
    `${count(ranked.length)} positions · by ${AXES[axis].label.toLowerCase()}`);
}

function render() {
  if (!data) return;
  const rows = AXES[axis].rows(data);
  const host = $("allocPositions");

  // An axis whose rollup the adapter did not send. Name what supplies it
  // rather than drawing an empty ring — a server started before the rollup
  // existed keeps serving payloads without it until it is restarted.
  if (!rows.length) {
    $("allocViewLegend").innerHTML = "";
    $("allocDonut").replaceChildren();
    const what = esc(AXES[axis].label.toLowerCase());
    host.innerHTML = `
      <div class="collecting">
        <h3>No ${what} breakdown yet</h3>
        <p>This payload carries no ${what} split. Restart <code>serve.py</code>,
           or run <code>adapter/build.py</code>, to fill it in.</p>
      </div>`;
    $("allocPosNote").textContent = "";
    posKey = null;
    return;
  }

  if (chartMode === "map") {
    $("allocSplit").hidden = true;
    $("allocMap").hidden = false;
    renderMap(rows);
    // The ring still holds the shared active state; keep its data current so
    // the KPI swap has rows to read even while the svg is hidden.
    ring.render(rows, data.kpis?.invested ?? 0);
  } else {
    $("allocSplit").hidden = false;
    $("allocMap").hidden = true;
    ring.render(rows, data.kpis?.invested ?? 0);
  }
  renderConcentration(rows);
  renderRules();
}

/* ---------------- treemap ----------------
 * Cells are positions sized by value; the border hue names the slice the
 * position belongs to on the current axis, and the printed figures — ticker,
 * weight, signed return — carry the reading. The P&L tint is a low-alpha
 * second cue, never the encoding. Hovering a cell drives the same active
 * state the arcs and legend do, so the KPI strip and positions list follow. */
function renderMap(rows) {
  const host = $("allocMap");
  const all = [...(data?.positions || [])].sort((a, b) => b.value_gbp - a.value_gbp);
  if (!all.length) { host.innerHTML = ""; return; }

  const field = AXES[axis].field;
  const sliceIndex = new Map(rows.map((r, i) => [r.name, i]));
  const hue = new Map(rows.map((r) => [r.name, catColor(r.color_index)]));
  const invested = data.kpis?.invested || 1;

  const rects = squarify(all.map((p) => p.value_gbp));
  host.innerHTML = rects.map(({ i, x, y, w, h }) => {
    const p = all[i];
    const slice = p[field] ?? "—";
    const ret = p.unrealised_pct;
    const big = w * h > 0.02;
    return `
      <div class="allocmap__cell" tabindex="0"
           data-slice-i="${sliceIndex.get(slice) ?? ""}"
           style="left:${(x * 100).toFixed(2)}%; top:${(y * 100).toFixed(2)}%;
                  width:${(w * 100).toFixed(2)}%; height:${(h * 100).toFixed(2)}%;
                  border-color:${hue.get(slice) || "var(--border-strong)"};
                  background:${ret > 0 ? "var(--pos-bg)" : ret < 0 ? "var(--neg-bg)" : "var(--wash-1)"}">
        <span class="allocmap__sym">${esc(p.symbol)}</span>
        ${big ? `<span class="allocmap__figs num">${pct((p.value_gbp / invested) * 100)}
                 · ${Number.isFinite(ret) ? pctSigned(ret) : "—"}</span>` : ""}
      </div>`;
  }).join("");

  host.onpointerover = (e) => {
    const cell = e.target.closest(".allocmap__cell");
    const i = cell?.dataset.sliceI;
    if (i !== undefined && i !== "") ring.setActive(Number(i));
  };
  host.onpointerout = () => ring.setActive(null);
  host.onfocusin = host.onpointerover;
  host.onfocusout = host.onpointerout;
}

/* ---------------- concentration ----------------
 * Slice weights on the current axis against a single cap — the config-free
 * answer to "am I too heavy anywhere". The rules card handles the position
 * grain; this one handles the slice grain, and the cap marker on every bar
 * keeps the threshold visible even when nothing breaches it. */
function renderConcentration(rows) {
  const host = $("concBody");
  if (!host || !rows.length) { if (host) host.innerHTML = ""; return; }

  const cap = intent?.rules?.max_slice_pct ?? 35;
  const ranked = [...rows].sort((a, b) => b.weight_pct - a.weight_pct);
  const span = Math.max(ranked[0].weight_pct, cap) * 1.12;

  host.innerHTML = ranked.map((r) => {
    const heavy = r.weight_pct > cap;
    return `
      <div class="conc__row${heavy ? " is-heavy" : ""}">
        <span class="conc__dot" style="background:${catColor(r.color_index)}"></span>
        <span class="conc__name">${esc(r.name)}</span>
        <span class="conc__bar">
          <i class="conc__fill" style="width:${(r.weight_pct / span) * 100}%"></i>
          <i class="conc__cap" style="left:${(cap / span) * 100}%"></i>
        </span>
        <span class="conc__pct num">${pct(r.weight_pct, 1)}</span>
        <span class="conc__val num">${money(r.value_gbp)}</span>
        <span class="conc__flag">${heavy ? "▲ heavy" : ""}</span>
      </div>`;
  }).join("");

  const top2 = ranked.slice(0, 2).reduce((s2, r) => s2 + r.weight_pct, 0);
  $("concNote").textContent =
    `cap ${cap}% ${intent?.rules_source === "config" ? "(config)" : "(default)"}`
    + ` · top ${pct(ranked[0].weight_pct, 0)} · top 2 ${pct(top2, 0)}`;
}

/* ---------------- rules ---------------- */
const HHI = (rows, total) =>
  rows.reduce((s, p) => s + ((p.value_gbp / total) * 100) ** 2, 0);

function renderRules() {
  const host = $("rulesBody");
  if (!host || !data) return;
  const rules = intent?.rules || {};
  const positions = data.positions || [];
  const invested = data.kpis?.invested || 0;
  const nav = data.kpis?.net_liquidation || 0;

  const rows = [];
  const add = (label, value, limit, ok, fmtV = (v) => pct(v, 1)) =>
    rows.push({ label, value: fmtV(value), limit, ok });

  if (rules.max_position_pct != null && positions.length && invested) {
    const top = positions.reduce((a, b) => (a.value_gbp > b.value_gbp ? a : b));
    const w = (top.value_gbp / invested) * 100;
    add(`Largest position (${top.symbol})`, w, `≤ ${rules.max_position_pct}%`,
        w <= rules.max_position_pct);
  }
  if (rules.cash_floor_pct != null && nav) {
    const cash = ((data.kpis?.cash_available || 0) / nav) * 100;
    add("Cash", cash, `≥ ${rules.cash_floor_pct}%`, cash >= rules.cash_floor_pct);
  }
  for (const [ccy, cap] of Object.entries(rules.currency_band || {})) {
    const row = (data.currencies || []).find((c) => c.code === ccy);
    const w = row?.weight_pct ?? 0;
    add(`${ccy} exposure`, w, `≤ ${cap}%`, w <= cap);
  }

  // The concentration figures: computed, not thresholds.
  if (positions.length && invested) {
    const hhi = HHI(positions, invested);
    rows.push({ label: "Effective positions", value: (10000 / hhi).toFixed(1),
                limit: `of ${positions.length}`, ok: null });
    rows.push({ label: "HHI", value: Math.round(hhi).toString(), limit: "", ok: null });
  }

  host.innerHTML = rows.map((r) => `
    <div class="rules__row">
      <span class="rules__state ${r.ok === false ? "is-breach" : r.ok === true ? "is-ok" : ""}"
            aria-hidden="true">${r.ok === false ? "▲" : r.ok === true ? "●" : "·"}</span>
      <span class="rules__label">${esc(r.label)}</span>
      <span class="rules__value num">${esc(r.value)}</span>
      <span class="rules__limit num">${esc(r.limit)}</span>
      <span class="rules__word">${r.ok === false ? "breach" : r.ok === true ? "ok" : ""}</span>
    </div>`).join("");
  $("rulesNote").textContent = intent?.rules_source === "config"
    ? "thresholds from config.local.json" : "default thresholds · set yours in config.local.json";
}

/** Called by main.js whenever fresh figures land, live tick included. */
export function update(next) {
  data = next;
  if ($("view-allocation")?.hidden) return;   // repaint on show instead
  render();
}

/** The axis named in `#allocation/<axis>`, or null. */
function fromHash() {
  const part = (location.hash || "").replace(/^#/, "").split("/")[1];
  return part && AXES[part] ? part : null;
}

export function route() {
  const wanted = fromHash();
  if (wanted) setAxis(wanted);
  render();
}

export function init() {
  const svg = $("allocDonut");
  const legend = $("allocViewLegend");
  if (!svg || !legend) return;

  ring = allocRing({ svg, legend, caption: "Invested", onActive: paintKpi });

  // A hash change while the view is already showing — clicking "View all" on
  // Overview's currency card from the region axis, say — has to move the axis.
  window.addEventListener("hashchange", () => {
    if (!$("view-allocation")?.hidden) route();
  });

  $("allocMode")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-mode]");
    if (!button || button.dataset.mode === chartMode) return;
    chartMode = button.dataset.mode;
    for (const other of $("allocMode").querySelectorAll("[data-mode]")) {
      other.setAttribute("aria-pressed", String(other === button));
    }
    render();
  });

  // Operator intent: fetched once per session — it changes when the config
  // file does, which is not while the page is open.
  fetch("api/intent").then((r) => (r.ok ? r.json() : null))
    .then((d) => { intent = d; if (!$("view-allocation")?.hidden) render(); })
    .catch(() => {});

  $("allocAxis")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-axis]");
    if (!button || button.dataset.axis === axis) return;
    axis = button.dataset.axis;
    for (const other of $("allocAxis").querySelectorAll("[data-axis]")) {
      other.setAttribute("aria-pressed", String(other === button));
    }
    // The hash carries the axis, so #allocation/sector is linkable and the two
    // "View all" links on Overview can land on the axis they came from.
    const want = `#allocation/${axis}`;
    if (location.hash !== want) history.replaceState(null, "", want);
    render();
  });
}

/** Point the view at an axis from the hash. Unknown values keep the current. */
export function setAxis(name) {
  if (!name || !AXES[name] || name === axis) return;
  axis = name;
  for (const other of $("allocAxis")?.querySelectorAll("[data-axis]") || []) {
    other.setAttribute("aria-pressed", String(other.dataset.axis === axis));
  }
}
