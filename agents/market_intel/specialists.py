"""The LinkedIn team's specialists. Each one is fn(state, ctx, cfg) -> (update, summary, output); see core/orchestrator.py.

scout → researcher → writer ⇄ {editor, fact_checker, final_check} → onepager, in the order the coordinator picks.
"""
import json
import re

from core import drafting, humanizer, llm, novelty
from agents.market_intel.roster import AUTHOR

SCOUT_REPEAT = 0.4  # an angle this close to a recent one loses to a fresher candidate


def current(state) -> dict | None:
    return state["drafts"][-1] if state.get("drafts") else None


def version(state) -> int:
    return current(state)["version"] if state.get("drafts") else 0


def _g():
    from agents.market_intel import graph  # late: graph imports this module; tests patch graph.fetch_article
    return graph


def _tier(cfg, default: str) -> str:
    return cfg.tier or default


def _take(state) -> str:
    return (f"\n\n# {AUTHOR}'s own take (build the post around this: it's their opinion in their words, so keep its "
            f"meaning and as much of its phrasing as fits)\n{state['take']}") if state.get("take") else ""


def _lessons(state) -> str:
    return f"\n\n{state['lessons']}" if state.get("lessons") else ""


def listing(items: list[dict]) -> str:
    return "\n".join(json.dumps({"id": n, "source": i["source"], "layer": i.get("layer"), "importance": i.get("importance"),
                                 "title": i["title"], "summary": (i.get("summary") or "")[:400], "why": i.get("why")},
                                ensure_ascii=False) for n, i in enumerate(items))


def fact_sheet(state) -> str:
    facts = state.get("facts") or []
    lines = [f"- {f['claim']} (source {f['source_id']}: \"{f['evidence']}\")" for f in facts] or ["(no facts extracted; use the source items)"]
    if state.get("context"):
        lines += ["", "Context:"] + [f"- {c}" for c in state["context"]]
    return "\n".join(lines)


def sources_text(state) -> str:
    return drafting.sources_block(state.get("sources", [])) + "\n\n# Fact sheet\n" + fact_sheet(state)


def _numbered(sources: list[dict]) -> str:
    return "\n\n".join(f"(source {n})\n{drafting.sources_block([s])}" for n, s in enumerate(sources))


# --------------------------------------------------------------------------- scout

def scout(state, ctx, cfg):
    recent = "\n".join(f"- {a}" for a in state.get("recent_angles") or []) or "(none yet)"
    r = llm.call_json(ctx, _tier(cfg, "judge"), "scout", drafting.fill(cfg.prompt, AUTHOR=AUTHOR, RECENT=recent),
                      listing(state["items"]) + _take(state) + _lessons(state), model=cfg.model)
    items = state["items"]
    cands = [c for c in (r.get("candidates") or ([r] if r.get("item_ids") else [])) if isinstance(c, dict)]
    for c in cands:
        c["item_ids"] = [i for i in c.get("item_ids") or [] if isinstance(i, int) and 0 <= i < len(items)][:3]
        text = novelty.words(f"{c.get('angle', '')} {c.get('hook', '')} " + " ".join(items[i]["title"] for i in c["item_ids"]))
        c["repeat"] = round(max([novelty.jaccard(text, novelty.words(a)) for a in state.get("recent_angles") or []] or [0]), 2)
    cands = [c for c in cands if c["item_ids"]] or [{"item_ids": [0], "angle": items[0]["title"], "hook": "", "why_now": "", "repeat": 0}]
    pick = r.get("pick") if isinstance(r.get("pick"), int) and 0 <= r.get("pick") < len(cands) else 0
    note = str(r.get("why", ""))[:200]
    if cands[pick]["repeat"] >= SCOUT_REPEAT and (fresher := min(cands, key=lambda c: c["repeat"]))["repeat"] < SCOUT_REPEAT:
        pick, note = cands.index(fresher), f"switched: the first pick was too close to a recent post ({cands[pick]['repeat']})"
    angle = {k: cands[pick].get(k) for k in ("angle", "hook", "why_now")}
    sources = [items[i] for i in cands[pick]["item_ids"]]
    summary = f"Angle: \"{angle['angle']}\" from {len(sources)} stor{'y' if len(sources) == 1 else 'ies'} ({len(cands)} candidates)"
    return {"angle": angle, "sources": sources}, summary, {"candidates": cands, "pick": pick, "why": note}


