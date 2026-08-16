/* ==========================================================================
   worldmap.js — geographic exposure.

   The landmasses are generated, not an image: a dot is drawn wherever a grid
   point falls inside a coarse continent polygon (or within a small island
   circle). That keeps the map a single vector layer that recolours cleanly
   between themes and stays crisp at any size.

   Pins are exchange locations, sized by the share of the portfolio traded
   there. Hue is assigned from the validated categorical palette by a fixed
   venue order — never by rank — so filtering or re-sorting never repaints a
   region. The legend carries every region's name and weight as text.
   ========================================================================== */

import { pctPlain, latlon } from "./format.js";

const NS = "http://www.w3.org/2000/svg";

/* Coarse outlines, [lon, lat]. Deliberately low-fidelity — at this dot pitch
   anything finer is invisible. */
const LAND = [
  // North America
  [[-168,65],[-160,71],[-140,70],[-125,70],[-110,68],[-95,70],[-82,73],[-75,68],[-64,60],[-56,52],[-66,45],[-70,42],[-75,37],[-81,31],[-80,25],[-90,29],[-97,26],[-105,20],[-96,16],[-92,15],[-105,22],[-115,30],[-124,40],[-124,48],[-135,57],[-150,59],[-165,60]],
  // Greenland
  [[-55,60],[-45,60],[-30,68],[-22,73],[-30,82],[-45,83],[-60,80],[-58,70]],
  // South America
  [[-81,8],[-75,11],[-60,11],[-52,5],[-50,0],[-35,-5],[-38,-13],[-48,-25],[-58,-34],[-62,-40],[-65,-48],[-70,-54],[-75,-50],[-73,-40],[-71,-30],[-70,-18],[-75,-14],[-81,-6]],
  // Africa
  [[-17,15],[-16,21],[-10,27],[0,32],[10,33],[20,32],[32,31],[35,24],[38,18],[43,12],[51,12],[48,5],[41,-2],[40,-10],[35,-19],[32,-26],[27,-33],[18,-34],[12,-18],[9,-2],[3,5],[-8,5],[-13,9]],
  // Eurasia
  [[-10,36],[-9,44],[-2,43],[3,42],[7,44],[13,45],[19,40],[24,40],[28,37],[30,41],[36,45],[45,42],[50,38],[48,30],[44,25],[52,25],[57,23],[62,25],[70,25],[73,20],[77,8],[80,15],[88,22],[92,21],[97,10],[100,5],[105,10],[110,20],[118,24],[122,32],[127,38],[132,43],[140,45],[143,52],[155,57],[162,60],[175,62],[180,65],[170,66],[150,70],[135,72],[120,74],[105,78],[90,76],[75,74],[60,72],[45,68],[30,66],[32,60],[24,60],[22,56],[12,54],[8,54],[4,52],[1,51],[-2,49],[-5,44]],
  // Scandinavia
  [[5,58],[5,62],[11,64],[15,68],[20,70],[28,70],[30,66],[25,61],[22,60],[18,59],[12,58],[8,58]],
  // Australia
  [[114,-22],[113,-26],[115,-34],[121,-34],[129,-32],[135,-35],[138,-35],[141,-38],[146,-39],[150,-37],[153,-31],[153,-25],[146,-19],[142,-11],[136,-12],[130,-11],[126,-14],[122,-17],[117,-20]],
];

/* Landmasses too small to survive a polygon at this scale: [lon, lat, radius°] */
const ISLES = [
  [138,37,3.0],[133,34,2.1],[131,32,1.5],[142,43,2.3],   // Japan
  [-2,53,2.3],[-4,56,1.9],[-8,53,1.4],                   // British Isles & Ireland
  [-19,65,2.0],                                          // Iceland
  [172,-42,2.4],[175,-38,1.9],                           // New Zealand
  [102,-2,2.5],[110,-2,2.3],[118,-2,2.1],[122,-4,1.9],   // Indonesia
  [145,-6,2.8],                                          // Papua New Guinea
  [122,13,2.1],                                          // Philippines
  [121,24,0.9],                                          // Taiwan
  [81,7,1.0],                                            // Sri Lanka
  [47,-19,2.5],                                          // Madagascar
  [-78,21,1.5],                                          // Cuba
  [147,-42,1.2],                                         // Tasmania
];

const BOUNDS = { lon0: -170, lon1: 180, lat0: -56, lat1: 78 };

/* Chip offsets in projected units, hand-set so no two labels collide and
   none of them covers its own pin. */
const CHIP_OFFSETS = {
  NYQ: [-10, 56],
  LSE: [-30, -46],
  HKG: [46, 46],
  SGX: [16, 54],
  KRX: [64, -32],
};

function inPoly(lon, lat, poly) {
  let hit = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    if (yi > lat !== yj > lat && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) {
      hit = !hit;
    }
  }
  return hit;
}

const isLand = (lon, lat) =>
  LAND.some((p) => inPoly(lon, lat, p)) ||
  ISLES.some(([x, y, r]) => (lon - x) ** 2 + (lat - y) ** 2 < r * r);

/**
 * @param {SVGSVGElement} svg
 * @param {HTMLElement} chipLayer
 * @param {Array} regions   from data.js
 * @param {boolean} reduced
 */
