"""The LinkedIn team's specialists. Each one is fn(state, ctx, cfg) -> (update, summary, output); see core/orchestrator.py.

scout → researcher → writer ⇄ {editor, fact_checker, final_check}, in the order the coordinator picks.
"""
import hashlib
import json
import re

from core import drafting, humanizer, llm, novelty
from core.budget import BudgetExceeded
from agents.market_intel.roster import AUTHOR, PROMPTS

SCOUT_REPEAT = 0.4  # an angle this close to a recent one loses to a fresher candidate
MAX_SOURCES = 5  # chosen stories plus the brief's corroborating links, for the researcher
PAIRS = 3  # the owner's rewrites shown to each writer candidate (rotated, so no one example gets copied)


def current(state) -> dict | None:
    return state["drafts"][-1] if state.get("drafts") else None


def version(state) -> int:
    return current(state)["version"] if state.get("drafts") else 0


def _g():
    from agents.market_intel import graph  # late: graph imports this module; tests patch graph.fetch_article
    return graph


def _tier(cfg, default: str) -> str:
    return cfg.tier if cfg and cfg.tier else default


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
    shapes = ", ".join(state.get("recent_formats") or []) or "(none yet)"
    top = "\n\n---\n\n".join(state.get("top_posts") or []) or "(no reader feedback yet)"
    r = llm.call_json(ctx, _tier(cfg, "judge"), "scout", drafting.fill(cfg.prompt, AUTHOR=AUTHOR, RECENT=recent, VIEWS=views(),
                                                                      FORMATS=formats(), RECENT_FORMATS=shapes, TOP_POSTS=top),
                      listing(state["items"]) + _take(state), model=cfg.model)
    items = state["items"]
    cands = [c for c in (r.get("candidates") or ([r] if r.get("item_ids") else [])) if isinstance(c, dict)]
    for c in cands:
        c["item_ids"] = [i for i in c.get("item_ids") or [] if isinstance(i, int) and 0 <= i < len(items)][:3]
        text = novelty.words(f"{c.get('angle', '')} {c.get('hook', '')} " + " ".join(items[i]["title"] for i in c["item_ids"]))
        c["repeat"] = round(max([novelty.jaccard(text, novelty.words(a)) for a in state.get("recent_angles") or []] or [0]), 2)
        c["thesis"] = drafting.thesis_contrast(f"{c.get('angle', '')} {c.get('hook', '')}")
        c["passes"] = passes_take_test(c)
        if c.get("format") not in FORMATS or c["format"] in (state.get("recent_formats") or []):
            c["format"] = None  # an unknown or just-used shape: the writer goes without one
    cands = [c for c in cands if c["item_ids"]] or [{"item_ids": [0], "angle": items[0]["title"], "hook": "", "why_now": "",
                                                     "repeat": 0, "passes": False}]
    pick = r.get("pick") if isinstance(r.get("pick"), int) and 0 <= r.get("pick") < len(cands) else 0
    note = str(r.get("why", ""))[:200]
    if not state.get("take") and not cands[pick]["passes"] and (good := [c for c in cands if c["passes"]]):
        pick, note = cands.index(good[0]), f"switched: the first pick's take was a {cands[pick].get('kind') or 'recap'}"
    if not state.get("take") and not cands[pick]["passes"] and state.get("can_skip"):
        why = "; ".join(f"{c.get('kind') or 'no take'}: {str(c.get('take') or c.get('angle'))[:80]}" for c in cands)
        return {"angle": {k: cands[pick].get(k) for k in ANGLE_KEYS}, "skip": f"no take passed the take test ({why})"}, \
            f"No post today: none of {len(cands)} takes passed the take test", {"candidates": cands, "pick": None, "why": why}
    fresh = [candidate for candidate in cands if candidate["repeat"] < SCOUT_REPEAT and candidate["passes"] >= cands[pick]["passes"]]
    if cands[pick]["repeat"] >= SCOUT_REPEAT and fresh:
        fresher = min(fresh, key=lambda candidate: (not candidate["passes"], candidate["repeat"]))
        pick, note = cands.index(fresher), f"switched: the first pick was too close to a recent post ({cands[pick]['repeat']})"
    if cands[pick].get("thesis") and (plain := [c for c in cands if not c.get("thesis") and c["repeat"] < SCOUT_REPEAT
                                                and c["passes"] >= cands[pick]["passes"]]):
        pick, note = cands.index(plain[0]), "switched: the first pick was an \"X, not Y\" thesis line"
    angle = {k: cands[pick].get(k) for k in ANGLE_KEYS}
    if state.get("take"):
        angle["take"] = state["take"]
    sources = _with_corroboration([items[i] for i in cands[pick]["item_ids"]])
    summary = f"Angle: \"{angle['angle']}\" from {len(sources)} stor{'y' if len(sources) == 1 else 'ies'} ({len(cands)} candidates)"
    return {"angle": angle, "sources": sources}, summary, {"candidates": cands, "pick": pick, "why": note}


