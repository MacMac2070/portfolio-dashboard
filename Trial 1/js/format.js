/* ==========================================================================
   format.js — every number the dashboard shows passes through here, so
   currency placement, precision and signing are consistent everywhere.
   Display currency is GBP.
   ========================================================================== */

const GBP = new Intl.NumberFormat("en-GB", {
  style: "currency",
  currency: "GBP",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const GBP_WHOLE = new Intl.NumberFormat("en-GB", {
  style: "currency",
  currency: "GBP",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

/** £1,234.56 */
export const money = (n) => GBP.format(n);

/** £1,235 — for headline figures where pence are noise */
export const moneyWhole = (n) => GBP_WHOLE.format(n);

/** £1.2M / £3.4B / £12.3k — compact, for values that would otherwise wrap */
export function moneyCompact(n) {
  const a = Math.abs(n);
  if (a >= 1e9) return `£${trim(n / 1e9)}B`;
  if (a >= 1e6) return `£${trim(n / 1e6)}M`;
  if (a >= 1e4) return `£${trim(n / 1e3)}k`;
  return GBP_WHOLE.format(n);
}

const trim = (n) => n.toFixed(1).replace(/\.0$/, "");

/** +£19,340 / −£2,180 — always signed, using a real minus sign */
export function moneySigned(n, { whole = true } = {}) {
  const body = (whole ? GBP_WHOLE : GBP).format(Math.abs(n));
  return `${sign(n)}${body}`;
}

/** +2.31% / −0.48% / 0.00% — two decimals, always signed when non-zero */
export function pct(n, dp = 2) {
  return `${sign(n)}${Math.abs(n).toFixed(dp)}%`;
}

/** 23.89% — unsigned, for weights and allocations */
export const pctPlain = (n, dp = 2) => `${n.toFixed(dp)}%`;

/** A real typographic minus, not a hyphen — it aligns with the digits. */
export function sign(n) {
  if (n > 0) return "+";
  if (n < 0) return "−";
  return "";
}

/** Direction token used to pick colour AND the arrow glyph, so hue is never
    the only channel carrying gain/loss. */
export function dir(n) {
  if (n > 0) return "up";
  if (n < 0) return "down";
  return "flat";
}

export const arrow = (n) => (n > 0 ? "↑" : n < 0 ? "↓" : "→");

/** HH:MM in the viewer's locale, for the "last updated" indicator */
export const clock = (d) =>
  d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });

export const clockSec = (d) =>
  d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

/** Degrees with hemisphere letters, e.g. 51.5072° N, 0.1276° W */
export function latlon(lat, lon) {
  const la = `${Math.abs(lat).toFixed(4)}° ${lat >= 0 ? "N" : "S"}`;
  const lo = `${Math.abs(lon).toFixed(4)}° ${lon >= 0 ? "E" : "W"}`;
  return `${la}, ${lo}`;
}
