"""CLI: uv run python -m agents.market_intel {collect|draft|style-guide}"""
import argparse
import sys

from core import llm, runner
from core.config import CONFIG, git_sha
from core.db import connect
from agents.market_intel import graph

AGENT = "market_intel"
PROMPT_VERSION = "v1"


def redraft_state(draft_id: str) -> dict:
    """Rewrite a saved draft around the owner's take, on the same stories, in place."""
    with connect() as db:
        d = db.draft(draft_id)
        if not d:
            raise SystemExit(f"no draft {draft_id}")
        items = [db.item(s["url"]) or {**s, "summary": "", "importance": 3} for s in d["sources"] or []]
    return {"items": items, "take": d.get("my_take") or "", "week": d["week"], "redraft_id": draft_id, "recent_angles": []}


def run(kind: str, skip_if_pending: int | None = None, redraft: str | None = None) -> int:
    builder = {"collect": graph.build_collect, "draft": graph.build_draft}[kind]
    if kind == "draft" and skip_if_pending:
        with connect() as db:
            if (n := db.pending_drafts()) >= skip_if_pending:  # scheduled runs don't pile up unreviewed posts
                run_id = db.start_run(AGENT, kind, git_sha())
                db.finish_run(run_id, "skipped", stats={"reason": f"{n} drafts already waiting for approval"})
                print(f"[{run_id}] skipped: {n} drafts already waiting for approval")
                return 0
    # a scheduled run may skip a day with no take worth posting; "Generate a new draft" and redrafts always write
    init = redraft_state(redraft) if redraft else {"can_skip": bool(skip_if_pending)}
    return runner.run_graph(AGENT, kind, builder(), init, lambda state: summarize(kind, state),
                            prompt_version=PROMPT_VERSION, scores=trace_scores)


def trace_scores(state: dict) -> dict:
    out = {f"critic.{name}": value for name, value in state.get("scores", {}).items()}
    if state.get("overall") is not None:
        out |= {"overall": state["overall"], "humanizer": state["accent"]["score"]}
    return out


def summarize(kind: str, state: dict) -> dict:
    if kind == "collect":
        scored = state.get("scored", [])
        return {"fetched": len(state.get("items", [])), "new": len(state.get("new", [])),
                "from_brief": len(state.get("pending", [])),
                "scored": sum(1 for i in scored if i.get("importance")), "feed_errors": len(state.get("feed_errors", []))}
    return {"draft_id": state.get("draft_id"), "week": state.get("week"), "revisions": state.get("revisions"),
            "chars": len(state.get("post", "")), "overall": state.get("overall"),
            "humanizer": (state.get("accent") or {}).get("score"), "scores": state.get("scores"), "violations": state.get("violations"),
            "learned": state.get("learned"), "redraft": bool(state.get("redraft_id")), "with_take": bool(state.get("take")),
            **({"skipped": state["skip"]} if state.get("skip") else {})}


def build_style_guide() -> int:
    examples = graph.style_examples(n=50, short_only=False)
    if len(examples) < 3:
        print("Paste at least 3 posts into config/style/examples.md (separated by ---) first.", file=sys.stderr)
        return 1
    limit = graph.SHORT_POST_WORDS
    labeled = [f"[SHORT POST]\n{e}" if len(e.split()) <= limit
               else f"[LONG-FORM ESSAY, first 400 words]\n{' '.join(e.split()[:400])}" for e in examples]
    with connect() as db:
        run_id = db.start_run(AGENT, "style_guide", git_sha())
        try:
            guide = llm.call(llm.RunContext(db=db, run_id=run_id), "writer", "style_guide",
                             graph.prompt("style_guide"), "\n\n---\n\n".join(labeled))
        except Exception as ex:
            db.finish_run(run_id, "failed", error=str(ex))
            raise
        db.finish_run(run_id, "succeeded", stats={"examples": len(examples)})
    (CONFIG / "style" / "guide.md").write_text(guide.strip() + "\n")
    print(f"Wrote config/style/guide.md from {len(examples)} examples. Review it before the next draft.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="agents.market_intel")
    ap.add_argument("command", choices=["collect", "draft", "style-guide"])
    ap.add_argument("--skip-if-pending", type=int, help="draft: do nothing if this many drafts are waiting for approval")
    ap.add_argument("--redraft", help="draft: rewrite this saved draft around the owner's take, in place")
    args = ap.parse_args()
    return build_style_guide() if args.command == "style-guide" else run(args.command, args.skip_if_pending, args.redraft)


if __name__ == "__main__":
    sys.exit(main())
