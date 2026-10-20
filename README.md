# Agent Ops Hub

**Live ops dashboard:** https://agent-ops-hub-dun.vercel.app

Agents run like production systems: **budget-capped, traced, evaluated, and human-approved**, for about **$1 a month**.

Agent #1 is a **market-intelligence agent**. Every day it reads 12 AI-industry feeds and scores what matters. Once a week it drafts **one short LinkedIn post** plus a sourced one-pager. Nothing is published until a human approves it in the dashboard.

| Layer | Choice | Why |
|---|---|---|
| Orchestration | LangGraph | Explicit state machine: critic → revise loop, conditional edges, testable nodes |
| Models | OpenRouter, three tiers (`config/models.yaml`) | Free model for bulk scoring, cheap writer, critic from a *different* model family; automatic fallbacks |
| Observability | Langfuse | One trace per run, per-node cost and latency, critic scores attached to traces |
| Runtime | GitHub Actions cron | Free, no server to babysit |
| State + auth | Supabase Postgres + RLS | Public read-only ops data; drafts editable only by the owner |
| Dashboard | Next.js on Vercel | Ops overview, run drill-down, approval queue, public one-pagers |

## The workflows

```
daily   fetch 12 feeds → dedupe (Postgres) → score importance [free model] → store

weekly  load top items → pick ONE angle → write post → critic → guardrails ─┬─ revise ↺ (max 2) ─→ critic
                                                                            └─ one-pager → save as pending → human approval
```

## Production controls

- **Budget guard** (`core/budget.py`): checked before *every* LLM call against daily and monthly caps (`config/budget.yaml`). If a cap is hit, the run stops cleanly with status `budget_exceeded`. A matching credit limit on the OpenRouter key backs this up.
- **Cost accounting**: each call's real cost, as reported by OpenRouter, goes into `llm_calls`. The dashboard shows spend per day, per tier and per draft.
- **Model fallbacks**: OpenRouter's `models` array lets a rate-limited free model fall through to a cheap paid one. The dashboard shows which models actually served calls.
- **Guardrails** (deterministic, `guardrail_violations`): ≤1,300 characters, 80–180 words, banned hype phrases, ≤2 hashtags, ≤8 list items, no links that don't come from the sources.
- **LLM critic**: scores hook, focus, brevity, concreteness, voice and grounding from 1–5. Any score below 4 triggers a revision.
- **Safe rendering**: the model returns JSON and the HTML is built server-side with everything escaped. The dashboard also shows the result in a script-less sandboxed iframe.
- **Human in the loop**: drafts are created as `pending`. The owner edits, then approves or rejects. Approved posts, including the owner's edits, become few-shot examples for the next week.
- **Evals in CI** (`evals/`): on PRs that touch prompts or agent code, the draft workflow runs on 5 golden weeks built from past briefs. The PR fails if the mean critic score drops more than 0.3 below `evals/baseline.json`, or if any post breaks a guardrail.
- **Offline tests**: `pytest` runs both workflows end to end with a fake LLM and SQLite, with no keys and no cost.

## Cost

| Workflow | Calls | Model | Cost/run |
|---|---|---|---|
| Daily collect | 1–3 scoring batches | Gemma 4 31B (free) → DeepSeek v4.1 Flash fallback | ~$0 |
| Weekly draft | 5–9 | Claude Haiku 5.5 writer, DeepSeek critic | ~$0.01–0.03 |
| PR evals | ~35 | same | ~$0.10 |

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

**Writing style:** paste 15–25 LinkedIn posts you admire into `config/style/examples.md`, separated by `---`. The file is git-ignored because the posts belong to their authors. Then run `uv run python -m agents.market_intel style-guide` to distill them into `config/style/guide.md`.

## Deploy

1. **Supabase**: create a project and run `supabase/schema.sql`, then `supabase/policies.sql` with your email filled in, in the SQL editor. Under Auth → URL configuration, add your Vercel URL.
2. **Langfuse Cloud**: create a project and copy its keys.
3. **OpenRouter**: create a key with a credit limit, e.g. $5.
4. **GitHub secrets**: `OPENROUTER_API_KEY`, `DATABASE_URL` (session pooler string), `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`.
5. **Vercel**: import the repo with root directory `web/` and set the variables in `web/.env.example`. Pushes to `main` redeploy.

## Adding an agent

Add `agents/<name>/graph.py` and use the shared `core.llm.call` / `call_json`. It inherits budget checks, cost logging, tracing and the dashboard for free.
