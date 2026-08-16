/* ==========================================================================
   data.js — the portfolio. Realistic mock values, shaped exactly like a real
   feed response so a live source can replace this module without touching
   any view code. Tickers match the folders in ../Stock research/.

   Money is GBP. Weights are derived, never hand-typed, so they always sum
   to 100%.
   ========================================================================== */

/* Five sector buckets — one per barcode strip. `slot` maps to the validated
   categorical palette in tokens.css (--sector-1 … --sector-5); the order is
   the validated order and must not be shuffled. */
export const SECTORS = [
  { id: "software", slot: 1, label: "Software & internet" },
  { id: "consumer", slot: 2, label: "Consumer tech" },
  { id: "semis", slot: 3, label: "Semiconductors" },
  { id: "funds", slot: 4, label: "Index funds" },
  { id: "finance", slot: 5, label: "Financials & transport" },
];

/* Exchange venues, with the real coordinates the map plots. `slot` is a fixed
   assignment into the validated categorical palette — it is keyed to the venue
   itself, never to the region's rank, so re-sorting never repaints the map. */
export const VENUES = {
  NYQ: { slot: 1, label: "New York", venue: "NASDAQ / NYSE", lat: 40.7128, lon: -74.006 },
  LSE: { slot: 2, label: "London", venue: "LSE", lat: 51.5072, lon: -0.1276 },
  HKG: { slot: 3, label: "Hong Kong", venue: "HKEX", lat: 22.3193, lon: 114.1694 },
  SGX: { slot: 4, label: "Singapore", venue: "SGX", lat: 1.3521, lon: 103.8198 },
  KRX: { slot: 5, label: "Seoul", venue: "KRX", lat: 37.5665, lon: 126.978 },
};

/* value  = current market value in GBP
   cost   = book cost in GBP
   day    = today's move in percent
   Nothing else is stored: weight, P&L and totals are all computed below. */
const RAW = [
  { tk: "AAPL",   name: "Apple",              sector: "consumer", venue: "NYQ", value: 18640, cost: 14980, day:  0.82 },
  { tk: "GOOGL",  name: "Alphabet",           sector: "software", venue: "NYQ", value: 14200, cost: 11340, day:  1.34 },
  { tk: "IUCS.L", name: "iShares S&P 500",    sector: "funds",    venue: "LSE", value: 12480, cost: 10920, day:  0.41 },
  { tk: "AMZN",   name: "Amazon",             sector: "consumer", venue: "NYQ", value: 11270, cost:  9640, day: -0.63 },
  { tk: "META",   name: "Meta Platforms",     sector: "software", venue: "NYQ", value:  9850, cost:  7180, day:  2.07 },
  { tk: "HSBA",   name: "HSBC Holdings",      sector: "finance",  venue: "LSE", value:  9730, cost:  8890, day: -0.18 },
  { tk: "MU",     name: "Micron Technology",  sector: "semis",    venue: "NYQ", value:  8940, cost:  6420, day:  3.16 },
  { tk: "XDJP.L", name: "Xtrackers Japan",    sector: "funds",    venue: "LSE", value:  7640, cost:  7010, day:  0.24 },
  { tk: "SMSN",   name: "Samsung Electronics",sector: "semis",    venue: "KRX", value:  7310, cost:  6740, day: -1.12 },
  { tk: "0700",   name: "Tencent Holdings",   sector: "software", venue: "HKG", value:  6420, cost:  5880, day:  0.00 },
  { tk: "HY9H",   name: "SK hynix",           sector: "semis",    venue: "KRX", value:  5880, cost:  3960, day:  4.28 },
  { tk: "ES3.SI", name: "STI ETF",            sector: "funds",    venue: "SGX", value:  5120, cost:  4830, day:  0.36 },
  { tk: "C6L",    name: "Singapore Airlines", sector: "finance",  venue: "SGX", value:  4180, cost:  4610, day: -0.94 },
  { tk: "3115.HK",name: "iShares Asia 50",    sector: "funds",    venue: "HKG", value:  3260, cost:  3040, day:  0.58 },
  { tk: "0293",   name: "Cathay Pacific",     sector: "finance",  venue: "HKG", value:  2620, cost:  2760, day: -1.47 },
];

