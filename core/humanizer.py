"""Adapter for the humanizer skill (skills/humanizer, MIT). Loads its scanner as a module so every draft gets
an accent score, a hit list, and a fact guard against the source items. Also exposes the skill's own rules so
the writer prompt stays in sync with the skill file instead of a copy."""
import importlib.util
import re
from functools import cache

from core.config import ROOT

SKILL = ROOT / "skills" / "humanizer"
# The skill's scanner scores 0-100 (lower is better). We report it on the critic's 1-5 scale (5 = most human)
# so every score in the dashboard reads the same way. Upper bounds of the raw score for 5, 4, 3, 2; above that is 1.
FIVE_SCALE = (5, 12, 20, 35)
MIN_HUMAN = 3  # below 3/5 (raw score above 20) the draft goes back for a rewrite


def to_five(raw: int) -> int:
    return next((5 - i for i, cap in enumerate(FIVE_SCALE) if raw <= cap), 1)

# Structural tells the scanner misses in short posts; caught here so they never reach a human reviewer.
EXTRA_TELLS = [
    (re.compile(r"\b(?:isn't|aren't|wasn't|is not|are not)\b[^.!?\n]{0,80}[.!?]\s+(?:It's|They're|It is|They are|That's)\b", re.I),
     'negation pivot ("X isn\'t Y. It\'s Z."): state the real claim directly'),
    (re.compile(r"^\s*(?:my take|hot take|the takeaway|bottom line|the lesson|here's the thing)\s*:", re.I | re.M),
     'labelled closer ("My take:"): just say the take'),
    (re.compile(r"\bwearing (?:a|an|the) [^.]{1,40}(?:clothes|costume|hat)\b", re.I),
     'costume metaphor ("X wearing Y\'s clothes"): say what it is plainly'),
]


QUOTED = re.compile(r'"[^"\n]{1,200}"|“[^”\n]{1,200}”')


@cache
def _tells():
    spec = importlib.util.spec_from_file_location("humanizer_tells", SKILL / "scripts" / "tells.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scan(text: str) -> dict:
    """Accent score (0-100), band, and the worst hits with the skill's fix hints, plus our structural extras."""
    r = _tells().scan_result(text, limit=8)
    hits = [f'{h.get("label") or h.get("id")}: "{h.get("text", "")}" → {h.get("fix", "")}'
            for h in r["hits"]]
    unquoted = QUOTED.sub(lambda m: " " * len(m.group(0)), text)  # quoting a pattern isn't using it
    extras = [f'{why}: "{m.group(0).strip()[:70]}"' for rx, why in EXTRA_TELLS for m in rx.finditer(unquoted)]
    # the scanner misses structural tells in short posts, so each one costs a point on the 1-5 scale
    return {"score": max(1, to_five(r["score"]) - len(extras)), "raw": r["score"], "band": r["band"], "hits": hits, "structural": extras,
            "contributors": [c["label"] for c in r["contributors"][:3]]}


def invented_facts(sources_text: str, post: str) -> list[str]:
    """Numbers, dates and links in the post that don't appear in the sources (the skill's fact guard, sources as BEFORE)."""
    g = _tells().fact_guard(sources_text, post)
    return [f'{p["kind"]} "{p["text"]}"' for p in g["problems"] 
            if p["type"] == "added" and p["sev"] == "error" and re.search(r"\d", p["text"])]


@cache
def rules() -> str:
    """The skill's 'worst offenders' and 'what to do instead' sections, verbatim, for the writer prompt."""
    body = (SKILL / "SKILL.md").read_text()
    keep = [m.group(0).strip() for m in re.finditer(r"^## (?:The worst offenders|What to do instead)\n.*?(?=^## |\Z)", body, re.S | re.M)]
    return "\n\n".join(keep)
