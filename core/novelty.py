"""Is a new post too close to a recent one? Word overlap on content words: no model call, so it runs on every draft.

Three checks against each recent post: the whole post (same ground covered), the first line (same hook) and the
last line (same ending; the first three words matching counts too, since the agent liked to reuse closers, and so
does a closer from the same family, like "Before you pick…" after "Before you buy…").
"""
import re

STOP = set("""a an the and or but if then so of to in on at by for with from as is are was were be been being it its
this that these those you your we our they their he she his her i me my not no do does did have has had will would
can could should just than more most also about into over what which who how when where why there here all any
some one two""".split())
POST, HOOK, ENDING = 0.35, 0.5, 0.5  # Jaccard thresholds
# Reader-instruction closers the agent rotated through until every post ended the same way (Nov 2026: 10 of 11 posts).
# Two endings in the same family read as the same ending even when the words differ.
ENDING_FAMILIES = [
    (r"^(?:so )?(?:the )?next time\b", "\"next time you see …\""),
    (r"^before (?:you|your|paying|buying|picking|trusting|quoting|switching)\b", "\"before you …\""),
    (r"^if (?:you|your)\b.*\b(?:ask|check|measure|run|compare|test|try)\b", "\"if your …, ask/measure …\""),
    (r"\bask (?:two|three|these|a few) questions\b", "\"ask two questions\""),
]


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9$%][a-z0-9$%.'-]*", text.lower()) if w not in STOP and len(w) > 2}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _lines(post: str) -> list[str]:
    return [l.strip() for l in post.strip().splitlines() if l.strip() and not l.strip().startswith("#")]


def ending_family(line: str) -> str:
    text = " ".join(re.findall(r"[a-z0-9']+", line.lower()))
    return next((name for pattern, name in ENDING_FAMILIES if re.search(pattern, text)), "")


def first_words(line: str, n: int = 3) -> str:
    return " ".join(re.findall(r"[a-z']+", line.lower())[:n])


def check(post: str, recent: list[str] | None) -> list[str]:
    """Problems as guardrail messages; empty when the post is new enough."""
    out, lines = [], _lines(post)
    if not lines:
        return out
    hook, end = lines[0], lines[-1]
    for r in recent or []:
        rl = _lines(r)
        if not rl:
            continue
        if jaccard(words(post), words(r)) >= POST:
            out.append("covers much the same ground as a recent post; pick another angle or a new detail")
        if jaccard(words(hook), words(rl[0])) >= HOOK:
            out.append("opens almost the same way as a recent post; write a different hook")
        if (first_words(end) and first_words(end) == first_words(rl[-1])) or jaccard(words(end), words(rl[-1])) >= ENDING:
            out.append(f"ends the same way as a recent post (\"{first_words(end)} …\"); find a different ending")
        elif (family := ending_family(end)) and family == ending_family(rl[-1]):
            out.append(f"ends with the same kind of closer as a recent post ({family}); end the way the structure says, "
                       "in different words")
    return list(dict.fromkeys(out))
