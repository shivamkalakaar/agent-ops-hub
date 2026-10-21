"""Writer: the only specialist on the voice tier (Sonnet). Writes the post from the brief, and on a rewrite
applies the editor's notes, the fact check and the final check's blockers together."""
import json
import re

from core import drafting, humanizer, llm
from agents.substack import checks
from agents.substack.roster import AUTHOR
from agents.substack.specialists import brief_block, current, numbered_sources, style_guide, version

ESSAY_WORDS = 450  # of each long-form example: enough to show pacing, cheap enough to send every time


def system(state, cfg) -> str:
    essays = drafting.style_examples(n=2, short_only=False, min_words=800)
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
        bad = [c for c in f.get("claims", []) if c.get("verdict") in ("hedge", "unsupported")]
        lines = [f'- [{c["verdict"]}] "{c.get("claim", "")}" → {c.get("fix", "")}' for c in bad]
        lines += [f"- {x} is not in the sources: remove it or use the sourced figure" for x in f.get("invented", [])]
        if lines:
            parts.append("# Fact check (must fix)\n" + "\n".join(lines))
    if (fr := state.get("final_report")) and fr.get("version") == v and not fr.get("passed"):
        parts.append("# Final check blockers (must fix)\n" + "\n".join(f"- {b}" for b in fr.get("blockers", [])))
    return "\n\n".join(parts)


def clean(text: str) -> str:
    text = text.strip()
    if m := re.match(r"^```(?:markdown|md)?\s*\n(.*?)\n```\s*$", text, re.S):
        text = m.group(1).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def run(state, ctx, cfg):
    user = f"# Brief\n{brief_block(state['brief'])}\n\n# Source items\n{numbered_sources(state['sources'])}"
    if seed := state.get("seed"):
        user += f"\n\n# The LinkedIn musing this post expands ({AUTHOR}'s own words; keep its idea, go deeper)\n{seed['post']}"
    draft = current(state)
    if draft:
        user += f"\n\n# Current draft (v{draft['version']})\n{draft['md']}\n\n{_feedback(state)}"
        user += "\n\nRewrite the whole post. Fix everything marked must-fix; keep what already works."
    if state.get("instructions"):
        user += f"\n\n# Managing editor's note\n{state['instructions']}"
    md = clean(llm.call(ctx, "voice", "writer", system(state, cfg), user, model=cfg.model))
    n = version(state) + 1
    p = checks.parse(md)
    summary = (f"{'Rewrote' if draft else 'Wrote'} v{n}: \"{p['title']}\", {checks.body_words(p['body'])} words, "
               f"{len(checks.sections(p['body']))} sections")
    return {"drafts": state.get("drafts", []) + [{"version": n, "md": md}]}, summary, {"version": n, "md": md}
