/* SVG chart primitives: sparkline, donut, equity curve, price chart.
 *
 * Built to the dataviz rules that apply here:
 *   - 2px lines, recessive gridlines, restrained axes
 *   - a 2px surface-coloured gap between donut segments
 *   - one axis, never two scales
 *   - identity never by colour alone: the donut ships with a labelled legend
 *   - crosshair + tooltip on the equity curve
 *   - draw-on uses stroke-dashoffset; nothing animates layout
 */

const NS = "http://www.w3.org/2000/svg";

function el(name, attrs = {}) {
  const node = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) {
    if (value != null) node.setAttribute(key, String(value));
  }
  return node;
}

/**
 * Gridline values a person would have chosen: multiples of 1, 2, 2.5 or 5 times
 * a power of ten, so the axis reads 0 / 20,000 / 40,000 rather than 22,149 /
 * 45,874. Zero always lands on a tick when it is inside the domain, which keeps
 * the axis from printing a line a hair away from the chart's own zero rule.
 */
function niceTicks(lo, hi, target = 5) {
  const span = hi - lo;
  if (!(span > 0)) return [lo];
  const raw = span / Math.max(1, target);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].find((m) => raw <= m * mag) * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) {
    out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  }
  return out;
}

function extent(values) {
  let lo = Infinity, hi = -Infinity;
  for (const v of values) { if (v < lo) lo = v; if (v > hi) hi = v; }
  if (!Number.isFinite(lo)) return [0, 1];
  if (lo === hi) { lo -= 1; hi += 1; }
  return [lo, hi];
}

/* ---------------- sparkline ---------------- */

/**
 * Small trend line for a table row or mover. Scaled to its own extent — a
 * sparkline shows shape, not level, so it carries no axis and no labels.
 */
export function sparkline(svg, series, { direction = "flat" } = {}) {
  svg.replaceChildren();
  if (!series || series.length < 2) return;

  const w = 100, h = 32, pad = 3;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "none");

  const [lo, hi] = extent(series);
  const x = (i) => (i / (series.length - 1)) * w;
  const y = (v) => h - pad - ((v - lo) / (hi - lo)) * (h - pad * 2);

  const stroke = direction === "up" ? "var(--pos)"
    : direction === "down" ? "var(--neg)" : "var(--flat)";

  const points = series.map((v, i) => `${x(i).toFixed(2)},${y(v).toFixed(2)}`);
  const id = `sparkfill-${Math.random().toString(36).slice(2, 9)}`;

  const defs = el("defs");
  const grad = el("linearGradient", { id, x1: 0, y1: 0, x2: 0, y2: 1 });
  grad.append(
    el("stop", { offset: "0%", "stop-color": stroke, "stop-opacity": 0.28 }),
    el("stop", { offset: "100%", "stop-color": stroke, "stop-opacity": 0 }),
  );
  defs.append(grad);

  svg.append(
    defs,
    el("polygon", {
      points: `0,${h} ${points.join(" ")} ${w},${h}`,
      fill: `url(#${id})`,
    }),
    el("polyline", {
      points: points.join(" "),
      fill: "none", stroke,
      "stroke-width": 1.6,
      "stroke-linejoin": "round", "stroke-linecap": "round",
      "vector-effect": "non-scaling-stroke",
    }),
  );
}

/* ---------------- donut ---------------- */

/**
 * Allocation ring. Segments are separated by a real gap in the surface colour
 * rather than a stroke, so adjacent slices stay distinguishable even when two
 * hues are close.
 */
