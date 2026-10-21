"""Fact-checker: the humanizer skill's fact guard (numbers and dates not in the sources) plus a claim-by-claim
check against the source text by the judge model. Unsupported claims and invented numbers block the post."""
from core import humanizer, llm
from agents.substack import checks
from agents.substack.specialists import current, sources_text

VERDICTS = ("supported", "hedge", "unsupported")


def run(state, ctx, cfg):
    draft, text = current(state), sources_text(state)
    invented = humanizer.invented_facts(text, checks.LINK.sub(r"\1", draft["md"]))
    review = llm.call_json(ctx, "check", "fact_checker", cfg.prompt,
                           f"# Source material\n{text}\n\n# Draft\n{draft['md']}", model=cfg.model)
    claims = [c for c in review.get("claims") or [] if isinstance(c, dict) and c.get("verdict") in VERDICTS]
    count = {v: sum(c["verdict"] == v for c in claims) for v in VERDICTS}
    # the prompt lists only problem claims (long lists made the judge loop); supported ones come as a count
    count["supported"] += review["supported_count"] if isinstance(review.get("supported_count"), int) else 0
    report = {"version": draft["version"], "claims": claims, "invented": invented, "supported": count["supported"],
              "blocking": count["unsupported"] + len(invented), "hedges": count["hedge"]}
    summary = (f"v{draft['version']}: {count['supported']} supported, {count['hedge']} need hedging, "
               f"{count['unsupported']} unsupported, {len(invented)} number{'s' * (len(invented) != 1)} not in the sources")
    update = {"fact_report": report}
    if (entry := state.get("candidates", {}).get(str(draft["version"]))) is not None:  # fact problems count in the ranking
        issues = [f'unsupported: "{c.get("claim", "")}"' for c in claims if c["verdict"] == "unsupported"]
        issues += [f'needs hedging: "{c.get("claim", "")}"' for c in claims if c["verdict"] == "hedge"]
        update["candidates"] = {**state["candidates"], str(draft["version"]): {**entry, "fact_issues": issues, "fact_checked": True}}
    return update, summary, report
