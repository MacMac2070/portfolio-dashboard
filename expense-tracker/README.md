# Expense tracker (backend, local-first)

Pulls bank transactions for Lloyds and HSBC through an Open Banking
aggregator (Enable Banking or Yapily), dedupes them, categorises them with a
local LLM, and stores them for a future dashboard tab. **Backend only — no
frontend in this phase.**

Completely isolated from the portfolio dashboard: nothing here imports from
`dashboard/`, and nothing there knows this folder exists. Shared
*infrastructure* (the machine, the Ollama box) is fine; shared *code, tables
and config* are not.

## Local-first

The original build plan targets Vercel Functions + Supabase + Vercel Cron.
This repo runs Python + launchd + local files, so this module does too, with
the seams cut where the migration will happen:

| now (local) | later (cloud) |
|---|---|
| `lib/store.py` → SQLite `data/expenses.sqlite3` | Supabase client, `db/schema.postgres.sql` |
| `api/server.py` (manual consent server, :5180) | `/api/oauth-start` + `/api/oauth-callback` functions |
| `api/cron_pull.py` + `scripts/install-pull-job.sh` (launchd 08:00/20:00) | Vercel Cron `0 */12 * * *` |
| `.env.expense-tracker` | Vercel env vars |

`db/schema.postgres.sql` is the Supabase schema, kept current alongside the
SQLite one, so migration is a data copy plus swapping `store.py`'s internals.

## Status

- [x] Scaffold: config, store (dedupe on `source_transaction_id`, INSERT OR
      IGNORE — manual `category`/`reviewed` edits are never overwritten),
      categoriser (fixed list, forced JSON, Ollama-down → null), consent
      server, pull job, sandbox dry-run script, launchd installer
- [ ] **Manual (you):** register with Enable Banking or Yapily, confirm
      Lloyds + HSBC in their sandbox, fill `.env.expense-tracker`
- [ ] Implement the four methods in `lib/aggregator_client.py` against the
      chosen provider's docs
- [ ] Run consent per bank: `python3 api/server.py` → visit
      `http://127.0.0.1:5180/oauth/start?bank=lloyds` (then `hsbc`)
- [ ] `python3 scripts/test_sandbox_pull.py` — inspect the payload shape
- [ ] A few clean sandbox cycles, then `scripts/install-pull-job.sh`
- [ ] STOP and confirm before production bank credentials

## Non-goals this phase

No UI, no multi-currency (GBP only), no automated re-consent (manual, ~90
days — expired consent is logged loudly by the pull, never fatal), no
review flow (the `reviewed`/`category` columns exist so it can come later
without a schema change).

Interpreter: `/opt/anaconda3/bin/python3`. Stdlib only, no new dependencies.