ANGLE_KEYS = ("angle", "take", "detail", "format", "hook", "why_now", "view")
WORDS_PER_SOURCE = 700  # the best posts' details came from deep in the article; 1,200 words split five ways didn't reach them


def views() -> str:
    return (PROMPTS / "views.md").read_text().strip()


def formats() -> str:
    return (PROMPTS / "formats.md").read_text().strip()


FORMATS = {line.split(":", 1)[0][2:] for line in formats().splitlines() if line.startswith("- ") and ":" in line}


def format_line(name: str | None) -> str:
    """The one shape the scout chose, as the writer sees it."""
    return next((l[2:] for l in formats().splitlines() if name and l.startswith(f"- {name}:")), "")


def passes_take_test(c: dict) -> bool:
    """The scout's own verdict, checked: a point of view (a reaction or a claim, not a recap, a forecast or a guess
    at a motive) built on a specific detail. Rejected drafts were mostly recaps with a "why it matters" line; the two
    posts that did best were reported stories with one striking detail and a dry one-line reaction."""
    return (c.get("kind") in ("reaction", "claim") and all(len(str(c.get(k) or "").split()) >= 4 for k in ("take", "detail"))
            and not re.search(r"\bby 20\d\d\b|\b(?:every|all|the whole) [\w ]{0,20}\bwill\b", str(c.get("take") or ""), flags=re.I))


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
    sources = drafting.read_sources(state["sources"], words=WORDS_PER_SOURCE * len(state["sources"]), fetch=_g().fetch_article)
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
    angle = dict(state["angle"])  # with the articles read, the researcher sharpens the detail and the take
    for k in ("detail", "take"):
        if isinstance(r.get(k), str) and len(r[k].split()) >= 4 and not (k == "take" and state.get("take")):
            angle[k] = r[k].strip()[:300]
    read = sum(bool(s.get("body")) for s in sources)
    summary = (f"Fact sheet: {len(facts)} facts with quotes from {len(sources)} sources ({read} read in full)"
               + (f"; dropped {dropped} without a matching quote" if dropped else ""))
    return ({"sources": sources, "facts": facts, "context": context, "angle": angle, "researched": True}, summary,
            {"facts": facts, "context": context, "detail": angle.get("detail"), "take": angle.get("take")})


# --------------------------------------------------------------------------- writer

def _feedback(state) -> str:
    v, parts = version(state), []
    if (e := state.get("edit_report")) and e.get("version") == v:
        fixes = "\n".join(f'- {x["kind"]}: "{x["quote"]}": {x["problem"]} → {x["fix"]}' for x in e.get("fixes") or [])
        priority = " (must fix)" if weak(e) else ""
        parts.append(f"# Editor{priority} (overall {e['overall']}/5, scores {json.dumps(e['scores'])})\n{fixes or e.get('critique', '')}")
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


TARGET_WORDS = (150, 220)


def saved_examples() -> list[dict]:
    return json.loads((PROMPTS.parent / "voice_examples.json").read_text())


