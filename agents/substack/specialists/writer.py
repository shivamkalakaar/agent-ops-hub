"""Writer: the only specialist on the voice tier (Sonnet). Writes the post from the brief, and on a rewrite
applies the editor's notes, the fact check and the final check's blockers together."""
import json
import re

from core import drafting, humanizer
from agents.substack import checks
from agents.substack.roster import AUTHOR
from agents.substack.specialists import allowed_links, brief_block, current, numbered_sources, sources_text, style_guide, version

TARGET_WORDS = 1000  # middle of the 800-1,200 band
ESSAY_WORDS = 450  # of each long-form example: enough to show pacing, cheap enough to send every time


def system(state, cfg, seed: str | None = None) -> str:
    essays = drafting.style_examples(n=2, short_only=False, min_words=800, seed=seed)
    structure = "\n\n---\n\n".join(" ".join(e.split()[:ESSAY_WORDS]) + " …" for e in essays) or "(no examples yet)"
    voice = "\n\n---\n\n".join(state.get("voice_posts") or []) or "(no approved posts yet; follow the style guide)"
    return drafting.fill(cfg.prompt, AUTHOR=AUTHOR, STYLE_GUIDE=style_guide(), VOICE=voice, STRUCTURE=structure,
                         HUMANIZER=humanizer.rules())


def _feedback(state) -> str:
    v, parts = version(state), []
    if (e := state.get("edit_report")) and e.get("version") == v:
        notes = "\n".join(f'- "{n.get("quote", "")}": {n.get("issue", "")} → {n.get("fix", "")}' for n in e.get("notes", []))
        parts.append(f"# Editor (overall {e['overall']}/5, scores {json.dumps(e['scores'])})\n{e.get('critique', '')}\n{notes}")
        if e.get("violations"):
            parts.append("# Rule violations (must fix)\n" + "\n".join(f"- {x}" for x in e["violations"]))
    if (f := state.get("fact_report")) and f.get("version") == v:
        lines = [f'- "{c.get("claim", "")}" → {c.get("fix", "")}' for c in f.get("claims", []) if c.get("verdict") == "unsupported"]
        lines += [f"- {x} is not in the sources: remove it or use the sourced figure" for x in f.get("invented", [])]
        if lines:
            parts.append("# Fact check: unsupported (must fix)\n" + "\n".join(lines))
        hedges = [f'- "{c.get("claim", "")}" → {c.get("fix", "")}' for c in f.get("claims", []) if c.get("verdict") == "hedge"]
        if hedges:
            parts.append("# Fact check: attribute more clearly (should fix)\n" + "\n".join(hedges))
    if (fr := state.get("final_report")) and fr.get("version") == v and not fr.get("passed"):
        parts.append("# Final check blockers (must fix)\n" + "\n".join(f"- {b}" for b in fr.get("blockers", [])))
    return "\n\n".join(parts)


def clean(text: str) -> str:
    text = text.strip()
    if m := re.match(r"^```(?:markdown|md)?\s*\n(.*?)\n```\s*$", text, re.S):
        text = m.group(1).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def run(state, ctx, cfg):
    user = (f"# Brief\n{brief_block(state['brief'])}\n\n# Source items\n{numbered_sources(state['sources'])}\n\n"
            f"# The only URLs you may link (copy them exactly; never link a URL that only appears inside article text)\n"
            f"{allowed_links(state['sources'])}")
    if seed := state.get("seed"):
        user += f"\n\n# The LinkedIn musing this post expands ({AUTHOR}'s own words; keep its idea, go deeper)\n{seed['post']}"
    draft = current(state)
    if draft:
        user += f"\n\n# Current draft (v{draft['version']})\n{draft['md']}\n\n{_feedback(state)}"
        user += "\n\nRewrite the whole post. Fix everything marked must-fix; keep what already works."
    if state.get("instructions"):
        user += f"\n\n# Managing editor's note\n{state['instructions']}"
    if state.get("lessons"):
        user += f"\n\n{state['lessons']}"
    # Best of N (config/models.yaml voice.candidates): repeats get different essay examples; the code's checks pick one.
    outs = drafting.write_candidates(ctx, "voice", "writer", drafting.candidates("voice", cfg.model, n=2 if draft else None),
                                     lambda i: system(state, cfg, seed=f"{state.get('slot')}-{i}"), user, clean, max_words=3 * 1200)
    urls, text = {s["url"] for s in state["sources"]}, sources_text(state)

    def score(md: str) -> tuple:
        body = checks.parse(md)["body"]
        return (len(checks.violations(md, urls, text)), -humanizer.scan(checks.LINK.sub(r"\1", body))["score"],
                abs(checks.body_words(body) - TARGET_WORDS))
    best, scored = drafting.pick(outs, score)
    md, n = best["text"], version(state) + 1
    p = checks.parse(md)
    short = lambda m: m.split("/")[-1]  # noqa: E731
    summary = (f"{'Rewrote' if draft else 'Wrote'} v{n}: \"{p['title']}\", {checks.body_words(p['body'])} words, "
               f"{len(checks.sections(p['body']))} sections"
               + (f"; best of {len(scored)}, kept {short(best['model'])}" if len(scored) > 1 else ""))
    out = {"version": n, "md": md, "model": best["model"],
           "candidates": [{"model": d["model"], "flags": d["score"][0], "humanizer": -d["score"][1]} for d in scored]}
    return {"drafts": state.get("drafts", []) + [{"version": n, "md": md}]}, summary, out
