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
alter table items add column if not exists appeal integer;  -- 1..5 how well it would do as a LinkedIn post, from the scorer
alter table items add column if not exists tldr boolean not null default false;  -- a brief TL;DR story
alter table items add column if not exists also jsonb;  -- the brief's corroborating links [{url, source}]

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
alter table drafts add column if not exists series_no integer;  -- "AI Musing #N", assigned at publish time

-- Company pages to @mention when a post names them. LinkedIn's lookup API needs partner access,
-- so the owner adds each company's numeric ID once from the queue.
create table if not exists linkedin_orgs (
  name    text primary key,        -- the company page's name exactly as LinkedIn shows it
  org_id  text not null,           -- numeric id, e.g. from a jobs URL: linkedin.com/jobs/search/?f_C=12345
  aliases text[] not null default '{}'   -- other spellings to match in posts, e.g. {"Google"} for "Google DeepMind"
);

-- ---------------------------------------------------------------------------------------------------------
-- Agent #2: Substack newsroom (a supervisor + specialists). See agents/substack/.

create table if not exists substack_drafts (
  id              text primary key,
  slot            text not null,              -- run date, e.g. 2026-10-22 (Wed and Sat)
  seed_draft_id   text references drafts(id), -- the LinkedIn musing this expands, if any
  brief           jsonb,                      -- research brief: thesis, outline, facts with evidence
  title           text not null,
  subtitle        text,
  body_md         text not null,
  body_html       text not null,              -- rendered from body_md by agents/substack/render.py (no raw HTML)
  images          jsonb,                      -- [{kind: cover|inline|chart, url, alt, caption, after_section}]
  sources         jsonb,
  judge_scores    jsonb,                      -- editor rubric + humanizer + overall
  fact_report     jsonb,
  final_report    jsonb,
  revisions       integer not null default 0,
  status          text not null default 'pending',   -- pending | approved | rejected | published
  edited_title    text,
  edited_subtitle text,
  edited_body_md  text,
  published_url   text,
  created_at      timestamptz not null,
  approved_at     timestamptz,
  run_id          text references runs(id)
);
create index if not exists substack_drafts_created_idx on substack_drafts (created_at);

-- One row per supervisor decision and per specialist turn: the live run timeline in the dashboard.
create table if not exists agent_steps (
  id          integer generated always as identity primary key,
  run_id      text references runs(id),
  seq         integer not null,
  agent       text not null,
  specialist  text not null,                -- supervisor | research | writer | ...
  kind        text not null,                -- decision | work
  reason      text,                         -- the supervisor's reason, or why a fallback was used
  summary     text,                         -- one line for the timeline
  output      jsonb,
  cost_usd    numeric not null default 0,
  latency_ms  integer,
  status      text not null,                -- running | ok | failed | skipped
  started_at  timestamptz not null,
  ended_at    timestamptz
);
create index if not exists agent_steps_run_idx on agent_steps (run_id, seq);

-- Written by the code on every run: which specialists exist, their defaults and the models they may use.
create table if not exists agent_registry (
  agent             text not null,
  specialist        text not null,
  position          integer not null default 0,
  role              text not null,
  description       text,
  default_model     text,
  allowed_models    jsonb,
  required          boolean not null default false,   -- can't be switched off (e.g. research, writer)
  default_max_calls integer,
  updated_at        timestamptz not null,
  primary key (agent, specialist)
);

-- Owner's overrides from the dashboard. A missing row or null field means "use the registry default".
create table if not exists agent_settings (
  agent             text not null,
  specialist        text not null,
  enabled           boolean not null default true,
  model             text,
  max_calls         integer,
  prompt_version_id text,
  updated_at        timestamptz not null,
  primary key (agent, specialist)
);

-- Versioned prompts, editable in the dashboard. v1 of each is seeded from the repo's prompt files.
create table if not exists prompt_versions (
  id          text primary key,
  agent       text not null,
  specialist  text not null,
  version     integer not null,
  body        text not null,
  note        text,
  created_at  timestamptz not null
);
create index if not exists prompt_versions_idx on prompt_versions (agent, specialist, version);

-- Learning loop (core/lessons.py): the owner's feedback in /queue, and what the agents learned from it.
alter table drafts add column if not exists feedback jsonb;  -- {"reasons": [...], "note": "..."} on reject
alter table drafts add column if not exists my_take text;  -- the owner's one-line take for a rewrite
alter table drafts add column if not exists learned_at timestamptz;  
alter table substack_drafts add column if not exists feedback jsonb;  
alter table substack_drafts add column if not exists my_take text;  
alter table substack_drafts add column if not exists learned_at timestamptz;  
create table if not exists lessons (
  id         text primary key,
  agent      text not null,
  lesson     text not null,
  kind       text,
  evidence   integer not null default 1,   -- decisions that back it up
  draft_ids  jsonb,
  active     boolean not null default true, -- the owner can switch one off from the dashboard
  created_at timestamptz not null,
  updated_at timestamptz not null
);
create index if not exists lessons_agent_idx on lessons (agent, active);

-- Spending dashboard: model spend per day, agent, run kind, tier and model. The web app reads this instead of
-- llm_calls, which PostgREST caps at 1,000 rows per request. security_invoker keeps the caller's RLS.
create or replace view spend_by_day with (security_invoker = true) as
select (c.created_at at time zone 'utc')::date as day, coalesce(r.agent, 'unknown') as agent, coalesce(r.kind, 'unknown') as kind,
       c.tier, coalesce(c.model, 'unknown') as model, count(*) as calls, count(*) filter (where not c.ok) as failed,
       sum(coalesce(c.prompt_tokens, 0) + coalesce(c.completion_tokens, 0)) as tokens, sum(c.cost_usd) as cost_usd
from llm_calls c left join runs r on r.id = c.run_id
group by 1, 2, 3, 4, 5;
