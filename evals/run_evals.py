"""Prompt regression evals: run the draft workflow (no save) on golden weeks and score the results.

uv run python -m evals.run_evals [--agent market_intel|substack] [--cases 5] [--workers 4] [--update-baseline] [--images]

Fails (exit 1) if the mean critic score drops more than TOLERANCE below the baseline,
or if any final post still breaks a deterministic guardrail. For the Substack agent it also checks
each run's trajectory: within the step limit, and the fact-checker and final check both ran.

Each eval is one `runs` row (kind 'eval') with one step per case, so the dashboard's /evals page and the run's
timeline show it live. Cases run concurrently (--workers), each case's own steps in its own block of seq numbers. The row's stats carry the trigger (pr / main / local), PR number, commit, baseline, gate
result and every case's post and scores. evals/results/<agent>.md is a summary for the PR comment.

Evals never learn from the queue and read a frozen set of lessons (evals/lessons.fixture.md), so the same
code and prompts always get the same inputs.
"""
import argparse
import contextvars
import json
import os
import sys
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import replace
from functools import partial
from pathlib import Path
from statistics import mean
from typing import Callable

from core import drafting, humanizer, lessons, llm, tracing
from core.budget import BudgetExceeded
from core.config import git_sha
from core.db import connect
from core.steps import step
from agents.market_intel import graph
from evals import reference as ref

HERE = Path(__file__).parent
BASELINES = {"market_intel": HERE / "baseline.json", "substack": HERE / "baseline.substack.json"}
LESSONS = (HERE / "lessons.fixture.md").read_text().strip()
# The owner's rewrites, frozen like the lessons. A reference case never sees the pair built from its own post.
EXAMPLES = json.loads((HERE.parent / "agents" / "market_intel" / "voice_examples.json").read_text())
TOLERANCE = 0.3
# The Substack agent saves its best version even when nothing passes, flagged for the owner, so one flagged post in
# the 2-case run isn't a regression; two are. LinkedIn is gated by the fixed judge instead (see check()).
MIN_GUARDRAIL_PASS = {"market_intel": 1.0, "substack": 1 / 2}
DAYS_PER_SUBSTACK_CASE = 3  # a Wed/Sat post covers 3-4 days of news
REJECTION_CASES = 1  # the owner's latest rejected LinkedIn draft (with a reason) is rerun as a "don't do this again" case
# PR set: the agent writes on the stories behind three of Shivam's published posts, and the fixed judge (evals/reference.py)
# compares it with his. --full adds the golden news days and the rest of the reference cases.
PR_REFERENCES = ("3fcc2862c829", "942e36b0112d", "977b5049c201")
# Quoted in the voice guide as the bar for a 5, so the agent has already seen it: never a case.
IN_PROMPTS = ("b9829b5dac61",)
WORKERS = 4  # cases at once: ~4x faster; more starts to hit the free models' rate limits (then their paid fallbacks)
CASE_SEQ = 100  # each case's own steps get a block of seq numbers (case n: 100n+1...), so they don't interleave


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
    return judged(ctx, r, case["items"])


def judged(ctx, r: dict, items: list[dict], reference: str | None = None) -> dict:
    """The fixed judge's score, and with Shivam's post for the same stories, which of the two it prefers."""
    r["judge"] = ref.judge(ctx, r["post"], items)
    r["compare"] = ref.compare(ctx, r["post"], reference, items) if reference else None
    if reference:  # how much of the draft Shivam would have kept: 1 - the share his own post changes
        r["kept"] = round(1 - lessons.edit_share(r["post"], reference), 2)
    return r


def run_reference_case(ctx, case: dict) -> dict:
    return judged(ctx, run_case(ctx, f"ref {case['id']}", case["items"], exclude=case["id"]), case["items"], case["reference"])


def frozen_articles(cases: list[dict]):
    """Reference cases read the article text frozen with them; everything else is fetched live as before."""
    bodies, live = {i["url"]: i["body"] for c in cases for i in c["items"] if i.get("body")}, graph.fetch_article
    graph.fetch_article = lambda url, *a, **k: bodies.get(url) or live(url, *a, **k)


