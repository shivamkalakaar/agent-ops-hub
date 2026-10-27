"""Step log for multi-agent runs: every supervisor decision and every specialist turn becomes a row in
agent_steps (the dashboard's live timeline), with its own cost (the llm_calls made through this RunContext during
the step, so concurrent eval cases in one run don't count each other's) and latency. In Langfuse the same steps show up as LangGraph node spans."""
import time
from contextlib import contextmanager

from core.llm import RunContext


class Step:
    def __init__(self):
        self.summary: str | None = None
        self.output = None
        self.reason: str | None = None
        self.status = "ok"


@contextmanager
def step(ctx: RunContext, agent: str, specialist: str, kind: str, seq: int, reason: str | None = None):
    """with step(ctx, "substack", "editor", "work", n) as s: ...; s.summary = "..."; s.output = {...}"""
    db, s = ctx.db, Step()
    step_id = db.start_step(ctx.run_id, seq + ctx.seq_offset, agent, specialist, kind, reason)
    before, start = ctx.spent[0], time.monotonic()
    try:
        yield s
    except Exception as ex:
        db.finish_step(step_id, "failed", summary=f"{type(ex).__name__}: {str(ex)[:200]}",
                       cost_usd=ctx.spent[0] - before, latency_ms=int((time.monotonic() - start) * 1000))
        raise
    db.finish_step(step_id, s.status, summary=s.summary, output=s.output, reason=s.reason,
                   cost_usd=ctx.spent[0] - before, latency_ms=int((time.monotonic() - start) * 1000))
