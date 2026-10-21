"""Market-intel agent: two LangGraph workflows.

collect (daily):  fetch → dedupe → score → store
draft (weekly):   load_items → pick_angle → write_post → critic → guardrails ─┬→ revise → critic …  (max 2 revisions)
                                                                              └→ onepager → save (pending approval)
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from core import drafting, humanizer, llm
from core.config import CONFIG, load_json
from core.drafting import (BANNED, HUMANIZER_WEIGHT, MIN_SCORE, SHORT_POST_WORDS, overall_score,  # noqa: F401 (re-exported)
                           style_examples)
from core.feeds import fetch_all, fetch_article
from agents.market_intel.render import render_onepager

PROMPTS = Path(__file__).parent / "prompts"
prompt = drafting.prompt_loader(PROMPTS)
_sources_block = drafting.sources_block
AUTHOR = "Shivam"
SCORE_BATCH = 20
MAX_REVISIONS = 2
MAX_CHARS = 1300
MAX_LIST_ITEMS = 8
WORDS = (60, 200)  # guardrail band; the prompt targets 80–180


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
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
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
            imp = r.get("importance")
            scored.append({**item, "layer": r.get("layer", item.get("layer_hint")),
                           "importance": imp if isinstance(imp, int) and 1 <= imp <= 5 else None,
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
    week: str
    items: list[dict]
    recent_posts: list[str]
    recent_angles: list[str]
    angle: dict
    sources: list[dict]
    post: str
    scores: dict
    critique: str
    violations: list[str]
    revisions: int
    onepager: dict
    accent: dict
    unsupported: list[str]
    overall: float
    best: dict
    draft_id: str


def load_items(state: DraftState, config: RunnableConfig) -> DraftState:
    db = ctx_of(config).db
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    items = state.get("items") or db.top_items(since)  # evals inject items directly
    if not items:
        raise RuntimeError("no scored items in the last 7 days; run collect first")
    week = state.get("week") or "{}-W{:02d}".format(*datetime.now(timezone.utc).isocalendar()[:2])
    return {"items": items, "week": week, "recent_posts": db.approved_posts(3),
            "recent_angles": state["recent_angles"] if "recent_angles" in state else db.recent_angles(5), "revisions": 0}


def _listing(items: list[dict]) -> str:
    return "\n".join(json.dumps({"id": n, "source": i["source"], "layer": i.get("layer"), "importance": i.get("importance"),
                                 "title": i["title"], "summary": (i.get("summary") or "")[:400], "why": i.get("why")},
                                ensure_ascii=False) for n, i in enumerate(items))


def read_sources(state: DraftState) -> DraftState:
    """Fetch the full text of the chosen stories so the post can be specific and grounded (free: plain HTTP)."""
    return {"sources": drafting.read_sources(state["sources"], fetch=fetch_article)}


def pick_angle(state: DraftState, config: RunnableConfig) -> DraftState:
    recent = "\n".join(f"- {a}" for a in state["recent_angles"]) or "(none yet)"
    angle = llm.call_json(ctx_of(config), "writer", "pick_angle", prompt("pick_angle", AUTHOR=AUTHOR, RECENT=recent),
                          _listing(state["items"]))
    ids = [i for i in angle.get("item_ids", []) if isinstance(i, int) and 0 <= i < len(state["items"])][:3] or [0]
    return {"angle": angle, "sources": [state["items"][i] for i in ids]}


def _post_system() -> str:
    examples = style_examples()
    return prompt("write_post", AUTHOR=AUTHOR, STYLE_GUIDE=style_guide(), HUMANIZER=humanizer.rules(),
                  EXAMPLES="\n\n---\n\n".join(examples) or "(no examples yet; follow the style guide)")


def write_post(state: DraftState, config: RunnableConfig) -> DraftState:
    recent = "\n\n---\n\n".join(state["recent_posts"])
    user = (f"Angle: {state['angle'].get('angle')}\nCandidate hook: {state['angle'].get('hook')}\n"
            f"Why now: {state['angle'].get('why_now')}\n\n# Source items\n{_sources_block(state['sources'])}")
    if recent:
        user += f"\n\n# {AUTHOR}'s recent approved posts (match this voice; they include the author's own edits)\n{recent}"
    return {"post": _clean_post(llm.call(ctx_of(config), "voice", "write_post", _post_system(), user))}


def _clean_post(text: str) -> str:
    text = text.strip().strip('"').strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def critic(state: DraftState, config: RunnableConfig) -> DraftState:
    user = f"# Source items\n{_sources_block(state['sources'])}\n\n# Post\n{state['post']}"
    review = llm.call_json(ctx_of(config), "judge", "critic", prompt("critic", STYLE_GUIDE=style_guide()), user)
    scores = {k: int(v) for k, v in (review.get("scores") or {}).items() if str(v).isdigit()}
    return {"scores": scores, "critique": review.get("critique", ""),
            "unsupported": [c for c in review.get("unsupported_claims", []) if isinstance(c, str)]}


def guardrail_violations(post: str, source_urls: set[str], sources_text: str | None = None) -> list[str]:
    """Deterministic checks that run on every draft, independent of the LLM critic.
    Includes the humanizer skill's accent scanner and, when sources are given, its fact guard."""
    v, words = drafting.common_violations(post, sources_text), len(post.split())
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


