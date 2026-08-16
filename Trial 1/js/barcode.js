/* ==========================================================================
   barcode.js — the five sector allocation strips.

   Five equal-width strips, one per sector. Within a strip, each bar is one
   holding and its height is that holding's share of the sector, so the
   texture encodes the concentration of the sector rather than decorating it.
   The strip label carries the sector name and its weight in text, which is
   what keeps the encoding legible without relying on hue.
   ========================================================================== */

import { pctPlain } from "./format.js";

/**
 * @param {HTMLElement} root      the .barcode container
 * @param {Array} sectors         from data.js
 * @param {boolean} reduced
 */
export function renderBarcode(root, sectors, reduced) {
  root.textContent = "";

  sectors.forEach((s, si) => {
    const cell = document.createElement("div");

    const strip = document.createElement("div");
    strip.className = "barcode__strip";
    strip.style.setProperty("--strip-hue", `var(--sector-${s.slot})`);

    // 22 bars per strip, distributed across the sector's holdings by weight,
    // so a sector carried by one position looks visibly different from one
    // spread across four.
    const BARS = 22;
    const total = s.members.reduce((a, m) => a + m.value, 0) || 1;
    let assigned = [];
    s.members.forEach((m) => {
      const n = Math.max(1, Math.round((m.value / total) * BARS));
      for (let i = 0; i < n; i++) assigned.push(m);
    });
    assigned = assigned.slice(0, BARS);
    while (assigned.length < BARS) assigned.push(s.members[s.members.length - 1]);

    assigned.forEach((m, i) => {
      const bar = document.createElement("i");
      bar.className = "barcode__bar";
      const share = m.value / total;
      // Height reads the member's share; the small ripple keeps the barcode
      // texture from banding into flat blocks.
      const h = 0.34 + share * 0.58 + (i % 3) * 0.035;
      bar.style.setProperty("--bar-h", reduced ? h.toFixed(3) : "0");
      bar.dataset.h = h.toFixed(3);
      bar.style.transitionDelay = reduced ? "0ms" : `${si * 40 + i * 6}ms`;
      strip.appendChild(bar);
    });

    const label = document.createElement("div");
    label.className = "barcode__label";
    label.style.setProperty("--strip-hue", `var(--sector-${s.slot})`);
    label.innerHTML =
      `<span><i class="barcode__swatch"></i>${s.label}</span>` +
      `<b>${pctPlain(s.weight, 1)}</b>`;

    strip.title = `${s.label} — ${pctPlain(s.weight, 1)} of portfolio, ${s.members.length} position${s.members.length === 1 ? "" : "s"}`;

    cell.appendChild(strip);
    cell.appendChild(label);
    root.appendChild(cell);
  });
}

/** Grow the bars in once the section scrolls into view. */
export function playBarcode(root) {
  root.querySelectorAll(".barcode__bar").forEach((bar) => {
    bar.style.setProperty("--bar-h", bar.dataset.h);
  });
}
