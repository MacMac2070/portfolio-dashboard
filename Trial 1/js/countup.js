/* ==========================================================================
   countup.js — rolls a figure up to its value on load and on update.

   Kept deliberately short (300ms ceiling): a dashboard has to feel fast and
   trustworthy before it feels delightful, and nobody should wait to read
   their own P&L. Reduced motion writes the final value immediately.
   ========================================================================== */

const easeOut = (t) => 1 - Math.pow(1 - t, 3);

/**
 * @param {HTMLElement} el
 * @param {number} to            target value
 * @param {(n:number)=>string} render  formats each frame
 * @param {{from?:number, duration?:number, reduced?:boolean}} opts
 */
export function countUp(el, to, render, { from = 0, duration = 300, reduced = false } = {}) {
  if (reduced) {
    el.textContent = render(to);
    return;
  }

  let done = false;
  let t0 = null;

  const step = (ts) => {
    if (done) return;
    if (t0 === null) t0 = ts;
    const p = Math.min(1, (ts - t0) / duration);
    el.textContent = render(from + (to - from) * easeOut(p));
    if (p < 1) requestAnimationFrame(step);
    else done = true;
  };
  requestAnimationFrame(step);

  // requestAnimationFrame is parked in a background tab, which would leave
  // the figure showing its placeholder. A timer still fires there, so this
  // guarantees the real number lands whether or not a frame is ever painted.
  setTimeout(() => {
    if (done) return;
    done = true;
    el.textContent = render(to);
  }, duration + 120);
}
