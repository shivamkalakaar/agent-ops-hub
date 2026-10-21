"""Final check: the gate before FINISH. Every deterministic guardrail, the fact check and editor results on this
exact version, image alt text, then one publish-readiness read for what rules can't catch."""
from core import drafting, llm
from agents.substack import checks
from agents.substack.roster import AUTHOR
from agents.substack.specialists import current, sources_text
from agents.substack.specialists.editor import weak


def warnings(state) -> list[str]:
    """Worth fixing, not worth blocking: claims the fact-checker would attribute more explicitly."""
    f, v = state.get("fact_report") or {}, current(state)["version"]
    return checks.warnings(current(state)["md"]) + [f'needs hedging: "{c.get("claim", "")}" → {c.get("fix", "")}'
                                                     for c in f.get("claims", []) if f.get("version") == v and c.get("verdict") == "hedge"]


def automated(state) -> list[str]:
    draft = current(state)
    v = draft["version"]
    out = checks.violations(draft["md"], {s["url"] for s in state["sources"]}, sources_text(state))
    f = state.get("fact_report") or {}
    if f.get("version") == v:
        out += [f'unsupported claim: "{c.get("claim", "")}" → {c.get("fix", "")}' for c in f.get("claims", [])
                if c.get("verdict") == "unsupported"]
    e = state.get("edit_report") or {}
    if e.get("version") == v and weak(e):
        low = ", ".join(f"{k} {s}/5" for k, s in e["scores"].items() if s < 3) or f"overall {e['overall']}/5"
        out.append(f"editor scores too low ({low}): {e.get('critique', '')}")
    out += [f"{i['kind']} image has no alt text" for i in state.get("images", []) if not (i.get("alt") or "").strip()]
    return list(dict.fromkeys(out))


def run(state, ctx, cfg):
    draft = current(state)
    auto, warn = automated(state), warnings(state)
    user = (f"# Automated check results (already handled: don't repeat these)\n" + ("\n".join(f"- {a}" for a in auto + warn) or "- all passed")
            + f"\n\n# Source material\n{sources_text(state)}\n\n# Post\n{draft['md']}")
    review = llm.call_json(ctx, "check", "final_check", drafting.fill(cfg.prompt, AUTHOR=AUTHOR), user, model=cfg.model)
    judged = [b for b in review.get("blockers") or [] if isinstance(b, str)][:5] if review.get("ready") is False else []
    blockers = auto + judged
    report = {"version": draft["version"], "passed": not blockers, "blockers": blockers, "automated": auto,
              "polish": warn + [p for p in review.get("polish") or [] if isinstance(p, str)][:5]}
    summary = (f"v{draft['version']}: PASS" if not blockers else
               f"v{draft['version']}: {len(blockers)} blocker{'s' * (len(blockers) != 1)}. {blockers[0][:140]}")
    return {"final_report": report}, summary, report
