"""Reviewer evals: run only the reviewers (no scout, researcher, writer or coordinator) on a frozen, labelled set of
posts, in parallel, and check each one catches what it should and leaves Shivam's own posts alone.

uv run python -m evals.run_reviewer_evals [--agent market_intel|substack] [--workers 8]

Cases live in evals/reviewers/<agent>.json: a post, the sources it was written from, and what each reviewer should
say (pass, flag, or words the flags must mention). The deterministic checks (guardrails, humanizer, copy check,
novelty) run on every case for free; an LLM reviewer runs on a case only when the case says what it should do.

Fails (exit 1) if a reviewer misses a planted defect it is responsible for (or a case marked gate), errors out, or
flags more than MAX_FALSE_ALARMS of Shivam's own posts. Other bad cases are reported, not gated.
Each run is one `runs` row (kind 'reviewer_eval'); evals/results/reviewers[.<agent>].md is the PR comment.
"""
import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core import llm, settings
from core.budget import BudgetExceeded
from core.db import DB, connect
from evals.run_evals import LESSONS, meta

HERE = Path(__file__).parent
MAX_FALSE_ALARMS = 0.25  # share of Shivam's own posts a reviewer may flag: 1 of 5 is noise, 2 of 5 is a broken reviewer


# --------------------------------------------------------------------------- agents

def _li_state(case: dict, src: dict) -> dict:
    return {"drafts": [{"version": 1, "post": case["post"]}], "sources": src["items"], "facts": src.get("facts", []),
            "context": src.get("context", []), "lessons": LESSONS, "recent_posts": case.get("recent", []), "top_posts": []}


def _li_guardrails(state) -> list[str]:
    from agents.market_intel import graph, specialists
    return graph.guardrail_violations(state["drafts"][-1]["post"], {s["url"] for s in state["sources"]},
                                      specialists.sources_text(state), state["recent_posts"])


def _li_flags(name: str, state, out: dict) -> tuple[list[str], list[str]]:
    """(flags, notes) from a reviewer's report: flags are what would send the draft back."""
    from agents.market_intel import specialists
    if name == "editor":  # weak scores send the draft back with its fixes; the fixes alone are notes
        r = out["edit_report"]
        fixes = [f"{x['kind']}: {x['quote']} ({x['problem']})" for x in r.get("fixes") or []]
        return ([f"weak scores {json.dumps(r['scores'])}, overall {r['overall']}"] + fixes if specialists.weak(r) else []) + \
            [f"unsupported: {c}" for c in r["unsupported"]], [] if specialists.weak(r) else fixes
    if name == "fact_checker":
        cl = out["fact_report"]["claims"]
        return [f"unsupported: {c['claim']}" for c in cl if c["verdict"] == "unsupported"], \
            [f"hedge: {c['claim']}" for c in cl if c["verdict"] == "hedge"]
    if name == "final_check":  # its own read only: the guardrails are scored separately and no upstream reports are passed
        r, own = out["final_report"], set(_li_guardrails(state))
        return [b for b in r["blockers"] if b not in own], r["polish"]
    raise KeyError(name)


def market_intel() -> dict:
    from agents.market_intel import specialists
    from agents.market_intel.roster import PROMPTS, SPECIALISTS
    return {"name": "LinkedIn agent", "state": _li_state, "guardrails": _li_guardrails, "flags": _li_flags,
            "runners": {n: specialists.RUNNERS[n] for n in ("editor", "fact_checker", "final_check")},
            "settings": settings.from_files(SPECIALISTS, PROMPTS)}


def _ss_state(case: dict, src: dict) -> dict:
    return {"drafts": [{"version": 1, "md": case["post"]}], "sources": src["items"], "brief": src.get("brief") or {},
            "seed": src.get("seed"), "lessons": LESSONS, "images": [], "candidates": {}}


def _ss_guardrails(state) -> list[str]:
    from agents.substack import checks
    from agents.substack.specialists import sources_text
    return checks.violations(state["drafts"][-1]["md"], {s["url"] for s in state["sources"]}, sources_text(state))


def _ss_flags(name: str, state, out: dict) -> tuple[list[str], list[str]]:
    from agents.substack.specialists import editor
    if name == "editor":
        r = out["edit_report"]
        return [f"weak scores {json.dumps(r['scores'])}, overall {r['overall']}"] if editor.weak(r) else [], [str(r["critique"])[:200]]
    if name == "fact_checker":
        r = out["fact_report"]
        return [f"unsupported: {c.get('claim', '')}" for c in r["claims"] if c["verdict"] == "unsupported"] + \
            [f"invented: {i}" for i in r["invented"]], [f"hedge: {c.get('claim', '')}" for c in r["claims"] if c["verdict"] == "hedge"]
    if name == "final_check":  # its own read only, without the automated results it was handed
        r = out["final_report"]
        return [b for b in r["blockers"] if b not in r["automated"]], r["polish"]
    raise KeyError(name)


