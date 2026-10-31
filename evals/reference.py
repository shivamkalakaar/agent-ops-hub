"""A fixed yardstick for the writing agents: an independent judge, calibrated on Shivam's own edits, and frozen
reference cases (the stories behind posts he published, with his final version).

uv run python -m evals.reference export [draft_id ...]   # write evals/reference/<id>.json from the drafts table
uv run python -m evals.reference calibrate               # how often the judge agrees with Shivam (~$0.07)

The judge never reads the agents' editor prompts or settings: judge.md / compare.md, a pinned model and the rules
below are the whole yardstick, and `version` (a hash of all three) changes whenever any of them does, so a baseline
recorded under one version is never compared with scores from another.
"""
import hashlib
import json
import re
import sys
from pathlib import Path
from statistics import mean

from core import drafting, humanizer, llm

HERE = Path(__file__).parent
CASES = HERE / "reference"
JUDGE_MODEL = "deepseek/deepseek-v4.1-flash"  # pinned, not the judge tier's primary; a different family from the GLM writer
DIMENSIONS = ("plain", "logic", "one_idea", "hook", "grounded", "human")
KINDS = {"linkedin": "short LinkedIn post", "substack": "Substack essay"}
LIMITS = {"linkedin": {"chars": 1400, "words": (60, 230), "hashtags": 4}, "substack": {"chars": 12000, "words": (500, 1800), "hashtags": 0}}
# The LinkedIn posts that were edited and published, picked for clear edits on different topics (see export()).
PICKS = {"4a50be497600": "inference: same open weights, different provider, 15-point gap (edited, performed well)",
         "942e36b0112d": "business: open models' token share vs revenue share (edited for plain words and logic)",
         "977b5049c201": "compute finance: neocloud debt at 15% yields (published as is, performed well)",
         "3fcc2862c829": "applications: fine-tuned 2015 CNN beats frontier models on skin lesions (edited hook and ending)"}


def _prompt(name: str, kind: str) -> str:
    return (HERE / f"{name}.md").read_text().replace("{{KIND}}", KINDS[kind])


def version() -> str:
    body = (HERE / "judge.md").read_text() + (HERE / "compare.md").read_text() + JUDGE_MODEL + json.dumps(LIMITS)
    return hashlib.sha256(body.encode()).hexdigest()[:10]


def rules(post: str, kind: str = "linkedin") -> list[str]:
    """Hard rules with fixed limits (set from Shivam's own published posts, not the agents' guardrails, which move)."""
    lim, words, v = LIMITS[kind], len(post.split()), []
    if len(post) > lim["chars"]:
        v.append(f"{len(post)} characters > {lim['chars']}")
    if not lim["words"][0] <= words <= lim["words"][1]:
        v.append(f"{words} words, outside {lim['words'][0]}-{lim['words'][1]}")
    if kind == "linkedin" and len(re.findall(r"(?<!\w)#\w+", post)) > lim["hashtags"]:
        v.append(f"more than {lim['hashtags']} hashtags")
    if kind == "linkedin" and re.search(r"https?://", post):
        v.append("link in the post body")
    if (h := humanizer.scan(post)["score"]) < humanizer.MIN_HUMAN:
        v.append(f"humanizer {h}/5")
    return v


def _sources(sources: list[dict] | str) -> str:
    return sources if isinstance(sources, str) else drafting.sources_block(sources)


def _score(v) -> int:
    try:
        return max(1, min(5, round(float(v))))
    except (TypeError, ValueError):
        return 1


def judge(ctx, post: str, sources: list[dict] | str, kind: str = "linkedin") -> dict:
    """Absolute score, 1-5 per dimension. `flags` are hard-rule breaks plus an ungrounded post (grounded <= 2)."""
    r = _call(ctx, "reference_judge", _prompt("judge", kind), f"# Sources\n{_sources(sources)}\n\n# The post\n{post}")
    raw = r.get("scores") if isinstance(r.get("scores"), dict) else {}
    scores = {k: _score(raw.get(k)) for k in DIMENSIONS}
    flags = rules(post, kind) + ([f"not grounded: {'; '.join(map(str, r.get('unsupported') or []))[:160]}"] if scores["grounded"] <= 2 else [])
    return {"overall": round(mean(scores.values()), 2), "scores": scores, "flags": flags, "chain": r.get("chain") or [],
            "unsupported": r.get("unsupported") or [], "fix": str(r.get("fix") or ""), "version": version()}


def _call(ctx, node: str, system: str, user: str, tries: int = 2) -> dict:
    """The pinned model now and then returns an empty reply; one retry, then raise (the judge) or no verdict (a pick)."""
    for n in range(tries):
        try:
            return llm.call_json(ctx, "judge", node, system, user, model=JUDGE_MODEL)
        except ValueError:
            if n == tries - 1:
                raise


