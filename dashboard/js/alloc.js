/* The allocation ring: a donut whose centre swaps to whatever is hovered.
 *
 * Lifted from inspiration/shadcn-fintech-abderrahimghazali. That build holds an
 * `activeIndex: number | null`, wires it to both the pie segments and the
 * legend rows, drops non-active segments to 40% opacity and derives the centre
 * label from whatever is active. The mechanism is the transferable part: it is
 * plain state, not an animation, so it ports to anything.
 *
 * Two departures from the reference:
 *
 *   Keyboard. The reference is pointer-only. A region's own value is reachable
 *   *only* through the swap, so legend rows here are focusable and focus drives
 *   the same state hover does. Nobody is shut out of the breakdown.
 *
 *   Reuse. Overview and the Allocation view both draw one of these, and the
 *   Allocation view drives a KPI strip off the same state — which is what the
 *   shadcn notes propose doing with a sector axis. Hence `onActive`, and hence
 *   this living in its own module rather than inside either view.
 *
 * State lives here rather than inside donut() because Overview redraws the ring
 * on every 3s live tick, and anything held inside the drawing would be lost.
 */
import { money, pct, esc } from "./format.js";
import { donut, donutActive } from "./charts.js";

/** Categorical slot -> token. Slot 7 is the neutral grey: no identity yet. */
export const catColor = (index) => `var(--cat-${Math.min((index ?? 6) + 1, 7)})`;

/**
 * @param svg      the donut's <svg>
 * @param legend   the container the legend rows are written into
 * @param caption  what the centre reads when nothing is active
 * @param onActive optional — called with (row|null, total) on every change
 */
export function allocRing({ svg, legend, caption = "Invested", onActive } = {}) {
  let active = null;
  let rows = [];
  let total = 0;

  /** What the middle of the ring should read, given what is active. */
  function centre() {
    const row = active == null ? null : rows[active];
    if (!row) return { value: money(total), caption, pct: "" };
    return {
      value: money(row.value_gbp),
      caption: row.name,
      // Same precision as the legend row directly beneath it. A centre reading
      // 31.3% above a row reading 31% looks like two different numbers rather
      // than one number twice.
      pct: pct(row.weight_pct, 0),
    };
  }

  /** Paint the active state. Cheap; safe to call on any tick. */
  function paint() {
    donutActive(svg, active, centre());
    // Scoped to this ring's own legend. A page with two rings on it must not
    // have one highlight rows belonging to the other.
    for (const row of legend.querySelectorAll(".legend__row")) {
      const on = active != null && Number(row.dataset.index) === active;
      if (row.classList.contains("is-active") !== on) row.classList.toggle("is-active", on);
    }
    onActive?.(active == null ? null : rows[active], total);
  }

  function setActive(index) {
    if (active === index) return;
    active = index;
    paint();
  }

  /** The ring itself, without rebuilding the legend — the live tick's path. */
  function draw() {
    donut(svg, rows.map((r) => ({
      label: r.name,
      value: r.value_gbp,
      color: catColor(r.color_index),
      display: `${pct(r.weight_pct, 0)} · ${money(r.value_gbp)}`,
    })), {
      centreValue: money(total),
      centreCaption: caption,
      onHover: setActive,
    });
  }

  /**
   * Hover and focus, delegated.
   *
   * Delegation rather than per-row listeners because render() replaces the
   * legend's innerHTML whenever the set of rows changes, which would discard
   * bound handlers. pointerover/pointerout and focusin/focusout all bubble;
   * mouseenter/mouseleave do not, which is why they are not used here.
   */
  const indexFrom = (event) => {
    const row = event.target.closest?.(".legend__row");
    return row ? Number(row.dataset.index) : null;
  };
  legend.addEventListener("pointerover", (e) => {
    const i = indexFrom(e);
    if (i != null) setActive(i);
  });
  legend.addEventListener("pointerout", (e) => {
    // Ignore moves between a row's own children.
    if (e.relatedTarget?.closest?.(".legend__row") === e.target.closest?.(".legend__row")) return;
    setActive(null);
  });
  legend.addEventListener("focusin", (e) => {
    const i = indexFrom(e);
    if (i != null) setActive(i);
  });
  legend.addEventListener("focusout", () => setActive(null));

  const setText = (node, text) => {
    if (node && node.textContent !== text) node.textContent = text;
  };

  /** The composition currently on screen, so render() can tell a change of
   *  membership from a change of figures. */
  let drawnKey = null;

  return {
    /**
     * Redraw with a new set of rows. `total` is what the centre reads idle.
     *
     * Called on every 3s live tick, so it does the least it can get away with:
     * the ring is always redrawn because the arcs move, but the legend is only
     * rebuilt when the *membership* changes. Replacing the legend's innerHTML
     * on a tick would drop whatever the pointer or keyboard was on, snapping
     * the centre label back to the total every three seconds while someone is
     * reading a row.
     */
    render(nextRows, nextTotal) {
      rows = nextRows || [];
      total = nextTotal ?? 0;
      // A row that has left the portfolio must not leave a stale index
      // pointing at something that no longer exists.
      if (active != null && active >= rows.length) active = null;

      draw();

      const key = rows.map((r) => r.name).join(",");
      if (key !== drawnKey) {
        drawnKey = key;
        // Rows are focusable so the breakdown is reachable without a pointer.
        legend.innerHTML = rows.map((r, i) => `
          <div class="legend__row" data-region="${esc(r.name)}" data-index="${i}" tabindex="0">
            <span class="legend__dot" style="background:${catColor(r.color_index)}"></span>
            <span class="legend__name">${esc(r.name)}</span>
            <span class="legend__pct num" data-f="pct">${pct(r.weight_pct, 0)}</span>
            <span class="legend__val num" data-f="val">${money(r.value_gbp)}</span>
          </div>`).join("");
      } else {
        rows.forEach((r, i) => {
          const row = legend.querySelector(`.legend__row[data-index="${i}"]`);
          if (!row) return;
          setText(row.querySelector('[data-f="pct"]'), pct(r.weight_pct, 0));
          setText(row.querySelector('[data-f="val"]'), money(r.value_gbp));
        });
      }

      // The ring was just rebuilt from scratch either way, so re-apply whatever
      // the pointer or keyboard was on.
      paint();
    },
    /** The active row, or null. For a caller that needs to read it back. */
    activeRow: () => (active == null ? null : rows[active]),
    /** Drive the shared active state from outside — the treemap's cells use
     *  the same channel the arcs and legend rows do, so every readout that
     *  follows hover follows it identically. */
    setActive,
  };
}
