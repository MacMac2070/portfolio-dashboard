/* ==========================================================================
   main.js — boots the dashboard and wires every section together.
   ========================================================================== */

import { holdings, sectors, regions, totals, equityCurve, gaugeScale } from "./data.js";
import { money, moneyWhole, moneySigned, pct, pctPlain, dir, arrow, clock, clockSec } from "./format.js";
import { renderAsciiCurve, animateAsciiCurve } from "./ascii-chart.js";
import { renderGauge } from "./gauge.js";
import { renderBarcode, playBarcode } from "./barcode.js";
import { sparkline } from "./sparkline.js";
import { renderTunnel, bindTunnelParallax, bindTunnelSearch } from "./tunnel.js";
import { renderWorldMap } from "./worldmap.js";
import { countUp } from "./countup.js";
import { currentOverride, setState, watchFeed } from "./states.js";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* --------------------------------------------------------------------------
   Theme
   -------------------------------------------------------------------------- */
function initTheme() {
  const saved = localStorage.getItem("pd-theme");
  if (saved === "light" || saved === "dark") {
    document.documentElement.dataset.theme = saved;
  }

  $("#themeToggle").addEventListener("click", () => {
    const isLight =
      document.documentElement.dataset.theme === "light" ||
      (!document.documentElement.dataset.theme &&
        matchMedia("(prefers-color-scheme: light)").matches);
    const next = isLight ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("pd-theme", next);
    // The map and gauge read theme colours through CSS variables, so they
    // recolour on their own — only the ASCII field needs a repaint.
    renderAsciiCurve($("#heroAscii"), equityCurve, 1);
  });
}

/* --------------------------------------------------------------------------
   Topbar feed indicator
   -------------------------------------------------------------------------- */
function paintFeed(state) {
  const feed = $("#feed");
  const now = new Date();
  feed.dataset.feed = state === "stale" ? "stale" : "live";
  $("#feedLabel").textContent =
    state === "stale" ? `Reconnecting · last updated ${clock(now)}` : `Live · ${clock(now)}`;
  $("#staleTime").textContent = clock(now);
}

/* --------------------------------------------------------------------------
   Hero
   -------------------------------------------------------------------------- */
function buildHero() {
  const valueEl = $("#heroValue");
  const deltaEl = $("#heroDelta");
  const d = dir(totals.pnl);

  countUp(valueEl, totals.value, (n) => moneyWhole(n), { reduced, duration: 300 });
  deltaEl.dataset.dir = d;
  countUp(
    deltaEl,
    totals.pnl,
    (n) => `${moneySigned(n)} ${arrow(totals.pnl)}`,
    { reduced, duration: 300 }
  );

  valueEl.setAttribute(
    "aria-label",
    `Total portfolio value ${money(totals.value)}, ${moneySigned(totals.pnl)} ` +
      `(${pct(totals.ret)}) since purchase`
  );

  const ascii = $("#heroAscii");
  animateAsciiCurve(ascii, equityCurve, reduced);

  let raf;
  new ResizeObserver(() => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => renderAsciiCurve(ascii, equityCurve, 1));
  }).observe(ascii);

  $("#heroTick").textContent = clockSec(new Date());
}

/* --------------------------------------------------------------------------
   Gauge
   -------------------------------------------------------------------------- */
function buildGauge() {
  const num = $("#gaugeNum");
  countUp(num, totals.ret, (n) => pct(n), { reduced, duration: 300 });
  $("#gaugeCost").textContent = moneyWhole(totals.cost);
  $("#gaugeValue").textContent = moneyWhole(totals.value);
  $("#gaugeStart").textContent = totals.inceptionYear;
  $("#gaugeEnd").textContent = totals.currentYear;
  renderGauge($("#gaugeArc"), totals.ret, gaugeScale, reduced);
}

/* --------------------------------------------------------------------------
   Holdings matrix
   -------------------------------------------------------------------------- */