def substack() -> dict:
    from agents.substack.roster import PROMPTS, SPECIALISTS
    from agents.substack.specialists import editor, fact_checker, final_check
    return {"name": "Substack agent", "state": _ss_state, "guardrails": _ss_guardrails, "flags": _ss_flags,
            "runners": {"editor": editor.run, "fact_checker": fact_checker.run, "final_check": final_check.run},
            "settings": settings.from_files(SPECIALISTS, PROMPTS)}


AGENTS = {"market_intel": market_intel, "substack": substack}


# --------------------------------------------------------------------------- running

def verdict(expected, flags: list[str], accept: list[str]) -> dict:
    """Score one reviewer on one case. Accepted flags (known and fine) are dropped first."""
    kept = [f for f in flags if not any(a.lower() in f.lower() for a in accept)]
    out = {"flags": kept, "accepted": [f for f in flags if f not in kept], "expected": expected}
    if expected == "pass":
        out["ok"] = not kept
    elif expected:
        want = [] if expected == "flag" else expected
        out["missing"] = [w for w in want if not any(w.lower() in f.lower() for f in kept)]
        out["ok"] = bool(kept) and not out["missing"]
    return out


def run_cases(db, run_id: str, spec: dict, fixture: dict, workers: int = 16) -> list[dict]:
    """Deterministic checks inline, then every (case, LLM reviewer) pair on a thread pool. Postgres shares the one
    connection (psycopg serializes it; the Supabase pooler caps sessions at 15); SQLite gets one per thread."""
    local, conns, lock = threading.local(), [], threading.Lock()

    def ctx() -> llm.RunContext:
        if db.pg:
            return llm.RunContext(db=db, run_id=run_id)
        if not hasattr(local, "db"):
            local.db = DB()
            with lock:
                conns.append(local.db)
        return llm.RunContext(db=local.db, run_id=run_id)

    results, jobs = [], []
    for case in fixture["cases"]:
        state = spec["state"](case, fixture["sources"][case["sources"]])
        exp, accept = case.get("expect", {}), case.get("accept", [])
        r = {k: case.get(k) for k in ("id", "label", "kind", "gate")} | {"reviewers": {}}
        r["reviewers"]["guardrails"] = verdict(exp.get("guardrails"), spec["guardrails"](state), accept)
        jobs += [(r, name, state, exp[name], accept) for name in spec["runners"] if exp.get(name)]
        results.append(r)

    def work(job):
        r, name, state, expected, accept = job
        start = time.monotonic()
        try:
            out, _, _ = spec["runners"][name](state, ctx(), spec["settings"][name])
            flags, notes = spec["flags"](name, state, out)
            v = verdict(expected, flags, accept) | {"notes": [n for n in notes if n]}
        except BudgetExceeded:
            raise
        except Exception as ex:  # a reviewer that crashes counts against it, it doesn't stop the run
            v = {"flags": [], "expected": expected, "ok": False, "error": f"{type(ex).__name__}: {str(ex)[:200]}"}
        r["reviewers"][name] = v | {"seconds": round(time.monotonic() - start, 1)}
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(work, jobs))
    finally:
        for c in conns:
            c.conn.close()
    order = ["guardrails", *spec["runners"]]  # threads finish in any order; reports list reviewers the same way every run
    return [r | {"reviewers": {n: r["reviewers"][n] for n in order if n in r["reviewers"]}} for r in results]


# --------------------------------------------------------------------------- scoring

def summarize(results: list[dict], costs: dict | None = None) -> dict:
    """Per reviewer: caught/missed on bad and planted cases, false alarms on Shivam's own posts."""
    per = {}
    for r in results:
        for name, v in r["reviewers"].items():
            if not v.get("expected"):
                continue
            s = per.setdefault(name, {"caught": 0, "missed": 0, "own": 0, "false_alarms": 0, "errors": 0, "missed_cases": [],
                                      "false_alarm_cases": []})
            if v.get("error"):
                s["errors"] += 1
            elif v["expected"] == "pass":
                s["own"] += 1
                if not v["ok"]:
                    s["false_alarms"] += 1
                    s["false_alarm_cases"].append(r["id"])
            elif v["ok"]:
                s["caught"] += 1
            else:
                s["missed"] += 1
                s["missed_cases"].append(r["id"])
    for name, s in per.items():
        s["cost_usd"] = round((costs or {}).get(name, 0.0), 4)
    return {"cases": len(results), "reviewers": per}


