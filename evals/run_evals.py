"""Prompt regression evals: run the draft workflow (no save) on golden weeks and score the results.

uv run python -m evals.run_evals [--agent market_intel|substack] [--cases 5] [--update-baseline] [--images]

Fails (exit 1) if the mean critic score drops more than TOLERANCE below the baseline,
or if any final post still breaks a deterministic guardrail. For the Substack newsroom it also checks
each run's trajectory: within the step limit, and the fact-checker and final check both ran.
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
SUBSTACK_BASELINE = HERE / "baseline.substack.json"
TOLERANCE = 0.3
# The newsroom saves its best version even when nothing passes, flagged for the owner, so one flagged post in a
# 3-case run isn't a regression; two are. The LinkedIn agent still needs every final post clean.
SUBSTACK_MIN_GUARDRAIL_PASS = 2 / 3
DAYS_PER_SUBSTACK_CASE = 3  # a Wed/Sat post covers 3-4 days of news


def run_case(ctx, name: str, items: list[dict]) -> dict:
    state = graph.build_draft(persist=False).invoke(
        {"items": items, "week": name, "recent_angles": []}, config={"configurable": {"ctx": ctx}, "callbacks": ctx.callbacks,
                                                "run_name": f"eval.{name}"})
    scores = state["scores"]
    return {"case": name, "mean": state["overall"], "critic_mean": round(mean(scores.values()), 2) if scores else 0.0,
            "scores": scores,
            "violations": state["violations"], "revisions": state["revisions"], "chars": len(state["post"]),
            "humanizer": humanizer.scan(state["post"])["score"],
            "post": state["post"]}


def substack_cases(n: int) -> list[tuple[str, list[dict]]]:
    """Newest n cases, each the union of DAYS_PER_SUBSTACK_CASE consecutive golden days (no overlap)."""
    days = sorted((HERE / "golden").glob("*.json"))
    cases = []
    while len(days) >= DAYS_PER_SUBSTACK_CASE and len(cases) < n:
        group, days = days[-DAYS_PER_SUBSTACK_CASE:], days[:-DAYS_PER_SUBSTACK_CASE]
        seen, items = set(), []
        for path in group:
            for i in json.loads(path.read_text())["items"]:
                if i["url"] not in seen:
                    seen.add(i["url"])
                    items.append(i)
        cases.append((f"{group[0].stem}..{group[-1].stem}", items))
    return cases


def run_substack_case(ctx, name: str, items: list[dict], images: bool) -> dict:
    from agents.substack import graph as newsroom
    from agents.substack.roster import MAX_STEPS, PROMPTS, SPECIALISTS
    from core import settings
    from dataclasses import replace
    cfg = settings.from_files(SPECIALISTS, PROMPTS)
    if not images:
        cfg["illustrator"] = replace(cfg["illustrator"], enabled=False)  # images cost ~$0.13/post; --images opts in
    state = newsroom.build().invoke(
        {"items": items, "musings": [], "recent_titles": [], "slot": name},
        config={"configurable": {"ctx": ctx, "settings": cfg, "persist": False}, "callbacks": ctx.callbacks,
                "run_name": f"eval.{name}", "recursion_limit": 60})
    judge, calls = state["judge"], state["calls"]
    trajectory = [p for ok, p in [(state["work_steps"] <= MAX_STEPS, f"{state['work_steps']} steps > {MAX_STEPS}"),
                                  (calls.get("fact_checker"), "fact_checker never ran"),
                                  (calls.get("final_check"), "final_check never ran")] if not ok]
    scores = {k: v for k, v in judge.items() if k in newsroom.editor.DIMENSIONS}
    return {"case": name, "mean": judge.get("overall") or 0.0, "scores": scores, "humanizer": judge.get("humanizer") or 1,
            "violations": judge.get("violations", []) + trajectory, "passed_final": state["final"]["passed"],
            "steps": state["work_steps"], "calls": calls, "title": state["final"]["title"], "post": state["final"]["body_md"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", choices=["market_intel", "substack"], default="market_intel")
    ap.add_argument("--cases", type=int, default=5, help="newest N golden weeks (cost scales linearly)")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--images", action="store_true", help="substack: keep the illustrator on (adds ~$0.13 per case)")
    args = ap.parse_args()
    if args.agent == "substack":
        return substack_main(args)

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
                    print(f"{r['case']}: mean {r['mean']}  humanizer {r['humanizer']}/5  chars {r['chars']}  revisions {r['revisions']}"
                          f"{'  VIOLATIONS ' + str(r['violations']) if r['violations'] else ''}")
            except BudgetExceeded as ex:
                db.finish_run(run_id, "budget_exceeded", trace_url=t.url, error=str(ex))
                print(f"stopped: {ex}", file=sys.stderr)
                return 1
            overall = round(mean(r["mean"] for r in results), 2)
            summary = {"mean": overall, "cases": len(results),
                       "humanizer_mean": round(mean(r["humanizer"] for r in results), 2),
                       "guardrail_pass_rate": sum(not r["violations"] for r in results) / len(results),
                       "per_dimension": {k: round(mean(r["scores"].get(k, 0) for r in results), 2)
                                         for k in results[0]["scores"]}}
            t.score("eval.mean", overall)
            t.score("eval.humanizer_mean", summary["humanizer_mean"])
            db.finish_run(run_id, "succeeded", trace_url=t.url, stats=summary)

    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "latest.json").write_text(json.dumps({"summary": summary, "results": results}, indent=1))
    print(json.dumps(summary, indent=1))
    return gate(summary, BASELINE, args.update_baseline)


def substack_main(args) -> int:
    results, sha = [], git_sha()
    with connect() as db:
        run_id = db.start_run("substack", "eval", sha)
        with tracing.trace("substack.eval", run_id=run_id, tags=["substack", "eval"], metadata={"git_sha": sha}) as t:
            ctx = llm.RunContext(db=db, run_id=run_id, callbacks=t.callbacks)
            try:
                for name, items in substack_cases(args.cases):
                    r = run_substack_case(ctx, name, items, args.images)
                    results.append(r)
                    print(f"{r['case']}: overall {r['mean']}  humanizer {r['humanizer']}/5  steps {r['steps']}  "
                          f"final {'PASS' if r['passed_final'] else 'FAIL'}  \"{r['title']}\""
                          f"{'  VIOLATIONS ' + str(r['violations']) if r['violations'] else ''}")
            except BudgetExceeded as ex:
                db.finish_run(run_id, "budget_exceeded", trace_url=t.url, error=str(ex))
                print(f"stopped: {ex}", file=sys.stderr)
                return 1
            if not results:
                print("no golden cases", file=sys.stderr)
                return 1
            summary = {"mean": round(mean(r["mean"] for r in results), 2), "cases": len(results),
                       "humanizer_mean": round(mean(r["humanizer"] for r in results), 2),
                       "guardrail_pass_rate": sum(not r["violations"] for r in results) / len(results),
                       "final_pass_rate": sum(r["passed_final"] for r in results) / len(results),
                       "mean_steps": round(mean(r["steps"] for r in results), 1),
                       "per_dimension": {k: round(mean(r["scores"].get(k, 0) for r in results), 2) for k in results[0]["scores"]}}
            t.score("eval.mean", summary["mean"])
            t.score("eval.humanizer_mean", summary["humanizer_mean"])
            db.finish_run(run_id, "succeeded", trace_url=t.url, stats=summary)
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "substack.json").write_text(json.dumps({"summary": summary, "results": results}, indent=1))
    print(json.dumps(summary, indent=1))
    return gate(summary, SUBSTACK_BASELINE, args.update_baseline, min_guardrail_pass=SUBSTACK_MIN_GUARDRAIL_PASS)


def gate(summary: dict, baseline: Path, update: bool, min_guardrail_pass: float = 1.0) -> int:
    if update:
        baseline.write_text(json.dumps(summary, indent=1) + "\n")
        print("baseline updated")
        return 0
    if baseline.exists():
        base = json.loads(baseline.read_text())["mean"]
        if summary["mean"] < base - TOLERANCE:
            print(f"REGRESSION: mean {summary['mean']} < baseline {base} - {TOLERANCE}", file=sys.stderr)
            return 1
    if summary["humanizer_mean"] < humanizer.MIN_HUMAN:
        print(f"REGRESSION: mean humanizer {summary['humanizer_mean']}/5 < {humanizer.MIN_HUMAN}", file=sys.stderr)
        return 1
    if summary["guardrail_pass_rate"] < min_guardrail_pass - 1e-9:
        print(f"REGRESSION: guardrail pass rate {summary['guardrail_pass_rate']:.2f} < {min_guardrail_pass:.2f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