def _pick(ctx, a: str, b: str, sources: str, kind: str) -> tuple[str, str]:
    try:
        r = _call(ctx, "reference_compare", _prompt("compare", kind), f"# Sources\n{sources}\n\n# Post A\n{a}\n\n# Post B\n{b}")
    except ValueError:
        return "tie", "no verdict: the judge returned nothing twice"
    better = str(r.get("better", "")).strip().upper()
    return (better if better in ("A", "B") else "tie"), str(r.get("why") or "")[:240]


def compare(ctx, draft: str, reference: str, sources: list[dict] | str, kind: str = "linkedin") -> dict:
    """Draft vs Shivam's post, judged in both orders to cancel position bias. win/loss only when both orders agree;
    `score` is +1 (draft better both times) to -1 (reference better both times)."""
    text = _sources(sources)
    (first, why1), (second, why2) = _pick(ctx, draft, reference, text, kind), _pick(ctx, reference, draft, text, kind)
    score = ({"A": 1, "B": -1}.get(first, 0) + {"A": -1, "B": 1}.get(second, 0)) / 2
    return {"result": "win" if score == 1 else "loss" if score == -1 else "tie", "score": score,
            "orders": [first, second], "why": [why1, why2], "version": version()}


def summarize(rows: list[dict]) -> dict:
    """rows: one per case, {"case", "judge": judge(), "compare": compare() or None}. `vs_shivam` is the share of
    judge orders that preferred the agent's draft over Shivam's post (0.5 = as good as Shivam; his own edits beat
    the agent's originals 9 of 12 times in calibration, so today's agent sits near 0.25)."""
    judged, compared = [r["judge"] for r in rows], [r["compare"] for r in rows if r.get("compare")]
    return {"version": version(), "cases": len(rows), "judge_mean": round(mean(j["overall"] for j in judged), 2),
            "per_dimension": {k: round(mean(j["scores"][k] for j in judged), 2) for k in DIMENSIONS},
            "flagged": [r["case"] for r in rows if r["judge"]["flags"]],
            **({"kept": round(mean(k), 2)} if (k := [r["kept"] for r in rows if r.get("kept") is not None]) else {}),
            **({"vs_shivam": round(mean((c["score"] + 1) / 2 for c in compared), 2), "vs_shivam_cases": len(compared),
                "results": {k: sum(c["result"] == k for c in compared) for k in ("win", "tie", "loss")}} if compared else {})}


TOLERANCE, VS_SHIVAM_DROP, KEPT_DROP, MAX_FLAGGED = 0.35, 0.25, 0.1, 1
# An absolute floor too: the first baseline sat at 0.00 (three losses), where a relative drop can never fail.
# 0.25 = on average a tie on half the cases, a loss on the rest.
MIN_VS_SHIVAM = 0.25


def gate(summary: dict, baseline: dict | None) -> list[str]:
    """Reasons to fail (empty = pass). Scores are only compared with a baseline from the same judge version; a
    single noisy case can't fail the gate on its own: the mean and the vs-Shivam share move with all cases, and one
    flagged case is allowed."""
    reasons, same = [], bool(baseline) and baseline.get("version") == summary["version"]
    allowed = max(MAX_FLAGGED, len(baseline["flagged"])) if same else MAX_FLAGGED  # gate regressions, not today's level
    if len(summary["flagged"]) > allowed:
        reasons.append(f"{len(summary['flagged'])} cases break a hard rule or aren't grounded (baseline allows {allowed}): "
                       f"{', '.join(summary['flagged'])}")
    if "vs_shivam" in summary and summary["vs_shivam"] < MIN_VS_SHIVAM:
        reasons.append(f"preferred over Shivam's post {summary['vs_shivam']:.2f} of the time; the floor is {MIN_VS_SHIVAM:.2f}")
    if not same:
        return reasons
    if summary["judge_mean"] < baseline["judge_mean"] - TOLERANCE:
        reasons.append(f"judge mean {summary['judge_mean']} is below baseline {baseline['judge_mean']} - {TOLERANCE}")
    if "vs_shivam" in summary and "vs_shivam" in baseline and summary["vs_shivam"] < baseline["vs_shivam"] - VS_SHIVAM_DROP:
        reasons.append(f"preferred over Shivam's post {summary['vs_shivam']:.2f} of the time, baseline {baseline['vs_shivam']:.2f}")
    if "kept" in summary and "kept" in baseline and summary["kept"] < baseline["kept"] - KEPT_DROP:
        reasons.append(f"Shivam would keep {summary['kept']:.0%} of the drafts, baseline {baseline['kept']:.0%}")
    return reasons


def reference_cases(kind: str = "linkedin") -> list[dict]:
    """Frozen cases, oldest first: {id, kind, topic, items (golden-day shape, with frozen article `body`), reference, ...}."""
    cases = [json.loads(p.read_text()) for p in sorted(CASES.glob("*.json"))]
    return sorted((c for c in cases if c.get("kind", "linkedin") == kind), key=lambda c: c["created_at"])


def frozen_fetch(items: list[dict]):
    """A fetch_article stand-in that returns each item's frozen article text, so a case reads the same pages every run."""
    bodies = {i["url"]: i.get("body") or "" for i in items}
    return lambda url: bodies.get(url, "")