export function donut(svg, rows, { centreValue, centreCaption } = {}) {
  svg.replaceChildren();
  const size = 240, r = 92, thickness = 26;
  const cx = size / 2, cy = size / 2;
  svg.setAttribute("viewBox", `0 0 ${size} ${size}`);

  const total = rows.reduce((sum, row) => sum + Math.max(0, row.value), 0);
  if (total <= 0) return;

  const circumference = 2 * Math.PI * r;
  const gap = 3;                       // px of surface between segments
  let offset = 0;

  const group = el("g", { transform: `rotate(-90 ${cx} ${cy})` });

  for (const row of rows) {
    const share = Math.max(0, row.value) / total;
    const length = Math.max(share * circumference - gap, 1);

    const arc = el("circle", {
      class: "donut__seg",
      cx, cy, r,
      fill: "none",
      stroke: row.color,
      "stroke-width": thickness,
      "stroke-dasharray": `${length} ${circumference - length}`,
      "stroke-dashoffset": -offset,
    });
    const title = el("title");
    title.textContent = `${row.label} — ${row.display}`;
    arc.append(title);
    group.append(arc);

    offset += share * circumference;
  }

  svg.append(group);

  if (centreValue) {
    const value = el("text", {
      class: "donut__value", x: cx, y: cy - 2,
      "text-anchor": "middle", "dominant-baseline": "middle",
    });
    value.textContent = centreValue;
    svg.append(value);
  }
  if (centreCaption) {
    const caption = el("text", {
      class: "donut__caption", x: cx, y: cy + 26, "text-anchor": "middle",
    });
    caption.textContent = centreCaption;
    svg.append(caption);
  }
}

/* ---------------- equity curve ---------------- */

/**
 * Portfolio value over time. One y-axis, four gridlines, five date ticks, and
 * a crosshair that snaps to the nearest point.
 *
 * `points` is [{ date: 'YYYY-MM-DD', value: Number }, ...] oldest first.
 *
 * `benchmark` is an optional array the SAME LENGTH as `points`, holding an
 * index already rebased onto this series' starting value by the server (see
 * adapter/benchmark.py — the page does no financial arithmetic of its own).
 * A null means the index had no close on that date and the line breaks there
 * rather than bridging a gap it has no data for. Both series share the one
 * axis; there is never a second scale.
 */
