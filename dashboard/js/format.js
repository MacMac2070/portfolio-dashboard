/* Number, currency and percentage formatting.
 *
 * One place decides how a figure reads, so the same value never appears two
 * ways on the same screen. GBP throughout; the minus sign is U+2212, which
 * aligns with digits, not the hyphen, which does not.
 */

const MINUS = "−";

const gbp0 = new Intl.NumberFormat("en-GB", {
  style: "currency", currency: "GBP",
  minimumFractionDigits: 0, maximumFractionDigits: 0,
});
const gbp2 = new Intl.NumberFormat("en-GB", {
  style: "currency", currency: "GBP",
  minimumFractionDigits: 2, maximumFractionDigits: 2,
});
const plain2 = new Intl.NumberFormat("en-GB", {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
});

/** Swap the hyphen Intl emits for a true minus. */
function fixMinus(text) {
  return text.replace(/-/g, MINUS);
}

/** £50,332 — the dashboard's default money format. */
export function money(value, { decimals = 0 } = {}) {
  if (value == null || !Number.isFinite(value)) return "—";
  return fixMinus((decimals === 2 ? gbp2 : gbp0).format(value));
}

/** £1.2M / £3.4B once figures outrun the tile. */
export function moneyCompact(value) {
  if (value == null || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  const sign = value < 0 ? MINUS : "";
  if (abs >= 1e9) return `${sign}£${(abs / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${sign}£${(abs / 1e6).toFixed(1)}M`;
  return money(value);
}

/**
 * Signed money for a change: +£1,554 / −£925.
 * The sign is part of the value, so gain and loss never rely on colour alone.
 */
export function moneySigned(value, opts) {
  if (value == null || !Number.isFinite(value)) return "—";
  const body = money(Math.abs(value), opts);
  return value < 0 ? `${MINUS}${body}` : `+${body}`;
}

/** +2.31% — always signed; decimals follow `digits`. One cached formatter per
 *  precision: round-tripping through the fixed 2dp `plain2` silently re-rounded
 *  every caller that asked for 1dp or 0dp back to 2 (+0.00% where +0.0% was
 *  meant). */
const pctFormats = new Map();
export function pctSigned(value, digits = 2) {
  if (value == null || !Number.isFinite(value)) return "—";
  let fmt = pctFormats.get(digits);
  if (!fmt) {
    fmt = new Intl.NumberFormat("en-GB", {
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    });
    pctFormats.set(digits, fmt);
  }
  const body = `${fmt.format(Math.abs(value))}%`;
  return value < 0 ? `${MINUS}${body}` : `+${body}`;
}

/** 31% — unsigned share, for weights. */
export function pct(value, digits = 1) {
  if (value == null || !Number.isFinite(value)) return "—";
  // fixMinus, like every other export here: a raw hyphen is narrower than a
  // digit, so a negative margin would sit a pixel out of line in a column of
  // figures that is otherwise decimal-aligned.
  return fixMinus(`${value.toFixed(digits)}%`);
}

/**
 * One unit for a whole statement, chosen from its largest figure.
 *
 * `compact()` above picks a unit per value, which is right for a KPI tile and
 * wrong here: a statement is read down a column, so "$52.9B" above "−$267M"
 * makes two figures three orders apart look comparable. Financial statements
 * state their unit once, in the header, and print bare grouped numbers under it.
 */
export function statementScale(values) {
  const max = Math.max(0, ...values.filter(Number.isFinite).map(Math.abs));
  if (max >= 1e12) return { divisor: 1e9, unit: "billions" };
  if (max >= 1e9) return { divisor: 1e6, unit: "millions" };
  if (max >= 1e6) return { divisor: 1e3, unit: "thousands" };
  return { divisor: 1, unit: "" };
}

/** One statement cell at the statement's scale: "52,853", "−267", "—". */
export function statementValue(value, scale, { digits = 0 } = {}) {
  if (value == null || !Number.isFinite(value)) return "—";
  const scaled = value / (scale?.divisor || 1);
  return fixMinus(new Intl.NumberFormat("en-GB", {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(scaled));
}

export function qty(value) {
  if (value == null || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("en-GB", { maximumFractionDigits: 2 }).format(value);
}

/**
 * Price, with precision scaled to magnitude — a penny share needs four decimals
 * where a four-figure GDR needs two. Fixed 2dp would round LLOY's 1.1255 to
 * 1.13 and lose a day's move.
 */
export function price(value) {
  if (value == null || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  const digits = abs >= 1000 ? 2 : abs >= 10 ? 2 : abs >= 1 ? 3 : 4;
  return new Intl.NumberFormat("en-GB", {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(value);
}

/* Symbol placement for the currencies this registry actually quotes in.
 * `money()` above stays GBP-only — it is for portfolio totals, which are always
 * converted. This is the other job: showing an instrument in its own currency
 * next to the GBP figure, as the stock page does.
 *
 * GBp never appears here. The backend divides pence to pounds before the page
 * ever sees a price (adapter/units.py), so a London name arrives as GBP. */
const CURRENCY_SYMBOL = {
  GBP: "£", USD: "$", HKD: "HK$", EUR: "€",
  SGD: "S$", JPY: "¥", KRW: "₩", TWD: "NT$", CNY: "CN¥",
};

/**
 * A price in its own currency: $91.55, HK$434.80, £15.53, €1,020.
 *
 * Precision follows the same magnitude rule as `price()` — a four-figure GDR
 * reads better without decimals, a penny share needs four. An unknown currency
 * falls back to a trailing code ("1,020 PLN") rather than guessing a glyph.
 */
export function priceNative(value, currency, { ref, digits } = {}) {
  if (value == null || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  // Precision normally follows the value's own magnitude, but a figure derived
  // from a price must borrow that price's precision instead. A $1.92 move on an
  // $89 stock is "−$1.92", not "−$1.920" — the extra digit implies a precision
  // the quote does not have, and it breaks the decimal alignment with the price
  // printed beside it.
  // Tiers match the design's own figures: $4,296 / $91.55 / HK$14.05 / S$5.67
  // are all shown at 0dp above a thousand and 2dp below it. Sub-unit prices
  // keep four, where the extra digits are the whole move rather than noise.
  // (This differs from `price()` above, which carries a 3dp tier for 1–10 —
  // that one has no percentage column beside it to carry the move.)
  const basis = Number.isFinite(ref) ? Math.abs(ref) : abs;
  const dp = Number.isFinite(digits) ? digits
    : basis >= 1000 ? 0 : basis >= 1 ? 2 : 4;
  const body = new Intl.NumberFormat("en-GB", {
    minimumFractionDigits: dp, maximumFractionDigits: dp,
  }).format(abs);
  const sign = value < 0 ? MINUS : "";
  const code = String(currency || "").toUpperCase();
  const glyph = CURRENCY_SYMBOL[code];
  return glyph ? `${sign}${glyph}${body}` : `${sign}${body}${code ? ` ${code}` : ""}`;
}

/** Signed native price for a change: +$1.24 / −HK$0.43. */
export function priceNativeSigned(value, currency, opts) {
  if (value == null || !Number.isFinite(value)) return "—";
  const body = priceNative(Math.abs(value), currency, opts);
  return value < 0 ? `${MINUS}${body}` : `+${body}`;
}

/** 128,441,905 — volumes and share counts. */
export function count(value) {
  if (value == null || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("en-GB", { maximumFractionDigits: 0 }).format(value);
}

/** 438.2B / 4.79B / 96.2M — market cap, enterprise value, share counts. */
export function compact(value, currency) {
  if (value == null || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  const sign = value < 0 ? MINUS : "";
  const glyph = currency ? (CURRENCY_SYMBOL[String(currency).toUpperCase()] || "") : "";
  const unit = abs >= 1e12 ? ["T", 1e12] : abs >= 1e9 ? ["B", 1e9]
    : abs >= 1e6 ? ["M", 1e6] : abs >= 1e3 ? ["K", 1e3] : ["", 1];
  const scaled = abs / unit[1];
  const digits = unit[0] && scaled < 100 ? (scaled < 10 ? 2 : 1) : 0;
  return `${sign}${glyph}${scaled.toFixed(digits)}${unit[0]}`;
}

/** Ratios: P/E, beta. Unitless, so no currency and no sign. */
export function ratio(value, digits = 2) {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(digits);
}

/** "22 Oct 2026" — a bare date, for earnings and ex-dividend. */
export function day(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

/**
 * HTML-escape. Lifted here from marketwatch.js so search and the stock page can
 * share it — search echoes the typed query back into its empty state, which is
 * the one place in this app where user input reaches innerHTML.
 */
export function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/** Direction as a word, so it can drive an arrow and a screen-reader label. */
export function direction(value) {
  if (value == null || !Number.isFinite(value) || value === 0) return "flat";
  return value > 0 ? "up" : "down";
}

/**
 * Text arrows. No longer used by the chips — those draw lucide's trending-up /
 * trending-down through a CSS mask keyed on their direction modifier, so the
 * glyph never enters the DOM and cannot be clobbered by the in-place patch on
 * the 3s poll. Kept because `direction()` returns these three keys and a
 * text-only context (a title attribute, an aria-label) still needs a glyph.
 */
export const ARROW = { up: "↗", down: "↘", flat: "→" };

/** "25 Jul 2026, 22:20" */
export function stamp(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-GB", {
    day: "2-digit", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).replace(",", "").toUpperCase();
}

export function clock(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** Two-letter avatar for a ticker chip. */
export function initials(symbol) {
  return String(symbol || "").replace(/[^A-Za-z0-9]/g, "").slice(0, 2).toUpperCase();
}
