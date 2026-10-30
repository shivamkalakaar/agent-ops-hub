"""Learning loop: what the owner's decisions in /queue teach each agent.

Every draft the owner approves (as written or with edits) or rejects (with a reason chip and an optional note)
is read once, at the start of the agent's next run. A free model turns the decisions into short, specific lessons
("cuts expert quotes and keeps their own reaction") and merges them with what's already known: new lessons are
added, ones the new decisions back up gain evidence, wrong ones are revised or retired. At most MAX_ACTIVE stay
active; the writer and the critic read them on every draft, and the owner can switch one off from the dashboard.
"""
import difflib
import json
import sys
from pathlib import Path

from core import drafting, llm
from core.db import new_id, now

MAX_ACTIVE = 15
# Per agent: how many lessons stay active and how many decisions must back one before the writer sees it. The
# LinkedIn agent's 19 one-off lessons contradicted each other (three different required endings), so its lessons
# need two decisions behind them and stay few; its voice guide (agents/market_intel/prompts/voice.md) is the base.
LIMITS = {"market_intel": {"max_active": 8, "min_evidence": 2}}
MAX_EVENTS = 8  # decisions read per run; the rest wait for the next run
prompt = drafting.prompt_loader(Path(__file__).parent / "prompts")

# Per agent: the drafts table and how to read one draft's text, the owner's final text and the headline.
TABLES = {
    "market_intel": ("drafts", lambda d: d["post_md"], lambda d: d.get("edited_post_md"), lambda d: ""),
    "substack": ("substack_drafts",
                 lambda d: f"# {d['title']}\n\n{d['body_md']}",
                 lambda d: (f"# {d.get('edited_title') or d['title']}\n\n{d['edited_body_md']}" if d.get("edited_body_md") else None),
                 lambda d: d["title"]),
}


def _diff(a: str, b: str, limit: int = 60) -> str:
    lines = list(difflib.unified_diff(a.splitlines(), b.splitlines(), "agent's draft", "owner's version", lineterm="", n=0))
    return "\n".join(lines[2:limit + 2]) + ("\n…" if len(lines) > limit + 2 else "")


def edit_share(draft: str, final: str | None) -> float:
    """How much of the draft the owner changed, 0-1 (0 = approved as written)."""
    return 0.0 if not final else round(1 - difflib.SequenceMatcher(None, draft.split(), final.split()).ratio(), 2)


def events(db, agent: str) -> list[dict]:
    table, text, final, _ = TABLES[agent]
    rows = db.all(f"select * from {table} where status in ('approved','published','rejected') and learned_at is null"
                  " order by created_at limit %s", (MAX_EVENTS,))
    out = []
    for d in rows:
        fb = d.get("feedback") or {}
        fb = json.loads(fb) if isinstance(fb, str) else fb
        e = {"draft_id": d["id"], "decision": "rejected" if d["status"] == "rejected" else "approved",
             "draft": text(d)[:6000]}
        if (f := final(d)) and f.strip() != text(d).strip():
            e["owner_edits"] = _diff(text(d), f)
            e["edited_share"] = edit_share(text(d), f)
        elif e["decision"] == "approved":
            e["note"] = "approved as written"
        if fb.get("reasons"):
            e["reasons"] = fb["reasons"]
        if fb.get("note"):
            e["owner_note"] = fb["note"]
        if d.get("my_take"):
            e["owner_take"] = d["my_take"]
        if fb.get("performance"):  # set after publishing: how readers responded
            e["performance"] = {"well": "did well with readers", "average": "did about average with readers",
                                "poorly": "didn't land with readers"}.get(fb["performance"], fb["performance"])
        out.append(e)
    return out


def active(db, agent: str) -> list[dict]:
    return db.all("select id, lesson, kind, evidence from lessons where agent=%s and active order by evidence desc, updated_at desc",
                  (agent,))