def check(results: list[dict], summary: dict, max_false_alarms: float = MAX_FALSE_ALARMS) -> list[str]:
    """Reasons the gate fails (empty = pass)."""
    reasons = []
    for r in results:
        for name, v in r["reviewers"].items():
            if v.get("error"):
                reasons.append(f"{name} errored on {r['id']}: {v['error']}")
            elif (r["kind"] == "planted" or r.get("gate")) and v.get("expected") not in (None, "pass") and not v["ok"]:
                why = f"missing {', '.join(v['missing'])}" if v.get("missing") and v["flags"] else "no flag"
                reasons.append(f"{name} missed {r['id']} ({r['label']}): {why}")
    for name, s in summary["reviewers"].items():
        if s["own"] and s["false_alarms"] / s["own"] > max_false_alarms + 1e-9:
            reasons.append(f"{name} flagged {s['false_alarms']} of {s['own']} of Shivam's own posts "
                           f"(allowed {max_false_alarms:.0%}): {', '.join(s['false_alarm_cases'])}")
    return reasons


def comment(agent_name: str, run_id: str, summary: dict, results: list[dict], reasons: list[str], seconds: float,
            cost: float) -> str:
    """Markdown for the PR comment, same shape as evals/results/<agent>.md."""
    head = f"### Reviewer evals: {agent_name} {'passed' if not reasons else 'failed'}"
    line = f"{summary['cases']} cases in {seconds:.0f}s · ${cost:.4f}"
    why = "\n\n" + "\n".join(f"- {r}" for r in reasons) if reasons else ""
    rows = "\n".join(f"| {n} | {s['caught']}/{s['caught'] + s['missed']} | {s['false_alarms']}/{s['own']} | ${s['cost_usd']:.4f} |"
                     for n, s in summary["reviewers"].items())
    mark = lambda v: "–" if not v.get("expected") else ("ok" if v["ok"] else ("error" if v.get("error") else  # noqa: E731
                                                        "false alarm" if v["expected"] == "pass" else "missed"))
    cases = "\n".join(f"| {r['id']} | {r['label'][:90]} | " + ", ".join(f"{n} {mark(v)}" for n, v in r["reviewers"].items()
                                                                       if v.get("expected")) + " |" for r in results)
    return (f"{head}\n\n{line}{why}\n\n| Reviewer | Caught (bad posts) | False alarms (Shivam's posts) | Cost |\n|---|---|---|---|\n"
            f"{rows}\n\n<details><summary>Cases</summary>\n\n| Case | Tests | Result |\n|---|---|---|\n{cases}\n\n</details>\n\n"
            f"Dashboard run `{run_id}`\n")


def run(agent: str, workers: int, fixture_path: Path | None = None) -> int:
    spec, info = AGENTS[agent](), meta()
    fixture = json.loads((fixture_path or HERE / "reviewers" / f"{agent}.json").read_text())
    start = time.monotonic()
    with connect() as db:
        run_id = db.start_run(agent, "reviewer_eval", info["commit"])
        try:
            results = run_cases(db, run_id, spec, fixture, workers)
        except BudgetExceeded as ex:
            db.finish_run(run_id, "budget_exceeded", error=str(ex), stats=info)
            print(f"stopped: {ex}", file=sys.stderr)
            return 1
        costs = {r["node"]: float(r["c"] or 0) for r in db.all(
            "select node, sum(cost_usd) as c from llm_calls where run_id=%s group by node", (run_id,))}
        summary = summarize(results, costs)
        reasons, seconds, cost = check(results, summary), round(time.monotonic() - start, 1), sum(costs.values())
        db.finish_run(run_id, "succeeded" if not reasons else "failed", error="; ".join(reasons) or None,
                      stats={**summary, **info, "seconds": seconds, "passed": not reasons, "gate": reasons, "results": results})
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    stem = "reviewers" if agent == "market_intel" else f"reviewers.{agent}"
    (out / f"{stem}.json").write_text(json.dumps({"summary": summary, **info, "seconds": seconds, "results": results}, indent=1))
    (out / f"{stem}.md").write_text(comment(spec["name"], run_id, summary, results, reasons, seconds, cost))
    for r in results:
        for n, v in r["reviewers"].items():
            if v.get("expected"):
                print(f"{r['id']:28} {n:14} {'ok ' if v['ok'] else 'BAD'} {'; '.join(v['flags'])[:150] or v.get('error') or '-'}")
    print(json.dumps(summary["reviewers"], indent=1), f"\n{seconds}s, ${cost:.4f}")
    for r in reasons:
        print(f"REGRESSION: {r}", file=sys.stderr)
    return 1 if reasons else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", choices=sorted(AGENTS), default="market_intel")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()
    return run(args.agent, args.workers)


if __name__ == "__main__":
    sys.exit(main())
