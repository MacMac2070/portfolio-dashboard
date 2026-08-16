# CLAUDE.md — Portfolio Dashboard Frontend Rules

## Always Do First
- **Invoke the `frontend-design` skill** before writing any frontend code, every session, no exceptions.
- **Invoke the `ui-ux-pro-max` skill** alongside it for any new page, component, or visual redesign — use it for style direction, color palette selection, layout patterns, and chart type choices before implementing, so decisions are deliberate rather than default.
- For any chart, KPI tile, sparkline, or data-viz work, also consult the **`dataviz` skill** before writing chart code.

## What This Project Is
- A **live investment portfolio dashboard** (data-driven app, not a marketing site).
- Primary display currency: **GBP (£)**.
- Design **desktop-first** (dense data: KPI row, equity curve, holdings table on screen at once), then adapt down to tablet/mobile.
- Design against **realistic mock financial data** (believable tickers, holdings, P&L values) — never `placehold.co` greyboxes or lorem ipsum for the data itself.

## Reference Images
- If a reference image is provided: match layout, spacing, typography, and color exactly. Swap in placeholder assets where needed. Do not improve or add to the design.
- If no reference image: design from scratch with high craft (see Anti-Generic Guardrails).
- Screenshot your output, compare against the reference, fix mismatches, re-screenshot. Do at least 2 comparison rounds. Stop only when no visible differences remain or the user says so.

## Anti-Generic Guardrails
- **Colors:** Never use the default Tailwind palette (indigo-500, blue-600, etc.). Pick a custom brand color and derive from it. Actively avoid generic, plain, or "safe" color choices (default grays, stock blue-on-white, the first swatch that comes to mind) — use the `ui-ux-pro-max` skill's color systems/palettes to choose something distinctive and considered, then derive tints/shades from it consistently.
- **Shadows:** Never use flat `shadow-md`. Use layered, color-tinted shadows with low opacity.
- **Typography:** Never use the same font for headings and body. Pair a display/serif with a clean sans. Apply tight tracking (`-0.03em`) on large headings, generous line-height (`1.7`) on body.
- **Gradients:** Layer multiple radial gradients. Add grain/texture via SVG noise filter for depth.
- **Animations:** Only animate `transform` and `opacity`. Never `transition-all`. Use spring-style easing.
- **Interactive states:** Every clickable element needs hover, focus-visible, and active states. No exceptions.
- **Spacing:** Use intentional, consistent spacing tokens — not random Tailwind steps.
- **Depth:** Surfaces should have a layering system (base → elevated → floating), not all sit at the same z-plane.

## Design Tokens
- Define **all** color, spacing, type-scale, radius, and elevation values as tokens (CSS variables), once — never hardcode ad-hoc values.
- Every color must have a **light-mode and dark-mode** value. Support **dark mode as a first-class theme**, not an afterthought (traders default to it).
- Spacing follows a single scale (e.g. 4 / 8 / 12 / 16 / 24 / 32 / 48). No off-scale magic numbers.

## Financial Semantics (P&L, Numbers, Currency)
- **Gain/loss color:** green = up, red = down, neutral = flat — but **never rely on hue alone**. Always pair with a sign (`+` / `−`) and/or a directional arrow so it reads for colorblind users. Define exact hex for positive / negative / neutral as tokens.
- **Number formatting:** thousands separators, consistent decimal precision, GBP symbol placement (`£1,234.56`). Use compact notation for large values (`£1.2M`, `£3.4B`).
- **Alignment:** all numeric columns use tabular figures (`font-variant-numeric: tabular-nums`) and right-align so digits line up.
- **Percentages:** consistent precision (e.g. 2 dp), always signed for changes (`+2.31%`).

## Data States (required — never ship without these)
- **Loading:** skeleton loaders for cards, table rows, and charts while data fetches. No layout shift when data lands.
- **Empty:** a designed empty-portfolio / no-positions state with helpful copy, not a blank panel.
- **Error / stale:** a clear "data disconnected" or "last updated HH:MM" indicator when the feed is stale or the connection drops.

## Charts & Data Viz
- One coherent chart **color system** derived from the brand palette — no rainbow / default category colors.
- **Equity curve / line charts:** draw-on animation on load (`transform`/`opacity` only). Restrained axes, subtle gridlines, minimal tooltips.
- **Sparklines** in table rows and KPI tiles for at-a-glance trend.
- Charts must be legible in both light and dark themes.

## Motion (extends the Animations guardrail)
- **KPI numbers:** animate/roll up to their value on load and on update (count-up), so changes are trackable.
- **Cards:** staggered entrance (opacity + small translate).
- **Charts:** draw-in / grow on load.
- Keep durations short — **150–300ms** — so the dashboard feels fast and trustworthy first, delightful second. Never delay a user seeing their P&L.
- **Always** provide a `prefers-reduced-motion: reduce` fallback that disables non-essential motion.

## Accessibility
- Meet **WCAG AA** contrast in both themes (check P&L green/red against their backgrounds specifically).
- `focus-visible` on all interactive elements (already required above).
- Respect `prefers-reduced-motion` and `prefers-color-scheme`.

## Brand Assets
- Always check the `brand_assets/` folder before designing. It may contain logos, color guides, style guides, or images.
- If assets exist there, use them. Do not use placeholders where real assets are available.
- If a logo is present, use it. If a color palette is defined, use those exact values — do not invent brand colors.

## Local Server & Screenshot Loop
- **Always serve on localhost** — never screenshot a `file:///` URL. Start the dev server in the background before taking screenshots; if one is already running, don't start a second.
- **Screenshot from localhost**, save to a `screenshots/` folder in the project root with auto-incremented names (never overwrite), then read the PNG back with the Read tool to see and analyze it directly.
- When comparing, be specific: "heading is 32px but reference shows ~24px", "card gap is 16px but should be 24px".
- Check: spacing/padding, font size/weight/line-height, colors (exact hex), alignment, border-radius, shadows, chart legibility, number alignment.
- NOTE: confirm the exact serve/screenshot commands for this machine's setup (macOS) before relying on them — do not hardcode another machine's tool paths.

## Hard Rules
- Do not use `transition-all`.
- Do not use default Tailwind blue/indigo as the primary color.
- Do not rely on color alone to convey gain/loss.
- Do not ship a data view without loading, empty, and error states.
- Do not stop after one screenshot pass.
- When a reference image is provided: match it, don't "improve" or add sections.
