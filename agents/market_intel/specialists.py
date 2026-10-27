"""The LinkedIn team's specialists. Each one is fn(state, ctx, cfg) -> (update, summary, output); see core/orchestrator.py.

scout → researcher → writer ⇄ {editor, fact_checker, final_check} → onepager, in the order the coordinator picks.
"""
import json
import re

from core import drafting, humanizer, llm, novelty
from agents.market_intel.roster import AUTHOR

SCOUT_REPEAT = 0.4  # an angle this close to a recent one loses to a fresher candidate
MAX_SOURCES = 5  # chosen stories plus the brief's corroborating links, for the researcher


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
    return "\n".join(json.dumps({"id": n, "source": i["source"], "from_brief": i.get("origin") == "brief",
                                 "tldr": bool(i.get("tldr")), "layer": i.get("layer"), "importance": i.get("importance"),
                                 "appeal": i.get("appeal"), "title": i["title"], "summary": (i.get("summary") or "")[:400], "why": i.get("why")},
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
        c["thesis"] = drafting.thesis_contrast(f"{c.get('angle', '')} {c.get('hook', '')}")
    cands = [c for c in cands if c["item_ids"]] or [{"item_ids": [0], "angle": items[0]["title"], "hook": "", "why_now": "", "repeat": 0}]
    pick = r.get("pick") if isinstance(r.get("pick"), int) and 0 <= r.get("pick") < len(cands) else 0
    note = str(r.get("why", ""))[:200]
    if cands[pick]["repeat"] >= SCOUT_REPEAT and (fresher := min(cands, key=lambda c: c["repeat"]))["repeat"] < SCOUT_REPEAT:
        pick, note = cands.index(fresher), f"switched: the first pick was too close to a recent post ({cands[pick]['repeat']})"
    if cands[pick].get("thesis") and (plain := [c for c in cands if not c.get("thesis") and c["repeat"] < SCOUT_REPEAT]):
        pick, note = cands.index(plain[0]), "switched: the first pick was an \"X, not Y\" thesis line"
    angle = {k: cands[pick].get(k) for k in ("angle", "hook", "why_now")}
    sources = _with_corroboration([items[i] for i in cands[pick]["item_ids"]])
    summary = f"Angle: \"{angle['angle']}\" from {len(sources)} stor{'y' if len(sources) == 1 else 'ies'} ({len(cands)} candidates)"
    return {"angle": angle, "sources": sources}, summary, {"candidates": cands, "pick": pick, "why": note}


def _with_corroboration(sources: list[dict], cap: int = MAX_SOURCES) -> list[dict]:
    """Add the brief's corroborating links (first one per story) so the researcher has a second source to quote,
    which matters most when the main link is paywalled."""
    extra = []
    for s in sources:
        also = s.get("also") if isinstance(s.get("also"), list) else []
        extra += [{"url": a["url"], "title": s["title"], "source": a.get("source") or s["source"], "summary": "",
                   "origin": s.get("origin", "rss"), "corroborates": s["url"]}
                  for a in also[:1] if isinstance(a, dict) and a.get("url")]
    return (sources + extra)[:max(cap, len(sources))]


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
    if (lg := state.get("logic_report")) and lg.get("version") == v and lg.get("issues"):
        label = "must fix" if lg.get("blocking") else "should fix"
        parts.append(f"# Logic check (logic {lg['logic']}/5, plain words {lg['plain']}/5; {label})\n"
                     + "\n".join(f'- {i["kind"]}: "{i["quote"]}": {i["problem"]} → {i["fix"]}' for i in lg["issues"]))
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
    if top := "\n\n---\n\n".join(state.get("top_posts") or []):
        user += (f"\n\n# {AUTHOR}'s posts that did well with readers (the best guide there is: use the same kind of moves,"
                 f" such as how they stack facts, where the opinion sits and how they end, on today's story. Don't copy their"
                 f" wording or topic)\n{top}")
    if recent := "\n\n---\n\n".join(state.get("recent_posts") or []):
        user += (f"\n\n# {AUTHOR}'s last few posts (already published: don't reuse their hook pattern, structure, "
                 f"stance line or closing move; this post should feel different)\n{recent}")
    user += _take(state) + _lessons(state)
    draft = current(state)
    if draft:
        user += (f"\n\n# Current draft (v{draft['version']})\n{draft['post']}\n\n{_feedback(state)}\n\n"
                 "Fix what the notes mark must-fix, and should-fix where it's easy. Keep every other sentence as it is: "
                 "don't restructure the post or add a new opening or closing line the notes didn't ask for.")
    if state.get("instructions"):
        user += f"\n\n# Coordinator's note\n{state['instructions']}"
    tier = _tier(cfg, "voice")

    def make_system(i: int) -> str:
        examples = drafting.style_examples(n=4, short_only=False, max_words=g.EXAMPLE_MAX_WORDS, seed=f"{state.get('week')}-{i}")
        return drafting.fill(cfg.prompt, AUTHOR=AUTHOR, STYLE_GUIDE=g.style_guide(), HUMANIZER=humanizer.rules(),
                             EXAMPLES="\n\n---\n\n".join(examples) or "(no examples yet; follow the style guide)")
    outs = drafting.write_candidates(ctx, tier, "writer", drafting.candidates(tier, cfg.model, n=2 if draft else None),
                                     make_system, user, _clean, max_words=3 * g.WORDS[1], min_words=int(0.6 * g.WORDS[0]))
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
    if state.get("top_posts"):
        user += ("\n\n# Posts by the author that did well with readers\n" + "\n\n---\n\n".join(state["top_posts"])
                 + "\nA draft that works the way these do (dry one-line reactions included) shouldn't lose points for it.")
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


# --------------------------------------------------------------------------- logic check

LOGIC_KINDS = ("non_sequitur", "guess_as_fact", "unclear_comparison", "contradiction", "vague_ending", "jargon")
MIN_LOGIC = 4  # logic or plain words below this sends the draft back; the owner's own posts score 4-5


def logic_checker(state, ctx, cfg):
    """Does each step follow, are guesses marked, are comparisons clear, are the words plain. Facts are the
    fact-checker's job; this reads the argument the way a reader would."""
    draft = current(state)
    try:
        r = llm.call_json(ctx, _tier(cfg, "judge"), "logic_checker", drafting.fill(cfg.prompt, AUTHOR=AUTHOR),
                          f"# Post\n{draft['post']}", model=cfg.model)
    except ValueError:
        r = {}
    score = lambda k: max(1, min(5, int(r[k]))) if str(r.get(k, "")).isdigit() else 5  # noqa: E731
    issues = [{"kind": i.get("kind"), **{k: str(i.get(k) or "")[:300] for k in ("quote", "problem", "fix")}}
              for i in r.get("issues") or [] if isinstance(i, dict) and i.get("kind") in LOGIC_KINDS][:5]
    logic, plain = score("logic"), score("plain")
    report = {"version": draft["version"], "logic": logic, "plain": plain, "issues": issues,
              "chain": [str(c)[:200] for c in r.get("chain") or [] if isinstance(c, str)][:6],
              "blocking": bool(issues) and min(logic, plain) < MIN_LOGIC}
    cands = dict(state.get("candidates", {}))
    if report["blocking"] and (key := str(draft["version"])) in cands:
        cands[key] = {**cands[key], "logic_issues": [f"logic ({i['kind']}): {i['quote']}" for i in issues]}
    summary = (f"v{draft['version']}: logic {logic}/5, plain words {plain}/5, {len(issues)} issue{'s' * (len(issues) != 1)}"
               + (f". {issues[0]['kind']}: \"{issues[0]['quote'][:80]}\"" if issues else ""))
    return {"logic_report": report, "candidates": cands}, summary, report


# --------------------------------------------------------------------------- final check

def final_check(state, ctx, cfg):
    g, draft = _g(), current(state)
    v = draft["version"]
    blockers = g.guardrail_violations(draft["post"], {s["url"] for s in state["sources"]}, sources_text(state),
                                      state.get("recent_posts"))
    f = state.get("fact_report") or {}
    if f.get("version") == v and f.get("blocking"):
        blockers.append(f"{f['blocking']} unsupported claim{'s' * (f['blocking'] != 1)} from the fact check")
    lg = state.get("logic_report") or {}
    if lg.get("version") == v and lg.get("blocking"):
        blockers.append(f"the argument doesn't hold together (logic {lg['logic']}/5, plain words {lg['plain']}/5): "
                        + "; ".join(f'{i["kind"]} "{i["quote"]}"' for i in lg["issues"][:3]))
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
           "logic_checker": logic_checker, "final_check": final_check, "onepager": onepager}
