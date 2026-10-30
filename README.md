# Agent Ops Hub

**Live ops dashboard:** https://agent-ops-hub-dun.vercel.app

Agents run like production systems: **budget-capped, traced, evaluated and human-approved**, for about **$3–4 a month** (the LinkedIn agent alone is ~$1).

Two agents run here today. Agent #1 is a **market-intelligence agent**. Every day it collects AI-industry news from two places: 12 RSS feeds, and the stories in my daily AI Stack Brief email (a separate agent). Every weekday it drafts **one short LinkedIn post** (a scheduled run skips if 2 drafts are already waiting for approval). Nothing goes out until I approve it in the dashboard. Then one click publishes it to my LinkedIn profile as the next post in the **AI Musing** series.

Agent #2 is a **Substack newsroom**: a super agent that manages six specialists (research, writer, editor, fact-checker, illustrator, final check). Twice a week it turns the same news into one 800–1,200 word post with a cover, inline images and a chart. Substack has no API, so an approved draft is copied into the editor with one click. One dashboard page shows every agent live and lets me switch specialists on or off, change their models and edit their prompts.

| Layer | Choice | Why |
|---|---|---|
| Orchestration | LangGraph | Explicit state machine: critic → rewrite loop, conditional edges, testable nodes |
| Models | OpenRouter, six tiers (`config/models.yaml`) | Free models for scoring, planning and checking; a strong model only for the words people read; checkers from a *different* model family; free → cheap fallbacks when free pools are rate-limited |
| Observability | Langfuse | One trace per run, per-node cost and latency, every score attached to the trace |
| Runtime | GitHub Actions cron | Free, no server to babysit |
| State + auth | Supabase Postgres + RLS | Public read-only ops data; drafts and the LinkedIn token readable only by the owner |
| Dashboard | Next.js on Vercel | Ops overview, run drill-down, approval queue, LinkedIn publishing, public one-pagers (older posts) |

## The workflows

```
daily   RSS feeds ─┐
                   ├→ dedupe (Postgres) → score importance [free model] → store
        AI brief ──┘   (pushed from the Mac right after the brief is built)

weekday stories since the last draft, minus ones already used (15 from the brief + 10 from RSS) → pick ONE angle → fetch the full articles
        → write post → critic + humanizer + guardrails ─┬─ rewrite ↺ (max 2)
                                                        └─ keep the best version → pending
        → human approval → Publish → LinkedIn post "AI Musing #N"

Wed+Sat stories since the last post (brief-first) + unexpanded musings
        → super agent ⇄ research · writer · editor · fact-checker · illustrator · final check
        → best version → pending → human approval → "Copy for Substack" (rich HTML with images) or .md/.html
```

## The Substack newsroom

A single writing loop produced decent LinkedIn posts. A 1,000-word essay needs more than one critic, so the second agent is a newsroom. `agents/substack/` has a supervisor and six specialists on one LangGraph state machine. Every specialist reports back to the supervisor, which picks the next move.

| Specialist | Model | Job |
|---|---|---|
| Super agent | Nemotron 3 Ultra (free) | Reads every report and picks who works next, with a reason shown on the timeline |
| Research | Nemotron 3 Ultra (free) | Picks the story (and a LinkedIn musing to build on, if one fits), reads the full articles, writes a brief with facts and evidence quotes. Can dig deeper on request |
| Writer | Sonnet 5.5 | The only call on the expensive tier: writes and rewrites in my voice |
| Editor | Nemotron 3 Ultra (free, other family) | Rubric on hook, thesis, structure, depth, concreteness, voice and human, plus the humanizer scanner at 40% |
| Fact-checker | Nemotron 3 Ultra (free) + fact guard | Checks each claim against the source text. Unsupported claims and invented numbers block the post |
| Illustrator | Nemotron plans, **Nano Banana 2.1** draws, a free vision model checks | Cover and inline images in one house style, regenerated once if the check fails. Charts are drawn in code and only from numbers that appear in both the sources and the post |
| Final check | Rules + Nemotron 3 Ultra (free) | Every guardrail on the exact version being saved, plus a publish-readiness read |

**How the super agent is kept on a leash.** Code decides what's *allowed*; the model only chooses among allowed moves. Some examples: no editing before a draft exists, no re-checking an unchanged draft, no FINISH before the final check passes on the current version. When only one move is allowed, no LLM call is made. An invalid choice falls back to the fixed newsroom order, and the timeline marks it. A run is capped at 14 specialist turns, each specialist has a call cap (writer 3, research 2), and the budget guard runs before every call. At a limit, the best-ranked version is saved and flagged "didn't pass review".

**What the first real run showed.** It cost $0.47 and took 11 minutes. v2 passed every check with an overall of 4.43/5. The vision check caught an inline image whose scale tipped the wrong way and regenerated it. It also surfaced problems I've since fixed:
- One fact-check burned 99k output tokens ($0.12, 4 minutes). The cause was hidden reasoning: at `effort: low`, DeepSeek and Qwen still spent every token thinking and returned nothing. The newsroom's checkers now run on a `check` tier with reasoning off, which takes ~70–800 tokens per call. Every tier also has a `max_tokens` cap, and an unparseable reply is retried on the tier's fallback model.
- Nano Banana was rate-limited upstream, so every image went to the 2× pricier fallback. It now retries the primary once before falling back.
- The chart plotted a number the post never mentioned. Chart values must now appear in the post.
- When no version passed, the saved one was ranked without its fact-check results. Fact problems now count, and a version that skipped an active fact-check can't win.

