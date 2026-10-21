"""Send a saved draft back through the newsroom to fix its blockers: writer → editor → fact-checker → final check,
up to MAX_ATTEMPTS rewrites. Starts from the owner's edits if there are any, keeps the images, updates the draft
in place. Triggered from the queue ("Let the agents fix this") or `python -m agents.substack revise <id>`."""
import re

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from core import drafting, settings as settings_mod
from core.steps import step
from agents.substack import checks, ranking, render
from agents.substack.graph import ctx_of, review_of, worker
from agents.substack.roster import AGENT, PROMPTS, SPECIALISTS, Workspace
from agents.substack.specialists import current, research

MAX_ATTEMPTS = 2
IMAGE = re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*$")


def strip_extras(body: str) -> str:
    """Body without the parts render.compose added: image lines, their caption lines, the Sources section."""
    body = re.split(r"^## Sources\s*$", body, flags=re.M)[0]
    out, skip_caption = [], False
    for block in body.split("\n\n"):
        b = block.strip()
        if IMAGE.match(b):
            skip_caption = True
            continue
        if skip_caption and re.fullmatch(r"\*[^*\n]+\*", b):
            skip_caption = False
            continue
        skip_caption = False
        out.append(block)
    return "\n\n".join(out).strip()


class Revision(Workspace, total=False):
    revise_draft_id: str
    original: dict
    attempts: int


def load_draft(state: Revision, config: RunnableConfig) -> Revision:
    ctx = ctx_of(config)
    db = ctx.db
    cfg = config["configurable"].get("settings") or settings_mod.load(db, AGENT, SPECIALISTS, PROMPTS)
    with step(ctx, AGENT, "supervisor", "work", 1, reason="revise a saved draft") as s:
        d = db.substack_draft(state["revise_draft_id"])
        if not d:
            raise RuntimeError(f"no Substack draft {state['revise_draft_id']}")
        title, subtitle = d.get("edited_title") or d["title"], d.get("edited_subtitle") or d.get("subtitle") or ""
        body = strip_extras(d.get("edited_body_md") or d["body_md"])
        v = (d.get("judge_scores") or {}).get("version") or d["revisions"] + 1
        sources = [{**x, "summary": db.item_summary(x["url"]) or ""} for x in d.get("sources") or []]
        sources = drafting.read_sources(sources, words=research.ARTICLE_WORDS, fetch=lambda url: research.fetch_article(url))
        blockers = (d.get("final_report") or {}).get("blockers") or []
        s.summary = (f'Revising "{title}" (v{v}{", with your edits" if d.get("edited_body_md") else ""}): '
                     f"{len(blockers)} blocker{'s' * (len(blockers) != 1)} to fix")
        s.output = {"draft_id": d["id"], "blockers": blockers}
    return {"settings": cfg, "slot": d["slot"], "brief": d.get("brief") or {}, "sources": sources,
            "seed": db.musing(d["seed_draft_id"]) if d.get("seed_draft_id") else None,
            "voice_posts": [" ".join(x.split()[:600]) for x in db.approved_posts(3) + db.approved_substack_posts(1)],
            "drafts": [{"version": v, "md": f"# {title}\n\n*{subtitle}*\n\n{body}"}],
            "final_report": {**(d.get("final_report") or {}), "version": v, "passed": False, "blockers": blockers},
            "fact_report": {**(d.get("fact_report") or {}), "version": v} if d.get("fact_report") else {},
            "images": d.get("images") or [], "candidates": {}, "original": d, "attempts": 0,
            "seq": 1, "work_steps": 0, "calls": {}, "history": [], "instructions": "Fix every final-check blocker."}


def count_attempt(state: Revision) -> Revision:
    return {"attempts": state.get("attempts", 0) + 1}


def again(state: Revision) -> str:
    fr = state.get("final_report") or {}
    return "save" if fr.get("passed") or state["attempts"] >= MAX_ATTEMPTS else "retry"


def save(state: Revision, config: RunnableConfig) -> Revision:
    ctx, seq = ctx_of(config), state["seq"] + 1
    persist = config["configurable"].get("persist", True)
    with step(ctx, AGENT, "supervisor", "work", seq, reason="save the revision") as s:
        d, cur, fr = state["original"], current(state), state.get("final_report") or {}
        before = len((d.get("final_report") or {}).get("blockers") or [])
        passed = fr.get("version") == cur["version"] and fr.get("passed")
        best = {"version": cur["version"], "md": cur["md"]} if passed else (ranking.best(state) or cur)
        after = len(fr.get("blockers") or []) if fr.get("version") == best["version"] else before
        if not passed and after >= before:
            s.summary = f"Kept the saved version: the rewrites still had {after} blocker{'s' * (after != 1)} (was {before})"
            s.output = {"kept": True, "blockers": fr.get("blockers")}
            return {"seq": seq, "final": {"passed": False, "kept": True}, "judge": d.get("judge_scores") or {}}
        p = checks.parse(best["md"])
        body = render.compose(p["body"], state.get("images", []), d.get("sources") or [])
        judge = {**review_of(state, best["version"]), "passed_review": bool(passed), "version": best["version"],
                 "finish_reason": "revised from the queue"}
        if persist:
            ctx.db.update_substack_draft(d["id"], {
                "title": p["title"] or d["title"], "subtitle": p["subtitle"], "body_md": body, "body_html": render.to_html(body),
                "judge_scores": judge, "fact_report": state.get("fact_report") or {}, "final_report": fr,
                "rewrites": state["attempts"]})
        s.summary = (f"Updated the draft with v{best['version']}: " +
                     ("passed every check" if passed else f"{after} blocker{'s' * (after != 1)} left (was {before})"))
        s.output = {"draft_id": d["id"], "version": best["version"], "passed": bool(passed)}
    return {"seq": seq, "final": {"title": p["title"], "version": best["version"], "passed": bool(passed)}, "judge": judge}


def build():
    g = StateGraph(Revision)
    g.add_node("load_draft", load_draft)
    g.add_node("count_attempt", count_attempt)
    for name in ("writer", "editor", "fact_checker", "final_check"):
        g.add_node(name, worker(name))
    g.add_node("save", save)
    g.add_edge(START, "load_draft")
    g.add_edge("load_draft", "count_attempt")
    g.add_edge("count_attempt", "writer")
    g.add_edge("writer", "editor")
    g.add_edge("editor", "fact_checker")
    g.add_edge("fact_checker", "final_check")
    g.add_conditional_edges("final_check", again, {"retry": "count_attempt", "save": "save"})
    g.add_edge("save", END)
    return g.compile()
