"""Prompt regression evals: run the draft workflow (no save) on golden weeks and score the results.

uv run python -m evals.run_evals [--agent market_intel|substack] [--cases 5] [--update-baseline] [--images]

Fails (exit 1) if the mean critic score drops more than TOLERANCE below the baseline,
or if any final post still breaks a deterministic guardrail. For the Substack agent it also checks
each run's trajectory: within the step limit, and the fact-checker and final check both ran.

Each eval is one `runs` row (kind 'eval') with one step per case, so the dashboard's /evals page and the run's
timeline show it live. The row's stats carry the trigger (pr / main / local), PR number, commit, baseline, gate
result and every case's post and scores. evals/results/<agent>.md is a summary for the PR comment.

Evals never learn from the queue and read a frozen set of lessons (evals/lessons.fixture.md), so the same
code and prompts always get the same inputs.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from statistics import mean
from typing import Callable

from core import drafting, humanizer, llm, tracing
from core.budget import BudgetExceeded
from core.config import git_sha
from core.db import connect
from core.steps import step
from agents.market_intel import graph

HERE = Path(__file__).parent
BASELINES = {"market_intel": HERE / "baseline.json", "substack": HERE / "baseline.substack.json"}
LESSONS = (HERE / "lessons.fixture.md").read_text().strip()
TOLERANCE = 0.3
# The Substack agent saves its best version even when nothing passes, flagged for the owner, so one flagged post in
# a 3-case run isn't a regression; two are. The LinkedIn agent still needs every final post clean.
MIN_GUARDRAIL_PASS = {"market_intel": 1.0, "substack": 2 / 3}
DAYS_PER_SUBSTACK_CASE = 3  # a Wed/Sat post covers 3-4 days of news
REJECTION_CASES = 3  # the owner's latest rejected LinkedIn drafts (with a reason) are rerun as "don't do this again" cases


def rejections(db, n: int = REJECTION_CASES) -> list[dict]:
    """Rejected LinkedIn drafts that came with a reason, newest first, with their stories as eval items."""
    out = []
    for d in db.all("select * from drafts where status='rejected' and feedback is not null order by created_at desc limit 20"):
        fb = json.loads(d["feedback"]) if isinstance(d["feedback"], str) else d["feedback"]
        sources = json.loads(d["sources"]) if isinstance(d["sources"], str) else d["sources"]
        if not (fb or {}).get("reasons") or not sources:
            continue
        items = [db.item(s["url"]) or {**s, "summary": "", "importance": 3} for s in sources]
        out.append({"id": d["id"], "reasons": fb["reasons"], "note": fb.get("note", ""), "post": d["post_md"], "items": items})
        if len(out) == n:
            break
    return out


def run_rejection_case(ctx, case: dict) -> dict:
    """Rerun the agent on a rejected draft's stories; a judge checks the new post doesn't repeat what was rejected."""
    r = run_case(ctx, f"rejected {case['id']}", case["items"])
    verdict = llm.call_json(ctx, "judge", "rejection_check",
                            drafting.fill((HERE / "rejection_check.md").read_text(), AUTHOR=graph.AUTHOR),
                            f"# Why {graph.AUTHOR} rejected it\n{', '.join(case['reasons'])}" + (f". {case['note']}" if case["note"] else "")
                            + f"\n\n# The rejected draft\n{case['post']}\n\n# The new draft\n{r['post']}")
    if verdict.get("repeats"):
        r["violations"] = r["violations"] + [f"repeats what was rejected ({', '.join(case['reasons'])}): {str(verdict.get('why', ''))[:160]}"]
    r["rejected"] = {"id": case["id"], "reasons": case["reasons"], "repeats": bool(verdict.get("repeats"))}
    return r


def run_case(ctx, name: str, items: list[dict]) -> dict:
    from agents.market_intel.roster import PROMPTS, SPECIALISTS
    from core import settings
    state = graph.build_draft(persist=False).invoke(
        {"items": items, "week": name, "recent_angles": [], "recent_posts": [], "top_posts": [], "learn": False, "lessons": LESSONS},
        config={"configurable": {"ctx": ctx, "settings": settings.from_files(SPECIALISTS, PROMPTS)}, "callbacks": ctx.callbacks,
                "run_name": f"eval.{name}", "recursion_limit": 60})
    scores = state["scores"]
    return {"case": name, "mean": state["overall"], "critic_mean": round(mean(scores.values()), 2) if scores else 0.0,
            "scores": scores, "violations": state["violations"], "revisions": state["revisions"], "chars": len(state["post"]),
            "humanizer": humanizer.scan(state["post"])["score"], "post": state["post"], "passed_final": state["passed"],
            "steps": state["work_steps"], "calls": state["calls"]}


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
        {"items": items, "musings": [], "recent_titles": [], "slot": name, "learn": False, "lessons": LESSONS},
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


def meta() -> dict:
    """Where this eval ran: set by CI (EVAL_TRIGGER, PR_NUMBER) and GitHub's own env vars."""
    return {"trigger": os.environ.get("EVAL_TRIGGER") or "local", "pr": os.environ.get("PR_NUMBER") or None,
            "branch": os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME") or None,
            "commit": (os.environ.get("GITHUB_SHA") or git_sha() or "")[:12] or None,
            "ci_url": (f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                       if os.environ.get("GITHUB_RUN_ID") else None)}