export function equityCurve(svg, points, {
  tooltip, formatValue, formatDate, benchmark = null, benchmarkName = "Benchmark",
} = {}) {
  svg.replaceChildren();
  if (!points || points.length < 2) return;

  // A benchmark of the wrong length would shear the two series apart — every
  // point would be plotted against the wrong date. Drop it rather than draw a
  // chart that is subtly and invisibly wrong.
  if (benchmark && benchmark.length !== points.length) benchmark = null;

  // The viewBox tracks the element's real pixel box rather than a fixed
  // 780x260. With a fixed box the SVG has to be stretched to fit, and a
  // non-uniform stretch squashes the axis *text* — vector-effect protects
  // stroke width, not glyphs. Since the plot is now viewport-sized and can be
  // ~120px tall, that distortion would be plainly visible.
  const box = svg.getBoundingClientRect();
  const w = Math.max(320, Math.round(box.width) || 780);
  const h = Math.max(90, Math.round(box.height) || 260);

  // Chrome enough for labels, but proportionally less when the plot is short.
  //
  // padL sizes the y-label gutter. The labels are monospace now (--font-figure,
  // 9.5px, ~5.7px per glyph) and right-anchored at padL - 8, so the gutter has
  // to fit the widest label the axis can ever print. At 52 that was 44px of
  // usable space: "£55,073" fits at 39.9px, but "£100,000" would not, and this
  // axis auto-scales with the portfolio. 60 buys room to "£1,000,000" and costs
  // 8px of plot width.
  const padL = 60, padR = 12;
  const padT = Math.min(14, h * 0.06);
  const padB = Math.min(26, h * 0.12) + 10;

  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  const values = points.map((p) => p.value);
  // The benchmark shares the axis, so it has to be inside the domain or it
  // would be clipped at the plot edge and read as flat.
  const benchValues = (benchmark || []).filter(Number.isFinite);
  let [lo, hi] = extent(benchValues.length ? values.concat(benchValues) : values);
  const headroom = (hi - lo) * 0.12;
  lo -= headroom; hi += headroom;

  const x = (i) => padL + (i / (points.length - 1)) * (w - padL - padR);
  const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB);

  const rising = values[values.length - 1] >= values[0];
  const stroke = rising ? "var(--pos)" : "var(--neg)";
  const id = `curve-${Math.random().toString(36).slice(2, 9)}`;

  // gridlines + y labels — fewer of them when the plot is short, so the
  // labels never collide
  const grid = el("g");
  const ticks = h < 150 ? 2 : h < 220 ? 3 : 4;
  for (let i = 0; i <= ticks; i++) {
    const value = lo + ((hi - lo) * i) / ticks;
    const yy = y(value);
    grid.append(el("line", {
      class: "plot__grid", x1: padL, x2: w - padR, y1: yy, y2: yy,
      "stroke-dasharray": "2 4",
    }));
    const label = el("text", {
      class: "plot__axis", x: padL - 8, y: yy + 3, "text-anchor": "end",
    });
    label.textContent = formatValue ? formatValue(value) : value.toFixed(0);
    grid.append(label);
  }
  svg.append(grid);

  // x labels — a handful, never one per point
  const xLabels = el("g");
  const xTicks = w < 460 ? 2 : 4;
  const stride = Math.max(1, Math.floor((points.length - 1) / xTicks));
  for (let i = 0; i < points.length; i += stride) {
    const label = el("text", {
      class: "plot__axis", x: x(i), y: h - 6,
      "text-anchor": i === 0 ? "start" : "middle",
    });
    label.textContent = formatDate ? formatDate(points[i].date) : points[i].date;
    xLabels.append(label);
  }
  svg.append(xLabels);

  const path = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(2)},${y(p.value).toFixed(2)}`).join(" ");

  const defs = el("defs");
  const grad = el("linearGradient", { id, x1: 0, y1: 0, x2: 0, y2: 1 });
  grad.append(
    el("stop", { offset: "0%", "stop-color": stroke, "stop-opacity": 0.24 }),
    el("stop", { offset: "100%", "stop-color": stroke, "stop-opacity": 0 }),
  );
  defs.append(grad);
  svg.append(defs);

  svg.append(el("path", {
    d: `${path} L${x(points.length - 1)},${h - padB} L${padL},${h - padB} Z`,
    fill: `url(#${id})`, stroke: "none",
  }));

  // Benchmark under the portfolio line: it is context, not the subject, so it
  // is dashed, unfilled and recessive. Drawn in segments so a null breaks the
  // line rather than bridging a session the index did not trade. Same
  // treatment priceChart gives the stock page's benchmark.
  if (benchmark && benchValues.length > 1) {
    let d = "";
    let open = false;
    benchmark.forEach((v, i) => {
      if (!Number.isFinite(v)) { open = false; return; }
      d += `${open ? "L" : "M"}${x(i).toFixed(2)},${y(v).toFixed(2)} `;
      open = true;
    });
    svg.append(el("path", {
      class: "plot__bench", d: d.trim(), fill: "none",
      "vector-effect": "non-scaling-stroke",
    }));
  }

  const line = el("path", {
    class: "plot__line plot__line--draw", d: path, stroke,
    "vector-effect": "non-scaling-stroke",
  });
  svg.append(line);
  requestAnimationFrame(() => {
    try { line.style.setProperty("--len", line.getTotalLength()); } catch { /* no layout yet */ }
  });

  // The shape as a sentence, so the chart is not silent to a screen reader.
  const firstV = values[0], lastV = values[values.length - 1];
  const fmtPlain = (v) => (formatValue ? formatValue(v, true) : String(v));
  const summary = el("title");
  summary.textContent =
    `Portfolio value over ${points.length} days, ${lastV >= firstV ? "up" : "down"} from `
    + `${fmtPlain(firstV)} to ${fmtPlain(lastV)}.`
    + (benchValues.length > 1
        ? ` ${benchmarkName}, rebased to the same start, ends at `
          + `${fmtPlain(benchValues[benchValues.length - 1])}.`
        : "");
  svg.append(summary);

  if (!tooltip) return;

  const crosshair = el("line", { class: "plot__crosshair", y1: padT, y2: h - padB, opacity: 0 });
  const dot = el("circle", { class: "plot__dot", r: 4.5, fill: stroke, opacity: 0 });
  const benchDot = el("circle", { class: "plot__dot plot__dot--bench", r: 3.5, opacity: 0 });
  svg.append(crosshair, dot, benchDot);

  const hit = el("rect", {
    x: padL, y: 0, width: w - padL - padR, height: h,
    fill: "transparent", style: "cursor:crosshair",
  });
  svg.append(hit);

  const move = (event) => {
    const box = svg.getBoundingClientRect();
    const ratio = (event.clientX - box.left) / box.width;
    const index = Math.round(((ratio * w) - padL) / (w - padL - padR) * (points.length - 1));
    const i = Math.min(points.length - 1, Math.max(0, index));
    const point = points[i];

    crosshair.setAttribute("x1", x(i));
    crosshair.setAttribute("x2", x(i));
    crosshair.setAttribute("opacity", 1);
    dot.setAttribute("cx", x(i));
    dot.setAttribute("cy", y(point.value));
    dot.setAttribute("opacity", 1);

    const bv = benchmark?.[i];
    if (Number.isFinite(bv)) {
      benchDot.setAttribute("cx", x(i));
      benchDot.setAttribute("cy", y(bv));
      benchDot.setAttribute("opacity", 1);
    } else {
      benchDot.setAttribute("opacity", 0);
    }

    const fmt = (v) => (formatValue ? formatValue(v, true) : String(v));
    tooltip.dataset.open = "true";
    tooltip.innerHTML =
      `<div class="tip__date">${formatDate ? formatDate(point.date) : point.date}</div>` +
      `<div class="tip__val">${fmt(point.value)}</div>` +
      (Number.isFinite(bv)
        ? `<div class="tip__bench">${benchmarkName} ${fmt(bv)}</div>` : "");
    tooltip.style.left = `${Math.min(event.clientX + 14, window.innerWidth - tooltip.offsetWidth - 8)}px`;
    tooltip.style.top = `${event.clientY - 8}px`;
  };

  const leave = () => {
    crosshair.setAttribute("opacity", 0);
    dot.setAttribute("opacity", 0);
    benchDot.setAttribute("opacity", 0);
    tooltip.dataset.open = "false";
  };

  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerleave", leave);
}