# --------------------------------------------------------------------------- researcher

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9$%. ]", " ", s.lower())).strip()


def researcher(state, ctx, cfg):
    sources = drafting.read_sources(state["sources"], fetch=_g().fetch_article)
    user = (f"# Angle\n{state['angle'].get('angle')}\n\n# Source items\n{_numbered(sources)}" + _take(state))
    try:
        r = llm.call_json(ctx, _tier(cfg, "check"), "researcher", drafting.fill(cfg.prompt, AUTHOR=AUTHOR), user, model=cfg.model)
    except ValueError:
        r = {}
    facts, dropped = [], 0
    for f in r.get("facts") or []:
        if not isinstance(f, dict) or not isinstance(f.get("source_id"), int) or not 0 <= f["source_id"] < len(sources):
            dropped += 1
            continue
        s = sources[f["source_id"]]
        haystack = _norm(" ".join([s.get("title", ""), s.get("summary") or "", s.get("body") or ""]))
        if f.get("claim") and f.get("evidence") and _norm(str(f["evidence"]))[:120] in haystack:  # quotes must be real
            facts.append({"claim": str(f["claim"])[:300], "evidence": str(f["evidence"])[:300], "source_id": f["source_id"]})
        else:
            dropped += 1
    context = [str(c)[:300] for c in (r.get("context") or []) if isinstance(c, str)][:3]
    read = sum(bool(s.get("body")) for s in sources)
    summary = (f"Fact sheet: {len(facts)} facts with quotes from {len(sources)} sources ({read} read in full)"
               + (f"; dropped {dropped} without a matching quote" if dropped else ""))
    return {"sources": sources, "facts": facts, "context": context, "researched": True}, summary, {"facts": facts, "context": context}


# --------------------------------------------------------------------------- writer

def _feedback(state) -> str:
    v, parts = version(state), []
    if (e := state.get("edit_report")) and e.get("version") == v:
        parts.append(f"# Editor (overall {e['overall']}/5, scores {json.dumps(e['scores'])})\n{e.get('critique', '')}")
        if e.get("violations"):
            parts.append("# Rule violations (must fix)\n" + "\n".join(f"- {x}" for x in e["violations"]))
        if e.get("unsupported"):
            parts.append("# The editor found these unsupported (must fix)\n" + "\n".join(f"- {x}" for x in e["unsupported"]))
    if (f := state.get("fact_report")) and f.get("version") == v:
        bad = [f'- "{c["claim"]}" → {c.get("fix") or "drop it"}' for c in f["claims"] if c["verdict"] == "unsupported"]
        hedge = [f'- "{c["claim"]}" → {c.get("fix") or "attribute it"}' for c in f["claims"] if c["verdict"] == "hedge"]
        if bad:
            parts.append("# Fact check: unsupported (must fix)\n" + "\n".join(bad))
        if hedge:
            parts.append("# Fact check: state more carefully (should fix)\n" + "\n".join(hedge))
    if (fr := state.get("final_report")) and fr.get("version") == v and not fr.get("passed"):
        parts.append("# Final check blockers (must fix)\n" + "\n".join(f"- {b}" for b in fr.get("blockers", [])))
    return "\n\n".join(parts)


def _clean(text: str) -> str:
    text = text.strip().strip('"').strip()
    return re.sub(r"\n{3,}", "\n\n", text)


TARGET_WORDS = 130