def summarize(agent: str, results: list[dict]) -> dict:
    """Scores over every case; the guardrail gate over the golden days only. Rejection cases (a judge's call on
    whether a rejected problem came back) are reported, not gated, so one strict verdict can't block every merge."""
    golden = [r for r in results if "rejected" not in r] or results
    s = {"mean": round(mean(r["mean"] for r in results), 2), "cases": len(results),
         "humanizer_mean": round(mean(r["humanizer"] for r in results), 2),
         "guardrail_pass_rate": round(sum(not r["violations"] for r in golden) / len(golden), 3), "golden_cases": len(golden),
         "per_dimension": {k: round(mean(r["scores"].get(k, 0) for r in results), 2) for k in results[0]["scores"]}}
    if rej := [r for r in results if "rejected" in r]:
        s["rejection_cases"], s["rejection_repeats"] = len(rej), sum(r["rejected"]["repeats"] for r in rej)
    if agent == "substack":
        s |= {"final_pass_rate": round(sum(r["passed_final"] for r in results) / len(results), 3),
              "mean_steps": round(mean(r["steps"] for r in results), 1)}
    return s


def check(summary: dict, baseline: Path, min_guardrail_pass: float = 1.0) -> tuple[list[str], float | None]:
    """Reasons the gate fails (empty = pass), and the baseline mean it compared against."""
    base = json.loads(baseline.read_text())["mean"] if baseline.exists() else None
    reasons = []
    if base is not None and summary["mean"] < base - TOLERANCE:
        reasons.append(f"mean {summary['mean']} is below baseline {base} - {TOLERANCE}")
    if summary["humanizer_mean"] < humanizer.MIN_HUMAN:
        reasons.append(f"mean humanizer {summary['humanizer_mean']}/5 is below {humanizer.MIN_HUMAN}")
    if summary["guardrail_pass_rate"] < min_guardrail_pass - 1e-9:
        reasons.append(f"guardrail pass rate {summary['guardrail_pass_rate']:.2f} is below {min_guardrail_pass:.2f}")
    return reasons, base


def gate(summary: dict, baseline: Path, update: bool, min_guardrail_pass: float = 1.0) -> int:
    if update:
        baseline.write_text(json.dumps(summary, indent=1) + "\n")
        print("baseline updated")
        return 0
    reasons, _ = check(summary, baseline, min_guardrail_pass)
    for r in reasons:
        print(f"REGRESSION: {r}", file=sys.stderr)
    return 1 if reasons else 0


def comment(agent: str, run_id: str, summary: dict, results: list[dict], reasons: list[str], base: float | None) -> str:
    """Markdown for the PR comment and the Actions job summary."""
    name = {"market_intel": "LinkedIn agent", "substack": "Substack agent"}[agent]
    head = f"### Evals: {name} {'passed' if not reasons else 'failed'}"
    line = (f"Mean **{summary['mean']}**" + (f" (baseline {base}, allowed drop {TOLERANCE})" if base is not None else "")
            + f" · humanizer {summary['humanizer_mean']}/5 · guardrails clean on "
            f"{round(summary['guardrail_pass_rate'] * summary.get('golden_cases', summary['cases']))}/{summary.get('golden_cases', summary['cases'])} news days"
            + (f" · repeats a rejected problem: {summary['rejection_repeats']} of {summary['rejection_cases']} (reported, not gated)"
               if summary.get("rejection_cases") else ""))
    rows = "\n".join(f"| {r['case']} | {r['mean']} | {r['humanizer']}/5 | {'; '.join(r['violations'])[:160] or '–'} |" for r in results)
    url = os.environ.get("DASHBOARD_URL", "").rstrip("/")
    link = f"\n\n[Posts and scores for each case]({url}/runs/{run_id})" if url else f"\n\nDashboard run `{run_id}`"
    why = "\n\n" + "\n".join(f"- {r}" for r in reasons) if reasons else ""
    return f"{head}\n\n{line}{why}\n\n| Case | Overall | Humanizer | Flags |\n|---|---|---|---|\n{rows}{link}\n"


