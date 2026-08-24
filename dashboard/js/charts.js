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

  const build = (step) => {
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) {
      out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    }
    return out;
  };

  const raw = span / Math.max(1, target);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = ([1, 2, 2.5, 5, 10].find((m) => raw <= m * mag) ?? 10) * mag;
  let out = build(step);

  // Rounding the step up can overshoot a domain that straddles zero
  // off-centre: monthly P&L over -£1,255..+£1,823 with a low tick target lands
  // on a step of 2,000 and labels nothing but £0. Fall to the widest narrower
  // step that puts a real scale on the axis — an axis with one tick is not one.
  if (out.length < 3) {
    for (const candidate of [10, 5, 2.5, 2, 1, 0.5, 0.25, 0.2, 0.1].map((m) => m * mag)) {
      if (candidate >= step) continue;
      out = build(candidate);
      if (out.length >= 3) break;
    }
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

  // Line only. The gradient wash under every 88x26 spark — and under all
  // eight market cards at once — was the loudest default on the board; at
  // this size the fill carried no information the stroke does not.
  svg.append(
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
 *
 * `onHover(index | null)` fires as the pointer crosses segments. The caller
 * owns the resulting state and calls `donutActive` below to reflect it —
 * this function does not repaint itself, because it is redrawn wholesale on
 * the 3s live tick and a hover that lived inside it would not survive that.
 *
 * Each arc carries data-seg so the caller can address it afterwards.
 */
export function donut(svg, rows, { centreValue, centreCaption, centrePct, onHover } = {}) {
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

  rows.forEach((row, i) => {
    const share = Math.max(0, row.value) / total;
    const length = Math.max(share * circumference - gap, 1);

    const arc = el("circle", {
      class: "donut__seg",
      "data-seg": i,
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
    if (onHover) {
      arc.addEventListener("pointerenter", () => onHover(i));
      arc.addEventListener("pointerleave", () => onHover(null));
    }
    group.append(arc);

    offset += share * circumference;
  });

  svg.append(group);

  // The centre is three stacked lines rather than two when a segment is
  // active. They are always created, empty when unused, so donutActive can
  // fill them without the caller having to redraw the ring to make room.
  const value = el("text", {
    class: "donut__value", x: cx, y: cy - 2,
    "text-anchor": "middle", "dominant-baseline": "middle",
  });
  value.textContent = centreValue || "";
  const caption = el("text", {
    class: "donut__caption", x: cx, y: cy + 26, "text-anchor": "middle",
  });
  caption.textContent = centreCaption || "";
  const share = el("text", {
    class: "donut__pct", x: cx, y: cy + 44, "text-anchor": "middle",
  });
  share.textContent = centrePct || "";
  svg.append(value, caption, share);
}

/**
 * Reflect a hovered segment without redrawing the ring.
 *
 * Non-active segments drop to 40% so the hovered one reads as highlighted
 * rather than merely labelled — the mechanism shadcn-fintech's allocation
 * donut uses. Attribute writes only: no node is replaced, so a pointer sitting
 * on an arc keeps its hover through the 3s live tick.
 *
 * `centre` is { value, caption, pct } — whatever the middle should now read.
 */
export function donutActive(svg, activeIndex, centre = {}) {
  if (!svg) return;
  for (const seg of svg.querySelectorAll(".donut__seg")) {
    const dim = activeIndex != null && Number(seg.dataset.seg) !== activeIndex;
    const want = dim ? "0.4" : "1";
    if (seg.getAttribute("opacity") !== want) seg.setAttribute("opacity", want);
  }
  const write = (sel, text) => {
    const node = svg.querySelector(sel);
    if (node && node.textContent !== (text || "")) node.textContent = text || "";
  };
  write(".donut__value", centre.value);
  write(".donut__caption", centre.caption);
  write(".donut__pct", centre.pct);
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
  // Headroom below the minimum must not carry a non-negative series past zero.
  // Over the whole span the low is the account's £10 opening balance, and 12%
  // of a £53k range is £6.4k — enough to print "-£6,434" on the axis of a
  // long-only portfolio. Clamped only when the subtraction actually crosses
  // zero, so an ordinary £48k-£53k window keeps its full headroom and does not
  // get flattened against a 0 baseline.
  lo = lo >= 0 ? Math.max(0, lo - headroom) : lo - headroom;
  hi += headroom;

  const x = (i) => padL + (i / (points.length - 1)) * (w - padL - padR);
  const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB);

  const rising = values[values.length - 1] >= values[0];
  const stroke = rising ? "var(--pos)" : "var(--neg)";
  const id = `curve-${Math.random().toString(36).slice(2, 9)}`;

  // gridlines + y labels — fewer of them when the plot is short, so the
  // labels never collide
  const grid = el("g");
  // Rounded tick values from the helper this file already had — "£50,406" as
  // an axis label is a value nobody chose. Out-of-range ticks are skipped the
  // way groupedBars already does.
  for (const value of niceTicks(lo, hi, h < 150 ? 3 : h < 220 ? 4 : 5)) {
    if (value < lo || value > hi) continue;
    const yy = y(value);
    grid.append(el("line", {
      class: "plot__grid", x1: padL, x2: w - padR, y1: yy, y2: yy,
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

  // Flat, faint, no gradient: the fill's one job is separating above-the-line
  // from below it; a fade adds a second, meaningless encoding of "recentness".
  svg.append(el("path", {
    d: `${path} L${x(points.length - 1)},${h - padB} L${padL},${h - padB} Z`,
    fill: stroke, "fill-opacity": 0.07, stroke: "none",
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
  for (const value of niceTicks(lo, hi, h < 170 ? 3 : h < 240 ? 4 : 5)) {
    if (value < lo || value > hi) continue;
    const yy = y(value);
    grid.append(el("line", {
      class: "plot__grid", x1: padL, x2: w - padR, y1: yy, y2: yy,
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

  // Flat, faint, no gradient: the fill's one job is separating above-the-line
  // from below it; a fade adds a second, meaningless encoding of "recentness".
  svg.append(el("path", {
    d: `${path} L${x(series.length - 1)},${h - padB} L${padL},${h - padB} Z`,
    fill: stroke, "fill-opacity": 0.07, stroke: "none",
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
  stacked = false, detail = null, totalLabel = "Total",
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

  // Stacked, the axis has to reach the column total, not the tallest single
  // series — otherwise every column overshoots the top of the plot.
  const totals = periods.map((_, p) =>
    live.reduce((sum, s) => sum + (Number.isFinite(s.values[p]) ? s.values[p] : 0), 0));
  const all = stacked ? totals : live.flatMap((s) => s.values).filter(Number.isFinite);
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
  // Stacked: one column per period. Grouped: one column per series.
  const bw = stacked ? band : band / live.length;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  periods.forEach((label, p) => {
    const gx = padL + gw * p + (gw - band) / 2;
    const text = el("text", {
      class: "plot__axis", x: padL + gw * p + gw / 2, y: h - 10,
      "text-anchor": "middle",
    });
    text.textContent = formatPeriod ? formatPeriod(label, p) : label;
    svg.append(text);

    // Stacked segments accumulate from the zero line outward, so each one is
    // drawn from where the previous ended rather than from the baseline.
    let upTo = 0;

    live.forEach((s, i) => {
      const v = s.values[p];
      if (!Number.isFinite(v)) return;      // no bar at all — see the docstring
      let top, height, x;
      if (stacked) {
        if (v === 0) return;                // nothing to stack, and no 1px sliver
        const base = upTo;
        upTo += v;
        top = Math.min(y(upTo), y(base));
        // -2 leaves a gap of surface between segments, so touching bands stay
        // countable — the same spacer donut() and the grouped path use.
        height = Math.max(1, Math.abs(y(upTo) - y(base)) - 2);
        x = gx;
      } else {
        top = Math.min(y(v), zeroY);
        height = Math.max(1, Math.abs(y(v) - zeroY));
        x = gx + bw * i;
      }
      // A series may hand back a colour per value rather than one for the
      // whole run. That is for a signed series like monthly P&L, where the
      // sign is the identity and one hue across the zero line would say the
      // opposite of what the bar means.
      const fill = typeof s.color === "function" ? s.color(v) : s.color;
      const rect = el("rect", {
        x, y: top, width: Math.max(1, bw - (stacked ? 0 : 2)), height,
        rx: 0, fill,
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
    //
    // `detail` itemises further than the stack draws. The cost chart draws
    // three bands because a five-hue palette does not separate, but the
    // underlying five figures are still what a person wants to read — so the
    // colour swatch belongs to the drawn series and the detail rows list
    // without one. A bold total closes it, which premium-tracker's README
    // calls the reusable half of this pattern.
    const rows = (detail?.length ? detail : live);
    const swatch = detail?.length ? null : true;
    const money = (v) => (Number.isFinite(v) ? (formatValue ? formatValue(v, true) : v) : "—");
    const sum = live.reduce(
      (acc, s) => acc + (Number.isFinite(s.values[p]) ? s.values[p] : 0), 0);
    tooltip.innerHTML =
      `<div class="tip__date">${formatPeriod ? formatPeriod(periods[p], p, true) : periods[p]}</div>` +
      rows.map((s) => {
        const v = s.values[p];
        return `<div class="tip__series">`
          + (swatch
              ? `<i style="background:${typeof s.color === "function" ? s.color(v) : s.color}"></i>`
              : `<i class="tip__pip"></i>`)
          + `<span>${s.label}</span><b>${money(v)}</b></div>`;
      }).join("") +
      `<div class="tip__total"><span>${totalLabel}</span><b>${money(sum)}</b></div>`;
    tooltip.style.left =
      `${Math.min(event.clientX + 14, window.innerWidth - tooltip.offsetWidth - 8)}px`;
    tooltip.style.top = `${event.clientY - 8}px`;
  });

  hit.addEventListener("pointerleave", () => {
    hover.setAttribute("opacity", 0);
    tooltip.dataset.open = "false";
  });
}

/* ---------------- sankey ---------------- */

/**
 * The NAV flow: what went into the pot, and what came back out of it.
 *
 * Two stages, as premium-tracker's README describes: sources feed one Gross
 * Value node, which splits into Ending NAV and the cost stack. That separation
 * is the point — "how big did the pot get" and "what was skimmed off it" are
 * different questions, and a flat set of flows answers neither cleanly.
 *
 * Hand-laid out rather than pulled from d3-sankey. The topology is fixed and
 * tiny — three columns, a handful of nodes — so a general solver would be a
 * dependency and a build step to compute what four lines of arithmetic do.
 *
 * `data` is what adapter/attribution.flow() returns: {nodes, links, gross, …},
 * every value a positive magnitude. A Sankey has no negative width; direction
 * is carried by which column a node sits in.
 */
export function sankey(svg, data, { formatValue, tooltip } = {}) {
  svg.replaceChildren();
  if (!data?.links?.length || !(data.gross > 0)) return;

  const ins0 = data.links.filter((l) => l.kind === "in");
  const outs0 = data.links.filter((l) => l.kind !== "in");

  const box = svg.getBoundingClientRect();
  const w = Math.max(360, Math.round(box.width) || 780);
  // Tall enough to clear a two-line label block (12px name + 11px value) plus
  // breathing room, because it is what keeps the fanned-out bands from
  // colliding — see stack().
  const LINE_H = 29;
  const padY = 10;
  // Bands are laid out against this height. The labels may then need more of
  // it than the bands do — see the de-collision below — so the final height is
  // settled after they are placed, not guessed at here.
  const h = Math.max(220, Math.round(box.height) || 340);
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  const fmt = (v) => (formatValue ? formatValue(v) : String(Math.round(v)));
  // Every column's labels are right-aligned into a gutter to its left, name
  // over value, the way the reference lays them out — clear of the ribbons
  // rather than printed on top of them.
  const padX = 4, nodeW = 9, labelGap = 12;
  const gutter = Math.max(92, Math.min(150, Math.round(w * 0.17)));
  const xs = [gutter, 0, w - padX - nodeW];
  xs[1] = Math.round((xs[0] + xs[2]) / 2);
  const plotH = h - padY * 2;

  const ins = ins0;
  const outs = outs0;
  const total = ins.reduce((s, l) => s + l.value, 0) || 1;

  // A gap between stacked bands, the same 2px-of-surface trick donut() and
  // groupedBars() use, so touching flows stay countable.
  const gap = 3;
  const scale = (v) => (v / total) * (plotH - gap * Math.max(ins.length, outs.length));

  /**
   * Stack one column.
   *
   * `minPitch` holds consecutive band *centres* that far apart so a two-line
   * label fits beside each one. Only the gaps grow: every band keeps a height
   * proportional to its value, which is the one thing a Sankey may not fudge.
   *
   * Spreading the outer columns is also what gives the ribbons their shape.
   * With the middle node packed tight and the outer ones fanned out, each link
   * has somewhere to travel and the bezier reads as a flow. Stack both ends
   * the same way — which is what this did before — and every control point
   * lands level with its anchor, so the curves come out as dead-flat
   * rectangles and the whole chart reads as a stacked bar.
   */
  function stack(links, x, minPitch = 0) {
    let y = padY, prevCentre = -Infinity;
    return links.map((l) => {
      const th = Math.max(1, scale(l.value));
      let top = y;
      if (minPitch && prevCentre > -Infinity) {
        top = Math.max(top, prevCentre + minPitch - th / 2);
      }
      prevCentre = top + th / 2;
      y = top + th + gap;
      return { ...l, x, y0: top, y1: top + th, h: th };
    });
  }
  const left = stack(ins, xs[0], LINE_H);
  const right = stack(outs, xs[2], LINE_H);

  // The middle node spans the whole flow, so both fans meet a single bar. Its
  // height is the sum of what arrives, not the extent of the spread-out outer
  // columns — those are fanned for their labels and are taller than the flow.
  const span = (segs) =>
    segs.reduce((sum, seg) => sum + seg.h, 0) + gap * Math.max(0, segs.length - 1);
  const midTop = padY;
  const midBot = padY + Math.max(span(left), span(right));

  const colour = (kind) =>
    kind === "in" ? "var(--flow-in)" : kind === "out" ? "var(--flow-kept)" : "var(--flow-cost)";

  // Ribbons first, so the node bars sit on top of their own ends.
  let midInY = midTop, midOutY = midTop;
  const ribbons = [];
  for (const seg of left) {
    const y0 = seg.y0, y1 = seg.y1;
    const m0 = midInY, m1 = midInY + seg.h;
    midInY = m1 + gap;
    ribbons.push({ seg, d: ribbon(seg.x + nodeW, y0, y1, xs[1], m0, m1) });
  }
  for (const seg of right) {
    const m0 = midOutY, m1 = midOutY + seg.h;
    midOutY = m1 + gap;
    ribbons.push({ seg, d: ribbon(xs[1] + nodeW, m0, m1, seg.x, seg.y0, seg.y1) });
  }

  function ribbon(x0, a0, a1, x1, b0, b1) {
    const cx = (x0 + x1) / 2;
    return `M${x0},${a0} C${cx},${a0} ${cx},${b0} ${x1},${b0}`
      + ` L${x1},${b1} C${cx},${b1} ${cx},${a1} ${x0},${a1} Z`;
  }

  for (const { seg, d } of ribbons) {
    const path = el("path", {
      class: "flow__link", d, fill: colour(seg.kind),
      "data-flow": seg.kind === "in" ? seg.source : seg.target,
    });
    const name = seg.kind === "in" ? seg.source : seg.target;
    // Named for assistive tech through aria-label, not a <title> child: the
    // browser renders <title> as its own native tooltip, so hovering a ribbon
    // produced two tooltips at once — the styled one and a grey OS bubble
    // floating over it. aria-label carries the same text with no chrome.
    path.setAttribute("role", "img");
    path.setAttribute("aria-label", `${name} — ${fmt(seg.value)}`);
    if (tooltip) {
      path.addEventListener("pointerenter", (event) => {
        tooltip.dataset.open = "true";
        tooltip.innerHTML =
          `<div class="tip__date">${name}</div><div class="tip__val">${fmt(seg.value)}</div>`;
        tooltip.style.left =
          `${Math.min(event.clientX + 14, window.innerWidth - tooltip.offsetWidth - 8)}px`;
        tooltip.style.top = `${event.clientY - 8}px`;
      });
      path.addEventListener("pointerleave", () => { tooltip.dataset.open = "false"; });
    }
    svg.append(path);
  }

  // Node bars, then labels outside them.
  const bar = (x, y0, y1, fill) => el("rect", {
    class: "flow__node", x, y: y0, width: nodeW, height: Math.max(1, y1 - y0),
    rx: 0, fill,
  });
  svg.append(bar(xs[1], midTop, midBot, "var(--text-3)"));
  for (const seg of left) svg.append(bar(seg.x, seg.y0, seg.y1, colour("in")));
  for (const seg of right) svg.append(bar(seg.x, seg.y0, seg.y1, colour(seg.kind)));

  /* Labels.
   *
   * A two-line block per node, which is taller than most bands: fees are a
   * fraction of a percent of a portfolio, so their ribbons are a pixel or two
   * and their labels would sit on top of each other. Each column is therefore
   * de-collided greedily — walk down, and push any block that would overlap
   * the one above it to just below it — and a leader line is drawn from the
   * band to the label whenever it has been moved far enough to need one.
   *
   * The bands themselves are never adjusted. A Sankey's only job is that width
   * is proportional to value; moving the text is honest, moving the ribbon is
   * not. LINE_H is set above, where it also decides the chart's height.
   */
  function place(segs) {
    let prev = -Infinity;
    return segs.map((seg) => {
      const want = (seg.y0 + seg.y1) / 2 - 2;
      const y = Math.max(want, prev + LINE_H);
      prev = y;
      return { seg, y, want };
    });
  }

  function draw(placed, anchor, textX, leaderFrom) {
    for (const { seg, y, want } of placed) {
      const g = el("g");
      // Keyed off the link's direction, not the text anchor. Both columns are
      // right-aligned now, so anchor no longer tells the two apart — reading it
      // instead labelled every source with its shared target, "Gross Value".
      const name = seg.kind === "in" ? seg.source : seg.target;
      const t1 = el("text", { class: "flow__name", x: textX, y, "text-anchor": anchor });
      t1.textContent = name;
      const t2 = el("text", { class: "flow__val", x: textX, y: y + 13, "text-anchor": anchor });
      t2.textContent = fmt(seg.value);
      g.append(t1, t2);
      // Only when the text has actually been pushed off its band, and only far
      // enough that the pairing is no longer obvious.
      if (Math.abs(y - want) > 4) {
        const bandY = (seg.y0 + seg.y1) / 2;
        const x0 = leaderFrom;
        const x1 = anchor === "end" ? textX + 4 : textX - 4;
        g.append(el("path", {
          class: "flow__leader",
          d: `M${x0},${bandY} L${(x0 + x1) / 2},${bandY} L${(x0 + x1) / 2},${y - 3} L${x1},${y - 3}`,
        }));
      }
      svg.append(g);
    }
  }

  const placedLeft = place(left);
  const placedRight = place(right);
  // Both columns anchor "end" into the gutter on their left, so a name and its
  // figure line up down a single edge instead of ragging against the ribbons.
  draw(placedLeft, "end", xs[0] - labelGap, xs[0]);
  draw(placedRight, "end", xs[2] - labelGap, xs[2]);

  // Now the labels are placed, grow the canvas to whatever they needed. The
  // cost bands sit near the bottom of the plot and de-collide downward from
  // there, so the last one routinely lands below the band area — which is
  // fine, as long as it is not cropped. Bands and ribbons keep the geometry
  // they were laid out with; only the canvas below them gets taller.
  const lowest = [...placedLeft, ...placedRight]
    .reduce((m, p) => Math.max(m, p.y + 14), 0);
  const finalH = Math.max(h, Math.ceil(lowest + padY));
  if (finalH !== h) svg.setAttribute("viewBox", `0 0 ${w} ${finalH}`);
  // Explicit, so the card grows with the chart rather than cropping it.
  svg.style.height = `${finalH}px`;

  const gtext = el("g");
  const gy = (midTop + midBot) / 2 - 2;
  const gn = el("text", { class: "flow__name", x: xs[1] - labelGap, y: gy,
                          "text-anchor": "end" });
  gn.textContent = "Gross value";
  const gv = el("text", { class: "flow__val", x: xs[1] - labelGap, y: gy + 13,
                          "text-anchor": "end" });
  gv.textContent = fmt(data.gross);
  gtext.append(gn, gv);
  svg.append(gtext);

  const summary = el("title");
  summary.textContent =
    `NAV flow: ${fmt(data.gross)} gross, ${fmt(data.ending_derived)} retained across `
    + `${outs.length - 1} cost categories.`;
  svg.append(summary);
}

/* ---------------- underwater ---------------- */

/**
 * Drawdown from peak: a series that lives at or below zero.
 *
 * The zero rule sits at the top of the plot and the red wash hangs beneath
 * it — the equity curve tells the up story, this one owes the reader the
 * pain, plainly. Same restraint as every other fill here: flat, faint, no
 * gradient. Green never appears; a drawdown of zero is the absence of the
 * mark, not a gain.
 */
export function underwater(svg, curve, { tooltip, formatDate } = {}) {
  svg.replaceChildren();
  if (!curve || curve.length < 2) return;

  const box = svg.getBoundingClientRect();
  const w = Math.max(280, Math.round(box.width) || 520);
  const h = Math.max(120, Math.round(box.height) || 170);
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  const padL = 40, padR = 8, padT = 6, padB = 22;
  const worst = Math.min(...curve.map((p) => p.dd));
  const floor = Math.min(worst * 1.15, -0.02);   // headroom below the trough

  const x = (i) => padL + (i / (curve.length - 1)) * (w - padL - padR);
  const y = (v) => padT + (v / floor) * (h - padT - padB);

  const grid = el("g");
  for (const value of niceTicks(floor, 0, 3)) {
    if (value > 0 || value < floor) continue;
    const yy = y(value);
    grid.append(el("line", { class: "plot__grid", x1: padL, x2: w - padR, y1: yy, y2: yy }));
    const label = el("text", { class: "plot__axis", x: padL - 6, y: yy + 3, "text-anchor": "end" });
    label.textContent = `${Math.round(value * 100)}%`;
    grid.append(label);
  }
  svg.append(grid);

  const pts = curve.map((p, i) => `${x(i).toFixed(2)},${y(p.dd).toFixed(2)}`);
  svg.append(el("path", {
    d: `M${padL},${y(0)} L${pts.join(" L")} L${x(curve.length - 1)},${y(0)} Z`,
    fill: "var(--neg)", "fill-opacity": 0.12, stroke: "none",
  }));
  svg.append(el("path", {
    d: `M${pts.join(" L")}`,
    fill: "none", stroke: "var(--neg)", "stroke-width": 1.4,
    "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke",
  }));
  // The zero rule the drawdown hangs from.
  svg.append(el("line", {
    x1: padL, x2: w - padR, y1: y(0), y2: y(0),
    stroke: "var(--border-strong)", "stroke-width": 1,
  }));

  // Sparse month labels along the base.
  const stride = Math.max(1, Math.round(curve.length / 5));
  const xLabels = el("g");
  for (let i = 0; i < curve.length; i += stride) {
    const label = el("text", {
      class: "plot__axis", x: x(i), y: h - 6, "text-anchor": "middle",
    });
    label.textContent = formatDate ? formatDate(curve[i].date) : curve[i].date;
    xLabels.append(label);
  }
  svg.append(xLabels);

  if (tooltip) {
    svg.addEventListener("pointermove", (event) => {
      const rect = svg.getBoundingClientRect();
      const frac = (event.clientX - rect.left) / rect.width;
      const i = Math.max(0, Math.min(curve.length - 1,
        Math.round(((frac * w) - padL) / (w - padL - padR) * (curve.length - 1))));
      const p = curve[i];
      tooltip.dataset.open = "true";
      tooltip.innerHTML = `<div class="tip__date">${formatDate ? formatDate(p.date) : p.date}</div>`
        + `<div class="tip__val">${(p.dd * 100).toFixed(2)}% from peak</div>`;
      tooltip.style.left = `${Math.min(event.clientX + 14, window.innerWidth - tooltip.offsetWidth - 8)}px`;
      tooltip.style.top = `${event.clientY - 8}px`;
    });
    svg.addEventListener("pointerleave", () => { tooltip.dataset.open = "false"; });
  }

  const summary = el("title");
  summary.textContent =
    `Drawdown: worst ${(worst * 100).toFixed(1)}% from peak; currently `
    + `${(curve[curve.length - 1].dd * 100).toFixed(1)}%.`;
  svg.append(summary);
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
