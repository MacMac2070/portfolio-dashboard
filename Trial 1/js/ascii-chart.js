/* ==========================================================================
   ascii-chart.js — renders the equity curve as a field of monospace glyphs.

   This is the hero's thesis: the shape behind the total value IS the
   portfolio's five-year history, not an ornament. Density is brightest along
   the curve itself and falls away beneath it, so the eye reads the line first
   and the mass second.
   ========================================================================== */

const GLYPHS = "0123456789£$%.:+=/\\|<>*#".split("");

/** Deterministic per-cell glyph pick — the field must not flicker on resize. */
function glyphAt(col, row) {
  const h = (col * 73856093) ^ (row * 19349663);
  return GLYPHS[Math.abs(h) % GLYPHS.length];
}

/** Opacity buckets keep the DOM small enough to re-render on resize. */
const BUCKETS = [0, 0.04, 0.09, 0.16, 0.26, 0.4, 0.62, 0.9];
const bucketFor = (v) => {
  let i = 0;
  while (i < BUCKETS.length - 1 && v >= BUCKETS[i + 1]) i++;
  return i;
};

/**
 * @param {HTMLElement} el   the .hero__ascii container
 * @param {number[]} series  the equity curve
 * @param {number} progress  0…1, used for the draw-on reveal
 */
export function renderAsciiCurve(el, series, progress = 1) {
  const cw = el.clientWidth;
  const ch = el.clientHeight;
  if (!cw || !ch) return;

  // Measure the actual glyph box rather than assuming it.
  const fs = parseFloat(getComputedStyle(el).fontSize) || 9;
  const lh = parseFloat(getComputedStyle(el).lineHeight) || fs;
  const colW = fs * 0.6; // JetBrains Mono advance width
  const cols = Math.max(8, Math.floor(cw / colW));
  const rows = Math.max(6, Math.floor(ch / lh));

  const min = Math.min(...series);
  const max = Math.max(...series);
  const span = max - min || 1;

  // Resample the series to one value per column.
  const line = new Array(cols);
  for (let c = 0; c < cols; c++) {
    const t = (c / (cols - 1)) * (series.length - 1);
    const i = Math.floor(t);
    const f = t - i;
    const v = series[i] + (series[Math.min(i + 1, series.length - 1)] - series[i]) * f;
    // Leave headroom top and bottom so the curve never touches the edges.
    const norm = (v - min) / span;
    line[c] = rows - 1 - (0.14 + norm * 0.62) * (rows - 1);
  }

  const revealCols = Math.round(cols * Math.min(1, Math.max(0, progress)));
  const out = [];

  for (let r = 0; r < rows; r++) {
    let html = "";
    let runBucket = -1;
    let runText = "";

    const flush = () => {
      if (!runText) return;
      const esc = runText.replace(/&/g, "&amp;").replace(/</g, "&lt;");
      if (runBucket === 0) {
        html += " ".repeat(runText.length);
      } else if (runBucket === BUCKETS.length - 1) {
        html += `<b>${esc}</b>`;
      } else {
        html += `<span style="opacity:${BUCKETS[runBucket]}">${esc}</span>`;
      }
      runText = "";
    };

    for (let c = 0; c < cols; c++) {
      let bucket = 0;
      let char = " ";

      if (c < revealCols) {
        const d = r - line[c]; // rows below the curve
        if (d >= -0.7) {
          // Bright right at the line, decaying with depth beneath it.
          const intensity =
            d < 0.9 ? 0.9 : Math.max(0, 0.58 * Math.exp(-d / (rows * 0.34)));
          bucket = bucketFor(intensity);
          if (bucket > 0) char = glyphAt(c, r);
        }
      }

      if (bucket !== runBucket) {
        flush();
        runBucket = bucket;
      }
      runText += char;
    }
    flush();
    out.push(html);
  }

  el.innerHTML = out.join("\n");
}

/** Draw-on reveal: sweeps the field left to right, then stops. */
export function animateAsciiCurve(el, series, reduced) {
  if (reduced) {
    renderAsciiCurve(el, series, 1);
    return;
  }
  const DURATION = 300;
  let done = false;
  let t0 = null;

  const step = (ts) => {
    if (done) return;
    if (t0 === null) t0 = ts;
    const p = Math.min(1, (ts - t0) / DURATION);
    renderAsciiCurve(el, series, p);
    if (p < 1) requestAnimationFrame(step);
    else done = true;
  };
  requestAnimationFrame(step);

  // Same guard as the count-up: a background tab never paints a frame, and
  // an empty hero is worse than an unanimated one.
  setTimeout(() => {
    if (done) return;
    done = true;
    renderAsciiCurve(el, series, 1);
  }, DURATION + 120);
}
