# Agent Ops Hub

**Live ops dashboard:** https://agent-ops-hub-dun.vercel.app

Agents run like production systems: **budget-capped, traced, evaluated and human-approved**, for about **$1 a month**.

Agent #1 is a **market-intelligence agent**. Every day it collects AI-industry news from two places: 12 RSS feeds, and the stories in my daily AI Stack Brief email (a separate agent). Once a week it drafts **one short LinkedIn post** plus a sourced one-pager. Nothing goes out until I approve it in the dashboard. Then one click publishes it to my LinkedIn profile as the next post in the **AI Musing** series.

| Layer | Choice | Why |
|---|---|---|
| Orchestration | LangGraph | Explicit state machine: critic → rewrite loop, conditional edges, testable nodes |
| Models | OpenRouter, four tiers (`config/models.yaml`) | Free model for bulk scoring, cheap model for planning, a strong model only for the words people read, critic from a *different* model family; automatic fallbacks |
| Observability | Langfuse | One trace per run, per-node cost and latency, every score attached to the trace |
| Runtime | GitHub Actions cron | Free, no server to babysit |
| State + auth | Supabase Postgres + RLS | Public read-only ops data; drafts and the LinkedIn token readable only by the owner |
| Dashboard | Next.js on Vercel | Ops overview, run drill-down, approval queue, LinkedIn publishing, public one-pagers |

## The workflows

```
daily   RSS feeds ─┐
                   ├→ dedupe (Postgres) → score importance [free model] → store
        AI brief ──┘   (pushed from the Mac right after the brief is built)

weekly  top stories (15 from the brief + 10 from RSS) → pick ONE angle → fetch the full articles
        → write post → critic + humanizer + guardrails ─┬─ rewrite ↺ (max 2)
                                                        └─ keep the best version → one-pager → pending
        → human approval → Publish → LinkedIn post "AI Musing #N" + one-pager link as the first comment
```

## Making it sound human

The hard part wasn't generating posts. It was making them not read like AI.

- **Humanizer skill** (`skills/humanizer`, MIT): its scanner checks every draft for stock AI words, em dashes and even rhythm. Its rules go into the writer's prompt word for word.
- **Structural tells**: the scanner rated an obviously AI-sounding draft 2/100 ("Clean") because it had no stock words. What gave it away was structure: a dress-up metaphor hook, an "X isn't Y. It's Z." reversal and a "My take:" closer. `core/humanizer.py` catches these and takes a point off the humanizer score for each one.
- **Weighting**: the overall score is **40% humanizer**, with the critic's seven dimensions (hook, focus, brevity, concreteness, voice, human, grounding) sharing the other 60%. Across the first draft and its rewrites, the best-ranked version is saved, so a rewrite that sounds more like AI never replaces a better one.
- **Grounding**: drafts kept failing because one-line RSS summaries forced the writer to pad. The agent now fetches the full article for the chosen story, treated strictly as data. The critic only asks for sources on factual claims, not on opinions framed as mine.

## Production controls

- **Budget guard** (`core/budget.py`): checked before *every* LLM call against daily and monthly caps (`config/budget.yaml`). If a cap is hit, the run stops cleanly with status `budget_exceeded`. A credit limit on the OpenRouter key backs this up.
- **Cost accounting**: each call's real cost, as reported by OpenRouter, goes into `llm_calls`. The dashboard shows spend per day, per tier and per draft.
- **Model fallbacks**: OpenRouter's `models` array lets a rate-limited free model fall through to a cheap paid one. The dashboard shows which models actually served each call.
- **Guardrails** (deterministic, `guardrail_violations`): ≤1,300 characters, 80–180 words, banned hype phrases, ≤2 hashtags, ≤8 list items, no links in the body, no numbers missing from the sources (the humanizer's fact guard).
- **Untrusted input**: fetched article text is fenced and labelled as data in every prompt. The one-pager is built from model JSON with everything escaped, and shown in a script-less sandboxed iframe.
- **Human in the loop**: drafts are created as `pending`. I edit, then approve or reject. Only approved drafts can be published, and only with a click and a confirm step. Approved posts, including my edits, become examples for the next week.
- **LinkedIn**: OAuth through the official Share on LinkedIn product. The token sits in a table only my signed-in session can read, so the server holds no service key. Series numbers are assigned at publish time, so rejected drafts never use one up.
- **Evals in CI** (`evals/`): on PRs that touch prompts or agent code, the draft workflow runs on 5 golden weeks built from past briefs. The PR fails if the weighted score drops more than 0.3 below `evals/baseline.json`, if the humanizer average falls below 3/5, or if any post breaks a guardrail.
- **Offline tests**: `pytest` runs both workflows end to end with a fake LLM and SQLite, with no keys and no cost.

## Cost (measured)

| Workflow | Calls | Models | Cost/run |
|---|---|---|---|
| Daily collect | 1–5 scoring batches | Gemma 4 31B (free), DeepSeek v4.1 Flash fallback | under $0.001 |
| Weekly draft | 5–9 | Haiku 5.5 to plan, Sonnet 5.5 to write, DeepSeek critic | $0.02–0.05 |
| PR evals | ~30 | same | ~$0.15–0.25 |

## Run it locally

```bash
uv sync
uv run pytest -q                                   # offline, free
cp .env.example .env && $EDITOR .env               # add OPENROUTER_API_KEY (Langfuse/DB optional)
set -a; source .env; set +a
uv run python -m agents.market_intel collect       # uses ./local.db when DATABASE_URL is unset
uv run python -m agents.market_intel draft
uv run python -m evals.run_evals --cases 2
```

**Writing style:** paste LinkedIn posts you admire into `config/style/examples.md`, separated by `---`. The file is git-ignored because the posts belong to their authors. Posts under 250 words are used as examples to imitate; longer essays only inform tone. Run `uv run python -m agents.market_intel style-guide` to distill them into `config/style/guide.md`.

## Deploy

1. **Supabase**: create a project and run `supabase/schema.sql`, then `supabase/policies.sql` with your email filled in, in the SQL editor. Under Auth → URL configuration, set the Site URL to your Vercel URL and add `<url>/queue` as a redirect.
2. **Langfuse Cloud**: create a project and copy its keys.
3. **OpenRouter**: create a key with a credit limit, e.g. $5.
4. **GitHub secrets**: `OPENROUTER_API_KEY`, `DATABASE_URL` (session pooler string), `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`.
5. **Vercel**: import the repo with root directory `web/` and set the variables in `web/.env.example`. Pushes to `main` redeploy.
6. **LinkedIn** (optional): create an app at linkedin.com/developers, add *Sign In with LinkedIn using OpenID Connect* and *Share on LinkedIn*, register `<url>/api/linkedin/callback`, and set `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` and `LINKEDIN_REDIRECT_URI` in Vercel.
7. **Brief hand-off** (optional): `integrations/push_to_hub.py` (copy it next to your brief agent, with `DATABASE_URL` in `hub.env`) upserts each day's stories with `origin='brief'`.

## Adding an agent

Add `agents/<name>/graph.py` and use the shared `core.llm.call` / `call_json`. It inherits budget checks, cost logging, tracing, the humanizer and the dashboard for free.
