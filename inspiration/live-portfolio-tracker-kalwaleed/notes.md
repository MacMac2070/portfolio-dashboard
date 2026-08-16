# live-portfolio-tracker — inspiration notes

Source repo: [Kalwaleed/live-portfolio-tracker](https://github.com/Kalwaleed/live-portfolio-tracker)
(React 19/TS/Vite/Tailwind 4/Recharts, client-side only, no backend)

Reviewed the running demo (`npm run dev`, screenshot below) on 14 Aug 2026. Overall
layout and holdings-book approach didn't land, but two specific implementation
details are worth lifting into the dashboard here.

## 1. The font

`index.css` at the repo root:

```css
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
...
--font-mono: 'JetBrains Mono', 'IBM Plex Mono', monospace;
--font-sans: 'IBM Plex Sans', -apple-system, system-ui, sans-serif;
```

JetBrains Mono is the whole terminal's body font, not just the numeric columns
(`font-family: var(--font-mono)` is set on `html, body, #root`). Two details that
matter as much as the font choice itself:

- `font-feature-settings: 'tnum' on, 'lnum' on;` turns on tabular figures, this is
  what keeps digits aligned in fixed-width columns rather than the font choice alone.
- `font-size: 12px` with `line-height: 1.45` is the actual terminal-density sizing.

## 2. The scrolling ticker tape

`components/terminal/Ticker.tsx`: a plain React component that takes the first 24
holdings and renders the same row twice back to back (`row('a')` then `row('b')`),
then lets a CSS animation slide the combined strip left by exactly 50% on an
infinite loop. That duplication is the trick for a seamless scroll with no JS
animation loop or requestAnimationFrame involved.

```css
@keyframes ticker-slide {
  0% { transform: translateX(0); }
  100% { transform: translateX(-50%); }
}
.ticker-slide { animation: ticker-slide 60s linear infinite; }
```

(defined in `index.css`, applied via the `ticker-slide` class in `Ticker.tsx`)

The green/red flip on the percentage figure comes from a `plClass` helper in
`utils/format.ts` (imported into `Ticker.tsx` alongside `fmtNum`/`fmtPct`), worth
grabbing too if the same colour logic is needed elsewhere on the dashboard.

## What wasn't taken

The rest of the terminal (holdings book layout, sector treemap, AI risk audit via
Gemini, keyboard-first command bar, help overlay) was reviewed but didn't make the
cut, noted separately in the "Portfolio Dashboard: Design & Feature Reference"
project doc. Only the font and the ticker animation are being carried forward from
this repo.
