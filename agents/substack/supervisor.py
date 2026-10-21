"""The super agent. Code decides what is *allowed*; the LLM decides what is *best* among the allowed moves.

- allowed(): preconditions per specialist (no editing before a draft, no FINISH before a passing final check,
  no repeating a check on an unchanged draft), call caps from settings, disabled specialists removed.
- With one allowed move there is nothing to decide, so no LLM call is made.
- An invalid or unparseable choice falls back to canonical(), the fixed newsroom order, and the fallback is logged.
- MAX_STEPS specialist turns per run; at the limit the run finishes with the best version so far.
"""
import json

from core import drafting, llm
from agents.substack import checks
from agents.substack.roster import AUTHOR, MAX_STEPS
from agents.substack.specialists import version
from agents.substack.specialists.editor import weak

FINISH = "FINISH"
ORDER = ["research", "writer", "editor", "fact_checker", "illustrator", "final_check", FINISH]
PREFERENCE = ["editor", "writer", "fact_checker", "illustrator", "final_check", FINISH, "research"]


def _on(state, settings, name) -> bool:
    return settings[name].enabled and state.get("calls", {}).get(name, 0) < settings[name].max_calls


def _done_or_off(state, settings, name, done: bool) -> bool:
    """A step counts as satisfied when it ran on this version, or when it's switched off or out of calls."""
    return done or not _on(state, settings, name)


def allowed(state, settings) -> list[str]:
    if not state.get("brief"):
        return ["research"]
    v = version(state)
    if v == 0:
        return ["writer"] if _on(state, settings, "writer") else [FINISH]
    e, f, fr = state.get("edit_report") or {}, state.get("fact_report") or {}, state.get("final_report") or {}
    edited, checked, finaled = e.get("version") == v, f.get("version") == v, fr.get("version") == v
    issues = ((edited and (weak(e) or e.get("violations"))) or (checked and (f.get("blocking") or f.get("hedges")))
              or (finaled and not fr.get("passed")))
    heads_now = set(state.get("image_headings") or [])
    current_heads = set(checks.sections(checks.parse(state["drafts"][-1]["md"])["body"]))
    images_stale = bool(state.get("images")) and heads_now != current_heads
    images_done = state.get("calls", {}).get("illustrator", 0) > 0 and not images_stale

    opts = []
    if not edited and _on(state, settings, "editor"):
        opts.append("editor")
    if not checked and _on(state, settings, "fact_checker"):
        opts.append("fact_checker")
    # The last rewrite is kept for the final check's blockers: they come last and matter most. Before the final
    # check has run, editor and fact notes may use every writer call but one.
    w = settings["writer"]
    writer_left = w.max_calls - state.get("calls", {}).get("writer", 0)
    if issues and _on(state, settings, "writer") and (writer_left > 1 or (finaled and not fr.get("passed"))):
        opts.append("writer")
    if (edited or checked) and _on(state, settings, "research"):
        opts.append("research")
    if _done_or_off(state, settings, "editor", edited) and not images_done and _on(state, settings, "illustrator"):
        opts.append("illustrator")
    ready = (_done_or_off(state, settings, "editor", edited) and _done_or_off(state, settings, "fact_checker", checked)
             and _done_or_off(state, settings, "illustrator", images_done))
    if ready and not finaled and _on(state, settings, "final_check"):
        opts.append("final_check")
    if finaled and fr.get("passed"):
        opts.append(FINISH)
    # Nothing left but optional extra research (or nothing at all): finishing is allowed too.
    if set(opts) <= {"research"}:
        opts.append(FINISH)
    return list(dict.fromkeys(opts))


def canonical(opts: list[str]) -> str:
    return next(o for o in PREFERENCE if o in opts)


def _workspace(state) -> str:
    v = version(state)
    e, f, fr = state.get("edit_report") or {}, state.get("fact_report") or {}, state.get("final_report") or {}
    lines = [f"Thesis: {state['brief'].get('thesis')}", f"Current draft: v{v}"]
    if e:
        lines.append(f"Editor on v{e['version']}: overall {e['overall']}/5, scores {json.dumps(e['scores'])}, "
                     f"{len(e.get('violations', []))} guardrail flags. {e.get('critique', '')}")
    if f:
        lines.append(f"Fact check on v{f['version']}: {f['blocking']} blocking, {f['hedges']} need hedging")
    if state.get("images") is not None and state.get("calls", {}).get("illustrator"):
        lines.append(f"Images: {len(state.get('images', []))} made")
    if fr:
        lines.append(f"Final check on v{fr['version']}: {'PASS' if fr['passed'] else 'FAIL: ' + '; '.join(fr['blockers'][:3])}")
    if gaps := state["brief"].get("gaps"):
        lines.append(f"Research gaps: {'; '.join(gaps[:3])}")
    return "\n".join(lines)


def decide(state, ctx, settings) -> dict:
    """Returns {"next", "reason", "instructions", "options", "by"}."""
    if state.get("work_steps", 0) >= MAX_STEPS:
        return {"next": FINISH, "reason": f"step limit ({MAX_STEPS}) reached; saving the best version",
                "instructions": "", "options": [FINISH], "by": "limit"}
    opts = allowed(state, settings)
    if len(opts) == 1:
        return {"next": opts[0], "reason": "only allowed move", "instructions": "", "options": opts, "by": "rule"}
    cfg = settings["supervisor"]
    calls = ", ".join(f"{k} {state.get('calls', {}).get(k, 0)}/{settings[k].max_calls}" for k in ORDER[:-1])
    history = "\n".join(f"- {h['who']}: {h['summary']}" for h in state.get("history", [])[-8:])
    user = (f"# Workspace\n{_workspace(state)}\n\n# Recent steps\n{history}\n\n# Calls used\n{calls}; "
            f"steps {state.get('work_steps', 0)}/{MAX_STEPS}\n\n# Allowed now\n{', '.join(opts)}")
    try:
        r = llm.call_json(ctx, "writer", "supervisor", drafting.fill(cfg.prompt, AUTHOR=AUTHOR), user, model=cfg.model)
    except ValueError:
        r = {}
    choice = r.get("next")
    if choice in opts:
        return {"next": choice, "reason": str(r.get("reason", ""))[:300], "instructions": str(r.get("instructions", ""))[:600],
                "options": opts, "by": "llm"}
    fallback = canonical(opts)
    return {"next": fallback, "reason": f"fallback: supervisor chose {choice!r}, not in {opts}", "instructions": "",
            "options": opts, "by": "fallback"}