def run_suite(agent: str, cases: list[tuple[str, Callable]], describe: Callable[[dict], str], update: bool) -> int:
    """Run each case as a step of one eval run, then record the summary and gate result on the run."""
    results, info = [], meta()
    baseline = BASELINES[agent] if agent in BASELINES else HERE / f"baseline.{agent}.json"
    with connect() as db:
        run_id = db.start_run(agent, "eval", info["commit"])
        with tracing.trace(f"{agent}.eval", run_id=run_id, tags=[agent, "eval", info["trigger"]], metadata=info) as t:
            ctx = llm.RunContext(db=db, run_id=run_id, callbacks=t.callbacks)
            first = 900  # case steps sit after the agents' own steps on the timeline (each case logs its own from seq 1)
            try:
                for n, (name, fn) in enumerate(cases):
                    with step(ctx, agent, "eval_case", "work", first + n, reason=f"case {n + 1} of {len(cases)}: {name}") as s:
                        r = fn(ctx)
                        results.append(r)
                        s.summary = describe(r)
                        s.status = "degraded" if r["violations"] else "ok"
                        s.output = {"case": name, "mean": r["mean"], "humanizer": r["humanizer"], "violations": r["violations"]}
                    print(describe(r))
            except BudgetExceeded as ex:
                db.finish_run(run_id, "budget_exceeded", trace_url=t.url, error=str(ex), stats={**info, "results": results})
                print(f"stopped: {ex}", file=sys.stderr)
                return 1
            if not results:
                db.finish_run(run_id, "failed", trace_url=t.url, error="no golden cases", stats=info)
                print("no golden cases", file=sys.stderr)
                return 1
            summary = summarize(agent, results)
            reasons, base = check(summary, baseline, MIN_GUARDRAIL_PASS[agent])
            t.score("eval.mean", summary["mean"])
            t.score("eval.humanizer_mean", summary["humanizer_mean"])
            db.finish_run(run_id, "succeeded" if not reasons else "failed", trace_url=t.url,
                          error="; ".join(reasons) or None,
                          stats={**summary, **info, "baseline": base, "passed": not reasons, "gate": reasons, "results": results})

    results_dir = HERE / "results"
    results_dir.mkdir(exist_ok=True)
    (results_dir / f"{agent}.json").write_text(json.dumps({"summary": summary, **info, "results": results}, indent=1))
    (results_dir / f"{agent}.md").write_text(comment(agent, run_id, summary, results, reasons, base))
    print(json.dumps(summary, indent=1))
    return gate(summary, baseline, update, MIN_GUARDRAIL_PASS[agent])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", choices=["market_intel", "substack"], default="market_intel")
    ap.add_argument("--cases", type=int, default=5, help="newest N golden weeks (cost scales linearly)")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--images", action="store_true", help="substack: keep the illustrator on (adds ~$0.13 per case)")
    args = ap.parse_args()
    if args.agent == "substack":
        cases = [(name, lambda ctx, n=name, i=items: run_substack_case(ctx, n, i, args.images))
                 for name, items in substack_cases(args.cases)]
        describe = lambda r: (f"{r['case']}: overall {r['mean']}, humanizer {r['humanizer']}/5, {r['steps']} steps, "  # noqa: E731
                              f"final check {'passed' if r['passed_final'] else 'failed'}: \"{r['title']}\""
                              + (f". Flags: {'; '.join(r['violations'])}" if r["violations"] else ""))
    else:
        cases = [(p.stem, lambda ctx, p=p: run_case(ctx, p.stem, json.loads(p.read_text())["items"]))
                 for p in sorted((HERE / "golden").glob("*.json"))[-args.cases:]]
        with connect() as db:
            cases += [(f"rejected {c['id']}", lambda ctx, c=c: run_rejection_case(ctx, c)) for c in rejections(db)]
        describe = lambda r: (f"{r['case']}: overall {r['mean']}, humanizer {r['humanizer']}/5, {r['chars']} chars, "  # noqa: E731
                              f"{r['revisions']} rewrites" + (f". Flags: {'; '.join(r['violations'])}" if r["violations"] else ""))
    return run_suite(args.agent, cases, describe, args.update_baseline)


if __name__ == "__main__":
    sys.exit(main())