/* ---------------- price chart ---------------- */

/**
 * One instrument's price over a chosen range, with an optional rebased
 * benchmark. Separate from `equityCurve` on purpose — Overview depends on that
 * one and it has different needs (dated points, colour derived from the data).
 * Here:
 *
 *   - `labels` are provider timestamps, formatted by the caller. An intraday
 *     range labels times, a daily range labels dates, and the chart should not
 *     have to know which it is holding.
 *   - colour comes from `direction`, not from the series. The header prints the
 *     range change beside the chart; if the line derived its own colour the two
 *     could disagree at the boundary and one of them would be lying.
 *   - the benchmark is already rebased onto this instrument's first close by the
 *     backend, so both series share one axis. Never a second scale.
 *
 * `series` is a plain number array; `benchmark` may hold nulls where the index
 * had no matching session, and the line breaks rather than bridging them.
 */
export function priceChart(svg, {
  series, labels, benchmark = null, direction = "flat",
  formatValue, formatLabel, tooltip, seriesName = "Price", benchmarkName = "Benchmark",
} = {}) {
  svg.replaceChildren();
  if (!series || series.length < 2) return;

  const box = svg.getBoundingClientRect();
  const w = Math.max(320, Math.round(box.width) || 780);
  const h = Math.max(120, Math.round(box.height) || 292);

  // Same monospace-gutter arithmetic as equityCurve above. The widest label
  // this axis prints is a four-figure native price with a two-glyph symbol —
  // "HK$4,296" at 45.6px — which left 0.4px of clearance in the old 46px
  // gutter. 60 restores a real margin.
  const padL = 60, padR = 14;
  const padT = Math.min(16, h * 0.06);
  const padB = Math.min(26, h * 0.1) + 12;

  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  // Both series share the extent, which is what makes one axis honest.
  const benchValues = (benchmark || []).filter((v) => Number.isFinite(v));
  let [lo, hi] = extent([...series, ...benchValues]);
  const headroom = (hi - lo) * 0.12;
  lo -= headroom; hi += headroom;

  const x = (i) => padL + (i / (series.length - 1)) * (w - padL - padR);
  const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB);

  const stroke = direction === "down" ? "var(--loss-soft)"
    : direction === "up" ? "var(--gain-soft)" : "var(--flat)";
  const id = `px-${Math.random().toString(36).slice(2, 9)}`;

  const grid = el("g");
  const ticks = h < 170 ? 2 : h < 240 ? 3 : 4;
  for (let i = 0; i <= ticks; i++) {
    const value = lo + ((hi - lo) * i) / ticks;
    const yy = y(value);
    grid.append(el("line", {
      class: "plot__grid", x1: padL, x2: w - padR, y1: yy, y2: yy,
      "stroke-dasharray": "2 4",
    }));
    const label = el("text", {
      class: "plot__axis", x: padL - 8, y: yy + 3, "text-anchor": "end",
    });
    label.textContent = formatValue ? formatValue(value) : value.toFixed(0);
    grid.append(label);
  }
  svg.append(grid);

  const xLabels = el("g");
  const xTicks = w < 460 ? 2 : 4;
  const stride = Math.max(1, Math.floor((series.length - 1) / xTicks));
  for (let i = 0; i < series.length; i += stride) {
    const label = el("text", {
      class: "plot__axis", x: x(i), y: h - 7,
      "text-anchor": i === 0 ? "start" : "middle",
    });
    label.textContent = formatLabel ? formatLabel(labels?.[i], i) : (labels?.[i] ?? "");
    xLabels.append(label);
  }
  svg.append(xLabels);

  const path = series
    .map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(2)},${y(v).toFixed(2)}`)
    .join(" ");

  const defs = el("defs");
  const grad = el("linearGradient", { id, x1: 0, y1: 0, x2: 0, y2: 1 });
  grad.append(
    el("stop", { offset: "0%", "stop-color": stroke, "stop-opacity": 0.24 }),
    el("stop", { offset: "100%", "stop-color": stroke, "stop-opacity": 0 }),
  );
  defs.append(grad);
  svg.append(defs);

  svg.append(el("path", {
    d: `${path} L${x(series.length - 1)},${h - padB} L${padL},${h - padB} Z`,
    fill: `url(#${id})`, stroke: "none",
  }));

  // Benchmark under the instrument line: it is context, not the subject. Drawn
  // in segments so a null (index closed that session) breaks the line instead
  // of drawing a straight bridge across a gap that has no data.
  if (benchmark && benchValues.length > 1) {
    let d = "";
    let open = false;
    benchmark.forEach((v, i) => {
      if (!Number.isFinite(v)) { open = false; return; }
      d += `${open ? "L" : "M"}${x(i).toFixed(2)},${y(v).toFixed(2)} `;
      open = true;
    });
    svg.append(el("path", {
      class: "plot__bench", d: d.trim(), fill: "none",
      "vector-effect": "non-scaling-stroke",
    }));
  }

  const line = el("path", {
    class: "plot__line plot__line--draw", d: path, stroke,
    "vector-effect": "non-scaling-stroke",
  });
  svg.append(line);
  requestAnimationFrame(() => {
    try { line.style.setProperty("--len", line.getTotalLength()); } catch { /* no layout yet */ }
  });

  // A screen reader gets the shape as a sentence; the chart is not the only
  // route to the number, but it should not be silent either.
  const first = series[0], last = series[series.length - 1];
  const move = last >= first ? "up" : "down";
  const title = el("title");
  title.textContent =
    `${seriesName} over ${series.length} points, ${move} from ` +
    `${formatValue ? formatValue(first) : first} to ${formatValue ? formatValue(last) : last}.`;
  svg.append(title);

  if (!tooltip) return;

  const crosshair = el("line", { class: "plot__crosshair", y1: padT, y2: h - padB, opacity: 0 });
  const dot = el("circle", { class: "plot__dot", r: 4.5, fill: stroke, opacity: 0 });
  const benchDot = el("circle", { class: "plot__dot plot__dot--bench", r: 3.5, opacity: 0 });
  svg.append(crosshair, dot, benchDot);

  const hit = el("rect", {
    x: padL, y: 0, width: w - padL - padR, height: h,
    fill: "transparent", style: "cursor:crosshair",
  });
  svg.append(hit);

  const onMove = (event) => {
    const rect = svg.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / rect.width;
    const raw = Math.round(((ratio * w) - padL) / (w - padL - padR) * (series.length - 1));
    const i = Math.min(series.length - 1, Math.max(0, raw));

    crosshair.setAttribute("x1", x(i));
    crosshair.setAttribute("x2", x(i));
    crosshair.setAttribute("opacity", 1);
    dot.setAttribute("cx", x(i));
    dot.setAttribute("cy", y(series[i]));
    dot.setAttribute("opacity", 1);

    const bv = benchmark?.[i];
    if (Number.isFinite(bv)) {
      benchDot.setAttribute("cx", x(i));
      benchDot.setAttribute("cy", y(bv));
      benchDot.setAttribute("opacity", 1);
    } else {
      benchDot.setAttribute("opacity", 0);
    }

    const fmt = (v) => (formatValue ? formatValue(v, true) : String(v));
    tooltip.dataset.open = "true";
    tooltip.innerHTML =
      `<div class="tip__date">${formatLabel ? formatLabel(labels?.[i], i, true) : (labels?.[i] ?? "")}</div>` +
      `<div class="tip__val">${fmt(series[i])}</div>` +
      (Number.isFinite(bv)
        ? `<div class="tip__bench">${benchmarkName} ${fmt(bv)}</div>` : "");
    tooltip.style.left =
      `${Math.min(event.clientX + 14, window.innerWidth - tooltip.offsetWidth - 8)}px`;
    tooltip.style.top = `${event.clientY - 8}px`;
  };

  const onLeave = () => {
    crosshair.setAttribute("opacity", 0);
    dot.setAttribute("opacity", 0);
    benchDot.setAttribute("opacity", 0);
    tooltip.dataset.open = "false";
  };

  hit.addEventListener("pointermove", onMove);
  hit.addEventListener("pointerleave", onLeave);
}