def pairs(examples: list[dict], seed: str, n: int = PAIRS) -> str:
    """The owner's rewrites as before/after pairs, a different PAIRS of them per seed."""
    if not examples:
        return "(none yet; follow the voice guide)"
    picked = sorted(examples, key=lambda e: hashlib.sha1(f"{seed}{e['id']}".encode()).hexdigest())[:n]
    return "\n\n".join(f"## Pair {k}\n### The agent's draft\n{e['draft']}\n\n### What {AUTHOR} posted\n{e['post']}"
                       for k, e in enumerate(picked, 1))


def review_prompt(template: str, state) -> str:
    return drafting.fill(template, AUTHOR=AUTHOR, STYLE_GUIDE=_g().style_guide(),
                         EXAMPLES=pairs(state.get("examples") or [], str(state.get("week")), n=1))


def choose_draft(state, ctx, best: dict, scored: list[dict]) -> tuple[dict, str]:
    """Compare distinct clean drafts blind. Keep the deterministic pick if the optional read is unavailable."""
    clean = {candidate["text"]: candidate for candidate in scored if candidate["score"][0] == 0}
    if len(clean) < 2:
        return best, ""
    options = list(clean.values())
    posts = "\n\n".join(f"# Candidate {index}\n{option['text']}" for index, option in enumerate(options, 1))
    user = (f"# Sources\n{sources_text(state)}\n\n# Intended take\n{state['angle'].get('take') or state['angle'].get('angle')}"
            + _take(state) + _lessons(state) + "\n\n" + _feedback(state) + f"\n\n{posts}")
    editor_cfg = state.get("settings", {}).get("editor")
    system = review_prompt((PROMPTS / "choose_draft.md").read_text(), state)
    try:
        reply = llm.call(ctx, _tier(editor_cfg, "review"), "choose_draft", system, user,
                         model=editor_cfg.model if editor_cfg else None)
        result = llm.parse_json(reply)
    except BudgetExceeded:
        raise
    except Exception:
        return best, ""
    choice = result.get("choice") if isinstance(result, dict) else None
    if type(choice) is int and 1 <= choice <= len(options):
        return options[choice - 1], str(result.get("reason") or "Selected by voice review")[:300]
    return best, ""


