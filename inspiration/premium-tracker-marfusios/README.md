# premium-tracker — inspiration notes

Source repo: [Marfusios/premium-tracker](https://github.com/Marfusios/premium-tracker)
(React/TS/Vite/Tailwind/Recharts, client-side only, MIT, live at premiumtracker.app)

Reviewed as part of the portfolio dashboard research pass (14 Aug 2026). Full context
in the `primo-grafana-dashboard.md` project doc and the "Portfolio Dashboard: Design &
Feature Reference" write-up.

## What's worth taking

**NAV Flow Sankey (`source/components/dashboard/NAVSankeyChart.tsx`).** The standout
idea from this repo. Two-stage structure rather than a flat one: source flows
(Starting NAV, Deposits, M2M Gains, Interest & FX Gains) all feed into a single
"Gross Value" node, which then splits into Ending NAV versus the cost stack
(Withdrawals, M2M Losses, Commissions, Fees & Tax, Interest Paid). That separation
(how big did the pot get, vs what got skimmed off it) is a cleaner model than dumping
every source and every cost into one flat set of flows.

The colour-per-link and custom node/tooltip components (`CustomNode`, `CustomLink`,
`CustomTooltip`) are the reusable bit: Recharts' `Sankey` needs custom node/link
renderers to look right, this repo's implementation is a solid reference for that.

For the version built here: needs FX isolated as its own labelled node rather than
netted into "Interest & FX Gains" as this repo does, given the concentration in HK,
China, Korea and Japan. That's a gap in this repo's design, not something to copy.

**Monthly Income chart (`source/components/dashboard/MonthlyIncomeChart.tsx`).**
Stacked bar chart with a mode toggle (Income vs P/L) and a custom tooltip that lists
every series plus a bold total at the bottom. The tooltip pattern (itemised
breakdown + total, `CustomTooltip` in this file) is the transferable part more than
the specific series. This repo's income series (options premium, SYEP income) don't
apply since there's no options-selling strategy here — dividends only, plus interest
if IBKR pays out on cash balances.

**Monthly Costs Breakdown (`source/components/dashboard/FeesChart.tsx`).** Same
stacked-bar-plus-detailed-tooltip pattern applied to commissions, other fees, sales
tax and interest paid. This one transfers close to as-is, IBKR fee categories map
directly.

**Legend-click-to-toggle-series.** Both bar charts wire the Recharts `Legend`
`onClick` handler to an `activeSeries` state object and use `hide={!activeSeries.x}`
on each `Bar`. Cheap pattern, worth keeping.

## What's in `source/` and why

Downloaded via the GitHub repo MCP directly from the upstream repo, kept as a
read-only reference, not meant to be run as-is (there's no build config bundled here,
just the component and data-layer files). Two things worth reading together:

- `components/dashboard/NAVSankeyChart.tsx`, `MonthlyIncomeChart.tsx`, `FeesChart.tsx`
  — the three chart components described above.
- `types.ts` — defines `NAVChange` and `MonthlySummary`, the shapes the charts consume.
- `services/csvParser.ts` — how this repo turns an IBKR Flex CSV export into those
  shapes. The relevant part is the `Change in NAV` section parsing (search for
  `sections['Change in NAV']`): it reads Starting Value, Mark-to-Market, Deposits &
  Withdrawals, Interest, Change in Interest Accruals, Other Fees, Commissions, Sales
  Tax, Other FX Translations and Ending Value directly off IBKR's own "Change in NAV"
  Flex report section. Since the dashboard here pulls via IBKR's Flex Web Service too,
  this confirms that section exists in the Flex XML/CSV and gives the exact field
  names to request, rather than needing to derive NAV attribution from raw trades.
  The rest of `csvParser.ts` (wheel cycle analysis, AROC, short put tracking) is
  options-strategy specific and not relevant here.

## What to leave out

Options premium and SYEP income series, wheel cycle summary, short put risk
components, public shareable dashboard view. None of it applies without an
options-selling strategy in play, and the public view is out of scope for a personal
tool.
