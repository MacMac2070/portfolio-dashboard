# Expense tracker — build plan

The module's own roadmap. The README says what this is; this file says how it
gets finished, in what order, and where the hard stops are. Status markers are
kept current as steps complete.

## Goal

Pull bank transactions for **Lloyds** and **HSBC** through an Open Banking
aggregator, dedupe them, categorise them with a local LLM, and store them so a
dashboard tab can be added later without schema changes. Backend only in this
phase.

## Ground rules

1. **Isolation.** Everything lives under `/expense-tracker`. Nothing imports
   from `dashboard/`; nothing there knows this exists. Shared *infrastructure*
   (this machine, the Ollama box, later a Supabase project) is fine; shared
   *code, tables, routes and config* are not. Tables carry the
   `expense_tracker_` prefix so they stay visually separate even inside a
   shared database.
2. **The dedupe contract.** `source_transaction_id` is UNIQUE and inserts are
   INSERT OR IGNORE — a re-pulled transaction is **skipped, never upserted**,
   because `category` and `reviewed` may carry manual edits a pull must never
   overwrite. This rule outranks convenience everywhere.
3. **Degrade, never abort.** An expired consent is logged loudly
   ("consent expired for {bank}, needs manual re-auth") and skipped. A
   sleeping Ollama leaves `category` null; nulls are re-attempted on the next
   run (`store.uncategorised`). One bank failing never blocks the other.
4. **GBP only.** No multi-currency this phase.
5. **No UI.** The `reviewed`/`category` columns exist so the review flow can
   arrive later without a schema change.
6. The one permitted touch outside this folder: the repo `.gitignore` lines
   for `.env.expense-tracker`, `data/`, `logs/` (done).

## Local-first, and the migration map

The original plan targeted Vercel Functions + Supabase + Vercel Cron. This
repo runs Python + launchd + local files, so the module does too — decision:
build local-first, push to Supabase/Vercel once the dashboard core is done.
Every seam is pre-cut so that push is a port, not a rewrite:

| concern | now (local) | later (cloud) | the seam |
|---|---|---|---|
| storage | SQLite `data/expenses.sqlite3` via `lib/store.py` | Supabase, `db/schema.postgres.sql` | `store.py` is the ONLY code that touches storage — swap its internals, callers unchanged |
| schema | `db/schema.sqlite.sql` (applied on first open) | `db/schema.postgres.sql` (kept current in lockstep) | migration = run the Postgres file, copy rows |
| OAuth endpoints | `api/server.py`, manual, 127.0.0.1:5180 | `/api/oauth-start` + `/api/oauth-callback` functions | handlers already split per route |
| schedule | `api/cron_pull.py` + launchd 08:00/20:00 (`scripts/install-pull-job.sh`) | Vercel Cron `0 */12 * * *` | `pull()` is one function; the runner is one file |
| config | `.env.expense-tracker` via `lib/config.py` | Vercel env vars | `config.get()` reads process env first already |

## Architecture and contracts

- **`lib/aggregator_client.py`** — the provider seam. `AggregatorClient`
  interface: `get_auth_url(bank, state)`, `exchange_token(code)` →
  `{access_token, refresh_token|None, consent_expires_at}`, `get_accounts`,
  `get_transactions(token, account_id, since)`. `EnableBankingClient` and
  `YapilyClient` are stubs raising NotImplementedError that names what docs
  are missing; `client()` picks by `AGGREGATOR_PROVIDER`. Implementations own
  all HTTP detail; callers never see provider shapes — they get the
  `Account`/`Transaction` dataclasses.
- **`lib/store.py`** — schema apply, connection CRUD (one row per bank,
  re-consent replaces), `insert_transactions` → (new ids, skipped count),
  `mark_pulled` (incremental window), `uncategorised` (the healing query).
- **`lib/categorise.py`** — `qwen3.5:9b` on `OLLAMA_HOST`, thinking off,
  forced JSON, temperature 0, fixed list: groceries, transport, dining,
  subscriptions, utilities, shopping, transfers, other. A model answer not on
  the list counts as no answer. No arithmetic in the model, ever.
- **`api/server.py`** — the consent flow, run by hand per bank while the
  human completes the bank's own login. State token maps callback → bank.
- **`api/cron_pull.py`** — per §6 of the original plan: valid connections →
  accounts → transactions since `last_pulled_at` → insert → categorise NEW
  rows only → healing pass. `--dry-run` prints instead of writing.
- **`scripts/test_sandbox_pull.py`** — the §8 gate: one sandbox cycle,
  payloads printed, nothing written. Runs before anything is scheduled.

## Order of operations

- [x] **1. Scaffold** — structure, stub client, both schemas, config, store,
      categoriser, consent server, pull, dry-run script, launchd installer
      (commit `5bf996f`). Store dedupe, categoriser degrade, and the 501
      stub-surfacing all smoke-tested.
- [ ] **2. HUMAN: aggregator signup** — register with Enable Banking or
      Yapily, register an app (client id + secret), confirm **both Lloyds and
      HSBC** appear as connectable institutions in the sandbox. Copy
      `.env.expense-tracker.example` → `.env.expense-tracker`, fill it in,
      bring back the API docs.
- [ ] **3. Implement the client** — the four methods against the chosen
      provider's real docs. Sandbox base URLs first.
- [ ] **4. HUMAN: consent, sandbox** — `python3 api/server.py`, visit
      `/oauth/start?bank=lloyds`, complete the flow; repeat for `hsbc`.
- [ ] **5. Payload gate** — `python3 scripts/test_sandbox_pull.py`; read the
      raw shapes; adjust the client's mapping if the dataclasses were guessed
      wrong. Nothing schedules before this passes eyeball review.
- [ ] **6. Live pull, sandbox** — `python3 api/cron_pull.py` for real writes;
      verify dedupe on a second run (0 new, N skipped); verify categories
      land when the Windows box is up and heal when it was not.
- [ ] **7. Schedule** — `scripts/install-pull-job.sh` (08:00/20:00). Let it
      run several sandbox cycles unattended; check `logs/pull.log`.
- [ ] **8. ⛔ HARD STOP** — confirm with the human before switching
      `.env.expense-tracker` to production bank credentials. Production
      consent is a deliberate, separate act.
- [ ] **9. (Separate phase)** — Supabase/Vercel migration per the map above,
      then the dashboard tab / review UI, designed fresh at that point.

## Risks and open items

- **Provider choice** decides step 3's shape; nothing else changes (that is
  what the seam is for). If neither sandbox carries both banks, the choice is
  forced by coverage, not preference.
- **90-day re-consent** is manual forever in this design. The pull's loud
  log line is the only reminder; if that proves too quiet, a notification
  hook can ride the same log path later.
- **`consent_expires_at` semantics** vary by provider (consent vs token
  expiry) — pin down which one the field stores during step 3 and note it in
  the client.
- **Amount sign convention** (spend negative) is assumed in the dataclass;
  verify against real payloads at step 5 before any reporting is built on it.
- The `qwen3.5:9b` model name and the Windows box address come from the other
  project's setup ("Agent 2") — confirm both when filling in `OLLAMA_HOST`.
