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
