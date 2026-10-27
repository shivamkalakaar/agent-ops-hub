"""LinkedIn agent: two LangGraph workflows.

collect (daily):  fetch → dedupe → score → store
draft (weekdays): a coordinator and seven specialists (core/orchestrator.py, agents/market_intel/supervisor.py):
                  load_items → coordinator ⇄ {scout, researcher, writer, editor, fact_checker, final_check, onepager}
                  → finish (pending approval). Every decision and turn is a row in agent_steps: the live timeline.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from core import drafting, humanizer, lessons, llm, novelty, orchestrator, settings as settings_mod
from core.steps import step
from core.config import CONFIG, load_json
from core.drafting import (BANNED, HUMANIZER_WEIGHT, MIN_SCORE, SHORT_POST_WORDS, overall_score,  # noqa: F401 (re-exported)
                           style_examples)
from core.feeds import fetch_all, fetch_article
from agents.market_intel.render import render_onepager
from agents.market_intel import specialists, supervisor
from agents.market_intel.roster import AGENT, AUTHOR, PROMPTS, SPECIALISTS

prompt = drafting.prompt_loader(PROMPTS)
_sources_block = drafting.sources_block
SCORE_BATCH = 20
MAX_CHARS = 1300
MAX_LIST_ITEMS = 8
WORDS = (60, 200)  # guardrail band; the prompt targets 80–180
EXAMPLE_MAX_WORDS = 550  # voice examples: short and mid-length posts, not the long essays
# Moves the agent kept reusing until every post had the same shape. Any of them sends a draft back for a rewrite.
TEMPLATE_MOVES = [
    (r"\bnext time (?:a|your) vendor\b", "the \"next time a vendor pitches you\" closer"),
    (r"\bbefore your next (?:renewal|contract|demo|pitch|meeting|vendor)\b", "the \"before your next renewal\" closer"),
    (r"\bI'd stop \w+.{0,80}?\band start\b", "the \"I'd stop X and start Y\" line"),
    (r"\bjust became an?\b", "the \"X just became Y\" hook"),
    (r"\bread it that way\b", "\"read it that way\""),
    (r"(?:^|\n|\. )(?:my read|my bet|my guess|here's my take|the real story)\s*(?::|is that)", "a labelled pivot (\"My read:\", \"The real story is…\")"),
    (r"(?:^|\n)\s*(?:why [\w' ]{1,30}|the (?:catch|kicker|twist|takeaway|upshot|point|bottom line)|here's (?:why|what|the thing))\s*:",
     "a labelled lead-in (\"Why it matters:\", \"The catch:\")"),
    (r"\bthat's the (?:part|number|thing|detail|bit|line|question|real \w+) (?:that matters|to (?:sit with|watch|remember|notice))",
     "a punchline frame (\"That's the number to sit with\")"),
    (r"\b(?:most|many|every|all|the) [\w ]{0,30}\bI (?:see|talk to|work with|know|meet|advise)\b",
     "claims first-hand experience the author never gave (\"most teams I talk to\"); cut it or attribute it"),
]
DAY = timedelta(hours=24)  # a post is about today's news: only stories from the last 24 hours
USED_DAYS = 21  # don't reuse a story that a draft from the last 3 weeks was built on


def ctx_of(config: RunnableConfig) -> llm.RunContext:
    return config["configurable"]["ctx"]


def style_guide() -> str:
    return (CONFIG / "style" / "guide.md").read_text()


# --------------------------------------------------------------------------- collect

class CollectState(TypedDict, total=False):
    items: list[dict]
    new: list[dict]
    pending: list[dict]
    scored: list[dict]
    feed_errors: list[dict]


def fetch(state: CollectState) -> CollectState:
    items, errors = fetch_all(load_json("sources.json")["feeds"])
    return {"items": items, "feed_errors": errors}


def dedupe(state: CollectState, config: RunnableConfig) -> CollectState:
    db = ctx_of(config).db
    known = db.known_urls([i["url"] for i in state["items"]])
    since = (datetime.now(timezone.utc) - DAY).isoformat()  # older stories won't be drafted, so don't score them
    return {"new": [i for i in state["items"] if i["url"] not in known], "pending": db.unscored_items(since)}


def score(state: CollectState, config: RunnableConfig) -> CollectState:
    ctx, scored = ctx_of(config), []
    new = state["new"] + state.get("pending", [])
    for start in range(0, len(new), SCORE_BATCH):
        batch = new[start:start + SCORE_BATCH]
        listing = "\n".join(json.dumps({"id": n, "source": i["source"], "title": i["title"], "summary": i["summary"][:300]},
                                       ensure_ascii=False) for n, i in enumerate(batch))
        try:
            rows = llm.call_json(ctx, "cheap", "score", prompt("score"), listing)
        except ValueError:
            rows = []  # unparseable batch: store unscored rather than fail the run
        by_id = {r.get("id"): r for r in rows if isinstance(r, dict)}
        for n, item in enumerate(batch):
            r = by_id.get(n, {})
            imp, appeal = r.get("importance"), r.get("appeal")
            scored.append({**item, "layer": r.get("layer", item.get("layer_hint")),
                           "importance": imp if isinstance(imp, int) and 1 <= imp <= 5 else None,
                           "appeal": appeal if isinstance(appeal, int) and 1 <= appeal <= 5 else None,
                           "why": r.get("why")})
    return {"scored": scored}


def store(state: CollectState, config: RunnableConfig) -> CollectState:
    ctx = ctx_of(config)
    pending = {i["url"] for i in state.get("pending", [])}
    ctx.db.insert_items([i for i in state["scored"] if i["url"] not in pending], ctx.run_id)
    ctx.db.update_scores([i for i in state["scored"] if i["url"] in pending])
    return {}


def build_collect():
    g = StateGraph(CollectState)
    for name, fn in [("fetch", fetch), ("dedupe", dedupe), ("score", score), ("store", store)]:
        g.add_node(name, fn)
    g.add_edge(START, "fetch")
    g.add_edge("fetch", "dedupe")
    g.add_edge("dedupe", "score")
    g.add_edge("score", "store")
    g.add_edge("store", END)
    return g.compile()


# --------------------------------------------------------------------------- draft

class DraftState(TypedDict, total=False):
    # inputs
    week: str
    items: list[dict]
    recent_posts: list[str]
    recent_angles: list[str]
    top_posts: list[str]  # published posts the owner said did well with readers
    lessons: str          # what the owner's past decisions taught the agent (core/lessons.py)
    learn: bool           # False in evals: don't consume the owner's queue decisions
    learned: dict
    take: str             # the owner's own one-line take, when they asked for a rewrite around it
    redraft_id: str       # rewrite this saved draft in place instead of saving a new one
    # specialists' work
    angle: dict
    sources: list[dict]
    researched: bool
    facts: list[dict]
    context: list[str]
    drafts: list[dict]    # [{"version", "post"}]
    edit_report: dict
    fact_report: dict
    logic_report: dict
    final_report: dict
    candidates: dict      # str(version) -> editor + fact-check results, for picking the best version
    onepager: dict
    onepager_for: str
    # orchestration
    settings: dict
    seq: int
    work_steps: int
    calls: dict
    history: list[dict]
    next: str
    instructions: str
    finish_reason: str
    # what was saved (also what evals and the run summary read)
    post: str
    scores: dict
    critique: str
    violations: list[str]
    unsupported: list[str]
    accent: dict
    overall: float
    revisions: int
    passed: bool
    draft_id: str


def load_items(state: DraftState, config: RunnableConfig) -> DraftState:
    ctx = ctx_of(config)
    db = ctx.db
    now = datetime.now(timezone.utc)
    cfg = config["configurable"].get("settings") or settings_mod.load(db, AGENT, SPECIALISTS, PROMPTS)
    with step(ctx, AGENT, "supervisor", "work", 1, reason="load context") as s:
        # first, learn from what the owner decided since the last run (evals pass learn=False and frozen lessons)
        learned = lessons.learn(db, ctx, AGENT, AUTHOR) if state.get("learn", True) else {"read": 0}
        items = state.get("items")  # evals and redrafts inject items directly
        if not items:
            used = db.used_draft_urls((now - timedelta(days=USED_DAYS)).isoformat())
            items = [i for i in db.top_items((now - DAY).isoformat()) if i["url"] not in used]
        if not items:
            raise RuntimeError("no unused scored stories from the last 24 hours; run collect first")
        week = state.get("week") or now.date().isoformat()  # one draft per weekday: the date is the key (and the one-pager URL)
        base, n = week, 2
        while not state.get("week") and db.week_taken(week):  # never take over another post's one-pager URL
            week, n = f"{base}-{n}", n + 1
        s.summary = (f"Loaded {len(items)} stories ({sum(i.get('origin') == 'brief' for i in items)} from the AI brief)"
                     + (f"; learned from {learned['read']} of your decisions" if learned.get("read") else "")
                     + ("; rewriting around your take" if state.get("take") else ""))
        s.output = {"items": len(items), "learned": learned, "prompt_versions": {k: v.prompt_version for k, v in cfg.items()},
                    "disabled": [k for k, v in cfg.items() if not v.enabled]}
    return {"items": items, "week": week, "learned": learned, "settings": cfg,
            "recent_posts": state["recent_posts"] if "recent_posts" in state else db.approved_posts(3),
            "top_posts": state["top_posts"] if "top_posts" in state else db.top_posts(3),
            "lessons": state["lessons"] if "lessons" in state else lessons.block(db, AGENT, AUTHOR),
            "recent_angles": state["recent_angles"] if "recent_angles" in state else db.recent_angles(5),
            "seq": 1, "work_steps": 0, "calls": {}, "history": [], "drafts": [], "candidates": {}}


def guardrail_violations(post: str, source_urls: set[str], sources_text: str | None = None,
                         recent: list[str] | None = None) -> list[str]:
    """Deterministic checks that run on every draft, independent of the LLM reviewers.
    Includes the humanizer skill's accent scanner, when sources are given its fact guard, and when recent posts
    are given the novelty check (same ground, same hook or same ending as one of them)."""
    v, words = drafting.common_violations(post, sources_text), len(post.split())
    for pattern, name in TEMPLATE_MOVES:
        if re.search(pattern, post, flags=re.I):
            v.append(f"template move: {name}; say it a different way or drop it")
    v += novelty.check(post, recent)
    if sources_text:
        v += [f"copies a source's words without quoting them (\"{c[:80]}…\"); say it your own way or quote it"
              for c in drafting.copied_spans(post, sources_text)[:2]]
    if len(post) > MAX_CHARS:
        v.append(f"{len(post)} characters; the cap is {MAX_CHARS}")
    if not WORDS[0] <= words <= WORDS[1]:
        v.append(f"{words} words; keep it between 80 and 180")
    if len(re.findall(r"(?<!\w)#\w+", post)) > 2:
        v.append("more than 2 hashtags")
    if len(re.findall(r"^\s*(?:[-•*]|\d+[.)])\s", post, flags=re.M)) > MAX_LIST_ITEMS:
        v.append(f"more than {MAX_LIST_ITEMS} list items")
    for url in re.findall(r"https?://\S+", post):
        v.append("link in the post body (links go in the first comment)" if url.rstrip(").,") in source_urls
                 else f"link not from the sources: {url}")
    return v


weak_scores = drafting.weak_scores


def best(state: DraftState) -> dict | None:
    """The version to save when none passed: fact-checked beats unchecked, then fewest problems, then the weighted
    score, then the more human one (core.drafting.rank). Same rule as the Substack agent (agents/substack/ranking.py)."""
    on = state["settings"]["fact_checker"].enabled
    entries = [{**e, "violations": e["violations"] + e.get("fact_issues", []) + e.get("logic_issues", [])
                + ([] if e.get("fact_checked") or not on else ["not fact-checked"])}
               for e in state.get("candidates", {}).values()]
    return max(entries, key=lambda e: (bool(e.get("fact_checked")) or not on, drafting.rank(e))) if entries else None


def finish_node(persist: bool):
    def finish(state: DraftState, config: RunnableConfig) -> DraftState:
        ctx, seq = ctx_of(config), state["seq"] + 1
        with step(ctx, AGENT, "supervisor", "work", seq, reason=state.get("finish_reason")) as s:
            cur, fr = specialists.current(state), state.get("final_report") or {}
            passed = bool(cur and fr.get("version") == cur["version"] and fr.get("passed"))
            chosen = state["candidates"].get(str(cur["version"])) if passed else best(state)
            if not chosen:  # the editor never ran (step limit): score what there is
                chosen = {"post": cur["post"], "scores": {}, "critique": "", "unsupported": [], "violations": [],
                          "accent": humanizer.scan(cur["post"]), "version": cur["version"]}
                chosen["overall"] = drafting.overall_score({}, chosen["accent"]["score"])
            update = {}
            if persist and state.get("onepager_for") != chosen["post"]:  # the one-pager must describe the saved version
                # (evals don't save or score a one-pager, so they skip it)
                update, _, _ = specialists.onepager(state, ctx, state["settings"]["onepager"], post=chosen["post"])
            page = update.get("onepager") or state.get("onepager") or {}
            out = {"post": chosen["post"], "scores": chosen["scores"], "critique": chosen.get("critique", ""),
                   "violations": chosen["violations"], "unsupported": chosen.get("unsupported", []), "accent": chosen["accent"],
                   "overall": chosen["overall"], "revisions": len(state["drafts"]) - 1, "passed": passed, "seq": seq, **update}
            draft_id = None
            if persist:
                sources = [{"url": x["url"], "title": x["title"], "source": x["source"], "origin": x.get("origin", "rss")}
                           for x in state["sources"]]
                draft = {"week": state["week"], "angle": state["angle"], "post_md": chosen["post"],
                         "onepager_html": render_onepager(page, sources, state["week"], AUTHOR),
                         "sources": sources, "revisions": out["revisions"],
                         "judge_scores": {**chosen["scores"], "violations": chosen["violations"], "humanizer": chosen["accent"]["score"],
                                          "critique": out["critique"], "unsupported_claims": out["unsupported"],
                                          "overall": chosen["overall"], "passed_review": passed, "version": chosen.get("version"),
                                          "final_blockers": fr.get("blockers", []) if not passed else [],
                                          "finish_reason": state.get("finish_reason", "")}}
                if state.get("redraft_id"):
                    ctx.db.replace_draft(state["redraft_id"], draft, ctx.run_id)
                    draft_id = state["redraft_id"]
                else:
                    draft_id = ctx.db.insert_draft(draft, ctx.run_id)
            s.summary = (f"Saved v{chosen.get('version')} for approval" if persist else f"Finished with v{chosen.get('version')}") + \
                        (" (passed every check)" if passed else " (didn't pass review: flagged in the queue)")
            s.output = {"draft_id": draft_id, "version": chosen.get("version"), "passed": passed}
        return {**out, "draft_id": draft_id}
    return finish


def build_draft(persist: bool = True):
    """persist=False (evals): nothing is saved, and the finish step doesn't write a one-pager for the chosen post."""
    return orchestrator.build(supervisor.TEAM, DraftState, specialists.RUNNERS, load_items, finish_node(persist))
