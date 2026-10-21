"""Deterministic guardrails for a Substack post, independent of any model. Used by the editor (to rank versions),
the final check (to block) and the evals. Shared rules (humanizer, structural tells, invented numbers, banned
phrases) come from core.drafting; the format rules here are Substack's."""
import re

from core import drafting

WORDS = (700, 1350)  # guardrail band; the prompt targets 800–1,200
MAX_TITLE = 70
MAX_SUBTITLE = 160
SECTIONS = (3, 5)
MAX_LIST_ITEMS = 12
CTA = re.compile(r"\b(?:subscribe|share this post|leave a comment|hit the like|smash that)\b", re.I)
LINK = re.compile(r"(?<!!)\[([^\]]*)\]\((\S+?)\)")  # links, not images


def parse(md: str) -> dict:
    """Split the writer's markdown into title, subtitle and body. Title is the first `# ` line; the subtitle is
    the next non-empty line if it's italic (*...* or _..._)."""
    lines = md.strip().splitlines()
    title, subtitle, start = "", "", 0
    if lines and lines[0].startswith("# "):
        title, start = lines[0][2:].strip(), 1
        rest = [(i, ln.strip()) for i, ln in enumerate(lines[1:], 1) if ln.strip()]
        if rest and re.fullmatch(r"([*_])(?!\1).+\1", rest[0][1]):
            subtitle, start = rest[0][1][1:-1].strip(), rest[0][0] + 1
    return {"title": title, "subtitle": subtitle, "body": "\n".join(lines[start:]).strip()}


def body_words(body: str) -> int:
    text = LINK.sub(r"\1", body)
    return len(re.sub(r"[#>*_`-]", " ", text).split())


def sections(body: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^##\s+(.+)$", body, re.M)]


def violations(md: str, source_urls: set[str], sources_text: str | None = None) -> list[str]:
    p, v = parse(md), []
    body = p["body"]
    if not p["title"]:
        v.append("no title: the first line must be '# Title'")
    elif len(p["title"]) > MAX_TITLE:
        v.append(f"title is {len(p['title'])} characters; keep it to {MAX_TITLE}")
    if not p["subtitle"]:
        v.append("no subtitle: the line after the title must be an italic subtitle")
    elif len(p["subtitle"]) > MAX_SUBTITLE:
        v.append(f"subtitle is {len(p['subtitle'])} characters; keep it to {MAX_SUBTITLE}")
    words = body_words(body)
    if not WORDS[0] <= words <= WORDS[1]:
        v.append(f"{words} words; keep it between 800 and 1,200")
    n = len(sections(body))
    if not SECTIONS[0] <= n <= SECTIONS[1]:
        v.append(f"{n} '##' sections; use {SECTIONS[0]}-{SECTIONS[1]}")
    if re.search(r"^#\s", body, re.M) or re.search(r"^###", body, re.M):
        v.append("only '##' headings in the body (one '# ' title at the top)")
    if re.search(r"^##\s+(?:sources|references)\s*$", body, re.M | re.I):
        v.append("don't write a Sources section; it's added automatically")
    if len(re.findall(r"^\s*(?:[-•*]|\d+[.)])\s", body, flags=re.M)) > MAX_LIST_ITEMS:
        v.append(f"more than {MAX_LIST_ITEMS} list items; it's an essay, not a listicle")
    if re.search(r"(?<![\w&/])#[A-Za-z]\w*", body):
        v.append("hashtags don't belong in a Substack post")
    if m := CTA.search(body):
        v.append(f'newsletter boilerplate "{m.group(0)}": Substack adds its own buttons')
    if re.search(r"<[a-zA-Z/!][^>]*>", body):
        v.append("raw HTML in the markdown")
    linked = {u.rstrip(").,") for _, u in LINK.findall(body)}
    bare = {u.rstrip(").,") for u in re.findall(r"(?<!\()https?://[^\s)]+", LINK.sub("", body))}
    v += [f"link not from the sources: {u}" for u in sorted(linked | bare) if u not in source_urls]
    v += drafting.common_violations(LINK.sub(r"\1", f"{p['title']}\n{p['subtitle']}\n{body}"), sources_text)
    return v