def writer(state, ctx, cfg):
    """Generate candidates, filter with guardrails, then compare clean alternatives against the author's edits."""
    g = _g()
    a = state["angle"]
    user = (f"Angle: {a.get('angle')}\nThe take (the post's one idea): {a.get('take') or a.get('angle')}\n"
            f"The striking detail: {a.get('detail') or '(see the fact sheet)'}\n"
            + (f"The shape (use it, loosely): {format_line(a.get('format'))}\n" if a.get("format") else "")
            + f"\n# Fact sheet\n{fact_sheet(state)}\n\n# Source items\n"
            # summaries only: the researcher already pulled the facts and quotes out of the article text, and with
            # 700 words a source in front of it the writer copied whole sentences and picked up stray figures
            f"{drafting.sources_block([{k: v for k, v in x.items() if k != 'body'} for x in state['sources']])}")
    if top := "\n\n---\n\n".join(state.get("top_posts") or []):
        user += (f"\n\n# {AUTHOR}'s posts that did well with readers (notice how he explains the evidence. "
                 f"Use the current voice guide for the opening, order and ending)\n{top}")
    if recent := "\n\n---\n\n".join(state.get("recent_posts") or []):
        user += (f"\n\n# {AUTHOR}'s last few posts (avoid repeating their topics and wording; "
                 f"keep his usual structure)\n{recent}")
    user += _take(state) + _lessons(state)
    draft = current(state)
    if draft:
        user += (f"\n\n# Current draft (v{draft['version']})\n{draft['post']}\n\n{_feedback(state)}\n\n"
                 "Address the required edits first. Move or rewrite the surrounding sentences if the reader needs "
                 "them to follow the point. Preserve passages that already work.")
    if state.get("instructions"):
        user += f"\n\n# Coordinator's note\n{state['instructions']}"
    tier = _tier(cfg, "voice")

    def make_system(i: int) -> str:
        return drafting.fill(cfg.prompt, AUTHOR=AUTHOR, STYLE_GUIDE=g.style_guide(),
                             HUMANIZER=humanizer.rules() if "{{HUMANIZER}}" in cfg.prompt else "",
                             EXAMPLES=pairs(state.get("examples") or [], f"{state.get('week')}-{i}"))
    outs = drafting.write_candidates(ctx, tier, "writer", drafting.candidates(tier, cfg.model, n=2 if draft else None),
                                     make_system, user, _clean, max_words=3 * g.WORDS[1], min_words=int(0.6 * g.WORDS[0]))
    urls, text = {s["url"] for s in state["sources"]}, sources_text(state)

    def score(post: str) -> tuple:
        flags = g.guardrail_violations(post, urls, text, state.get("recent_posts"))
        words = len(post.split())
        return len(flags), -humanizer.scan(post)["score"], max(TARGET_WORDS[0] - words, words - TARGET_WORDS[1], 0)
    best, scored = drafting.pick(outs, score)
    best, reason = choose_draft(state, ctx, best, scored)
    post, n = best["text"], version(state) + 1
    short = lambda m: m.split("/")[-1]  # noqa: E731
    summary = (f"{'Rewrote' if draft else 'Wrote'} v{n}: {len(post.split())} words"
               + (f", best of {len(scored)} ({', '.join(short(d['model']) for d in scored)}): kept {short(best['model'])}"
                  if len(scored) > 1 else ""))
    out = {"version": n, "post": post, "model": best["model"], "selection": reason or "Automatic checks",
           "candidates": [{"model": d["model"], "flags": d["score"][0], "humanizer": -d["score"][1], "words": len(d["text"].split())}
                          for d in scored]}
    return {"drafts": (state.get("drafts") or []) + [{"version": n, "post": post}]}, summary, out


# --------------------------------------------------------------------------- editor

def editor(state, ctx, cfg):
    g, draft = _g(), current(state)
    user = f"# Source items\n{sources_text(state)}" + _take(state) + _lessons(state) + f"\n\n# Post\n{draft['post']}"
    review = llm.call_json(ctx, _tier(cfg, "review"), "editor", review_prompt(cfg.prompt, state), user, model=cfg.model)
    scores = {k: max(1, min(5, int(v))) for k, v in (review.get("scores") or {}).items() if str(v).isdigit()}
    fixes = [{"kind": str(x.get("kind") or ""), **{k: str(x.get(k) or "")[:300] for k in ("quote", "problem", "fix")}}
             for x in review.get("fixes") or [] if isinstance(x, dict)][:4]
    scores, fixes, jargon = cap_plain(scores, fixes, review.get("unexplained"), draft["post"])
    critique = " ".join(f'"{x["quote"]}": {x["problem"]} → {x["fix"]}' if x["quote"] else f'{x["problem"]} → {x["fix"]}'
                        for x in fixes) or str(review.get("critique", ""))
    violations = g.guardrail_violations(draft["post"], {s["url"] for s in state["sources"]}, sources_text(state),
                                        state.get("recent_posts"))
    accent = humanizer.scan(draft["post"])
    overall = drafting.overall_score(scores, accent["score"])
    report = {"version": draft["version"], "scores": scores, "critique": critique, "fixes": fixes, "unexplained": jargon,
              "unsupported": [c for c in review.get("unsupported_claims") or [] if isinstance(c, str)],
              "violations": violations, "accent": accent, "overall": overall}
    entry = {**report, "post": draft["post"]}
    summary = (f"v{draft['version']}: overall {overall}/5 (humanizer {accent['score']}/5), {len(violations)} "
               f"guardrail flag{'s' * (len(violations) != 1)}. {report['critique'][:140]}")
    out = {k: report[k] for k in ("version", "scores", "critique", "fixes", "unexplained", "unsupported", "violations", "overall")}
    return {"edit_report": report, "candidates": {**state.get("candidates", {}), str(draft["version"]): entry}}, summary, out