export function renderWorldMap(svg, chipLayer, regions, reduced) {
  const W = 1000;
  const H = 560;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.setAttribute("role", "img");
  svg.setAttribute(
    "aria-label",
    "World map showing portfolio exposure by exchange: " +
      regions.map((r) => `${r.label} ${pctPlain(r.weight, 1)}`).join(", ")
  );
  svg.textContent = "";
  chipLayer.textContent = "";

  const project = (lon, lat) => [
    ((lon - BOUNDS.lon0) / (BOUNDS.lon1 - BOUNDS.lon0)) * W,
    ((BOUNDS.lat1 - lat) / (BOUNDS.lat1 - BOUNDS.lat0)) * H,
  ];

  // ---- Dot matrix -------------------------------------------------------
  const PITCH = 8.6; // in projected units — tight enough that coastlines read
  const DOT_R = 2.4;
  const dots = document.createElementNS(NS, "g");
  dots.setAttribute("class", "geo__dots");

  const cols = Math.floor(W / PITCH);
  const rows = Math.floor(H / PITCH);
  let path = "";

  for (let r = 0; r <= rows; r++) {
    for (let c = 0; c <= cols; c++) {
      const px = c * PITCH + PITCH / 2;
      const py = r * PITCH + PITCH / 2;
      const lon = BOUNDS.lon0 + (px / W) * (BOUNDS.lon1 - BOUNDS.lon0);
      const lat = BOUNDS.lat1 - (py / H) * (BOUNDS.lat1 - BOUNDS.lat0);
      if (!isLand(lon, lat)) continue;
      // One path of tiny circles is far cheaper than ~2000 <circle> nodes.
      path += `M${px.toFixed(1)},${py.toFixed(1)}m${-DOT_R},0a${DOT_R},${DOT_R} 0 1,0 ${DOT_R * 2},0a${DOT_R},${DOT_R} 0 1,0 ${-DOT_R * 2},0`;
    }
  }

  const dotPath = document.createElementNS(NS, "path");
  dotPath.setAttribute("d", path);
  dotPath.setAttribute("class", "geo__dot");
  dots.appendChild(dotPath);
  svg.appendChild(dots);

  // ---- Great-circle-ish arcs -------------------------------------------
  // Drawn from the home exchange to the two largest overseas exposures.
  const byId = Object.fromEntries(regions.map((r) => [r.id, r]));
  const home = byId.LSE || regions[0];
  const overseas = regions.filter((r) => r.id !== home.id).slice(0, 2);

  overseas.forEach((dest, i) => {
    const [x1, y1] = project(home.lon, home.lat);
    const [x2, y2] = project(dest.lon, dest.lat);
    const mx = (x1 + x2) / 2;
    const my = (y1 + y2) / 2 - Math.abs(x2 - x1) * 0.28 - 20;

    const arc = document.createElementNS(NS, "path");
    arc.setAttribute("d", `M${x1.toFixed(1)},${y1.toFixed(1)}Q${mx.toFixed(1)},${my.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}`);
    arc.setAttribute("class", `geo__arc ${i === 0 ? "geo__arc--primary" : "geo__arc--muted"}`);
    svg.appendChild(arc);

    const len = arc.getTotalLength();
    arc.style.setProperty("--len", len);
    if (reduced) {
      arc.classList.add("is-drawn");
    } else {
      // A plain timer, not rAF — a background tab would otherwise leave the
      // arc permanently offset by its own dash length, i.e. invisible.
      setTimeout(() => arc.classList.add("is-drawn"), 120 + i * 140);
    }
  });

  // ---- Pins + chips -----------------------------------------------------
  const maxW = Math.max(...regions.map((r) => r.weight));

  regions.forEach((r) => {
    const [x, y] = project(r.lon, r.lat);
    const hue = `var(--sector-${r.slot})`;
    const rad = 4.5 + (r.weight / maxW) * 5.5;

    const g = document.createElementNS(NS, "g");
    g.setAttribute("class", "geo__pin");

    const halo = document.createElementNS(NS, "circle");
    halo.setAttribute("cx", x.toFixed(1));
    halo.setAttribute("cy", y.toFixed(1));
    halo.setAttribute("r", (rad * 2.6).toFixed(1));
    halo.setAttribute("fill", hue);
    halo.setAttribute("class", "geo__pin-halo");
    g.appendChild(halo);

    const core = document.createElementNS(NS, "circle");
    core.setAttribute("cx", x.toFixed(1));
    core.setAttribute("cy", y.toFixed(1));
    core.setAttribute("r", rad.toFixed(1));
    core.setAttribute("fill", hue);
    core.setAttribute("class", "geo__pin-core");
    g.appendChild(core);

    const title = document.createElementNS(NS, "title");
    title.textContent =
      `${r.label} (${r.venue}) — ${pctPlain(r.weight, 1)} of portfolio, ` +
      `${r.members.length} position${r.members.length === 1 ? "" : "s"}. ${latlon(r.lat, r.lon)}`;
    g.appendChild(title);

    svg.appendChild(g);

    // Chip, offset off the pin so it never sits on top of it.
    const [dx, dy] = CHIP_OFFSETS[r.id] || [0, 48];
    const chip = document.createElement("div");
    chip.className = "geo__chip tipchip";
    chip.style.left = `${((x + dx) / W) * 100}%`;
    chip.style.top = `${((y + dy) / H) * 100}%`;
    chip.innerHTML =
      `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">` +
      `<path d="M8 14.5s5-4.2 5-8a5 5 0 1 0-10 0c0 3.8 5 8 5 8Z"/><circle cx="8" cy="6.5" r="1.8"/></svg>` +
      `<span>${r.label}</span><b>${pctPlain(r.weight, 1)}</b>`;
    chipLayer.appendChild(chip);
  });
}
