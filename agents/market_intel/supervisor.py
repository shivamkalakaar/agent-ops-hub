"""The LinkedIn coordinator's rules: code decides what is *allowed*, the coordinator picks among it (core/orchestrator.py).

- scout, then researcher, then the first draft: one allowed move each, so no LLM call.
- editor, fact-checker and final check review each version. The writer gets one rewrite, after all three have
  reported, so it fixes every note in one pass instead of chasing one reviewer at a time.
- Then FINISH (posts only: the one-pager was dropped 2026-10-30). A scheduled run whose scout found no take worth posting finishes without a draft.
"""
import json

from core import orchestrator
from core.orchestrator import FINISH, done_or_off, on
from agents.market_intel.roster import AGENT, AUTHOR, MAX_STEPS, WORKERS
from agents.market_intel.specialists import version, weak

PREFERENCE = ["editor", "fact_checker", "final_check", "writer", FINISH, "scout", "researcher"]


def allowed(state, settings) -> list[str]:
    if state.get("skip"):
        return [FINISH]
    if not state.get("angle"):
        return ["scout"]
    if not state.get("researched"):
        return ["researcher"]
    v = version(state)
    if v == 0:
        return ["writer"]
    e, f, fr = state.get("edit_report") or {}, state.get("fact_report") or {}, state.get("final_report") or {}
    edited, checked, finaled = e.get("version") == v, f.get("version") == v, fr.get("version") == v
    opts = []
    if not edited and on(state, settings, "editor"):
        opts.append("editor")
    if not checked and on(state, settings, "fact_checker"):
        opts.append("fact_checker")
    ready = done_or_off(state, settings, "editor", edited) and done_or_off(state, settings, "fact_checker", checked)
    if ready and not finaled and on(state, settings, "final_check"):
        opts.append("final_check")
    reviewed = ready and done_or_off(state, settings, "final_check", finaled)
    issues = ((edited and (weak(e) or e.get("violations") or e.get("unsupported"))) or (checked and f.get("blocking"))
              or (finaled and not fr.get("passed")))
    writer_left = settings["writer"].max_calls - state.get("calls", {}).get("writer", 0)
    if reviewed and issues and writer_left > 0 and on(state, settings, "writer"):
        opts.append("writer")
    if reviewed and not opts:
        opts.append(FINISH)
    return list(dict.fromkeys(opts)) or [FINISH]


def canonical(opts: list[str]) -> str:
    return next(o for o in PREFERENCE if o in opts)


def workspace(state) -> str:
    v = version(state)
    e, f, fr = state.get("edit_report") or {}, state.get("fact_report") or {}, state.get("final_report") or {}
    a = state.get("angle") or {}
    lines = [f"Angle: {a.get('angle')}", f"Take: {a.get('take')}", f"Fact sheet: {len(state.get('facts') or [])} facts",
             f"Current draft: v{v}"]
    if e:
        lines.append(f"Editor on v{e['version']}: overall {e['overall']}/5, scores {json.dumps(e['scores'])}, "
                     f"{len(e.get('violations', []))} guardrail flags. {e.get('critique', '')}")
    if f:
        lines.append(f"Fact check on v{f['version']}: {f['blocking']} unsupported, {f['hedges']} to state more carefully")
    if fr:
        lines.append(f"Final check on v{fr['version']}: {'PASS' if fr['passed'] else 'FAIL: ' + '; '.join(fr['blockers'][:3])}")
    return "\n".join(lines)


TEAM = orchestrator.Team(AGENT, AUTHOR, MAX_STEPS, allowed=allowed, canonical=canonical, workspace=workspace,
                         specialists=tuple(WORKERS))
