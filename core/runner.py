"""One run of an agent's graph, with everything production needs around it: a row in `runs`, a Langfuse trace,
a RunContext for the nodes, a clean stop when the budget guard trips, scores on the trace and final stats."""
import sys
from typing import Callable

from core import llm, tracing
from core.budget import BudgetExceeded
from core.config import git_sha
from core.db import connect


def run_graph(agent: str, kind: str, graph, init: dict, summarize: Callable[[dict], dict], *,
              prompt_version: str = "v1", scores: Callable[[dict], dict] | None = None) -> int:
    """Invoke a compiled graph. `scores(state)` returns {name: value} to attach to the trace."""
    sha = git_sha()
    with connect() as db:
        run_id = db.start_run(agent, kind, sha)
        with tracing.trace(f"{agent}.{kind}", run_id=run_id, tags=[agent, kind, prompt_version],
                           metadata={"git_sha": sha, "prompt_version": prompt_version}) as t:
            ctx = llm.RunContext(db=db, run_id=run_id, callbacks=t.callbacks)
            try:
                state = graph.invoke(init, config={"configurable": {"ctx": ctx}, "callbacks": t.callbacks,
                                                   "run_name": f"{agent}.{kind}", "recursion_limit": 60})
            except BudgetExceeded as ex:
                db.finish_run(run_id, "budget_exceeded", trace_url=t.url, error=str(ex))
                print(f"[{run_id}] stopped: {ex}", file=sys.stderr)
                return 0  # a budget stop is the guard working, not a crash
            except Exception as ex:
                db.finish_run(run_id, "failed", trace_url=t.url, error=f"{type(ex).__name__}: {ex}")
                raise
            stats = summarize(state)
            for name, value in (scores(state) if scores else {}).items():
                t.score(name, value)
            t.output(stats)
            db.finish_run(run_id, "succeeded", trace_url=t.url, stats=stats)
            print(f"[{run_id}] {kind} succeeded: {stats}" + (f"\ntrace: {t.url}" if t.url else ""))
    return 0
