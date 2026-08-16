/* ==========================================================================
   gauge.js — the total-return arc.

   Geometry: a circle whose centre sits just below the card's bottom edge, so
   the card clips it into a wide, shallow sweep. The gradient runs from the
   scale's start to the current value; the marker sits at the current value;
   the two track dots mark break-even and +25%, which are the numbers you
   actually compare a return against.
   ========================================================================== */

const NS = "http://www.w3.org/2000/svg";

/* Card geometry in reference units. The deck's content column is 1755u wide
   and this card is half of it, less the gutter; the row's aspect ratio fixes
   the height. */
const CARD_W = (1755 - 25) / 2;
const CARD_H = 861;

/* The arc's circle sits far below the card so only its shallow crown shows.
   ARC_TOP is where the crown peaks, as a fraction of card height. */
const ARC_TOP = 0.615;
const EDGE_INSET = 30; // how close the clipped ends run to the card's sides

const polar = (cx, cy, r, deg) => {
  const rad = (deg * Math.PI) / 180;
  return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
};

function arcPath(cx, cy, r, a0, a1) {
  const [x0, y0] = polar(cx, cy, r, a0);
  const [x1, y1] = polar(cx, cy, r, a1);
  const large = Math.abs(a1 - a0) > 180 ? 1 : 0;
  return `M${x0.toFixed(2)},${y0.toFixed(2)}A${r},${r} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)}`;
}

/**
 * @param {SVGSVGElement} svg   the .gauge__arc element
 * @param {number} value        current return, in percent
 * @param {{min:number,max:number,ticks:number[]}} scale
 * @param {boolean} reduced     prefers-reduced-motion
 */
export function renderGauge(svg, value, scale, reduced) {
  const W = CARD_W;
  const H = CARD_H;
  // The viewBox matches the card exactly, so one user unit is one card unit
  // and nothing gets letterboxed.
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  svg.textContent = "";

  const cx = W / 2;
  const crown = H * ARC_TOP;
  const halfSpan = cx - EDGE_INSET;

  // Solve for the radius of a circle whose crown sits at `crown` and which
  // crosses the card's bottom edge `EDGE_INSET` from each side.
  //   halfSpan² + (H − cy)² = r²,  cy = crown + r
  const r = (halfSpan * halfSpan + (H - crown) * (H - crown)) / (2 * (H - crown));
  const cy = crown + r;
  const thickness = 30;

  // Clip angles: where the circle meets the card's bottom edge. The endpoint
  // constraint is on y, so it's the sine that's pinned — asin, not acos.
  const half = 90 + (Math.asin((H - cy) / r) * 180) / Math.PI;
  const SWEEP_START = 270 - half;
  const SWEEP_END = 270 + half;

  const frac = (v) =>
    Math.min(1, Math.max(0, (v - scale.min) / (scale.max - scale.min)));
  const angleFor = (v) => SWEEP_START + frac(v) * (SWEEP_END - SWEEP_START);

  const defs = document.createElementNS(NS, "defs");
  defs.innerHTML = `
    <linearGradient id="gaugeGrad" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%"   stop-color="var(--accent-magenta)"/>
      <stop offset="46%"  stop-color="var(--accent-sky)"/>
      <stop offset="100%" stop-color="var(--accent-mint)"/>
    </linearGradient>`;
  svg.appendChild(defs);

  // Track
  const track = document.createElementNS(NS, "path");
  track.setAttribute("d", arcPath(cx, cy, r, SWEEP_START, SWEEP_END));
  track.setAttribute("class", "gauge__track");
  track.setAttribute("fill", "none");
  track.setAttribute("stroke-width", thickness);
  svg.appendChild(track);

  // Gradient fill, from the scale start to the current value
  const endAngle = angleFor(value);
  const fill = document.createElementNS(NS, "path");
  fill.setAttribute("d", arcPath(cx, cy, r, SWEEP_START, endAngle));
  fill.setAttribute("class", "gauge__fill");
  fill.setAttribute("fill", "none");
  fill.setAttribute("stroke-width", thickness);
  svg.appendChild(fill);

  // Reference ticks
  for (const t of scale.ticks) {
    const [tx, ty] = polar(cx, cy, r, angleFor(t));
    const dot = document.createElementNS(NS, "circle");
    dot.setAttribute("cx", tx.toFixed(2));
    dot.setAttribute("cy", ty.toFixed(2));
    dot.setAttribute("r", "6");
    dot.setAttribute("class", "gauge__tick");
    svg.appendChild(dot);
  }

  // Current-value marker
  const [mx, my] = polar(cx, cy, r, endAngle);
  const marker = document.createElementNS(NS, "circle");
  marker.setAttribute("cx", mx.toFixed(2));
  marker.setAttribute("cy", my.toFixed(2));
  marker.setAttribute("r", "11");
  marker.setAttribute("class", "gauge__marker");
  svg.appendChild(marker);

  // Draw-on: reveal the fill by its own length, and slide the marker with it.
  if (!reduced) {
    const len = fill.getTotalLength();
    fill.style.strokeDasharray = `${len}`;
    fill.style.strokeDashoffset = `${len}`;
    marker.style.opacity = "0";

    let released = false;
    const release = () => {
      if (released) return;
      released = true;
      fill.style.transition = `stroke-dashoffset 300ms var(--ease-out)`;
      fill.style.strokeDashoffset = "0";
      marker.style.transition = `opacity 150ms var(--ease-out) 260ms`;
      marker.style.opacity = "1";
    };

    requestAnimationFrame(release);
    // A background tab parks rAF, which would leave the arc hidden behind
    // its own dash offset. The timer releases it either way.
    setTimeout(release, 140);
  }
}
