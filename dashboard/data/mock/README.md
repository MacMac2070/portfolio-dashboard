# Mock dataset

No real account data lives in this repository. These CSVs are an invented
four-month history for a five-figure GBP portfolio over the same fifteen
contracts the app tracks, and `dashboard/scripts/build_mock_data.py` turns
them into every file the dashboard reads.

| File | One row per | Columns |
|---|---|---|
| `positions.csv` | held contract at the end of the window | the Flex Open Positions shape: `report_date, con_id, symbol, exchange, currency, asset, quantity, mark_price, value, average_cost, unrealized_pnl, fx_to_base, account_id` |
| `transactions.csv` | fill | the Flex Trades shape: `exec_id, time, con_id, symbol, currency, exchange, side, quantity, price, commission, asset, fx_to_base, realized_pnl, open_close, taxes, commission_currency` |
| `cash_transactions.csv` | deposit, dividend, withholding tax, interest or fee | `tx_id, date, type, amount, currency, fx_to_base, symbol, con_id` |
| `nav_history.csv` | weekday | `date, nav_gbp, cash_gbp, stock_gbp, accruals_gbp` |
| `nav_change.csv` | calendar month, plus the whole window | `from_date, to_date` and the Change in NAV names that are non-zero |
| `fx_rates.csv` | weekday and currency | `date, currency, rate` into GBP |
| `corporate_actions.csv` | action (one 2-for-1 split) | `action_id, con_id, symbol, date, code, quantity, currency, fx_to_base, description` |

Money is in the row's own currency; `fx_to_base` is that day's rate into
pounds. London lines are in pounds, as IBKR reports them, not pence.

Build the app's files somewhere and reconcile them:

    python3 dashboard/scripts/build_mock_data.py --out /tmp/mockdata --check

Install them as the app's data (refuses if a real ledger is already there):

    python3 dashboard/scripts/build_mock_data.py --install --check

The `--check` step pins "today" to the data's own last day, so the lag check
reflects the data rather than the calendar. To move the demo to a newer date,
regenerate the CSVs with `--write-csv --end YYYY-MM-DD` (a weekday) and
commit them; a test asserts the committed CSVs and the synthesis agree.
