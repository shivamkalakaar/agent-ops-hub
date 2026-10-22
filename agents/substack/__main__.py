"""CLI: uv run python -m agents.substack {draft [--seed DRAFT_ID] | revise DRAFT_ID | style-guide | export DRAFT_ID}"""
import argparse
import sys

from core import drafting, llm, runner
from core.config import CONFIG, ROOT, git_sha
from core.db import connect
from agents.substack import graph, render, revise
from agents.substack.roster import AGENT, PROMPTS

PROMPT_VERSION = "v1"


def summarize(state: dict) -> dict:
    final, judge = state.get("final") or {}, state.get("judge") or {}
    return {"draft_id": state.get("draft_id"), "slot": state.get("slot"), "title": final.get("title"),
            "version": final.get("version"), "passed": final.get("passed"), "steps": state.get("work_steps"),
            "calls": state.get("calls"), "overall": judge.get("overall"), "humanizer": judge.get("humanizer"),
            "images": len(state.get("images") or []), "seed": (state.get("seed") or {}).get("id")}


def trace_scores(state: dict) -> dict:
    judge = state.get("judge") or {}
    out = {f"editor.{k}": v for k, v in judge.items() if k in graph.editor.DIMENSIONS}
    if judge.get("overall") is not None:
        out |= {"overall": judge["overall"], "humanizer": judge["humanizer"]}
    return out | {"passed_review": float(bool(judge.get("passed_review")))}


def draft(seed: str | None) -> int:
    init = {"seed_draft_id": seed} if seed else {}
    return runner.run_graph(AGENT, "draft", graph.build(), init, summarize, prompt_version=PROMPT_VERSION, scores=trace_scores)


def revise_draft(draft_id: str) -> int:
    return runner.run_graph(AGENT, "revise", revise.build(), {"revise_draft_id": draft_id}, summarize,
                            prompt_version=PROMPT_VERSION, scores=trace_scores)


def build_style_guide() -> int:
    """Voice from the author's approved musings, structure from the long essays in examples.md."""
    essays = drafting.style_examples(n=6, short_only=False, min_words=800)
    with connect() as db:
        own = db.approved_posts(10)
        if len(own) + len(essays) < 3:
            print("Need at least 3 samples: approve some musings or add long essays to config/style/examples.md.", file=sys.stderr)
            return 1
        labeled = [f"[AUTHOR POST]\n{p}" for p in own] + [f"[LONG-FORM ESSAY, first 700 words]\n{' '.join(e.split()[:700])}" for e in essays]
        run_id = db.start_run(AGENT, "style_guide", git_sha())
        try:
            guide = llm.call(llm.RunContext(db=db, run_id=run_id), "writer", "style_guide",
                             (PROMPTS / "style_guide.md").read_text(), "\n\n---\n\n".join(labeled))
        except Exception as ex:
            db.finish_run(run_id, "failed", error=str(ex))
            raise
        db.finish_run(run_id, "succeeded", stats={"author_posts": len(own), "essays": len(essays)})
    (CONFIG / "style" / "substack_guide.md").write_text(guide.strip() + "\n")
    print(f"Wrote config/style/substack_guide.md from {len(own)} of your posts and {len(essays)} essays. Review it.")
    return 0


def export(draft_id: str) -> int:
    with connect() as db:
        d = db.one("select * from substack_drafts where id=%s", (draft_id,))
    if not d:
        print(f"no Substack draft {draft_id}", file=sys.stderr)
        return 1
    title, subtitle = d.get("edited_title") or d["title"], d.get("edited_subtitle") or d.get("subtitle") or ""
    body = d.get("edited_body_md") or d["body_md"]
    out = ROOT / "out" / "substack"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{d['slot']}-{draft_id}.md").write_text(f"# {title}\n\n*{subtitle}*\n\n{body}\n")
    (out / f"{d['slot']}-{draft_id}.html").write_text(render.page(title, subtitle, render.to_html(body)))
    print(f"Wrote out/substack/{d['slot']}-{draft_id}.md and .html")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="agents.substack")
    sub = ap.add_subparsers(dest="command", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--seed", help="id of a published LinkedIn musing to expand")
    r = sub.add_parser("revise")
    r.add_argument("draft_id")
    sub.add_parser("style-guide")
    e = sub.add_parser("export")
    e.add_argument("draft_id")
    args = ap.parse_args()
    if args.command == "draft":
        return draft(args.seed or None)
    if args.command == "revise":
        return revise_draft(args.draft_id)
    return build_style_guide() if args.command == "style-guide" else export(args.draft_id)


if __name__ == "__main__":
    sys.exit(main())
