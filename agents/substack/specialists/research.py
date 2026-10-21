"""Research: choose the story (and the musing to build on), read the full articles, extract facts with evidence.
A second call digs deeper on the supervisor's request, searching 30 days of collected items (no paid web search)."""
import json
from datetime import datetime, timedelta, timezone

from core import drafting, llm
from core.feeds import fetch_article
from agents.substack.roster import AUTHOR, PROMPTS
from agents.substack.specialists import numbered_sources

prompt = drafting.prompt_loader(PROMPTS)
ARTICLE_WORDS = 3600  # split across the chosen sources; a long post needs ~3x a LinkedIn post's grounding
MAX_SOURCES = 5


def _listing(items: list[dict]) -> str:
    return "\n".join(json.dumps({"id": n, "source": i["source"], "origin": i.get("origin"), "layer": i.get("layer"),
                                 "importance": i.get("importance"), "title": i["title"],
                                 "summary": (i.get("summary") or "")[:400], "why": i.get("why")}, ensure_ascii=False)
                     for n, i in enumerate(items))


def _musings(musings: list[dict]) -> str:
    return "\n".join(json.dumps({"id": m["id"], "week": m.get("week"), "post": m["post"][:700]}, ensure_ascii=False)
                     for m in musings) or "(none)"


def _facts(ctx, brief: dict, sources: list[dict]) -> dict:
    user = (f"# Thesis\n{brief.get('thesis')}\n\n# Outline\n{json.dumps(brief.get('outline'), ensure_ascii=False)}\n\n"
            f"# Source items\n{numbered_sources(sources)}")
    return llm.call_json(ctx, "writer", "research_facts", prompt("research_facts"), user)


def _clean_facts(raw: dict, n_sources: int, offset: int = 0) -> list[dict]:
    out = []
    for f in raw.get("facts") or []:
        if isinstance(f, dict) and f.get("claim") and isinstance(f.get("source_id"), int) and 0 <= f["source_id"] < n_sources:
            out.append({**f, "source_id": f["source_id"] + offset})
    return out


def run(state, ctx, cfg):
    return follow_up(state, ctx, cfg) if state.get("brief") else first(state, ctx, cfg)


def first(state, ctx, cfg):
    items, musings = state["items"], state.get("musings", [])
    forced = state.get("seed_draft_id")
    user = (f"# Candidate items (this week's AI news)\n{_listing(items)}\n\n"
            f"# {AUTHOR}'s recent LinkedIn musings (possible seeds)\n{_musings(musings)}\n\n"
            "# Recent Substack titles (don't repeat these angles)\n"
            + ("\n".join(f"- {t}" for t in state.get("recent_titles", [])) or "(none yet)"))
    if forced:
        user += f"\n\n# Required seed\n{AUTHOR} asked for this post to expand musing {forced}. Use it as the seed."
    if state.get("instructions"):
        user += f"\n\n# Managing editor's note\n{state['instructions']}"
    pick = llm.call_json(ctx, "writer", "research", drafting.fill(cfg.prompt, AUTHOR=AUTHOR), user, model=cfg.model)
    ids = [i for i in pick.get("item_ids", []) if isinstance(i, int) and 0 <= i < len(items)]
    ids = list(dict.fromkeys(ids))[:MAX_SOURCES] or [0]
    by_id = {m["id"]: m for m in musings}
    seed = by_id.get(forced) if forced else by_id.get(pick.get("seed_draft_id"))
    sources = drafting.read_sources([items[i] for i in ids], words=ARTICLE_WORDS, fetch=fetch_article)
    outline = [o for o in pick.get("outline", []) if isinstance(o, dict) and o.get("heading")][:4]
    brief = {k: pick.get(k, "") for k in ("thesis", "title", "subtitle", "why_now")} | {"outline": outline}
    facts = _facts(ctx, brief, sources)
    brief |= {"facts": _clean_facts(facts, len(sources)), "counterpoints": facts.get("counterpoints") or [],
              "chart": facts.get("chart"), "gaps": facts.get("gaps") or [], "seed_draft_id": seed["id"] if seed else None}
    read = sum(1 for s in sources if s.get("body"))
    summary = (f'Brief: "{brief["title"]}". {len(sources)} sources ({read} read in full), {len(brief["facts"])} facts'
               + (f", building on musing {seed.get('week')}" if seed else ""))
    return ({"brief": brief, "sources": sources, "seed": seed}, summary,
            {"brief": brief, "sources": [{"title": s["title"], "url": s["url"], "source": s["source"]} for s in sources]})


def follow_up(state, ctx, cfg):
    brief, sources = state["brief"], state["sources"]
    ask = state.get("instructions") or "Find more evidence for: " + "; ".join(brief.get("gaps") or [brief.get("thesis", "")])
    terms = llm.call_json(ctx, "writer", "research_followup", prompt("research_followup"),
                          f"# Request\n{ask}\n\n# Thesis\n{brief.get('thesis')}").get("search_terms", [])
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    known = {s["url"] for s in sources}
    found = [i for i in ctx.db.search_items([t for t in terms if isinstance(t, str)], since) if i["url"] not in known]
    found = found[:max(0, MAX_SOURCES + 2 - len(sources))]
    if not found:
        return {}, f"Dug into {', '.join(terms) or 'the request'}: nothing new in the archive", {"search_terms": terms}
    new = drafting.read_sources(found, words=ARTICLE_WORDS // 2, fetch=fetch_article)
    facts = _clean_facts(_facts(ctx, brief, new), len(new), offset=len(sources))
    brief = {**brief, "facts": brief.get("facts", []) + facts}
    return ({"brief": brief, "sources": sources + new}, f"Dug into {', '.join(terms)}: +{len(new)} sources, +{len(facts)} facts",
            {"search_terms": terms, "added": [{"title": s["title"], "url": s["url"]} for s in new], "facts": facts})
