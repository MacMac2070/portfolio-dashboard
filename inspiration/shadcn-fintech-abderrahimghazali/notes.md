# shadcn-fintech — inspiration notes

Source repo: [abderrahimghazali/shadcn-fintech](https://github.com/abderrahimghazali/shadcn-fintech)
(Next.js 16, shadcn/ui, Tailwind v4, Recharts, Motion, MIT)
Live demo: [shadcn-fintech.vercel.app](https://shadcn-fintech.vercel.app)

Reviewed 15 Aug 2026, `/investments` page specifically. This was the first repo in the
inspiration set where the aesthetic itself landed, not just individual features: clean
dark theme, restrained colour tones (`--color-chart-1` through `-5` CSS variables
rather than a loud palette), good use of whitespace in the cards. Two interaction
patterns are being lifted from it.

## 1. Performance chart hover (price + date tooltip)

`source/performance-chart.tsx`. An area chart (Recharts `AreaChart`) with a period
toggle (1M/3M/6M/1Y) built as plain buttons, not a dropdown, state held in
`useState<number>` and sliced against `portfolioHistory`. The hover behaviour itself
is standard Recharts `ChartTooltip` / `ChartTooltipContent` from shadcn's chart
wrapper, nothing exotic, the value is in how clean the result looks: gradient fill
under the portfolio line via an SVG `linearGradient`, a dashed benchmark line (S&P
500) with no fill, and a formatter that renders the tooltip value as `$12,345` rather
than a raw number.

Relevant to the dashboard here: this is a directly reusable pattern for the equity
curve, and the dual-line-with-benchmark approach (solid portfolio line, dashed index
line) is worth carrying over for comparing performance against a relevant benchmark.

## 2. Portfolio allocation donut with hover-swap centre label

`source/portfolio-allocation.tsx`. This is the more interesting one. A donut chart
(Recharts `PieChart` with `innerRadius`/`outerRadius`) that shows "Total Value" and
the full portfolio value in the centre by default. On hover over a segment (or the
matching legend row underneath, both are wired to the same `activeIndex` state), the
centre label swaps to that sector's name, its value, and its percentage of the total.
Non-hovered segments drop to 40% opacity (`activeIndex === null || activeIndex === i
? 1 : 0.4`) so the hovered slice reads as highlighted rather than just labelled.

The mechanism is plain React state, `activeIndex: number | null`, set via
`onMouseEnter`/`onMouseLeave` on both the `Pie` element and each legend row, then a
`useMemo` (`centerLabel`) that derives what to render based on whether anything is
active. No animation library involved, this is a state-driven swap, not a Motion
transition, worth noting since it means it's simple to port.

**Why this matters for the dashboard here specifically:** the same trick applies
directly to the sector tab. Right now the plan is a sector treemap (from
live-portfolio-tracker's inspiration) plus a separate KPI strip. This pattern
suggests a cheaper alternative or complement: hover a sector in the treemap or a pie
chart, and have the KPI strip's total swap to show that sector's contribution instead
of the whole portfolio, using the exact same `activeIndex`-driven state swap rather
than a new component or a fetch.

## 3. Font: Geist / Geist Mono

Loaded in `src/app/layout.tsx`:

```tsx
import { Geist, Geist_Mono } from "next/font/google"

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] })
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] })
```

applied on `<html>` as `${geistSans.variable} ${geistMono.variable} h-full antialiased
font-sans`, then mapped in `globals.css`'s `@theme inline` block to `--font-sans` /
`--font-mono`, with `html { @apply font-sans; }` making it the global default. Geist is
Vercel's own typeface and is free on Google Fonts, so it's a one-line swap into any
Next.js `layout.tsx`.

## 4. Logos: static files by domain name, not a live API

`src/data/seed.ts` has a small helper:

```ts
const logo = (domain: string) => `/logos/${domain.replace(/\./g, "-")}.png`
```

Every holding calls it with its own domain (e.g. `logo("apple.com")`). The images are
pre-downloaded static files sitting in `/public/logos/`, rendered in
`holdings-table.tsx` via `<Image src={h.logo} width={28} height={28} unoptimized
className="rounded-full" />`. No runtime logo API call at all.

Directly relevant to the open logo-fetching decision on the dashboard here: this repo
sidesteps the "fetch live" question by treating logos as static assets checked into the
repo. No rate limits, no runtime failure mode, but manual upkeep every time a new
ticker enters the holdings.

## 5. Red/green colour tones and the up/down arrow

Straight Tailwind, no custom palette. Gains: `text-emerald-600 dark:text-emerald-400`.
Losses: `text-rose-600 dark:text-rose-400`. Paired with lucide-react's `TrendingUp` /
`TrendingDown` icons rendered inline before the percentage figure in
`holdings-table.tsx`. These are separate from the app's OKLCH `--chart-1` through
`--chart-5` tokens, which only drive the pie and area charts elsewhere in the app.

**Bonus, tied to the same cell:** the Current Price cell flashes on every simulated
price tick, a Motion `motion.span` animating `backgroundColor` from a translucent
emerald/rose tint down to `transparent` over `0.6s`, keyed by `${holding.id}-${price}`
so it retriggers on each update. Cheap, no layout shift, worth lifting alongside the
colour/arrow pattern if the Finnhub WebSocket prices are going to be live-updating on
the Holdings tab.

## What wasn't taken

The rest of the app (cards page, 3D-flip credit card, crypto candlestick chart,
drag-and-drop dashboard, budget rings, Clerk auth, 3D globe on the auth pages) was
reviewed but isn't relevant, none of it maps to a personal read-only portfolio
dashboard. Only the two investments-page chart components above are being carried
forward.
