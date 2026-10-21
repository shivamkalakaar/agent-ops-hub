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
  run_id       text references runs(id),
  origin       text not null default 'rss'   -- rss | brief (pushed by ~/AI_update_agent after each daily brief)
);
create index if not exists items_published_idx on items (published_at);
alter table items add column if not exists origin text not null default 'rss';

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

-- LinkedIn publishing (owner-only; see policies.sql)
alter table drafts add column if not exists published_at timestamptz;
alter table drafts add column if not exists linkedin_url text;

create table if not exists linkedin_auth (
  owner_email  text primary key,
  person_urn   text not null,
  name         text,
  access_token text not null,               -- member token, ~60 days; refreshed by reconnecting
  expires_at   timestamptz not null,
  updated_at   timestamptz not null
);
alter table drafts add column if not exists series_no integer;   -- "AI Musing #N", assigned at publish time

-- Company pages to @mention when a post names them. LinkedIn's lookup API needs partner access,
-- so the owner adds each company's numeric ID once from the queue.
create table if not exists linkedin_orgs (
  name    text primary key,        -- the company page's name exactly as LinkedIn shows it
  org_id  text not null,           -- numeric id, e.g. from a jobs URL: linkedin.com/jobs/search/?f_C=12345
  aliases text[] not null default '{}'   -- other spellings to match in posts, e.g. {"Google"} for "Google DeepMind"
);
