/* ==========================================================================
   states.js — loading / ready / stale / empty.

   The state lives on <html data-state>, so CSS alone decides what renders.
   `?state=` forces one for review; without it the page runs its real
   sequence: loading while data resolves, then ready, then stale if the feed
   stops ticking.
   ========================================================================== */

const VALID = ["loading", "ready", "stale", "empty"];

/** How long the feed can go quiet before the numbers are marked stale. */
const STALE_AFTER = 45_000;

export function currentOverride() {
  const q = new URLSearchParams(location.search).get("state");
  return VALID.includes(q) ? q : null;
}

export function setState(state) {
  document.documentElement.dataset.state = state;
}

/**
 * Watches the feed and flips to stale when it stops ticking. Returns a
 * `tick()` you call whenever fresh data arrives.
 */
export function watchFeed(onChange) {
  let timer = null;

  const tick = () => {
    if (document.documentElement.dataset.state === "stale") {
      setState("ready");
      onChange?.("ready");
    }
    clearTimeout(timer);
    timer = setTimeout(() => {
      setState("stale");
      onChange?.("stale");
    }, STALE_AFTER);
  };

  return { tick };
}
