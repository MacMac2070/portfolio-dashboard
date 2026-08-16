# premium-tracker — screenshots

Screenshots Mac sent from his own premium-tracker instance, showing the three
patterns being taken from this repo. Kept alongside the source code and notes as a
visual reference for what "done" looks like.

## The three things being taken from this build

**1. NAV Flow Sankey (`nav-flow-sankey.png`).** A two-stage flow rather than a flat
waterfall: Deposits and M2M Gains feed into a single "Gross Value" node, which then
splits out into Ending NAV versus what got taken off the top (Commissions, Fees &
Tax, Withholding Tax, Interest Paid). This is the standout idea from the repo, worth
building close to as-is, with FX pulled out as its own labelled node given the
HK/China/Korea/Japan concentration (this repo nets FX into M2M gains, which loses
that visibility).

**2. Monthly Income Tracker (`monthly-income-tracker.png`).** Stacked monthly bar
chart with an Income/P&L toggle at the top right and a rich hover tooltip that lists
every income source for that month plus a bold total at the bottom. The series in
this repo (options premium, SYEP income, interest) don't map directly since there's
no options-selling strategy here, this version would be dividends-only (plus interest
if IBKR pays out on cash balances), but the toggle and tooltip mechanics carry over
directly.

**3. Monthly Costs Breakdown (`monthly-costs-breakdown.png`).** Same stacked-bar and
detailed-tooltip pattern applied to commissions, fees, sales tax and interest paid.
This one transfers almost as-is, IBKR's fee categories map cleanly onto the same
series without needing to drop anything.

The common thread across all three: the itemised hover tooltip with a bold total is
the reusable interaction pattern, more than any single chart type.
