"""Editor: a rubric from a different model family than the writer, plus the deterministic humanizer and guardrails.
Overall = humanizer 40% + rubric 60%. Every edited version becomes a candidate (see agents/substack/ranking.py),
so a worse rewrite never wins."""
from core import drafting, humanizer, llm
from agents.substack import checks
from agents.substack.roster import AUTHOR
from agents.substack.specialists import current, sources_text, style_guide

DIMENSIONS = ("hook", "thesis", "structure", "depth", "concreteness", "voice", "human")


def run(state, ctx, cfg):
    draft = current(state)
    review = llm.call_json(ctx, "check", "editor", drafting.fill(cfg.prompt, AUTHOR=AUTHOR, STYLE_GUIDE=style_guide()),
                           f"# Thesis the post should argue\n{state['brief'].get('thesis')}\n\n# Draft\n{draft['md']}"
                           + (f"\n\n{state['lessons']}\nA draft that breaks one of these lessons loses a point on voice or "
                              "human, and a note must name the lesson." if state.get("lessons") else ""),
                           model=cfg.model)
    raw = review.get("scores") or {}
    scores = {k: max(1, min(5, int(raw[k]))) for k in DIMENSIONS if str(raw.get(k, "")).isdigit()}
    p = checks.parse(draft["md"])
    accent = humanizer.scan(checks.LINK.sub(r"\1", p["body"]))
    overall = drafting.overall_score(scores, accent["score"])
    violations = checks.violations(draft["md"], {s["url"] for s in state["sources"]}, sources_text(state))
    notes = [n for n in review.get("notes") or [] if isinstance(n, dict)][:6]
    report = {"version": draft["version"], "scores": scores, "notes": notes, "critique": review.get("critique", ""),
              "accent": {"score": accent["score"], "hits": accent["hits"][:5], "structural": accent["structural"]},
              "overall": overall, "violations": violations}
    entry = {"version": draft["version"], "md": draft["md"], "scores": scores, "violations": violations,
             "overall": overall, "accent": report["accent"], "critique": report["critique"]}
    summary = (f"v{draft['version']}: overall {overall}/5 (humanizer {accent['score']}/5), "
               f"{len(violations)} guardrail flag{'s' * (len(violations) != 1)}. {report['critique'][:140]}")
    return {"edit_report": report, "candidates": {**state.get("candidates", {}), str(draft["version"]): entry}}, summary, report


def weak(report: dict) -> bool:
    return drafting.weak_scores(report["scores"], report["accent"]["score"], must_be_high=("thesis",))
