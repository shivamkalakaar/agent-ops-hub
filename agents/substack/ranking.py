"""Which version to save when none passed the final check. Each edited version is a candidate; its rank counts
the editor's guardrail flags, the fact-checker's problems and, while the fact-checker is on, never having been
fact-checked. Ties go to the higher weighted score, then the more human one (core.drafting.rank)."""
from core import drafting


def problems(entry: dict, fact_checker_on: bool) -> list[str]:
    out = entry["violations"] + entry.get("fact_issues", [])
    if fact_checker_on and not entry.get("fact_checked"):
        out = out + ["not fact-checked"]
    return out


def best(state) -> dict | None:
    """A fact-checked version always beats an unchecked one: "not checked" hides an unknown number of problems."""
    on = state["settings"]["fact_checker"].enabled
    entries = [{**e, "violations": problems(e, on)} for e in state.get("candidates", {}).values()]
    return max(entries, key=lambda e: (bool(e.get("fact_checked")) or not on, drafting.rank(e))) if entries else None