def run_case(ctx, name: str, items: list[dict], exclude: str | None = None) -> dict:
    from agents.market_intel.roster import PROMPTS, SPECIALISTS
    from core import settings
    cfg = settings.from_files(SPECIALISTS, PROMPTS)
    cfg["onepager"] = replace(cfg["onepager"], enabled=False)  # never scored; it only runs once the post is final
    state = graph.build_draft(persist=False).invoke(
        {"items": items, "week": name, "recent_angles": [], "recent_posts": [], "top_posts": [], "learn": False, "lessons": LESSONS,
         "examples": [e for e in EXAMPLES if e["id"] != exclude]},
        config={"configurable": {"ctx": ctx, "settings": cfg}, "callbacks": ctx.callbacks,
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
    if judged_rows := [{"case": r["case"], "judge": r["judge"], "compare": r.get("compare"), "kept": r.get("kept")}
                       for r in results if r.get("judge")]:
        s["judge"] = ref.summarize(judged_rows)
    if agent == "substack":
        s |= {"final_pass_rate": round(sum(r["passed_final"] for r in results) / len(results), 3),
              "mean_steps": round(mean(r["steps"] for r in results), 1)}
    return s


def check(summary: dict, baseline: Path, min_guardrail_pass: float = 1.0) -> tuple[list[str], float | None]:
    """Reasons the gate fails (empty = pass), and the baseline mean it compared against. When the fixed judge ran
    (LinkedIn), it decides: the agent's own editor score and its guardrail rate move with every prompt change, so
    they're reported, not gated."""
    saved = json.loads(baseline.read_text()) if baseline.exists() else {}
    base = saved.get("mean")
    if "judge" in summary:
        reasons = ref.gate(summary["judge"], saved.get("judge"))
        if summary["humanizer_mean"] < humanizer.MIN_HUMAN:
            reasons.append(f"mean humanizer {summary['humanizer_mean']}/5 is below {humanizer.MIN_HUMAN}")
        return reasons, base
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
    if j := summary.get("judge"):
        line = (f"Judge **{j['judge_mean']}**/5" + (f" · preferred over Shivam's post {j['vs_shivam']:.2f} of the time "
                f"({j['results']['win']} win, {j['results']['tie']} tie, {j['results']['loss']} loss; 0.50 = as good as Shivam)" if "vs_shivam" in j else "")
                + (f" · Shivam would keep **{j['kept']:.0%}** of the draft" if "kept" in j else "")
                + f" · {len(j['flagged'])} case(s) break a hard rule · agent's own editor {summary['mean']} (reported, not gated)"
                + (f" · repeats a rejected problem: {summary['rejection_repeats']} of {summary['rejection_cases']}" if summary.get("rejection_cases") else ""))
        vs = lambda r: (r.get("compare") or {}).get("result", "–")  # noqa: E731
        kept = lambda r: f"{r['kept']:.0%}" if r.get("kept") is not None else "–"  # noqa: E731
        rows = "\n".join(f"| {r['case']} | {r['judge']['overall']} | {vs(r)} | {kept(r)} | {r['mean']} | "
                         f"{'; '.join(r['judge']['flags'] + r['violations'])[:160] or '–'} |" for r in results)
        url = os.environ.get("DASHBOARD_URL", "").rstrip("/")
        link = f"\n\n[Posts and scores for each case]({url}/runs/{run_id})" if url else f"\n\nDashboard run `{run_id}`"
        why = "\n\n" + "\n".join(f"- {r}" for r in reasons) if reasons else ""
        return (f"{head}\n\n{line}{why}\n\n| Case | Judge | vs Shivam | Kept | Editor | Flags |\n|---|---|---|---|---|---|\n{rows}{link}\n")
    rows = "\n".join(f"| {r['case']} | {r['mean']} | {r['humanizer']}/5 | {'; '.join(r['violations'])[:160] or '–'} |" for r in results)
    url = os.environ.get("DASHBOARD_URL", "").rstrip("/")
    link = f"\n\n[Posts and scores for each case]({url}/runs/{run_id})" if url else f"\n\nDashboard run `{run_id}`"
    why = "\n\n" + "\n".join(f"- {r}" for r in reasons) if reasons else ""
    return f"{head}\n\n{line}{why}\n\n| Case | Overall | Humanizer | Flags |\n|---|---|---|---|\n{rows}{link}\n"


def concurrently(fns: list[Callable], workers: int):
    """Run fns on a thread pool, each in a copy of the caller's context (so Langfuse spans nest under the eval's trace).
    The first failure cancels what hasn't started and is raised once the running ones finish."""
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(contextvars.copy_context().run, fn) for fn in fns]
        wait(futures, return_when=FIRST_EXCEPTION)
        for f in futures:
            f.cancel()
    if err := next((f.exception() for f in futures if not f.cancelled() and f.exception()), None):
        raise err


def run_suite(agent: str, cases: list[tuple[str, Callable]], describe: Callable[[dict], str], update: bool,
              workers: int = WORKERS) -> int:
    """Run the cases (concurrently) as steps of one eval run, then record the summary and gate result on the run."""
    done, info = {}, meta()
    baseline = BASELINES[agent] if agent in BASELINES else HERE / f"baseline.{agent}.json"
    with connect() as db:
        run_id = db.start_run(agent, "eval", info["commit"])
        with tracing.trace(f"{agent}.eval", run_id=run_id, tags=[agent, "eval", info["trigger"]], metadata=info) as t:
            first = max(900, CASE_SEQ * len(cases))  # case steps sit after the agents' own steps on the timeline

            def run_one(n: int, name: str, fn: Callable):
                ctx = llm.RunContext(db=db, run_id=run_id, callbacks=t.callbacks, seq_offset=CASE_SEQ * n)  # its own cost meter
                with step(replace(ctx, seq_offset=0), agent, "eval_case", "work", first + n,
                          reason=f"case {n + 1} of {len(cases)}: {name}") as s:
                    done[n] = r = fn(ctx)
                    s.summary = describe(r)
                    s.status = "degraded" if r["violations"] else "ok"
                    s.output = {"case": name, "mean": r["mean"], "humanizer": r["humanizer"], "violations": r["violations"]}
                print(describe(r), flush=True)
            try:
                concurrently([partial(run_one, n, name, fn) for n, (name, fn) in enumerate(cases)], workers)
            except BudgetExceeded as ex:
                results = [done[n] for n in sorted(done)]
                db.finish_run(run_id, "budget_exceeded", trace_url=t.url, error=str(ex), stats={**info, "results": results})
                print(f"stopped: {ex}", file=sys.stderr)
                return 1
            results = [done[n] for n in sorted(done)]
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
    ap.add_argument("--cases", type=int, default=None, help="newest N golden days (LinkedIn: only with --full; Substack default 2)")
    ap.add_argument("--full", action="store_true", help="LinkedIn: also the golden days and every reference case")
    ap.add_argument("--workers", type=int, default=WORKERS, help="cases run at once")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--images", action="store_true", help="substack: keep the illustrator on (adds ~$0.13 per case)")
    args = ap.parse_args()
    if args.agent == "substack":
        cases = [(name, lambda ctx, n=name, i=items: run_substack_case(ctx, n, i, args.images))
                 for name, items in substack_cases(args.cases or 2)]
        describe = lambda r: (f"{r['case']}: overall {r['mean']}, humanizer {r['humanizer']}/5, {r['steps']} steps, "  # noqa: E731
                              f"final check {'passed' if r['passed_final'] else 'failed'}: \"{r['title']}\""
                              + (f". Flags: {'; '.join(r['violations'])}" if r["violations"] else ""))
    else:
        refs = [c for c in ref.reference_cases() if (args.full or c["id"] in PR_REFERENCES) and c["id"] not in IN_PROMPTS]
        frozen_articles(refs)
        cases = [(f"ref {c['id']}", lambda ctx, c=c: run_reference_case(ctx, c)) for c in refs]
        if args.full:
            cases += [(p.stem, lambda ctx, p=p: judged(ctx, run_case(ctx, p.stem, json.loads(p.read_text())["items"]), json.loads(p.read_text())["items"]))
                      for p in sorted((HERE / "golden").glob("*.json"))[-(args.cases or 5):]]
        with connect() as db:
            cases += [(f"rejected {c['id']}", lambda ctx, c=c: run_rejection_case(ctx, c)) for c in rejections(db)]
        describe = lambda r: (f"{r['case']}: overall {r['mean']}, humanizer {r['humanizer']}/5, {r['chars']} chars, "  # noqa: E731
                              f"{r['revisions']} rewrites" + (f". Flags: {'; '.join(r['violations'])}" if r["violations"] else ""))
    return run_suite(args.agent, cases, describe, args.update_baseline, args.workers)


if __name__ == "__main__":
    sys.exit(main())