/* ---------------- grouped columns ---------------- */

/**
 * A statement trend: magnitude across a handful of discrete periods.
 *
 * Columns, not a line. Four annual figures are categories, not a continuum —
 * there is nothing between 2023 and 2024 to interpolate, and a line would
 * imply there is.
 *
 * One shared linear axis that always contains zero, and never a second scale.
 * Revenue against net income really is two orders of magnitude apart — INTC
 * 2025 is 52,853 against −267 — and a dual axis would fake a comparability the
 * numbers do not have. The zero line is drawn distinctly instead, so a loss
 * reads as direction rather than as a very short positive bar, and the caller
 * prints the margin as text where the second axis would have been.
 *
 * `series` is [{ key, label, color, values }], 1–3 entries, each `values` the
 * same length as `periods`. A null draws NO bar, rather than a zero-height one
 * at the baseline — "did not report" must not look like "reported zero".
 */
export function groupedBars(svg, {
  periods, series, formatValue, formatPeriod, tooltip, chartLabel = "Trend",
} = {}) {
  svg.replaceChildren();
  const live = (series || []).filter((s) => s.values?.some(Number.isFinite));
  if (!periods?.length || !live.length) return;

  const box = svg.getBoundingClientRect();
  const w = Math.max(320, Math.round(box.width) || 780);
  const h = Math.max(120, Math.round(box.height) || 260);
  const padL = 62, padR = 14, padT = 14, padB = 32;

  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  const all = live.flatMap((s) => s.values).filter(Number.isFinite);
  let lo = Math.min(0, ...all);          // zero is always in the domain
  let hi = Math.max(0, ...all);
  if (lo === hi) hi = lo + 1;
  const pad = (hi - lo) * 0.08;
  if (lo < 0) lo -= pad;
  if (hi > 0) hi += pad;

  const plotW = w - padL - padR;
  const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB);
  const zeroY = y(0);

  const grid = el("g");
  for (const value of niceTicks(lo, hi, h < 200 ? 3 : 5)) {
    const yy = y(value);
    if (yy < padT - 1 || yy > h - padB + 1) continue;
    grid.append(el("line", {
      class: "plot__grid", x1: padL, x2: w - padR, y1: yy, y2: yy,
      "stroke-dasharray": "2 4",
    }));
    const label = el("text", {
      class: "plot__axis", x: padL - 8, y: yy + 3, "text-anchor": "end",
    });
    label.textContent = formatValue ? formatValue(value) : Math.round(value);
    grid.append(label);
  }
  svg.append(grid);

  // Drawn after the gridlines and distinctly: it is the reference every bar is
  // read against, not another tick.
  svg.append(el("line", {
    class: "plot__zero", x1: padL, x2: w - padR, y1: zeroY, y2: zeroY,
  }));

  const gw = plotW / periods.length;
  const band = gw * 0.68;
  const bw = band / live.length;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  periods.forEach((label, p) => {
    const gx = padL + gw * p + (gw - band) / 2;
    const text = el("text", {
      class: "plot__axis", x: padL + gw * p + gw / 2, y: h - 10,
      "text-anchor": "middle",
    });
    text.textContent = formatPeriod ? formatPeriod(label, p) : label;
    svg.append(text);

    live.forEach((s, i) => {
      const v = s.values[p];
      if (!Number.isFinite(v)) return;      // no bar at all — see the docstring
      const top = Math.min(y(v), zeroY);
      const height = Math.max(1, Math.abs(y(v) - zeroY));
      // width - 2 gives the 2px surface gap between adjacent bars without
      // stroking them, the same technique donut() uses for its segments.
      const rect = el("rect", {
        x: gx + bw * i, y: top, width: Math.max(1, bw - 2), height,
        rx: 2, fill: s.color,
      });
      const title = el("title");
      title.textContent =
        `${label} · ${s.label} ${formatValue ? formatValue(v, true) : v}`;
      rect.append(title);
      if (!reduce) {
        // transform only, anchored at the zero line so a negative bar grows
        // downward rather than flipping through it.
        rect.style.transformOrigin = `0px ${zeroY}px`;
        rect.style.animation = "barGrow 320ms cubic-bezier(.22,1,.36,1) both";
        rect.style.animationDelay = `${p * 40}ms`;
      }
      svg.append(rect);
    });
  });

  const summary = el("title");
  const last = live[0].values.findIndex(Number.isFinite);
  summary.textContent =
    `${chartLabel}: ${live.map((s) => s.label).join(", ")} across ${periods.length} periods` +
    (last >= 0 ? `, latest ${live[0].label} ${formatValue ? formatValue(live[0].values[last], true) : ""}` : "");
  svg.append(summary);

  if (!tooltip) return;

  const hover = el("rect", { class: "plot__band", x: padL, y: padT, width: 0,
                             height: h - padT - padB, opacity: 0 });
  svg.append(hover);

  const hit = el("rect", { x: padL, y: 0, width: plotW, height: h, fill: "transparent" });
  svg.append(hit);

  hit.addEventListener("pointermove", (event) => {
    const rect = svg.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / rect.width;
    const p = Math.min(periods.length - 1,
      Math.max(0, Math.floor(((ratio * w) - padL) / gw)));

    hover.setAttribute("x", padL + gw * p);
    hover.setAttribute("width", gw);
    hover.setAttribute("opacity", 1);

    tooltip.dataset.open = "true";
    // Every series for the hovered period, not just the bar under the cursor:
    // the comparison between them is the reason the chart is grouped.
    tooltip.innerHTML =
      `<div class="tip__date">${formatPeriod ? formatPeriod(periods[p], p, true) : periods[p]}</div>` +
      live.map((s) => {
        const v = s.values[p];
        return `<div class="tip__series"><i style="background:${s.color}"></i>` +
          `<span>${s.label}</span><b>${Number.isFinite(v)
            ? (formatValue ? formatValue(v, true) : v) : "—"}</b></div>`;
      }).join("");
    tooltip.style.left =
      `${Math.min(event.clientX + 14, window.innerWidth - tooltip.offsetWidth - 8)}px`;
    tooltip.style.top = `${event.clientY - 8}px`;
  });

  hit.addEventListener("pointerleave", () => {
    hover.setAttribute("opacity", 0);
    tooltip.dataset.open = "false";
  });
}

/* ---------------- count-up ---------------- */

/**
 * Roll a KPI to its value so a change is trackable rather than a silent swap.
 * Skipped entirely under reduced motion — the number appears immediately,
 * because nobody should wait to see their P&L.
 */
export function countUp(node, to, render, duration = 260) {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce || !Number.isFinite(to)) { node.textContent = render(to); return; }

  const from = Number(node.dataset.value ?? 0);
  const start = performance.now();
  node.dataset.value = String(to);

  let settled = false;
  const settle = () => {
    if (settled) return;
    settled = true;
    node.textContent = render(to);
  };

  const tick = (now) => {
    if (settled) return;
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    node.textContent = render(from + (to - from) * eased);
    if (t < 1) requestAnimationFrame(tick);
    else settled = true;
  };
  requestAnimationFrame(tick);

  // requestAnimationFrame is suspended entirely while the tab is in the
  // background, so without this the animation never starts and the tile is
  // left reading £0 — someone who loads the dashboard and switches tabs comes
  // back to a zeroed portfolio. setTimeout is throttled in background tabs but
  // still fires, so it guarantees the real value lands either way.
  setTimeout(settle, duration + 80);
}
