"""Prompt regression evals: run the draft workflow (no save) on golden weeks and score the results.

uv run python -m evals.run_evals [--cases 5] [--update-baseline]

Fails (exit 1) if the mean critic score drops more than TOLERANCE below the baseline,
or if any final post still breaks a deterministic guardrail.
"""
import argparse
import json
import sys
from pathlib import Path
from statistics import mean

from core import humanizer, llm, tracing
from core.budget import BudgetExceeded
from core.config import git_sha
from core.db import connect
from agents.market_intel import graph

HERE = Path(__file__).parent
BASELINE = HERE / "baseline.json"
TOLERANCE = 0.3


def run_case(ctx, name: str, items: list[dict]) -> dict:
    state = graph.build_draft(persist=False).invoke(
        {"items": items, "week": name, "recent_angles": []}, config={"configurable": {"ctx": ctx}, "callbacks": ctx.callbacks,
                                                "run_name": f"eval.{name}"})
    scores = state["scores"]
    return {"case": name, "mean": round(mean(scores.values()), 2) if scores else 0.0, "scores": scores,
            "violations": state["violations"], "revisions": state["revisions"], "chars": len(state["post"]),
            "accent": humanizer.scan(state["post"])["score"],
            "post": state["post"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=5, help="newest N golden weeks (cost scales linearly)")
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args()

    cases = sorted((HERE / "golden").glob("*.json"))[-args.cases:]
    results, sha = [], git_sha()
    with connect() as db:
        run_id = db.start_run("market_intel", "eval", sha)
        with tracing.trace("market_intel.eval", run_id=run_id, tags=["market_intel", "eval"],
                           metadata={"git_sha": sha}) as t:
            ctx = llm.RunContext(db=db, run_id=run_id, callbacks=t.callbacks)
            try:
                for path in cases:
                    r = run_case(ctx, path.stem, json.loads(path.read_text())["items"])
                    results.append(r)
                    print(f"{r['case']}: mean {r['mean']}  accent {r['accent']}  chars {r['chars']}  revisions {r['revisions']}"
                          f"{'  VIOLATIONS ' + str(r['violations']) if r['violations'] else ''}")
            except BudgetExceeded as ex:
                db.finish_run(run_id, "budget_exceeded", trace_url=t.url, error=str(ex))
                print(f"stopped: {ex}", file=sys.stderr)
                return 1
            overall = round(mean(r["mean"] for r in results), 2)
            summary = {"mean": overall, "cases": len(results),
                       "accent_mean": round(mean(r["accent"] for r in results), 1),
                       "guardrail_pass_rate": sum(not r["violations"] for r in results) / len(results),
                       "per_dimension": {k: round(mean(r["scores"].get(k, 0) for r in results), 2)
                                         for k in results[0]["scores"]}}
            t.score("eval.mean", overall)
            t.score("eval.accent_mean", summary["accent_mean"])
            db.finish_run(run_id, "succeeded", trace_url=t.url, stats=summary)

    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "latest.json").write_text(json.dumps({"summary": summary, "results": results}, indent=1))
    print(json.dumps(summary, indent=1))

    if args.update_baseline:
        BASELINE.write_text(json.dumps(summary, indent=1) + "\n")
        print("baseline updated")
        return 0
    if BASELINE.exists():
        base = json.loads(BASELINE.read_text())["mean"]
        if overall < base - TOLERANCE:
            print(f"REGRESSION: mean {overall} < baseline {base} - {TOLERANCE}", file=sys.stderr)
            return 1
    if summary["accent_mean"] > humanizer.MAX_ACCENT:
        print(f"REGRESSION: mean accent {summary['accent_mean']} > {humanizer.MAX_ACCENT}", file=sys.stderr)
        return 1
    if summary["guardrail_pass_rate"] < 1:
        print("REGRESSION: some final posts still violate guardrails", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