/* --------------------------------------------------------------------------
   Deterministic pseudo-random series. Seeded from the ticker so a reload
   never reshuffles the sparklines — the dashboard has to look stable.
   -------------------------------------------------------------------------- */
function seeded(seed) {
  let s = 0;
  for (let i = 0; i < seed.length; i++) s = (s * 31 + seed.charCodeAt(i)) | 0;
  s = Math.abs(s) || 1;
  return () => {
    s = (s * 1103515245 + 12345) & 0x7fffffff;
    return s / 0x7fffffff;
  };
}

/** A plausible price walk that lands on the holding's real total return. */
function walk(seed, points, totalReturn) {
  const rnd = seeded(seed);
  const out = [];
  let v = 100;
  for (let i = 0; i < points; i++) {
    const drift = (totalReturn / points) * 100;
    const noise = (rnd() - 0.5) * 5.2;
    v = Math.max(12, v + drift + noise);
    out.push(v);
  }
  // Pin the ends so the sparkline agrees with the stated return.
  const target = 100 * (1 + totalReturn);
  const shift = target - out[out.length - 1];
  return out.map((p, i) => p + shift * (i / (points - 1)));
}

/* --------------------------------------------------------------------------
   Derived portfolio
   -------------------------------------------------------------------------- */
export const holdings = RAW.map((h) => {
  const pnl = h.value - h.cost;
  const ret = pnl / h.cost;
  return {
    ...h,
    pnl,
    ret: ret * 100,
    dayValue: (h.value * h.day) / (100 + h.day),
    spark: walk(h.tk, 24, ret),
  };
});

const sum = (k) => holdings.reduce((a, h) => a + h[k], 0);

export const totals = (() => {
  const value = sum("value");
  const cost = sum("cost");
  const dayValue = sum("dayValue");
  return {
    value,
    cost,
    pnl: value - cost,
    ret: ((value - cost) / cost) * 100,
    dayValue,
    dayPct: (dayValue / (value - dayValue)) * 100,
    positions: holdings.length,
    inceptionYear: 2020,
    currentYear: 2025,
  };
})();

/* Weights are computed, so the barcode always totals 100%. */
holdings.forEach((h) => {
  h.weight = (h.value / totals.value) * 100;
});

export const sectors = SECTORS.map((s) => {
  const members = holdings
    .filter((h) => h.sector === s.id)
    .sort((a, b) => b.value - a.value);
  const value = members.reduce((a, h) => a + h.value, 0);
  const cost = members.reduce((a, h) => a + h.cost, 0);
  return {
    ...s,
    members,
    value,
    weight: (value / totals.value) * 100,
    ret: ((value - cost) / cost) * 100,
  };
});

/* Geographic exposure, aggregated by venue for the map. */
export const regions = Object.entries(VENUES)
  .map(([id, v]) => {
    const members = holdings.filter((h) => h.venue === id);
    const value = members.reduce((a, h) => a + h.value, 0);
    return {
      id,
      ...v,
      members,
      value,
      weight: (value / totals.value) * 100,
    };
  })
  .filter((r) => r.value > 0)
  .sort((a, b) => b.value - a.value);

/* The portfolio equity curve — 5 years of monthly closes from book cost to
   today's value. Drawn as the ASCII field behind the hero figure. */
export const equityCurve = (() => {
  const months = 60;
  const rnd = seeded("portfolio-equity");
  const start = 48200; // first deposit
  const end = totals.value;
  const out = [];
  let v = start;
  for (let i = 0; i < months; i++) {
    const t = i / (months - 1);
    const trend = start + (end - start) * Math.pow(t, 1.12);
    // Mean-revert toward the trend, with drawdowns that read as real.
    v = v + (trend - v) * 0.34 + (rnd() - 0.47) * 3600;
    out.push(Math.max(start * 0.72, v));
  }
  out[0] = start;
  out[months - 1] = end;
  return out;
})();

/* Gauge scale. The two ticks are meaningful reference points — break-even
   and the +25% mark — not decoration. */
export const gaugeScale = { min: -25, max: 50, ticks: [0, 25] };