def learn(db, ctx, agent: str, author: str) -> dict:
    """Read new decisions and update the lessons. Never raises: learning must not stop a draft."""
    try:
        evs = events(db, agent)
        if not evs:
            return {"read": 0}
        known = active(db, agent)
        user = (f"# Current lessons\n{json.dumps(known, ensure_ascii=False) if known else '(none yet)'}\n\n"
                f"# New decisions by {author}\n{json.dumps(evs, ensure_ascii=False, indent=1)}")
        ops = llm.call_json(ctx, "judge", "learn", prompt("learn", AUTHOR=author,
                                                          KIND="LinkedIn posts" if agent == "market_intel" else "Substack posts"), user)
        stats = apply(db, agent, ops, {e["draft_id"] for e in evs},
                      strong={e["draft_id"] for e in evs if e.get("performance") == "did well with readers"})
        table = TABLES[agent][0]
        for e in evs:
            db.execute(f"update {table} set learned_at=%s where id=%s", (now(), e["draft_id"]))
        return {"read": len(evs), **stats}
    except Exception as ex:  # noqa: BLE001
        print(f"lessons: learning skipped: {type(ex).__name__}: {ex}", file=sys.stderr)
        return {"read": 0, "error": str(ex)[:200]}


def apply(db, agent: str, ops: dict, draft_ids: set[str], strong: set[str] | None = None) -> dict:
    """strong: drafts readers responded well to; lessons drawn from them start with extra evidence."""
    known = {l["id"]: l for l in active(db, agent)}
    added = confirmed = revised = retired = 0
    for a in (ops.get("add") or [])[:5]:
        text = str(a.get("lesson") or "").strip()
        if 8 <= len(text) <= 300:
            src = [i for i in a.get("from") or [] if i in draft_ids]
            weight = max(1, len(src)) + 2 * len(set(src) & (strong or set()))  # readers' response counts for more
            db.execute("insert into lessons (id, agent, lesson, kind, evidence, draft_ids, active, created_at, updated_at)"
                       " values (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       (new_id(), agent, text, str(a.get("kind") or "voice")[:20], weight, db.json(src), True, now(), now()))
            added += 1
    boost = 3 if strong else 1  # a post readers responded well to backs a lesson more than an approval does
    for i in ops.get("confirm") or []:
        if i in known:
            db.execute("update lessons set evidence=evidence+%s, updated_at=%s where id=%s", (boost, now(), i))
            confirmed += 1
    for r in ops.get("revise") or []:
        text = str(r.get("lesson") or "").strip()
        if r.get("id") in known and 8 <= len(text) <= 300:
            db.execute("update lessons set lesson=%s, updated_at=%s where id=%s", (text, now(), r["id"]))
            revised += 1
    for i in ops.get("retire") or []:
        if i in known:
            db.execute("update lessons set active=%s, updated_at=%s where id=%s", (False, now(), i))
            retired += 1
    for extra in active(db, agent)[LIMITS.get(agent, {}).get("max_active", MAX_ACTIVE):]:  # keep the best-supported ones
        db.execute("update lessons set active=%s, updated_at=%s where id=%s", (False, now(), extra["id"]))
        retired += 1
    return {"added": added, "confirmed": confirmed, "revised": revised, "retired": retired}


def block(db, agent: str, author: str) -> str:
    """The active lessons, for the writer's and the critic's prompts ('' when there are none yet)."""
    rows = [r for r in active(db, agent) if r["evidence"] >= LIMITS.get(agent, {}).get("min_evidence", 1)]
    if not rows:
        return ""
    lines = "\n".join(f"- {r['lesson']}" + (f" (seen {r['evidence']}×)" if r["evidence"] > 1 else "") for r in rows)
    return (f"# What {author}'s feedback has taught you (from drafts they approved, edited or rejected)\n"
            f"These come from the actual person and refine the voice guide; follow them where they apply.\n{lines}")
