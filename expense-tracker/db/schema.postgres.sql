-- The Supabase/Postgres schema, for the LATER migration — local-first runs on
-- schema.sqlite.sql next to this file. The expense_tracker_ prefix on every
-- table is deliberate: it keeps this module visually and namespace-separated
-- from the portfolio tables even when it shares a Supabase project.

create table expense_tracker_bank_connections (
  id uuid primary key default gen_random_uuid(),
  bank text not null,               -- 'lloyds' or 'hsbc'
  access_token text not null,
  refresh_token text,
  consent_expires_at timestamptz not null,
  -- Tracks the incremental pull window (build plan §6).
  last_pulled_at timestamptz,
  created_at timestamptz default now()
);

create table expense_tracker_transactions (
  id uuid primary key default gen_random_uuid(),
  bank text not null,
  account_id text not null,
  source_transaction_id text not null unique,
  date date not null,
  amount numeric not null,
  currency text not null default 'GBP',
  raw_description text not null,
  category text,
  reviewed boolean not null default false,
  created_at timestamptz default now()
);

-- Dedupe contract: source_transaction_id carries the unique constraint, and an
-- insert with a conflicting id is SKIPPED, never upserted — `category` and
-- `reviewed` may have been hand-edited after the fact and a pull must never
-- overwrite that. (insert ... on conflict (source_transaction_id) do nothing)