def guardrails(state: DraftState) -> DraftState:
    """Deterministic checks, then the weighted score. Keeps the best version seen so far: a rewrite can
    make a post worse, and the more human version wins (fewest violations, then overall, then humanizer)."""
    sources_text = _sources_block(state["sources"])
    violations = guardrail_violations(state["post"], {s["url"] for s in state["sources"]}, sources_text)
    accent = humanizer.scan(state["post"])
    overall = overall_score(state["scores"], accent["score"])
    current = {"post": state["post"], "scores": state["scores"], "critique": state.get("critique", ""),
               "unsupported": state.get("unsupported", []), "violations": violations, "accent": accent, "overall": overall}
    return {"violations": violations, "accent": accent, "overall": overall,
            "best": drafting.keep_best(state.get("best"), current)}


weak_scores = drafting.weak_scores


def needs_revision(state: DraftState) -> str:
    weak = weak_scores(state["scores"], state["accent"]["score"])
    if (weak or state["violations"]) and state["revisions"] < MAX_REVISIONS:
        return "revise"
    return "use_best"


def use_best(state: DraftState) -> DraftState:
    """Publish the best-ranked version, not necessarily the last rewrite."""
    b = state["best"]
    return {"post": b["post"], "scores": b["scores"], "critique": b["critique"], "unsupported": b["unsupported"],
            "violations": b["violations"], "accent": b["accent"], "overall": b["overall"]}


def revise(state: DraftState, config: RunnableConfig) -> DraftState:
    issues = "\n".join(f"- {v}" for v in state["violations"]) or "- none"
    user = (f"# Source items\n{_sources_block(state['sources'])}\n\n# Current post\n{state['post']}\n\n"
            f"# Editor scores\n{json.dumps(state['scores'])}\n\n# Editor notes\n{state['critique']}\n\n"
            f"# Rule violations (must fix)\n{issues}")
    text = llm.call(ctx_of(config), "voice", "revise", _post_system() + "\n\n" + prompt("revise"), user)
    return {"post": _clean_post(text), "revisions": state["revisions"] + 1}


def onepager(state: DraftState, config: RunnableConfig) -> DraftState:
    user = f"# Post\n{state['post']}\n\n# Source items\n{_sources_block(state['sources'])}"
    return {"onepager": llm.call_json(ctx_of(config), "writer", "onepager", prompt("onepager"), user)}


def save(state: DraftState, config: RunnableConfig) -> DraftState:
    ctx = ctx_of(config)
    sources = [{"url": s["url"], "title": s["title"], "source": s["source"], "origin": s.get("origin", "rss")}
               for s in state["sources"]]
    draft_id = ctx.db.insert_draft({
        "week": state["week"], "angle": state["angle"], "post_md": state["post"],
        "onepager_html": render_onepager(state["onepager"], sources, state["week"], AUTHOR),
        "sources": sources, "revisions": state["revisions"],
        "judge_scores": {**state["scores"], "violations": state["violations"], "humanizer": state["accent"]["score"],
                         "critique": state.get("critique", ""), "unsupported_claims": state.get("unsupported", []),
                         "overall": state["overall"],
                         "passed_review": not state["violations"] and not weak_scores(state["scores"], state["accent"]["score"])},
    }, ctx.run_id)
    return {"draft_id": draft_id}


def build_draft(persist: bool = True):
    g = StateGraph(DraftState)
    nodes = [("load_items", load_items), ("pick_angle", pick_angle), ("read_sources", read_sources), ("write_post", write_post), ("critic", critic),
             ("guardrails", guardrails), ("revise", revise), ("use_best", use_best), ("onepager", onepager)]
    if persist:
        nodes.append(("save", save))
    for name, fn in nodes:
        g.add_node(name, fn)
    g.add_edge(START, "load_items")
    g.add_edge("load_items", "pick_angle")
    g.add_edge("pick_angle", "read_sources")
    g.add_edge("read_sources", "write_post")
    g.add_edge("write_post", "critic")
    g.add_edge("critic", "guardrails")
    g.add_conditional_edges("guardrails", needs_revision, {"revise": "revise", "use_best": "use_best"})
    g.add_edge("use_best", "onepager")
    g.add_edge("revise", "critic")
    if persist:
        g.add_edge("onepager", "save")
        g.add_edge("save", END)
    else:
        g.add_edge("onepager", END)
    return g.compile()
