/* The scrolling ticker strip under the topbar.
 *
 * Adapted from inspiration/live-portfolio-tracker-kalwaleed/notes.md section 2.
 * The trick worth taking is the seamless loop: render the same run of holdings
 * twice back to back, then slide the pair left by exactly 50%. At the moment
 * the animation restarts, the second copy is sitting precisely where the first
 * began, so the seam is invisible and there is no JS animation loop, no
 * requestAnimationFrame and no per-frame work at all — one CSS animation on a
 * transform, which the compositor owns.
 *
 * Why this file rebuilds so rarely
 * --------------------------------
 * The strip is on screen on every tab and the position feed recomposes every
 * three seconds. Rebuilding the markup on each poll would restart the
 * animation from zero four times a minute — the strip would visibly jump back
 * to its start and never actually travel. So structure is rebuilt only when
 * the SET of holdings changes, and the figures are patched in place otherwise,
 * the same discipline ovholdings.js applies to its table.
 *
 * Both copies have to be patched, not just the first: the one a person is
 * reading at any moment depends on how far the strip has travelled.
 */
import { pctSigned, price, direction, esc } from "./format.js";

const $ = (id) => document.getElementById(id);

/* The set currently rendered. Compared per poll to decide rebuild vs patch. */
let structureKey = "";

/**
 * One holding. `data-tk` addresses both copies of a row for patching, and the
 * direction class carries the colour and the trend icon from the shared P&L
 * convention rather than inventing a second one.
 */
function cell(p) {
  const dir = direction(p.day_change_pct);
  return `<span class="tape__item" data-tk="${esc(p.symbol)}">`
    + `<span class="tape__sym">${esc(p.symbol)}</span>`
    + `<span class="tape__px num" data-f="px">${price(p.price)}</span>`
    + `<span class="dchip dchip--${dir === "up" ? "up" : dir === "down" ? "down" : "flat"}" data-f="chip">`
    + `${Number.isFinite(p.day_change_pct) ? pctSigned(p.day_change_pct) : "—"}</span>`
    + `</span>`;
}

/** Largest first, so the strip leads with what actually moves the portfolio. */
function ordered(data) {
  return (data.positions || [])
    .filter((p) => p.symbol)
    .slice()
    .sort((a, b) => (b.value_gbp || 0) - (a.value_gbp || 0));
}

export function update(data) {
  const tape = $("tape");
  const track = $("tapeTrack");
  if (!tape || !track) return;

  const rows = ordered(data);
  if (!rows.length) {
    tape.hidden = true;
    structureKey = "";
    return;
  }
  tape.hidden = false;

  const key = rows.map((p) => p.symbol).join("|");
  if (key !== structureKey) {
    structureKey = key;
    // Back to back copies. The duplication is the whole mechanism — see the
    // header — and it is why the strip is aria-hidden.
    //
    // Two copies is the usual answer, but only if one copy is already wider
    // than the strip: the animation slides by 50% of the track, so half the
    // copies have to cover the visible width or a gap opens behind the last
    // one before the loop restarts. A short holdings list needs more copies.
    // Rendered once to measure, then topped up — cheap, and only on the rare
    // poll where the set of positions actually changed.
    const run = rows.map(cell).join("");
    track.innerHTML = `<div class="tape__run">${run}</div>`;
    const runWidth = track.firstElementChild?.getBoundingClientRect().width || 0;
    const visible = tape.getBoundingClientRect().width || 0;
    const halves = runWidth > 0 ? Math.max(1, Math.ceil(visible / runWidth)) : 1;
    track.innerHTML = `<div class="tape__run">${run}</div>`.repeat(halves * 2);
    return;
  }

  // Patch both copies. querySelectorAll rather than querySelector: the row a
  // person is reading depends on how far the strip has scrolled.
  for (const p of rows) {
    const dir = direction(p.day_change_pct);
    const chipCls = `dchip dchip--${dir === "up" ? "up" : dir === "down" ? "down" : "flat"}`;
    const pxText = price(p.price);
    const chipText = Number.isFinite(p.day_change_pct) ? pctSigned(p.day_change_pct) : "—";

    for (const item of track.querySelectorAll(`[data-tk="${CSS.escape(p.symbol)}"]`)) {
      const px = item.querySelector('[data-f="px"]');
      if (px && px.textContent !== pxText) px.textContent = pxText;
      const chip = item.querySelector('[data-f="chip"]');
      if (!chip) continue;
      if (chip.className !== chipCls) chip.className = chipCls;
      if (chip.textContent !== chipText) chip.textContent = chipText;
    }
  }
}
