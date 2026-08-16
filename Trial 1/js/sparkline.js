/* ==========================================================================
   sparkline.js — the small trend line used in matrix rows, the holdings
   table and the tunnel tiles.

   Spec: 2px stroke, no axes, no gridlines, a single end dot to mark "now".
   Colour comes from the row's direction token, which is always accompanied
   by a signed number, so the line is reinforcement rather than the only cue.
   ========================================================================== */

const NS = "http://www.w3.org/2000/svg";

/**
 * @param {number[]} series
 * @param {{w:number,h:number,dir:string,fill:boolean}} opts
 * @returns {SVGSVGElement}
 */
export function sparkline(series, { w = 62, h = 18, dir = "up", fill = false } = {}) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("width", w);
  svg.setAttribute("height", h);
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");

  const min = Math.min(...series);
  const max = Math.max(...series);
  const span = max - min || 1;
  const pad = 2;
  const x = (i) => (i / (series.length - 1)) * w;
  const y = (v) => h - pad - ((v - min) / span) * (h - pad * 2);

  const d = series.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(2)},${y(v).toFixed(2)}`).join("");

  const stroke =
    dir === "down" ? "var(--pnl-neg)" : dir === "flat" ? "var(--pnl-flat)" : "var(--pnl-pos)";

  if (fill) {
    const area = document.createElementNS(NS, "path");
    area.setAttribute("d", `${d}L${w},${h}L0,${h}Z`);
    area.setAttribute("fill", stroke);
    area.setAttribute("opacity", "0.12");
    svg.appendChild(area);
  }

  const path = document.createElementNS(NS, "path");
  path.setAttribute("d", d);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", stroke);
  path.setAttribute("stroke-width", "2");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  svg.appendChild(path);

  const dot = document.createElementNS(NS, "circle");
  dot.setAttribute("cx", w);
  dot.setAttribute("cy", y(series[series.length - 1]));
  dot.setAttribute("r", "2");
  dot.setAttribute("fill", stroke);
  svg.appendChild(dot);

  return svg;
}
