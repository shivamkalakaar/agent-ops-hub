"""Specialists share one shape: fn(state, ctx, cfg) -> (update, summary, output).

- update: the workspace fields the specialist owns
- summary: one line for the dashboard timeline and the supervisor's history
- output: what the dashboard shows when the step is expanded
"""
import json

from core import drafting
from core.config import CONFIG

DEFAULT_GUIDE = """- First person, plain words, specific nouns. Opinions framed as the author's own.
- Open with the news and the claim in the first paragraph. Sections build an argument; each earns its place.
- Numbers and names over adjectives. End with a prediction or a recommendation, never a recap."""


def style_guide() -> str:
    path = CONFIG / "style" / "substack_guide.md"
    return path.read_text() if path.exists() else DEFAULT_GUIDE


def current(state) -> dict | None:
    return state["drafts"][-1] if state.get("drafts") else None


def version(state) -> int:
    return current(state)["version"] if state.get("drafts") else 0


def numbered_sources(sources: list[dict]) -> str:
    """Each source with its URL: the writer may only link these, so it has to see them (it used to guess)."""
    return "\n\n".join(f"(source {n}) URL: {s['url']}\n{drafting.sources_block([s])}" for n, s in enumerate(sources))


def allowed_links(sources: list[dict]) -> str:
    return "\n".join(f"- {s['url']}  ({s['source']}: {s['title']})" for s in sources)


def sources_text(state) -> str:
    """Everything a fact may come from: the source items (with article text) and the seed musing."""
    text = drafting.sources_block(state.get("sources", []))
    if seed := state.get("seed"):
        text += f"\n\n[{seed.get('week')} LinkedIn musing by the author]\n{seed['post']}"
    return text


def brief_block(brief: dict) -> str:
    keep = {k: brief.get(k) for k in ("thesis", "title", "subtitle", "why_now", "outline", "facts", "counterpoints")}
    return json.dumps(keep, ensure_ascii=False, indent=1)
