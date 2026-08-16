/* The cached symbol directory, client side.
 *
 * ~16k symbols, 190 KB gzipped. Downloaded once and kept in IndexedDB, so the
 * search bar filters the whole list locally with no request per keystroke —
 * which is the entire point of caching it rather than querying a server.
 *
 * IndexedDB rather than localStorage: localStorage would hold 0.8 MB inside its
 * quota, but every page load would pay a synchronous main-thread JSON.parse of
 * that string before anything rendered. IDB stores the array natively and off
 * the main thread.
 *
 * The version handshake is `?have=<version>`, not an ETag. serve.py sets
 * no-store on every response — load-bearing for the CSS/JS reload loop — and
 * every /api/* answers 200 with meta.error, which a 304 cannot do.
 */

const DB_NAME = "portfolio-dashboard";
const STORE = "kv";
const KEY = "directory";

let rows = [];        // [symbol, name, type, exchange, currency]
let lower = [];       // parallel lowercase names, built once
let bySymbol = new Map();
let status = "idle";  // idle | loading | ready | failed
let meta = { version: "", count: 0, generated_at: null };

/* ---------------- IndexedDB ---------------- */

function openDb() {
  return new Promise((resolve, reject) => {
    let req;
    try { req = indexedDB.open(DB_NAME, 1); } catch (err) { reject(err); return; }
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function idb(mode, fn) {
  return openDb().then((db) => new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, mode);
    const req = fn(tx.objectStore(STORE));
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  }));
}

const readCache = () => idb("readonly", (s) => s.get(KEY));
const writeCache = (value) => idb("readwrite", (s) => s.put(value, KEY));

/* ---------------- index ---------------- */

function adopt(payload) {
  rows = payload.rows || [];
  lower = rows.map((r) => (r[1] || "").toLowerCase());
  bySymbol = new Map(rows.map((r, i) => [r[0].toLowerCase(), i]));
  meta = {
    version: payload.version || payload.meta?.version || "",
    count: rows.length,
    generated_at: payload.generated_at || payload.meta?.generated_at || null,
  };
}

/* ---------------- public ---------------- */

export function state() { return status; }
export function info() { return { ...meta }; }
export function has(symbol) { return bySymbol.has(String(symbol || "").toLowerCase()); }

/**
 * Ranked local matches.
 *
 * Exact symbol first, then symbol prefix, then name prefix, then name
 * substring — so typing "intc" cannot be pushed below something merely
 * containing those letters. A linear scan of 16k rows is single-digit
 * milliseconds; a trie is the escape hatch if this list ever grows an order of
 * magnitude.
 */
export function find(query, limit = 5) {
  const q = String(query || "").trim().toLowerCase();
  if (!q || !rows.length) return [];

  const hits = [];
  const exact = bySymbol.get(q);
  if (exact !== undefined) hits.push([0, 0, exact]);

  // The full scan is deliberate. Capping the raw loop truncated it
  // ALPHABETICALLY, not by relevance — rows arrive ordered by symbol, and a
  // query like "in" matches thousands of A-companies on "Inc" in their name,
  // so the cap filled inside the A's and INTC was never even considered. Worse,
  // those junk hits then counted towards THIN and suppressed the wider search.
  // A linear pass over ~16k short strings is a few milliseconds; correctness
  // wins. Bail early only on enough tier-0/1 hits, which nothing later can beat.
  let strong = exact !== undefined ? 1 : 0;
  for (let i = 0; i < rows.length; i++) {
    if (i === exact) continue;
    const sym = rows[i][0].toLowerCase();
    const name = lower[i];
    let tier = -1;
    if (sym.startsWith(q)) tier = 1;
    else if (name.startsWith(q)) tier = 2;
    else if (name.includes(q)) tier = 3;
    else if (sym.includes(q)) tier = 4;
    if (tier < 0) continue;
    hits.push([tier, rows[i][0].length, i]);
    if (tier <= 1 && ++strong >= limit) break;
  }

  hits.sort((a, b) => a[0] - b[0] || a[1] - b[1] || (rows[a[2]][0] < rows[b[2]][0] ? -1 : 1));
  return hits.slice(0, limit).map(([, , i]) => {
    const [symbol, name, type, exchange, currency, venue] = rows[i];
    // `venue` is resolved server-side from the one venue table, so search and
    // the stock page cannot disagree about what to call an exchange.
    return { symbol, name, kind: type || "", exchange, currency, venue: venue || exchange };
  });
}

/**
 * Cache first, then check the server. The search bar is usable from the cached
 * copy before any network call completes; a failed check keeps whatever we had.
 */
export async function load() {
  status = "loading";
  let cached = null;
  try {
    cached = await readCache();
    if (cached?.rows?.length) {
      adopt(cached);
      status = "ready";
    }
  } catch {
    /* IDB unavailable (private window, quota) — the network path still works. */
  }

  try {
    const have = cached?.version || "";
    const res = await fetch(`api/directory?have=${encodeURIComponent(have)}`,
      { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();

    if (payload.unchanged) {
      status = rows.length ? "ready" : "failed";
      return { source: "cache", ...meta };
    }
    if (payload.meta?.error && !payload.rows?.length) throw new Error(payload.meta.error);

    const record = {
      version: payload.meta?.version || "",
      generated_at: payload.meta?.generated_at || null,
      rows: payload.rows || [],
    };
    adopt(record);
    status = rows.length ? "ready" : "failed";
    try { await writeCache(record); } catch { /* over quota — memory copy still serves */ }
    return { source: "network", ...meta };
  } catch (err) {
    // A cached copy outranks a failed refresh: yesterday's 16k symbols are far
    // more useful than an empty directory.
    status = rows.length ? "ready" : "failed";
    return { source: rows.length ? "cache" : "none", error: String(err.message || err), ...meta };
  }
}
