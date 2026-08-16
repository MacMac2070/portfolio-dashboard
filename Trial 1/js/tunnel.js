/* ==========================================================================
   tunnel.js — the CSS 3D holdings gallery.

   A real perspective tunnel: nested wireframe frames receding toward a
   vanishing point, with one tile per holding pinned to the left, right, top
   or floor wall at a depth set by its weight. The biggest positions sit
   nearest the viewer, so depth is the allocation encoding — the section
   reads as "what am I actually holding, and how much".
   ========================================================================== */

import { pct, dir, arrow } from "./format.js";
import { sparkline } from "./sparkline.js";

const NS = "http://www.w3.org/2000/svg";

const FRAMES = 11;
const FRAME_STEP = 132; // reference units between frames
const WALLS = ["left", "right", "top", "floor"];

/* Half-extents of the card in reference units. The deck's content column is
   1755u wide by definition, and this row's aspect ratio fixes its height. */
const HALF_W = 1755 / 2;
const HALF_H = 1023 / 2;

export function renderTunnel(space, holdings, reduced) {
  space.textContent = "";

  // ---- Receding frames --------------------------------------------------
  for (let i = 0; i < FRAMES; i++) {
    const f = document.createElement("div");
    f.className = "tunnel__frame";
    f.style.setProperty("--z", `calc(${-i * FRAME_STEP} * var(--u))`);
    f.style.setProperty("--frame-op", (0.82 - i * 0.06).toFixed(3));
    space.appendChild(f);
  }

  // ---- Corner rails to the vanishing point ------------------------------
  const rail = document.createElementNS(NS, "svg");
  rail.setAttribute("class", "tunnel__rail");
  rail.setAttribute("viewBox", "0 0 1000 620");
  rail.setAttribute("preserveAspectRatio", "none");
  rail.setAttribute("aria-hidden", "true");
  [
    [0, 0],
    [1000, 0],
    [0, 620],
    [1000, 620],
  ].forEach(([x, y]) => {
    const l = document.createElementNS(NS, "line");
    l.setAttribute("x1", x);
    l.setAttribute("y1", y);
    l.setAttribute("x2", 500);
    l.setAttribute("y2", 310);
    rail.appendChild(l);
  });
  space.appendChild(rail);

  // ---- Tiles ------------------------------------------------------------
  // Ordered by weight so the largest holdings land at the shallowest depth.
  const ordered = [...holdings].sort((a, b) => b.weight - a.weight);

  ordered.forEach((h, i) => {
    const depth = -(70 + i * 64);
    const wall = WALLS[i % WALLS.length];
    const lane = Math.floor(i / WALLS.length);

    let tx = "0px";
    let ty = "0px";
    let ry = 0;
    let rx = 0;

    // Sit each tile against its wall, just inside the tunnel's mouth, and
    // rotate it to lie flat along that wall.
    if (wall === "left") {
      tx = `calc(${-(HALF_W - 160) - lane * 14} * var(--u))`;
      ty = `calc(${-190 + lane * 150} * var(--u))`;
      ry = 38;
    } else if (wall === "right") {
      tx = `calc(${HALF_W - 160 + lane * 14} * var(--u))`;
      ty = `calc(${-120 + lane * 146} * var(--u))`;
      ry = -38;
    } else if (wall === "top") {
      tx = `calc(${-330 + lane * 300} * var(--u))`;
      ty = `calc(${-(HALF_H - 120) - lane * 10} * var(--u))`;
      rx = -26;
    } else {
      tx = `calc(${-300 + lane * 290} * var(--u))`;
      ty = `calc(${HALF_H - 120 + lane * 10} * var(--u))`;
      rx = 26;
    }

    const tile = document.createElement("button");
    tile.type = "button";
    tile.className = "tunnel__tile";
    tile.dataset.ticker = h.tk;
    tile.dataset.name = h.name.toLowerCase();
    tile.style.setProperty("--tx", tx);
    tile.style.setProperty("--ty", ty);
    tile.style.setProperty("--tz", `calc(${depth} * var(--u))`);
    tile.style.setProperty("--ry", `${ry}deg`);
    tile.style.setProperty("--rx", `${rx}deg`);

    const d = dir(h.day);
    tile.innerHTML =
      `<span class="tunnel__tile-tk">${h.tk}` +
      `<span class="tunnel__tile-ch" data-dir="${d}">${arrow(h.day)} ${pct(h.day)}</span>` +
      `</span>`;
    tile.appendChild(sparkline(h.spark, { w: 164, h: 30, dir: d, fill: true }));

    tile.setAttribute(
      "aria-label",
      `${h.tk}, ${h.name}. ${pct(h.day)} today. ${h.weight.toFixed(1)}% of portfolio.`
    );

    if (!reduced) {
      tile.style.opacity = "0";
      tile.style.transitionDelay = `${60 + i * 26}ms`;
      const show = () => (tile.style.opacity = "1");
      requestAnimationFrame(() => requestAnimationFrame(show));
      // Timer fallback so a background tab still ends up with visible tiles.
      setTimeout(show, 80);
    }

    space.appendChild(tile);
  });
}

/** Pointer parallax. Transform only, and skipped entirely for reduced motion. */
export function bindTunnelParallax(card, space, reduced) {
  if (reduced) return;

  const onMove = (e) => {
    const r = card.getBoundingClientRect();
    const nx = (e.clientX - r.left) / r.width - 0.5;
    const ny = (e.clientY - r.top) / r.height - 0.5;
    space.style.setProperty("--px", `${(-nx * 26).toFixed(1)}px`);
    space.style.setProperty("--py", `${(-ny * 18).toFixed(1)}px`);
  };
  const onLeave = () => {
    space.style.setProperty("--px", "0px");
    space.style.setProperty("--py", "0px");
  };

  card.addEventListener("pointermove", onMove, { passive: true });
  card.addEventListener("pointerleave", onLeave, { passive: true });
}

/** Filters the tiles from the centre search field. */
export function bindTunnelSearch(input, space, countEl) {
  const apply = () => {
    const q = input.value.trim().toLowerCase();
    const tiles = space.querySelectorAll(".tunnel__tile");
    let shown = 0;

    tiles.forEach((t) => {
      const hit =
        !q || t.dataset.ticker.toLowerCase().includes(q) || t.dataset.name.includes(q);
      t.hidden = !hit;
      if (hit) shown++;
    });

    if (!q) {
      countEl.textContent = `${tiles.length} positions`;
    } else if (shown === 0) {
      countEl.textContent = `No holding matches “${input.value.trim()}”`;
    } else {
      countEl.textContent = `${shown} of ${tiles.length} positions`;
    }
  };

  input.addEventListener("input", apply);
  apply();
}