def writer(state, ctx, cfg):
    """Best of N: the draft is written by each candidate model (repeats get different voice examples), and the code's
    own checks pick one: fewest guardrail flags (invented facts, template moves, repeats), then the more human one,
    then the length closest to TARGET_WORDS. First drafts use every candidate; rewrites use two."""
    g = _g()
    a = state["angle"]
    user = (f"Angle: {a.get('angle')}\nCandidate hook: {a.get('hook')}\nWhy now: {a.get('why_now')}\n\n"
            f"# Fact sheet\n{fact_sheet(state)}\n\n# Source items\n{drafting.sources_block(state['sources'])}")
    if recent := "\n\n---\n\n".join(state.get("recent_posts") or []):
        user += (f"\n\n# {AUTHOR}'s last few posts (already published: don't reuse their hook pattern, structure, "
                 f"stance line or closing move; this post should feel different)\n{recent}")
    user += _take(state) + _lessons(state)
    draft = current(state)
    if draft:
        user += f"\n\n# Current draft (v{draft['version']})\n{draft['post']}\n\n{_feedback(state)}\n\nRewrite the whole post."
    if state.get("instructions"):
        user += f"\n\n# Coordinator's note\n{state['instructions']}"
    tier = _tier(cfg, "voice")

    def make_system(i: int) -> str:
        examples = drafting.style_examples(n=4, short_only=False, max_words=g.EXAMPLE_MAX_WORDS, seed=f"{state.get('week')}-{i}")
        return drafting.fill(cfg.prompt, AUTHOR=AUTHOR, STYLE_GUIDE=g.style_guide(), HUMANIZER=humanizer.rules(),
                             EXAMPLES="\n\n---\n\n".join(examples) or "(no examples yet; follow the style guide)")
    outs = drafting.write_candidates(ctx, tier, "writer", drafting.candidates(tier, cfg.model, n=2 if draft else None),
                                     make_system, user, _clean, max_words=3 * g.WORDS[1])
    urls, text = {s["url"] for s in state["sources"]}, sources_text(state)

    def score(post: str) -> tuple:
        flags = g.guardrail_violations(post, urls, text, state.get("recent_posts"))
        return len(flags), -humanizer.scan(post)["score"], abs(len(post.split()) - TARGET_WORDS)
    best, scored = drafting.pick(outs, score)
    post, n = best["text"], version(state) + 1
    short = lambda m: m.split("/")[-1]  # noqa: E731
    summary = (f"{'Rewrote' if draft else 'Wrote'} v{n}: {len(post.split())} words"
               + (f", best of {len(scored)} ({', '.join(short(d['model']) for d in scored)}): kept {short(best['model'])}"
                  if len(scored) > 1 else ""))
    out = {"version": n, "post": post, "model": best["model"],
           "candidates": [{"model": d["model"], "flags": d["score"][0], "humanizer": -d["score"][1], "words": len(d["text"].split())}
                          for d in scored]}
    return {"drafts": (state.get("drafts") or []) + [{"version": n, "post": post}]}, summary, out


# --------------------------------------------------------------------------- editor

def editor(state, ctx, cfg):
    g, draft = _g(), current(state)
    user = f"# Source items\n{sources_text(state)}\n\n# Post\n{draft['post']}"
    if state.get("lessons"):
        user += (f"\n\n{state['lessons']}\nA draft that breaks one of these lessons loses a point on voice or human, "
                 "and the critique must name the lesson.")
    review = llm.call_json(ctx, _tier(cfg, "judge"), "editor", drafting.fill(cfg.prompt, STYLE_GUIDE=g.style_guide()), user,
                           model=cfg.model)
    scores = {k: max(1, min(5, int(v))) for k, v in (review.get("scores") or {}).items() if str(v).isdigit()}
    violations = g.guardrail_violations(draft["post"], {s["url"] for s in state["sources"]}, sources_text(state),
                                        state.get("recent_posts"))
    accent = humanizer.scan(draft["post"])
    overall = drafting.overall_score(scores, accent["score"])
    report = {"version": draft["version"], "scores": scores, "critique": str(review.get("critique", "")),
              "unsupported": [c for c in review.get("unsupported_claims") or [] if isinstance(c, str)],
              "violations": violations, "accent": accent, "overall": overall}
    entry = {**report, "post": draft["post"]}
    summary = (f"v{draft['version']}: overall {overall}/5 (humanizer {accent['score']}/5), {len(violations)} "
               f"guardrail flag{'s' * (len(violations) != 1)}. {report['critique'][:140]}")
    out = {k: report[k] for k in ("version", "scores", "critique", "unsupported", "violations", "overall")}
    return {"edit_report": report, "candidates": {**state.get("candidates", {}), str(draft["version"]): entry}}, summary, out


