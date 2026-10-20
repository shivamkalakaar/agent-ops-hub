"""CLI: uv run python -m agents.market_intel {collect|draft|style-guide}"""
import argparse
import sys

from core import llm, tracing
from core.budget import BudgetExceeded
from core.config import CONFIG, git_sha
from core.db import connect
from agents.market_intel import graph

AGENT = "market_intel"
PROMPT_VERSION = "v1"


def run(kind: str) -> int:
    builder = {"collect": graph.build_collect, "draft": graph.build_draft}[kind]
    sha = git_sha()
    with connect() as db:
        run_id = db.start_run(AGENT, kind, sha)
        with tracing.trace(f"{AGENT}.{kind}", run_id=run_id, tags=[AGENT, kind, PROMPT_VERSION],
                           metadata={"git_sha": sha, "prompt_version": PROMPT_VERSION}) as t:
            ctx = llm.RunContext(db=db, run_id=run_id, callbacks=t.callbacks)
            try:
                state = builder().invoke({}, config={"configurable": {"ctx": ctx}, "callbacks": t.callbacks,
                                                     "run_name": f"{AGENT}.{kind}"})
            except BudgetExceeded as ex:
                db.finish_run(run_id, "budget_exceeded", trace_url=t.url, error=str(ex))
                print(f"[{run_id}] stopped: {ex}", file=sys.stderr)
                return 0  # a budget stop is the guard working, not a crash
            except Exception as ex:
                db.finish_run(run_id, "failed", trace_url=t.url, error=f"{type(ex).__name__}: {ex}")
                raise
            stats = summarize(kind, state)
            for name, value in state.get("scores", {}).items():
                t.score(f"critic.{name}", value)
            t.output(stats)
            db.finish_run(run_id, "succeeded", trace_url=t.url, stats=stats)
            print(f"[{run_id}] {kind} succeeded: {stats}" + (f"\ntrace: {t.url}" if t.url else ""))
    return 0


def summarize(kind: str, state: dict) -> dict:
    if kind == "collect":
        scored = state.get("scored", [])
        return {"fetched": len(state.get("items", [])), "new": len(state.get("new", [])),
                "from_brief": len(state.get("pending", [])),
                "scored": sum(1 for i in scored if i.get("importance")), "feed_errors": len(state.get("feed_errors", []))}
    return {"draft_id": state.get("draft_id"), "week": state.get("week"), "revisions": state.get("revisions"),
            "chars": len(state.get("post", "")), "scores": state.get("scores"), "violations": state.get("violations")}


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
    args = ap.parse_args()
    return build_style_guide() if args.command == "style-guide" else run(args.command)


if __name__ == "__main__":
    sys.exit(main())