def _load(v):
    return json.loads(v) if isinstance(v, str) else v


def draft_items(db, d: dict) -> list[dict]:
    """A draft's stories as eval items: the items-table row when its title matches the draft's source (a brief can
    reuse one URL for two stories), one item per story."""
    out, titles = [], set()
    for s in _load(d["sources"]) or []:
        it = db.item(s["url"])
        if s["title"] in titles or (it and it["title"] != s["title"]):
            continue
        titles.add(s["title"])
        it = it or {}
        out.append({"url": s["url"], "title": s["title"], "summary": it.get("summary") or "", "why": it.get("why") or "",
                    "source": s.get("source") or it.get("source") or "", "layer": it.get("layer") or "business",
                    "importance": int(it.get("importance") or 4), "origin": s.get("origin") or it.get("origin") or "brief",
                    **({"also": _load(it["also"])} if it.get("also") else {})})
    return out


def export(db, ids: list[str], fetch=None) -> list[Path]:
    from core.feeds import fetch_article
    CASES.mkdir(exist_ok=True)
    paths = []
    for draft_id in ids:
        d = db.draft(draft_id)
        items = draft_items(db, d)
        items = drafting.read_sources(items, fetch=fetch or fetch_article)
        fb = _load(d["feedback"]) or {}
        case = {"id": d["id"], "kind": "linkedin", "topic": PICKS.get(d["id"], ""), "created_at": str(d["created_at"])[:19],
                "linkedin_url": d["linkedin_url"], "performance": fb.get("performance"), "items": items,
                "reference": d["edited_post_md"] or d["post_md"], "agent_post": d["post_md"]}
        path = CASES / f"{d['id']}.json"
        path.write_text(json.dumps(case, indent=1, ensure_ascii=False) + "\n")
        paths.append(path)
        print(f"{path.name}: {len(items)} items, {sum(bool(i.get('body')) for i in items)} with article text")
    return paths


def calibrate(ctx, db, fetch=None) -> dict:
    """Agreement with Shivam on real drafts: pairwise on his edits (agent original vs his version), and absolute
    scores for what he published, what he edited, and what he rejected."""
    rows = db.all("select * from drafts where sources is not null order by created_at")
    groups, pairs = {"rejected": [], "published": [], "agent_original_of_edited": [], "shivam_edited": [], "well": []}, []
    for d in rows:
        items = drafting.read_sources(draft_items(db, d), fetch=fetch) if fetch else draft_items(db, d)
        edited = d["edited_post_md"] and len(set(d["edited_post_md"].split()) ^ set(d["post_md"].split())) > 10
        perf = (_load(d["feedback"]) or {}).get("performance")
        jobs = ([("rejected", d["post_md"])] if d["status"] == "rejected" else
                [("agent_original_of_edited", d["post_md"]), ("shivam_edited", d["edited_post_md"])] if edited else
                [("published", d["post_md"])] if d["status"] == "approved" else [])
        for group, post in jobs:
            j = judge(ctx, post, items)
            groups[group].append({"id": d["id"], **{k: j[k] for k in ("overall", "scores", "flags")}})
            if perf == "well" and group in ("published", "shivam_edited"):
                groups["well"].append({"id": d["id"], "overall": j["overall"]})
            print(f"{group:26} {d['id']} {j['overall']} {j['scores']} {j['flags'] or ''}")
        if edited:
            c = compare(ctx, d["post_md"], d["edited_post_md"], items)
            pairs.append({"id": d["id"], **c})
            print(f"{'pair':26} {d['id']} agent draft vs Shivam: {c['result']} (orders {c['orders']})")
    means = {g: round(mean(r["overall"] for r in v), 2) for g, v in groups.items() if v}
    picks = [o for p in pairs for o in p["orders"]]
    out = {"version": version(), "means": means, "pairs": len(pairs),
           "prefers_shivam": round(sum(p["result"] == "loss" for p in pairs) / max(1, len(pairs)), 2),
           "orders_prefer_shivam": f"{sum(o == ('B', 'A')[n % 2] for n, o in enumerate(picks))}/{len(picks)}",
           "pairs_detail": pairs, "groups": groups}
    print(json.dumps({k: out[k] for k in ("version", "means", "pairs", "prefers_shivam", "orders_prefer_shivam")}, indent=1))
    return out


def main(argv: list[str]) -> int:
    from core.db import connect
    cmd, args = (argv[0] if argv else "calibrate"), argv[1:]
    with connect() as db:
        if cmd == "export":
            export(db, args or list(PICKS))
            return 0
        run_id = db.start_run("evals", "calibrate", version())
        ctx = llm.RunContext(db=db, run_id=run_id)
        from core.feeds import fetch_article
        out = calibrate(ctx, db, fetch_article)
        db.finish_run(run_id, "succeeded", stats={k: v for k, v in out.items() if k != "groups"})
        (HERE / "results").mkdir(exist_ok=True)
        (HERE / "results" / "calibration.json").write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
