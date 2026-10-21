-- Row-level security for the dashboard. GitHub Actions uses the service role (bypasses RLS).
-- Replace the email below with the one allowed to approve drafts.
alter table runs      enable row level security;
alter table items     enable row level security;
alter table drafts    enable row level security;
alter table llm_calls enable row level security;

-- The ops dashboard is public: anyone can read run, cost and item stats.
create policy "public read runs"      on runs      for select using (true);
create policy "public read items"     on items     for select using (true);
create policy "public read llm_calls" on llm_calls for select using (true);

-- Drafts: the public sees approved ones (the /p/[week] one-pager); the owner sees and edits all.
create policy "public read approved drafts" on drafts for select using (status = 'approved');
create policy "owner reads drafts"  on drafts for select using (auth.jwt() ->> 'email' = 'owner@example.com');
create policy "owner updates drafts" on drafts for update using (auth.jwt() ->> 'email' = 'owner@example.com');

-- LinkedIn token: only the owner's signed-in session can read or write it. No public policy at all.
alter table linkedin_auth enable row level security;
create policy "owner manages linkedin" on linkedin_auth for all
  using (auth.jwt() ->> 'email' = 'owner@example.com') with check (auth.jwt() ->> 'email' = 'owner@example.com');

alter table linkedin_orgs enable row level security;
create policy "owner manages linkedin orgs" on linkedin_orgs for all
  using (auth.jwt() ->> 'email' = 'owner@example.com') with check (auth.jwt() ->> 'email' = 'owner@example.com');

-- Agent #2 (Substack newsroom)
alter table substack_drafts enable row level security;
alter table agent_steps     enable row level security;
alter table agent_registry  enable row level security;
alter table agent_settings  enable row level security;
alter table prompt_versions enable row level security;

create policy "owner reads substack"   on substack_drafts for select using (auth.jwt() ->> 'email' = 'owner@example.com');
create policy "owner updates substack" on substack_drafts for update using (auth.jwt() ->> 'email' = 'owner@example.com');

-- The run timeline and the agent roster are public ops data, like runs.
create policy "public read agent_steps"    on agent_steps    for select using (true);
create policy "public read agent_registry" on agent_registry for select using (true);
create policy "public read agent_settings" on agent_settings for select using (true);
create policy "owner writes agent_settings" on agent_settings for all
  using (auth.jwt() ->> 'email' = 'owner@example.com') with check (auth.jwt() ->> 'email' = 'owner@example.com');
create policy "owner manages prompts" on prompt_versions for all
  using (auth.jwt() ->> 'email' = 'owner@example.com') with check (auth.jwt() ->> 'email' = 'owner@example.com');

-- Live timeline: stream new steps to the dashboard.
alter publication supabase_realtime add table agent_steps;

-- Post images: a public bucket (they get published anyway). Uploads use the service key from GitHub Actions.
insert into storage.buckets (id, name, public) values ('substack-images', 'substack-images', true) on conflict (id) do nothing;