def weak(report: dict) -> bool:
    return drafting.weak_scores(report["scores"], report["accent"]["score"])


# --------------------------------------------------------------------------- fact checker

def fact_checker(state, ctx, cfg):
    draft = current(state)
    try:
        r = llm.call_json(ctx, _tier(cfg, "check"), "fact_checker", drafting.fill(cfg.prompt, AUTHOR=AUTHOR),
                          f"{sources_text(state)}\n\n# Post\n{draft['post']}", model=cfg.model)
    except ValueError:
        r = {}
    claims = [{"claim": str(c.get("claim", ""))[:300], "verdict": c.get("verdict"), "fix": str(c.get("fix") or "")[:300]}
              for c in r.get("claims") or [] if isinstance(c, dict) and c.get("verdict") in ("supported", "hedge", "unsupported")]
    count = {v: sum(c["verdict"] == v for c in claims) for v in ("supported", "hedge", "unsupported")}
    report = {"version": draft["version"], "claims": claims, "blocking": count["unsupported"], "hedges": count["hedge"]}
    cands = dict(state.get("candidates", {}))
    if (key := str(draft["version"])) in cands:
        cands[key] = {**cands[key], "fact_checked": True,
                      "fact_issues": [f"unsupported: {c['claim']}" for c in claims if c["verdict"] == "unsupported"]}
    summary = f"v{draft['version']}: {count['supported']} supported, {count['hedge']} to state more carefully, {count['unsupported']} unsupported"
    return {"fact_report": report, "candidates": cands}, summary, report


# --------------------------------------------------------------------------- final check

def final_check(state, ctx, cfg):
    g, draft = _g(), current(state)
    v = draft["version"]
    blockers = g.guardrail_violations(draft["post"], {s["url"] for s in state["sources"]}, sources_text(state),
                                      state.get("recent_posts"))
    f = state.get("fact_report") or {}
    if f.get("version") == v and f.get("blocking"):
        blockers.append(f"{f['blocking']} unsupported claim{'s' * (f['blocking'] != 1)} from the fact check")
    try:
        r = llm.call_json(ctx, _tier(cfg, "check"), "final_check", drafting.fill(cfg.prompt, AUTHOR=AUTHOR),
                          f"# Post\n{draft['post']}\n\n# Fact sheet\n{fact_sheet(state)}{_lessons(state)}", model=cfg.model)
    except ValueError:
        r = {"passed": True, "blockers": [], "polish": ["the final read returned nothing usable"]}
    if r.get("passed") is False:
        blockers += [str(b)[:300] for b in r.get("blockers") or [] if isinstance(b, str)] or ["the final read didn't pass it"]
    report = {"version": v, "passed": not blockers, "blockers": blockers,
              "polish": [str(p)[:300] for p in r.get("polish") or [] if isinstance(p, str)][:4]}
    summary = f"v{v}: " + ("passed" if report["passed"] else f"{len(blockers)} blocker{'s' * (len(blockers) != 1)}: {blockers[0][:120]}")
    return {"final_report": report}, summary, report


# --------------------------------------------------------------------------- one-pager

def onepager(state, ctx, cfg, post: str | None = None):
    post = post or current(state)["post"]
    page = llm.call_json(ctx, _tier(cfg, "writer"), "onepager", cfg.prompt,
                         f"# Post\n{post}\n\n# Fact sheet\n{fact_sheet(state)}\n\n# Source items\n{drafting.sources_block(state['sources'])}",
                         model=cfg.model)
    return {"onepager": page, "onepager_for": post}, f"One-pager: \"{page.get('title', '')}\"", {"title": page.get("title")}


RUNNERS = {"scout": scout, "researcher": researcher, "writer": writer, "editor": editor, "fact_checker": fact_checker,
           "final_check": final_check, "onepager": onepager}