**Why only the writer is paid.** I ran each free model on the same real brief. They wrote about half the length with humanizer 2/5, and one invented two source links. Sonnet wrote 806 words at 4/5 with no flags. For planning, research and judging, Nemotron 3 Ultra (free) matched Haiku and DeepSeek: it gave valid JSON as often and caught a planted false claim every time. Free pools get rate-limited upstream, so each tier falls back to a cheap paid model (~$0.001 per call) instead of failing.

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
- **Agent controls** (`/agents`): each newsroom specialist can be switched off (research, writer and final check can't), moved to another model, or given fewer calls. Prompts are versioned in the database with diffs. The run validates every override: models must be on the allowlist in `config/models.yaml` and call caps are clamped, so a bad row can't send calls to an expensive model.
- **Live timeline** (`/runs/<id>`): every supervisor decision and specialist turn is a row in `agent_steps` with its reason, output, cost and latency, streamed to the page over Supabase Realtime.
- **Evals in CI** (`evals/`): on PRs that touch prompts or agent code, the draft workflow runs on 5 golden weeks built from past briefs. The PR fails if the weighted score drops more than 0.3 below `evals/baseline.json`, if the humanizer average falls below 3/5, or if any post breaks a guardrail.
The newsroom has its own eval (`--agent substack`): 3 cases of 3 merged brief days each, with images off. It has the same gates plus trajectory checks: within the step limit, and the fact-checker and final check both ran.
- **Offline tests**: `pytest` runs every workflow end to end with a fake LLM, fake images and SQLite, with no keys and no cost. That covers supervisor fallbacks, limits, disabled specialists, invented numbers and HTML escaping.

## Cost (measured)

| Workflow | Calls | Models | Cost/run |
|---|---|---|---|
| Daily collect | 1–5 scoring batches | Gemma 4 31B (free), DeepSeek v4.1 Flash fallback | under $0.001 |
| LinkedIn draft (weekdays) | 5–9 | free Nemotron to plan, Sonnet 5.5 to write, DeepSeek critic | $0.02–0.05 |
| Substack post (Wed, Sat) | ~20 | free models for everything but the writer (Sonnet) and images (Nano Banana 2.1) | $0.47 on the first run; ~$0.20–0.30 expected now (each Sonnet draft ~$0.06, images ~$0.12) |
| PR evals | ~30 (+~45 for the newsroom) | same, images off | ~$0.15–0.25 (+~$0.30–0.50) |

Caps are $3/day and $10/month (`config/budget.yaml`): weekday LinkedIn posts (~$1/month) plus two illustrated Substack posts a week (~$4/month) and PR evals.

## Run it locally

```bash
uv sync
uv run pytest -q                                   # offline, free
cp .env.example .env && $EDITOR .env               # add OPENROUTER_API_KEY (Langfuse/DB optional)
set -a; source .env; set +a
uv run python -m agents.market_intel collect       # uses ./local.db when DATABASE_URL is unset
uv run python -m agents.market_intel draft
uv run python -m evals.run_evals --cases 2
uv run python -m agents.substack style-guide      # long-form guide from your approved posts + the essays in examples.md
uv run python -m agents.substack draft            # one newsroom run; --seed <drafts.id> expands a specific musing
uv run python -m agents.substack export <id>      # out/substack/<slot>-<id>.md and .html
uv run python -m evals.run_evals --agent substack --cases 1
```

**Writing style:** the LinkedIn writer and editor work from `agents/market_intel/prompts/voice.md` (how Shivam writes, distilled from his published posts and edits) and see his rewrites as before/after pairs: in production the latest from the database, in evals the frozen `agents/market_intel/voice_examples.json`. The scout tests each take against `prompts/views.md` (his recurring positions). `config/style/examples.md` (git-ignored posts by other writers) and `guide.md` now only feed the Substack agent's long-form guide.

## Deploy

1. **Supabase**: create a project and run `supabase/schema.sql`, then `supabase/policies.sql` with your email filled in, in the SQL editor. Under Auth → URL configuration, set the Site URL to your Vercel URL and add `<url>/queue` as a redirect.
2. **Langfuse Cloud**: create a project and copy its keys.
3. **OpenRouter**: create a key with a credit limit, e.g. $5.
4. **GitHub secrets**: `OPENROUTER_API_KEY`, `DATABASE_URL` (session pooler string), `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`.
5. **Vercel**: import the repo with root directory `web/` and set the variables in `web/.env.example`. Pushes to `main` redeploy.
6. **LinkedIn** (optional): create an app at linkedin.com/developers, add *Sign In with LinkedIn using OpenID Connect* and *Share on LinkedIn*, register `<url>/api/linkedin/callback`, and set `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` and `LINKEDIN_REDIRECT_URI` in Vercel.
7. **Substack newsroom**: re-run `supabase/schema.sql` and `supabase/policies.sql`. That adds the newsroom tables, the public `substack-images` bucket and Realtime on `agent_steps`. Add the GitHub secrets `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (used only to upload images), and optionally `NEXT_PUBLIC_SUBSTACK_URL` in Vercel for the "Open Substack" button. Set `NEXT_PUBLIC_MONTHLY_CAP_USD=6` in Vercel to match the new cap.
8. **Brief hand-off** (optional): `integrations/push_to_hub.py` (copy it next to your brief agent, with `DATABASE_URL` in `hub.env`) upserts each day's stories with `origin='brief'`.

## Adding an agent

Add `agents/<name>/graph.py` and use the shared `core.llm.call` / `call_json`, with `core.runner.run_graph` as the CLI entry point. It inherits budget checks, cost logging, tracing, the humanizer and the dashboard for free. A multi-agent workflow also gets `core.steps` (the live timeline), `core.settings` (dashboard controls with validation), `core.drafting` (sources, style examples, the weighted score, keep-best) and `core.images` / `core.storage`.