def cap_plain(scores: dict, fixes: list[dict], unexplained, post: str) -> tuple[dict, list[dict], list[str]]:
    """The editor's own plain score ran high on posts full of jargon (it gave 4/5 to a post that never explained
    Lean, GRH or 'axioms'). So it lists the unexplained terms, and two or more cap plain at 2, which sends the draft
    back. One term is only a fix: his own posts use a field word or two ("harness") and he posts them as is.
    Terms that aren't in the post are dropped."""
    terms = [t for t in dict.fromkeys(str(t).strip() for t in unexplained or [] if str(t).strip())
             if t.lower() in post.lower()][:8]
    if not terms:
        return scores, fixes, []
    if len(terms) > 1:
        scores = {**scores, "plain": min(scores.get("plain", 5), 2)}
    noted = " ".join(f["quote"] + " " + f["fix"] for f in fixes if f["kind"] == "plain").lower()
    extra = {"kind": "plain", "quote": ", ".join(t for t in terms if t.lower() not in noted)[:300],
             "problem": "A reader outside this field won't know these terms, and the post doesn't explain them.",
             "fix": "Explain each one in a few everyday words where it first appears, or cut it."}
    return scores, (([extra] if extra["quote"] else []) + fixes)[:4], terms


VOICE_SCORES = ("voice", "ending")
MUST_BE_HIGH = ("grounding", "take", "logic", "plain", *VOICE_SCORES)


def weak(report: dict) -> bool:
    return drafting.weak_scores(report["scores"], report["accent"]["score"], must_be_high=MUST_BE_HIGH)


# --------------------------------------------------------------------------- fact checker

def fact_checker(state, ctx, cfg):
    draft = current(state)
    note = f"\n\n# {AUTHOR}'s note for this story\n{state['take']}" if state.get("take") else ""
    try:
        r = llm.call_json(ctx, _tier(cfg, "check"), "fact_checker", drafting.fill(cfg.prompt, AUTHOR=AUTHOR),
                          f"{sources_text(state)}{note}\n\n# Post\n{draft['post']}", model=cfg.model)
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
    edit = state.get("edit_report") or {}
    if edit.get("version") == v:
        for dimension in VOICE_SCORES:
            score = edit["scores"].get(dimension)
            if score is not None and score < drafting.MIN_SCORE:
                blockers.append(f"Editor rated {dimension} {score}/5; needs {drafting.MIN_SCORE}/5")
    f = state.get("fact_report") or {}
    if f.get("version") == v and f.get("blocking"):
        blockers.append(f"{f['blocking']} unsupported claim{'s' * (f['blocking'] != 1)} from the fact check")
    try:
        r = llm.call_json(ctx, _tier(cfg, "check"), "final_check", drafting.fill(cfg.prompt, AUTHOR=AUTHOR),
                          f"# Source items\n{sources_text(state)}\n\n# Post\n{draft['post']}", model=cfg.model)
    except ValueError:
        r = {"passed": True, "blockers": [], "polish": ["the final read returned nothing usable"]}
    if r.get("passed") is False:
        blockers += [str(b)[:300] for b in r.get("blockers") or [] if isinstance(b, str)] or ["the final read didn't pass it"]
    report = {"version": v, "passed": not blockers, "blockers": blockers,
              "polish": [str(p)[:300] for p in r.get("polish") or [] if isinstance(p, str)][:4]}
    cands = dict(state.get("candidates", {}))
    if (key := str(v)) in cands:
        cands[key] = {**cands[key], "passed_final": report["passed"], "final_blockers": blockers}
    summary = f"v{v}: " + ("passed" if report["passed"] else f"{len(blockers)} blocker{'s' * (len(blockers) != 1)}: {blockers[0][:120]}")
    return {"final_report": report, "candidates": cands}, summary, report


RUNNERS = {"scout": scout, "researcher": researcher, "writer": writer, "editor": editor, "fact_checker": fact_checker,
           "final_check": final_check}