function buildMatrix() {
  const list = $("#matrixList");
  const venues = $("#matrixVenues");
  list.textContent = "";
  venues.textContent = "";

  const top = [...holdings].sort((a, b) => b.value - a.value).slice(0, 11);

  top.forEach((h) => {
    const d = dir(h.day);

    const row = document.createElement("div");
    row.className = "matrix__row";
    row.innerHTML =
      `<span class="matrix__ticker">${h.tk}</span>` +
      `<span class="matrix__name">${h.name}</span>` +
      `<span class="chip">${pctPlain(h.weight, 1)}</span>`;

    const spark = document.createElement("span");
    spark.className = "matrix__spark";
    spark.appendChild(sparkline(h.spark, { w: 58, h: 16, dir: d }));
    row.appendChild(spark);

    const delta = document.createElement("span");
    delta.className = "matrix__delta";
    delta.dataset.dir = d;
    delta.textContent = `${arrow(h.day)} ${pct(h.day)}`;
    row.appendChild(delta);

    list.appendChild(row);

    const venue = document.createElement("div");
    venue.className = "matrix__venue";
    venue.textContent = h.venue === "NYQ" ? "NASDAQ / NYSE" : { LSE: "LSE", HKG: "HKEX", SGX: "SGX", KRX: "KRX" }[h.venue];
    venues.appendChild(venue);
  });

  // Full table behind the chevron
  const tbody = $("#htableBody");
  tbody.textContent = "";
  [...holdings]
    .sort((a, b) => b.value - a.value)
    .forEach((h) => {
      const cls = dir(h.ret) === "down" ? "neg" : dir(h.ret) === "flat" ? "flat" : "pos";
      const dcls = dir(h.day) === "down" ? "neg" : dir(h.day) === "flat" ? "flat" : "pos";
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td class="htable__tk">${h.tk}</td>` +
        `<td class="htable__hide-sm">${h.name}</td>` +
        `<td class="t-num">${money(h.value)}</td>` +
        `<td class="t-num htable__hide-sm">${money(h.cost)}</td>` +
        `<td class="t-num ${cls}">${moneySigned(h.pnl)}</td>` +
        `<td class="t-num ${cls}">${arrow(h.ret)} ${pct(h.ret)}</td>` +
        `<td class="t-num ${dcls}">${arrow(h.day)} ${pct(h.day)}</td>` +
        `<td class="t-num">${pctPlain(h.weight, 2)}</td>`;
      tbody.appendChild(tr);
    });

  const toggle = $("#matrixToggle");
  const panel = $("#matrixExpand");
  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", String(!open));
    panel.dataset.open = String(!open);
    $("#matrixToggleLabel").textContent = open ? "All 15 positions" : "Collapse";
  });

  renderBarcode($("#barcode"), sectors, reduced);
}

/* --------------------------------------------------------------------------
   Tunnel + map
   -------------------------------------------------------------------------- */
function buildTunnel() {
  const space = $("#tunnelSpace");
  renderTunnel(space, holdings, reduced);
  bindTunnelParallax($("#tunnelCard"), space, reduced);
  bindTunnelSearch($("#tunnelSearch"), space, $("#tunnelCount"));
}

function buildMap() {
  renderWorldMap($("#geoSvg"), $("#geoChips"), regions, reduced);

  const legend = $("#geoLegend");
  legend.textContent = "";
  regions.forEach((r) => {
    const item = document.createElement("span");
    item.className = "geo__legend-item";
    item.innerHTML =
      `<i class="geo__legend-swatch" style="background:var(--sector-${r.slot})"></i>` +
      `${r.label} <b>${pctPlain(r.weight, 1)}</b>`;
    legend.appendChild(item);
  });
}

/* --------------------------------------------------------------------------
   Entrance
   -------------------------------------------------------------------------- */
function playEntrance() {
  const items = $$(".reveal");
  if (reduced) {
    items.forEach((el) => el.classList.add("is-in"));
    playBarcode($("#barcode"));
    return;
  }

  items.forEach((el, i) => {
    el.style.setProperty("--reveal-delay", `${i * 60}ms`);
  });

  const reveal = (el) => {
    if (el.classList.contains("is-in")) return;
    el.classList.add("is-in");
    if (el.contains($("#barcode"))) playBarcode($("#barcode"));
  };

  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        reveal(e.target);
        io.unobserve(e.target);
      });
    },
    { rootMargin: "0px 0px -8% 0px", threshold: 0.06 }
  );

  items.forEach((el) => io.observe(el));

  // IntersectionObserver is driven by the rendering pipeline, so a tab loaded
  // in the background delivers no callbacks and every section would sit at
  // opacity 0. It recovers on focus, but a dashboard should never be capable
  // of rendering blank — so reveal anything still hidden after a short grace
  // period, animation or not.
  setTimeout(() => {
    items.forEach((el) => {
      el.style.setProperty("--reveal-delay", "0ms");
      reveal(el);
    });
    io.disconnect();
  }, 1200);
}

/* --------------------------------------------------------------------------
   Boot
   -------------------------------------------------------------------------- */
function boot() {
  initTheme();

  const override = currentOverride();

  // Loading and empty render no live content at all — nothing to build.
  if (override === "loading" || override === "empty") {
    setState(override);
    return;
  }

  setState("ready");
  buildHero();
  buildGauge();
  buildMatrix();
  buildTunnel();
  buildMap();
  paintFeed("ready");
  playEntrance();

  if (override === "stale") {
    setState("stale");
    paintFeed("stale");
    return;
  }

  // Real feed behaviour: tick, and mark the numbers stale if it goes quiet.
  const feed = watchFeed(paintFeed);
  feed.tick();
  const beat = setInterval(() => {
    $("#heroTick").textContent = clockSec(new Date());
    feed.tick();
    paintFeed("ready");
  }, 15_000);

  window.addEventListener("pagehide", () => clearInterval(beat));
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
