"""Substack newsroom: one supervisor and six specialists on a LangGraph state machine.

load_context → supervisor ⇄ {research, writer, editor, fact_checker, illustrator, final_check} → finish (pending approval)

Every specialist returns to the supervisor, which picks the next move from the moves the code allows
(agents/substack/supervisor.py). Each decision and each turn is a row in agent_steps: the live timeline.
"""
from datetime import datetime, timedelta, timezone

from langchain_core.runnables import RunnableConfig

from core import lessons, orchestrator, settings as settings_mod
from core.orchestrator import ctx_of  # noqa: F401 (revise.py imports it from here)
from core.steps import step
from agents.substack import checks, ranking, render, supervisor
from agents.substack.roster import AUTHOR, AGENT, PROMPTS, SPECIALISTS, Workspace
from agents.substack.specialists import current, editor, fact_checker, final_check, illustrator, research, writer

RUNNERS = {"research": research.run, "writer": writer.run, "editor": editor.run, "fact_checker": fact_checker.run,
           "illustrator": illustrator.run, "final_check": final_check.run}
WINDOW_DAYS = (3, 7)  # a run covers items since the last draft, but at least 3 and at most 7 days back
VOICE_WORDS = 600


def _since(last: str | None) -> str:
    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(days=WINDOW_DAYS[1]), now - timedelta(days=WINDOW_DAYS[0])
    t = datetime.fromisoformat(last) if last else hi
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(lo, min(t, hi)).isoformat()


def load_context(state: Workspace, config: RunnableConfig) -> Workspace:
    ctx = ctx_of(config)
    db = ctx.db
    cfg = config["configurable"].get("settings") or settings_mod.load(db, AGENT, SPECIALISTS, PROMPTS)
    with step(ctx, AGENT, "supervisor", "work", 1, reason="load context") as s:
        # first, learn from what the owner decided since the last run (evals pass learn=False and frozen lessons)
        learned = lessons.learn(db, ctx, AGENT, AUTHOR) if state.get("learn", True) else {"read": 0}
        since = _since(db.last_substack_at())
        used = db.used_substack_urls()
        items = state.get("items") or [i for i in db.top_items(since, {"brief": 18, "rss": 12}) if i["url"] not in used]
        if not items:
            raise RuntimeError(f"no new scored items since {since[:10]}; run collect first")
        musings = state.get("musings") if "musings" in state else db.published_musings(
            (datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
        if (forced := state.get("seed_draft_id")) and forced not in {m["id"] for m in musings}:
            if m := db.musing(forced):
                musings = [m, *musings]
        voice = [" ".join(p.split()[:VOICE_WORDS]) for p in db.approved_posts(3) + db.approved_substack_posts(1)]
        s.summary = (f"Loaded {len(items)} stories since {since[:10]} "
                     f"({sum(i.get('origin') == 'brief' for i in items)} from the AI brief), {len(musings)} unexpanded musings"
                     + (f"; learned from {learned['read']} of your decisions" if learned.get("read") else ""))
        s.output = {"since": since, "items": len(items), "musings": [m["id"] for m in musings],
                    "prompt_versions": {k: v.prompt_version for k, v in cfg.items()},
                    "disabled": [k for k, v in cfg.items() if not v.enabled], "learned": learned}
    return {"items": items, "musings": musings, "voice_posts": state.get("voice_posts") or voice,
            "lessons": state["lessons"] if "lessons" in state else lessons.block(db, AGENT, AUTHOR), "learned": learned,
            "recent_titles": state.get("recent_titles", db.recent_substack_angles()),
            "slot": state.get("slot") or datetime.now(timezone.utc).date().isoformat(),
            "settings": cfg, "seq": 1, "work_steps": 0, "calls": {}, "history": [], "drafts": [], "images": []}


def chosen(state: Workspace) -> tuple[dict, bool]:
    """The version to save: the current one if it passed the final check, otherwise the best-ranked edited version."""
    cur, fr = current(state), state.get("final_report") or {}
    if cur and fr.get("version") == cur["version"] and fr.get("passed"):
        return cur, True
    best = ranking.best(state)
    return ({"version": best["version"], "md": best["md"]} if best else cur), False


def review_of(state: Workspace, v: int) -> dict:
    """Editor results for version v, flattened for the queue: rubric scores, overall, humanizer, violations."""
    for r in (state.get("edit_report") or {}, state.get("candidates", {}).get(str(v)) or {}):
        if r.get("version") == v:
            return {**r.get("scores", {}), "overall": r.get("overall"), "humanizer": r["accent"]["score"],
                    "violations": r.get("violations", []), "critique": r.get("critique", "")}
    return {}


def finish(state: Workspace, config: RunnableConfig) -> Workspace:
    ctx, seq = ctx_of(config), state["seq"] + 1
    persist = config["configurable"].get("persist", True)
    with step(ctx, AGENT, "supervisor", "work", seq, reason=state.get("finish_reason")) as s:
        draft, passed = chosen(state)
        p = checks.parse(draft["md"])
        sources = [{"url": x["url"], "title": x["title"], "source": x["source"], "origin": x.get("origin", "rss")}
                   for x in state["sources"]]
        body = render.compose(p["body"], state.get("images", []), sources)
        judge = {**review_of(state, draft["version"]), "passed_review": passed, "version": draft["version"],
                 "finish_reason": state.get("finish_reason", "")}
        final = {"title": p["title"], "subtitle": p["subtitle"], "body_md": body, "version": draft["version"], "passed": passed}
        draft_id = None
        if persist:
            draft_id = ctx.db.insert_substack_draft({
                "slot": state["slot"], "seed_draft_id": (state.get("seed") or {}).get("id"), "brief": state["brief"],
                "title": p["title"] or state["brief"].get("title", "Untitled"), "subtitle": p["subtitle"], "body_md": body,
                "body_html": render.to_html(body), "images": state.get("images", []), "sources": sources,
                "judge_scores": judge, "fact_report": state.get("fact_report") or {}, "final_report": state.get("final_report") or {},
                "revisions": len(state.get("drafts", [])) - 1}, ctx.run_id)
        s.summary = (f"Saved v{draft['version']} for approval" if persist else f"Finished with v{draft['version']}") + \
                    (" (passed every check)" if passed else " (didn't pass review: flagged in the queue)")
        s.output = {"draft_id": draft_id, "version": draft["version"], "passed": passed, "title": p["title"]}
    return {"draft_id": draft_id, "seq": seq, "final": final, "judge": judge}


def build():
    return orchestrator.build(supervisor.TEAM, Workspace, RUNNERS, load_context, finish)
