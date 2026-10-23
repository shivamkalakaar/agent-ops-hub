"""A coordinator and specialists on a LangGraph state machine, shared by every multi-agent workflow.

load_context → supervisor ⇄ {specialists} → finish

Code decides what is *allowed* (each team's allowed()); the coordinator LLM decides what is *best* among the allowed
moves. With one allowed move nothing is decided, so no LLM call is made. An invalid or unparseable choice falls
back to the team's canonical() order, and the fallback is logged. At max_steps specialist turns the run finishes
with the best version so far. Every decision and every specialist turn is a row in agent_steps: the live timeline.

A specialist is fn(state, ctx, cfg) -> (update, summary, output):
- update: the workspace fields the specialist owns
- summary: one line for the dashboard timeline and the coordinator's history
- output: what the dashboard shows when the step is expanded
"""
from dataclasses import dataclass
from typing import Callable

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from core import drafting, llm
from core.steps import step

FINISH = "FINISH"


@dataclass(frozen=True)
class Team:
    agent: str
    author: str
    max_steps: int                                  # specialist turns per run; decisions don't count
    allowed: Callable[[dict, dict], list[str]]      # (state, settings) -> allowed moves, FINISH included
    canonical: Callable[[list[str]], str]           # the fixed preference among allowed moves
    workspace: Callable[[dict], str]                # what the coordinator reads about the current state
    specialists: tuple[str, ...]                    # workers, in display order (call counts for the coordinator)
    supervisor_tier: str = "writer"


def ctx_of(config: RunnableConfig):
    return config["configurable"]["ctx"]


def on(state, settings, name) -> bool:
    """Enabled and not out of calls."""
    return settings[name].enabled and state.get("calls", {}).get(name, 0) < settings[name].max_calls


def done_or_off(state, settings, name, done: bool) -> bool:
    """A step counts as satisfied when it ran on this version, or when it's switched off or out of calls."""
    return done or not on(state, settings, name)


def decide(team: Team, state, ctx, settings) -> dict:
    """Returns {"next", "reason", "instructions", "options", "by"}."""
    if state.get("work_steps", 0) >= team.max_steps:
        return {"next": FINISH, "reason": f"step limit ({team.max_steps}) reached; saving the best version",
                "instructions": "", "options": [FINISH], "by": "limit"}
    opts = team.allowed(state, settings)
    if len(opts) == 1:
        return {"next": opts[0], "reason": "only allowed move", "instructions": "", "options": opts, "by": "rule"}
    cfg = settings["supervisor"]
    calls = ", ".join(f"{k} {state.get('calls', {}).get(k, 0)}/{settings[k].max_calls}" for k in team.specialists)
    history = "\n".join(f"- {h['who']}: {h['summary']}" for h in state.get("history", [])[-8:])
    user = (f"# Workspace\n{team.workspace(state)}\n\n# Recent steps\n{history}\n\n# Calls used\n{calls}; "
            f"steps {state.get('work_steps', 0)}/{team.max_steps}\n\n# Allowed now\n{', '.join(opts)}")
    try:
        r = llm.call_json(ctx, team.supervisor_tier, "supervisor", drafting.fill(cfg.prompt, AUTHOR=team.author), user,
                          model=cfg.model)
    except ValueError:
        r = {}
    choice = r.get("next")
    if choice in opts:
        return {"next": choice, "reason": str(r.get("reason", ""))[:300], "instructions": str(r.get("instructions", ""))[:600],
                "options": opts, "by": "llm"}
    return {"next": team.canonical(opts), "reason": f"fallback: coordinator chose {choice!r}, not in {opts}", "instructions": "",
            "options": opts, "by": "fallback"}


def decision_node(team: Team):
    def supervisor(state, config: RunnableConfig):
        ctx, seq = ctx_of(config), state["seq"] + 1
        with step(ctx, team.agent, "supervisor", "decision", seq) as s:
            d = decide(team, state, ctx, state["settings"])
            s.summary, s.reason = f"→ {d['next']}", d["reason"]
            s.output = d
        return {"next": d["next"], "instructions": d["instructions"], "seq": seq,
                "finish_reason": d["reason"] if d["next"] == FINISH else ""}
    return supervisor


def worker_node(team: Team, name: str, run: Callable):
    def node(state, config: RunnableConfig):
        ctx, seq = ctx_of(config), state["seq"] + 1
        with step(ctx, team.agent, name, "work", seq, reason=state.get("instructions") or None) as s:
            update, s.summary, s.output = run(state, ctx, state["settings"][name])
        return {**update, "seq": seq, "work_steps": state["work_steps"] + 1,
                "calls": {**state["calls"], name: state["calls"].get(name, 0) + 1},
                "history": state["history"] + [{"who": name, "summary": s.summary}], "instructions": ""}
    node.__name__ = name
    return node


def route(state) -> str:
    return "finish" if state["next"] == FINISH else state["next"]


def build(team: Team, workspace_type: type, runners: dict[str, Callable], load_context: Callable, finish: Callable):
    g = StateGraph(workspace_type)
    g.add_node("load_context", load_context)
    g.add_node("supervisor", decision_node(team))
    for name in team.specialists:
        g.add_node(name, worker_node(team, name, runners[name]))
        g.add_edge(name, "supervisor")
    g.add_node("finish", finish)
    g.add_edge(START, "load_context")
    g.add_edge("load_context", "supervisor")
    g.add_conditional_edges("supervisor", route, {**{n: n for n in team.specialists}, "finish": "finish"})
    g.add_edge("finish", END)
    return g.compile()
