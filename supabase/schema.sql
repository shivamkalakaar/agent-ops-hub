-- Agent Ops Hub schema (Postgres / Supabase). Run once in the Supabase SQL editor.
-- core/db.py derives the local SQLite schema from this file, so keep it to portable column types.

create table if not exists runs (
  id          text primary key,
  agent       text not null,
  kind        text not null,                -- collect | draft | eval
  status      text not null,                -- running | succeeded | failed | budget_exceeded
  started_at  timestamptz not null,
  ended_at    timestamptz,
  cost_usd    numeric not null default 0,
  trace_url   text,
  git_sha     text,
  error       text,
  stats       jsonb
);

create table if not exists items (
  url          text primary key,             -- also the dedupe key across runs
  title        text not null,
  source       text not null,
  layer        text,
  importance   integer,                      -- 1..5 from the scorer, null until scored
  why          text,
  summary      text,
  published_at timestamptz,
  collected_at timestamptz not null,
  run_id       text references runs(id)
);
create index if not exists items_published_idx on items (published_at);

create table if not exists drafts (
  id             text primary key,
  week           text not null,              -- ISO week, e.g. 2026-W43
  angle          jsonb,
  post_md        text not null,
  onepager_html  text not null,
  sources        jsonb,
  judge_scores   jsonb,
  revisions      integer not null default 0,
  status         text not null default 'pending',   -- pending | approved | rejected
  edited_post_md text,
  created_at     timestamptz not null,
  approved_at    timestamptz,
  run_id         text references runs(id)
);
create index if not exists drafts_status_idx on drafts (status, created_at);

create table if not exists llm_calls (
  id                integer generated always as identity primary key,
  run_id            text references runs(id),
  node              text not null,
  tier              text not null,
  model             text,
  prompt_tokens     integer,
  completion_tokens integer,
  cost_usd          numeric not null default 0,
  latency_ms        integer,
  ok                boolean not null,
  created_at        timestamptz not null
);
create index if not exists llm_calls_created_idx on llm_calls (created_at);
