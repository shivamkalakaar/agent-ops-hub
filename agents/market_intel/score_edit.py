"""Score the owner's edit of a saved draft next to the agent's own version, so the queue can show what the edit
changed: each editor score, the humanizer, guardrail flags and fact-check results, before and after.

Both versions are scored in the same run, against the same sources and reviewers, so a difference comes from the
edit and not from a different day's reviewer. Nothing is rewritten; the result is saved in judge_scores.edit_check.

uv run python -m agents.market_intel score-edit --draft <id>
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from core import drafting, lessons, llm, settings as settings_mod
from core.config import git_sha
from core.db import connect
from agents.market_intel import graph, specialists
from agents.market_intel.roster import AGENT, AUTHOR, PROMPTS, SPECIALISTS


def _report(state, ctx, cfg) -> dict:
    """The editor's and fact checker's read of one version, plus the free checks (guardrails, humanizer)."""
    e = specialists.editor(state, ctx, cfg["editor"])[0]["edit_report"]
    acc = e["accent"]
    why = acc["hits"] + acc["structural"] + ([f"sounds machine-written in: {', '.join(acc['contributors'])}"]
                                             if acc["contributors"] and acc["score"] < 5 else [])
    out = {"scores": e["scores"], "humanizer": acc["score"], "humanizer_why": why[:5], "overall": e["overall"],
           "weak": specialists.weak(e), "violations": e["violations"], "fixes": e["fixes"],
           "unexplained": e.get("unexplained", [])}
    if cfg["fact_checker"].enabled:
        claims = specialists.fact_checker(state, ctx, cfg["fact_checker"])[0]["fact_report"]["claims"]
        out["unsupported"] = [c["claim"] for c in claims if c["verdict"] == "unsupported"]
        out["hedges"] = [c["claim"] for c in claims if c["verdict"] == "hedge"]
    return out


def score_edit(draft_id: str) -> int:
    with connect() as db:
        d = db.draft(draft_id)
        if not d:
            raise SystemExit(f"no draft {draft_id}")
        edit = (d.get("edited_post_md") or "").strip()
        run_id = db.start_run(AGENT, "score_edit", git_sha())
        result = {"text": edit, "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_id": run_id}
        if not edit or edit == d["post_md"].strip():
            db.set_edit_check(draft_id, result | {"error": "no edits to score yet"})
            db.finish_run(run_id, "skipped", stats={"reason": "no edits"})
            print(f"[{run_id}] skipped: draft {draft_id} has no saved edits")
            return 0
        ctx = llm.RunContext(db=db, run_id=run_id)
        try:
            cfg = settings_mod.load(db, AGENT, SPECIALISTS, PROMPTS)
            items = [db.item(s["url"]) or {**s, "summary": "", "importance": 3} for s in d["sources"] or []]
            items = drafting.read_sources(items, words=specialists.WORDS_PER_SOURCE * max(1, len(items)), fetch=graph.fetch_article)
            base = {"sources": items, "facts": [], "context": [], "take": d.get("my_take") or "", "week": d["week"],
                    "lessons": lessons.block(db, AGENT, AUTHOR), "recent_posts": db.approved_posts(3),
                    "examples": db.voice_examples() or specialists.saved_examples()}
            versions = {"agent": d["post_md"], "edit": edit}
            with ThreadPoolExecutor(max_workers=2) as pool:  # psycopg serializes the shared connection
                reports = dict(zip(versions, pool.map(
                    lambda post: _report({**base, "drafts": [{"version": 1, "post": post}]}, ctx, cfg), versions.values())))
        except Exception as ex:
            db.set_edit_check(draft_id, result | {"error": f"{type(ex).__name__}: {str(ex)[:200]}"})
            db.finish_run(run_id, "failed", error=f"{type(ex).__name__}: {ex}")
            raise
        result |= reports | {"cost_usd": round(ctx.spent[0], 4)}
        db.set_edit_check(draft_id, result)
        stats = {"draft_id": draft_id, "overall": {k: r["overall"] for k, r in reports.items()}, "cost_usd": result["cost_usd"]}
        db.finish_run(run_id, "succeeded", stats=stats)
        print(f"[{run_id}] score-edit succeeded: {stats}")
    return 0
