#!/usr/bin/env python3
"""grill.py - question bank, coverage and spec tools for the grill-me skill.

Picks the interview questions that change the build most, tracks what is settled,
turns skipped questions into listed assumptions, validates a build spec, renders it
as Markdown or printable HTML, and prints the post-build acceptance checklist.

Examples:
  python3 scripts/grill.py types --detect "I need a landing page for my course"
  python3 scripts/grill.py ask --type website --round 1 --n 5
  python3 scripts/grill.py ask --type app --known u-purpose,app-job --skip-areas taste
  python3 scripts/grill.py ask --type website --fast          # user said "just build it": top 3 only
  python3 scripts/grill.py coverage --type website --known u-purpose,web-action
  python3 scripts/grill.py assume --type website --known u-purpose --json
  python3 scripts/grill.py check spec.json
  python3 scripts/grill.py render spec.json --format html --out spec.html
  python3 scripts/grill.py accept spec.json

Exit codes: 0 ok, 1 findings or errors in the checked thing, 2 usage error.
Use "-" as the spec path to read it from stdin. Standard library only.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

BUDGET = 15
ROUND_LABELS = {1: "foundations", 2: "shape", 3: "edges"}
RISK_BY_WEIGHT = {3: "high", 2: "med", 1: "low"}
BANK_PATH = Path(__file__).resolve().parent.parent / "assets" / "question-bank.json"

STOPWORDS = set(
    "the and for with from that this into onto over under also only than then them they their there here "
    "one all any each every can will may must should could would are was were been being have has had not "
    "its per via use used using user users page pages thing things new old get gets got make makes made "
    "who what when where which while about after before".split()
)
VAGUE_ACCEPT = re.compile(
    r"^\W*(it )?(works?( well| fine| correctly)?|good|fine|ok(ay)?|done|looks? (good|fine|right|nice)|is good|correct|passes|"
    r"as expected|is fine|is ok(ay)?|nice|great)\W*$",
    re.I,
)
OBSERVABLE = re.compile(
    r"\b(can|could|opens?|opened|sees?|shows?|shown|sends?|sent|receives?|received|completes?|completed|finish(es|ed)?|within|under|"
    r"less than|fewer|more than|at least|at most|without|every|all|zero|before|after|per|passes?|returns?|matches|equals?|"
    r"reach(es)?|publish(es|ed)?|approves?|signs?|clicks?|taps?|reads?|prints?|loads?|replies|reply|books?|buys?|pays?|"
    r"signs? up|subscribes?|finds?|checks?|uses?|answers?|stops?|keeps?|drops?|grows?|rises?|falls?)\b",
    re.I,
)
VAGUE_SUCCESS = re.compile(
    r"\b(good|nice|great|happy|easy|intuitive|user-friendly|better|successful|works? well|looks? good|clean|modern|seamless|"
    r"satisf(ied|ying)|love[sd]?|enjoy(s|ed)?)\b",
    re.I,
)


class UserError(Exception):
    """A mistake by the user: one line message, exit code 2 (or 1 when the checked thing is bad)."""

    def __init__(self, msg, code=2):
        super().__init__(msg)
        self.code = code


# ----------------------------------------------------------------- loading

def read_text(path):
    if path == "-":
        data = sys.stdin.buffer.read()
    else:
        p = Path(path)
        if not p.is_file():
            raise UserError("file not found: %s" % path)
        data = p.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UserError("not valid UTF-8 text: %s" % path)


def load_json(path, what):
    text = read_text(path)
    if not text.strip():
        raise UserError("%s is empty: %s" % (what, path), 1)
    try:
        return json.loads(text)
    except ValueError as ex:
        raise UserError("%s is not valid JSON (%s): %s" % (what, ex, path), 1)


def load_bank(path=None):
    p = Path(path) if path else BANK_PATH
    if not p.is_file():
        raise UserError("question bank not found: %s" % p)
    try:
        bank = json.loads(p.read_text(encoding="utf-8-sig"))
        for key in ("types", "questions", "areas"):
            if key not in bank:
                raise ValueError("missing %s" % key)
    except ValueError as ex:
        raise UserError("question bank is invalid (%s): %s" % (ex, p))
    for i, q in enumerate(bank["questions"]):
        q["_i"] = i
    return bank


def csv_arg(value):
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def resolve_types(bank, raw):
    ids = [t["id"] for t in bank["types"]]
    chosen = csv_arg(raw)
    bad = [t for t in chosen if t not in ids]
    if bad:
        raise UserError("unknown type: %s (valid: %s)" % (", ".join(bad), ", ".join(ids)))
    return chosen


def resolve_known(bank, raw):
    known = csv_arg(raw)
    valid = {q["id"] for q in bank["questions"]}
    bad = [k for k in known if k not in valid]
    if bad:
        raise UserError("unknown question id: %s (run: ask ... --json to see ids)" % ", ".join(bad))
    return set(known)


def applicable(bank, types):
    """Universal questions plus those of the chosen types, in bank order."""
    out = []
    for q in bank["questions"]:
        if not q["types"] or any(t in types for t in q["types"]):
            out.append(q)
    return out


def sort_key(q):
    return (-q["weight"], q["round"], 0 if q["types"] else 1, q["_i"])


# ----------------------------------------------------------------- types

def detect_types(bank, text):
    low = text.lower()
    found = []
    for t in bank["types"]:
        score, matched = 0, []
        for cue in t["cues"]:
            if re.search(r"(?<![a-z0-9])%s(?:s|es)?(?![a-z0-9])" % re.escape(cue.lower()), low):
                score += 1 + cue.count(" ")
                matched.append(cue)
        if score:
            found.append({"id": t["id"], "score": score, "matched": matched})
    found.sort(key=lambda d: (-d["score"], d["id"]))
    return found[:3]


def cmd_types(a):
    bank = load_bank(a.bank)
    counts = {}
    for q in bank["questions"]:
        for t in q["types"]:
            counts[t] = counts.get(t, 0) + 1
    universal = sum(1 for q in bank["questions"] if not q["types"])
    if a.detect is not None:
        text = read_text("-") if a.detect == "-" else a.detect
        det = detect_types(bank, text)
        if a.json:
            print(json.dumps({"detected": det}, indent=1))
        elif det:
            print("Likely type: " + ", ".join("%s (%s)" % (d["id"], "/".join(d["matched"])) for d in det))
        else:
            print("No type detected. Use universal questions only, or pick the closest type by hand.")
        return 0
    if a.json:
        rows = [dict(t, questions=counts.get(t["id"], 0)) for t in bank["types"]]
        print(json.dumps({"universal_questions": universal, "types": rows}, indent=1, ensure_ascii=False))
        return 0
    print("%-13s %-26s %4s  cues" % ("id", "label", "qs"))
    for t in bank["types"]:
        print("%-13s %-26s %4d  %s" % (t["id"], t["label"], counts.get(t["id"], 0), ", ".join(t["cues"][:6])))
    print("universal questions (asked for every type): %d" % universal)
    return 0


# ----------------------------------------------------------------- ask

def pick(cands, n, area_order):
    """Choose n questions: reserve one universal when n >= 3, one question per area first."""
    cands = sorted(cands, key=sort_key)
    chosen, areas = [], set()
    if n >= 3:
        for q in cands:
            if not q["types"]:
                chosen.append(q)
                areas.add(q["area"])
                break
    for q in cands:
        if len(chosen) >= n:
            break
        if q not in chosen and q["area"] not in areas:
            chosen.append(q)
            areas.add(q["area"])
    for q in cands:  # fill when areas run out
        if len(chosen) >= n:
            break
        if q not in chosen:
            chosen.append(q)
    order = {name: i for i, name in enumerate(area_order)}
    return sorted(chosen, key=lambda q: (order.get(q["area"], 99), sort_key(q)))



def cmd_ask(a):
    bank = load_bank(a.bank)
    types = resolve_types(bank, a.type)
    known = resolve_known(bank, a.known)
    skip = csv_arg(a.skip_areas)
    bad = [s for s in skip if s not in bank["areas"]]
    if bad:
        raise UserError("unknown area: %s (valid: %s)" % (", ".join(bad), ", ".join(bank["areas"])))
    if a.round is not None and a.round not in (1, 2, 3):
        raise UserError("--round must be 1, 2 or 3")
    n = 3 if a.fast and a.n is None else (a.n if a.n is not None else 5)
    if not 1 <= n <= 8:
        raise UserError("--n must be between 1 and 8 (batch, do not barrage)")
    used = len(known) + max(a.extra, 0)
    left = BUDGET - used
    pool = [q for q in applicable(bank, types) if q["id"] not in known and q["area"] not in skip]
    rnd = None
    if a.fast:
        cands = pool
    else:
        rnd = a.round
        if rnd is None:
            for r in (1, 2, 3):
                if any(q["round"] == r and q["weight"] >= 2 for q in pool):
                    rnd = r
                    break
            if rnd is None:
                rnd = next((r for r in (1, 2, 3) if any(q["round"] == r for q in pool)), 3)
        cands = [q for q in pool if q["round"] == rnd]
    picked = pick(cands, min(n, max(left, 0)), bank["areas"]) if left > 0 else []
    start = used + 1
    rows = []
    for i, q in enumerate(picked):
        rows.append({
            "number": start + i, "id": q["id"], "area": q["area"], "round": q["round"], "weight": q["weight"],
            "q": q["q"], "kind": q["kind"], "options": q["options"], "changes": q["changes"], "if_skipped": q["if_skipped"],
        })
    if left <= 0:
        note = "Question budget used (%d of %d). Stop asking: run assume, write the spec." % (min(used, BUDGET), BUDGET)
    elif not rows:
        note = "Nothing left to ask for this selection. Run assume, write the spec."
    elif a.fast:
        note = "Fast mode: only the highest-weight questions. Everything else becomes an assumption (run assume)."
    else:
        note = "Ask these together, then wait. Skip any you can already answer from context."
    if a.json:
        print(json.dumps({"round": rnd, "fast": bool(a.fast), "asked_so_far": used, "budget_left": max(left, 0),
                          "note": note, "questions": rows}, indent=1, ensure_ascii=False))
        return 0
    head = "Fast mode" if a.fast else "Round %d of 3 (%s)" % (rnd, ROUND_LABELS[rnd])
    print("%s | asked so far %d of %d | showing %d" % (head, used, BUDGET, len(rows)))
    for r in rows:
        print("")
        print("%d. %s" % (r["number"], r["q"]))
        if r["options"]:
            print("   Options: " + " / ".join(r["options"]))
        print("   Drives: %s  [id %s, weight %d]" % (r["changes"], r["id"], r["weight"]))
    print("")
    print(note)
    return 0


# ----------------------------------------------------------------- coverage / assume

def coverage_data(bank, types, known, extra):
    pool = applicable(bank, types)
    areas = {}
    for q in pool:
        d = areas.setdefault(q["area"], {"area": q["area"], "answered": 0, "total": 0, "remaining_weight": 0, "open_weight3": 0})
        d["total"] += 1
        if q["id"] in known:
            d["answered"] += 1
        else:
            d["remaining_weight"] += q["weight"]
            if q["weight"] == 3:
                d["open_weight3"] += 1
    for d in areas.values():
        d["status"] = "untouched" if d["answered"] == 0 else ("open" if d["open_weight3"] else "settled")
        if d["answered"] == 0 and d["open_weight3"] == 0:
            d["status"] = "low-stakes"
    unanswered = [q for q in pool if q["id"] not in known]
    used = len(known) + max(extra, 0)
    rem3 = sum(1 for q in unanswered if q["weight"] == 3)
    total_w = sum(q["weight"] for q in pool)
    rem_w = sum(q["weight"] for q in unanswered)
    if used >= BUDGET:
        advice, stop = "STOP: %d of %d questions used. Write the spec now; unanswered questions become assumptions." % (used, BUDGET), True
    elif rem3 == 0:
        advice, stop = "STOP: no high-weight question is left. The rest are low-stakes; list them as assumptions (run assume) and write the spec.", True
    elif used >= 10 and rem3 <= 1:
        advice, stop = "STOP: %d asked and only %d high-weight question left; cost now beats value. Write the spec and list it as an assumption." % (used, rem3), True
    else:
        top = sorted([q for q in unanswered if q["weight"] == 3], key=sort_key)[:3]
        advice = "CONTINUE: %d high-weight questions still open (next: %s). At most %d more questions." % (rem3, ", ".join(q["id"] for q in top), BUDGET - used)
        stop = False
    order = {name: i for i, name in enumerate(bank["areas"])}
    return {
        "asked": used, "budget": BUDGET, "remaining_weight": rem_w, "total_weight": total_w,
        "open_high_weight": rem3, "stop": stop, "advice": advice,
        "areas": sorted(areas.values(), key=lambda d: order.get(d["area"], 99)),
    }


def cmd_coverage(a):
    bank = load_bank(a.bank)
    data = coverage_data(bank, resolve_types(bank, a.type), resolve_known(bank, a.known), a.extra)
    if a.json:
        print(json.dumps(data, indent=1))
        return 0
    print("%-12s %-10s %8s %8s %6s" % ("area", "status", "answered", "left wt", "open3"))
    for d in data["areas"]:
        print("%-12s %-10s %5d/%-2d %8d %6d" % (d["area"], d["status"], d["answered"], d["total"], d["remaining_weight"], d["open_weight3"]))
    print("")
    print("Weight left: %d of %d | high-weight open: %d | asked %d of %d" % (
        data["remaining_weight"], data["total_weight"], data["open_high_weight"], data["asked"], data["budget"]))
    print(data["advice"])
    return 0


def assumptions_for(bank, types, known, min_weight):
    rows = []
    for q in sorted(applicable(bank, types), key=sort_key):
        if q["id"] in known or q["weight"] < min_weight:
            continue
        rows.append({"text": q["if_skipped"], "from": q["id"], "risk": RISK_BY_WEIGHT[q["weight"]]})
    return rows


def cmd_assume(a):
    bank = load_bank(a.bank)
    if not 1 <= a.min_weight <= 3:
        raise UserError("--min-weight must be 1, 2 or 3")
    rows = assumptions_for(bank, resolve_types(bank, a.type), resolve_known(bank, a.known), a.min_weight)
    if a.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0
    if not rows:
        print("Every question at weight %d or more is answered. No default assumptions needed." % a.min_weight)
        return 0
    print("Assumptions I will proceed on unless you correct them:")
    for i, r in enumerate(rows, 1):
        print("%d. [%s risk] %s  (%s)" % (i, r["risk"], r["text"], r["from"]))
    return 0


# ----------------------------------------------------------------- spec checks

def sig_terms(text):
    out = set()
    for tok in re.findall(r"[a-z0-9]+", str(text).lower()):
        if len(tok) < 3 or tok in STOPWORDS:
            continue
        if len(tok) > 4 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        out.add(tok)
    return out


def term_conflict(x, y):
    a, b = sig_terms(x), sig_terms(y)
    if not a or not b:
        return set()
    if a <= b or b <= a or len(a & b) >= 2:
        return a & b
    return set()


def sentence_count(text):
    t = re.sub(r"\b(e\.g|i\.e|vs|etc|approx|no|mr|mrs|dr|inc|ltd)\.", lambda m: m.group(0).replace(".", ""), text, flags=re.I)
    t = re.sub(r"(\d)\.(\d)", r"\1\2", t)
    parts = [p for p in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", t.strip()) if p.strip()]
    return len(parts)


def nonempty_str(v):
    return isinstance(v, str) and v.strip() != ""


def check_spec(spec, bank=None):
    """Return a list of findings: dicts with level (error|warn), code, msg."""
    f = []

    def err(code, msg):
        f.append({"level": "error", "code": code, "msg": msg})

    def warn(code, msg):
        f.append({"level": "warn", "code": code, "msg": msg})

    if not isinstance(spec, dict):
        err("shape", "spec must be a JSON object")
        return f
    for key in ("title", "goal", "success", "users"):
        if not nonempty_str(spec.get(key)):
            err("missing-" + key, "%s is missing or empty" % key)
    types = spec.get("type")
    if not isinstance(types, list) or not types or not all(nonempty_str(t) for t in types):
        err("missing-type", "type must be a non-empty list of type ids")
    elif bank:
        known_types = {t["id"] for t in bank["types"]}
        for t in types:
            if t not in known_types:
                warn("unknown-type", 'type "%s" is not in the question bank' % t)
    goal = spec.get("goal")
    if nonempty_str(goal):
        if sentence_count(goal) > 1:
            err("goal-long", "goal is more than one sentence; keep one sentence")
        elif len(goal) > 240:
            warn("goal-long", "goal is %d characters; shorten it" % len(goal))
    success = spec.get("success")
    if nonempty_str(success):
        has_digit = bool(re.search(r"\d", success))
        if not has_digit and (not OBSERVABLE.search(success) or VAGUE_SUCCESS.search(success)):
            err("success-vague", "success is not observable: add a number, a time, or something a person can do or see")
    must = spec.get("must_have")
    items = []
    if not isinstance(must, list) or not must:
        err("missing-must_have", "must_have needs at least one item")
    else:
        for i, m in enumerate(must, 1):
            if not isinstance(m, dict) or not nonempty_str(m.get("item")):
                err("must-shape", "must_have #%d needs an item" % i)
                continue
            items.append(m["item"])
            acc = m.get("accept")
            if not nonempty_str(acc):
                err("no-accept", 'must_have #%d has no accept test: "%s"' % (i, m["item"]))
            elif VAGUE_ACCEPT.match(acc) or len(re.findall(r"[A-Za-z0-9]+", acc)) < 3:
                err("accept-vague", 'must_have #%d accept test is not testable ("%s"): say what to do and what must be seen' % (i, acc))
        if len(must) > 8:
            warn("too-many-must", "%d must-haves; a spec this long usually hides a nice-to-have" % len(must))
    oos = spec.get("out_of_scope")
    if not isinstance(oos, list) or not [x for x in oos if nonempty_str(x)]:
        err("out-of-scope-empty", "out_of_scope is empty: name at least one related thing that will not be built")
        oos = []
    for x in oos:
        for it in items:
            shared = term_conflict(it, x)
            if shared:
                err("scope-conflict", 'must_have "%s" and out_of_scope "%s" share: %s' % (it, x, ", ".join(sorted(shared))))
    nice = spec.get("nice_to_have") or []
    if isinstance(nice, list):
        for x in nice:
            for it in items:
                if nonempty_str(x) and term_conflict(it, x):
                    warn("nice-repeats-must", 'nice_to_have "%s" overlaps must_have "%s"' % (x, it))
    for key in ("constraints", "reject_if", "open_risks", "nice_to_have", "out_of_scope"):
        v = spec.get(key)
        if v is not None and not (isinstance(v, list) and all(nonempty_str(x) for x in v)):
            err("list-shape", "%s must be a list of non-empty strings" % key)
    ass = spec.get("assumptions") or []
    if not isinstance(ass, list):
        err("assumption-shape", "assumptions must be a list")
        ass = []
    for i, x in enumerate(ass, 1):
        if not isinstance(x, dict) or not nonempty_str(x.get("text")):
            err("assumption-shape", "assumption #%d needs a text" % i)
            continue
        risk = x.get("risk")
        if risk not in ("low", "med", "high"):
            err("assumption-risk", 'assumption #%d risk must be low, med or high (got "%s")' % (i, risk))
        elif risk == "high":
            warn("high-risk-assumption", 'high-risk assumption still open: "%s". Ask the user to confirm it before building.' % x["text"])
    if not spec.get("reject_if"):
        warn("no-reject-if", "no reject_if lines: ask what would make the user send it back")
    return f


def cmd_check(a):
    bank = None
    try:
        bank = load_bank(a.bank)
    except UserError:
        pass
    spec = load_json(a.spec, "spec")
    findings = check_spec(spec, bank)
    errors = [x for x in findings if x["level"] == "error"]
    warns = [x for x in findings if x["level"] == "warn"]
    bad = bool(errors) or (a.strict and bool(warns))
    if a.json:
        print(json.dumps({"ok": not bad, "errors": len(errors), "warnings": len(warns), "findings": findings}, indent=1, ensure_ascii=False))
    else:
        for x in findings:
            print("%-5s %-22s %s" % (x["level"].upper(), x["code"], x["msg"]))
        print("ok: spec passes (%d warnings)" % len(warns) if not bad else "%d errors, %d warnings" % (len(errors), len(warns)))
    return 1 if bad else 0


# ----------------------------------------------------------------- render

def md_cell(s):
    return re.sub(r"\s+", " ", str(s)).replace("|", "\\|").strip()


def spec_sections(spec):
    """Ordered (heading, kind, payload) for every non-empty section, in the fixed section order."""
    sec = [
        ("Goal", "text", spec["goal"]),
        ("Success looks like", "text", spec["success"]),
        ("Users", "text", spec["users"]),
        ("Must have", "must", spec["must_have"]),
    ]
    for head, key in (("Nice to have", "nice_to_have"), ("Out of scope", "out_of_scope"), ("Constraints", "constraints"),
                      ("Reject if", "reject_if"), ("Open risks", "open_risks")):
        if spec.get(key):
            sec.append((head, "list", spec[key]))
    if spec.get("assumptions"):
        sec.append(("Correct me before I build", "assume", spec["assumptions"]))
    return sec


CLOSING = "Reply yes to build, or tell me which line to change. Anything under Correct me is a guess, not a fact."


def render_md(spec):
    out = ["# Build spec: %s" % spec["title"], "", "Type: %s" % ", ".join(spec["type"]), ""]
    for head, kind, data in spec_sections(spec):
        out.append("## " + head)
        out.append("")
        if kind == "text":
            out.append(data.strip())
        elif kind == "list":
            out.extend("- " + x.strip() for x in data)
        elif kind == "must":
            out += ["| # | Must have | Accept test |", "|---|---|---|"]
            out += ["| %d | %s | %s |" % (i, md_cell(m["item"]), md_cell(m["accept"])) for i, m in enumerate(data, 1)]
        else:
            out += ["| # | I am assuming | Risk |", "|---|---|---|"]
            out += ["| %d | %s | %s |" % (i, md_cell(x["text"]), x["risk"]) for i, x in enumerate(data, 1)]
        out.append("")
    out.append(CLOSING)
    return "\n".join(out) + "\n"


HTML_CSS = """
:root{--ink:#1a1a1a;--muted:#666;--rule:#e6e6e6;--accent:#ff6719}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:760px;margin:0 auto;padding:48px 20px 64px}
h1,h2{font-family:Lora,Georgia,"Times New Roman",serif;font-weight:600;line-height:1.25}
h1{font-size:2rem;margin:0 0 4px;padding-top:14px;border-top:4px solid var(--accent)}
h2{font-size:1.2rem;margin:2rem 0 .5rem;padding-bottom:4px;border-bottom:1px solid var(--rule)}
.type{color:var(--muted);margin:0 0 8px;font-size:.9rem}
p{margin:.4rem 0}
ul{margin:.4rem 0;padding-left:1.2rem}
table{width:100%;border-collapse:collapse;margin:.5rem 0;font-size:.95rem}
th,td{text-align:left;vertical-align:top;padding:8px 10px;border-bottom:1px solid var(--rule)}
th{font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
td.n{width:2.2rem;color:var(--accent);font-weight:600}
td.risk{width:4rem;color:var(--muted)}td.risk.high{color:var(--accent);font-weight:600}
.correct h2{border-bottom-color:var(--accent)}
.closing{margin-top:2rem;padding-top:1rem;border-top:1px solid var(--rule);color:var(--muted);font-size:.95rem}
tr{break-inside:avoid}h2{break-after:avoid}
@page{margin:18mm}
@media print{main{padding:0;max-width:none}body{font-size:11pt}}
@media (max-width:520px){main{padding:28px 16px 48px}h1{font-size:1.6rem}}
"""


def render_html(spec):
    e = html.escape
    out = ['<!doctype html>', '<html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           "<title>Build spec: %s</title>" % e(spec["title"]), "<style>%s</style></head><body><main>" % HTML_CSS,
           "<h1>Build spec: %s</h1>" % e(spec["title"]), '<p class="type">Type: %s</p>' % e(", ".join(spec["type"]))]
    for head, kind, data in spec_sections(spec):
        out.append('<section class="correct">' if kind == "assume" else "<section>")
        out.append("<h2>%s</h2>" % e(head))
        if kind == "text":
            out.append("<p>%s</p>" % e(data.strip()))
        elif kind == "list":
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % e(x.strip()) for x in data))
        elif kind == "must":
            rows = "".join('<tr><td class="n">%d</td><td>%s</td><td>%s</td></tr>' % (i, e(m["item"]), e(m["accept"])) for i, m in enumerate(data, 1))
            out.append("<table><thead><tr><th>#</th><th>Must have</th><th>Accept test</th></tr></thead><tbody>%s</tbody></table>" % rows)
        else:
            rows = "".join('<tr><td class="n">%d</td><td>%s</td><td class="risk %s">%s</td></tr>' % (i, e(x["text"]), x["risk"], x["risk"])
                           for i, x in enumerate(data, 1))
            out.append("<table><thead><tr><th>#</th><th>I am assuming</th><th>Risk</th></tr></thead><tbody>%s</tbody></table>" % rows)
        out.append("</section>")
    out.append('<p class="closing">%s</p></main></body></html>' % e(CLOSING))
    return "\n".join(out) + "\n"


def write_out(path, text):
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    except OSError as ex:
        raise UserError("cannot write %s (%s)" % (path, ex.strerror or ex))
    print("wrote %s (%d bytes)" % (path, len(text.encode("utf-8"))))


def cmd_render(a):
    spec = load_json(a.spec, "spec")
    if not a.force:
        errors = [x for x in check_spec(spec) if x["level"] == "error"]
        if errors:
            for x in errors:
                print("ERROR %-22s %s" % (x["code"], x["msg"]), file=sys.stderr)
            print("not rendered: fix %d error(s) (or use --force)" % len(errors), file=sys.stderr)
            return 1
    elif not isinstance(spec, dict) or any(k not in spec for k in ("title", "type", "goal", "success", "users", "must_have")):
        raise UserError("--force still needs title, type, goal, success, users and must_have", 1)
    text = render_md(spec) if a.format == "md" else render_html(spec)
    if a.out:
        write_out(a.out, text)
    else:
        sys.stdout.write(text)
    return 0


# ----------------------------------------------------------------- accept

def accept_rows(spec, full):
    rows = []
    for i, m in enumerate(spec["must_have"], 1):
        rows.append({"id": "M%d" % i, "kind": "must have", "check": m["item"], "how": m["accept"]})
    for i, r in enumerate(spec.get("reject_if") or [], 1):
        rows.append({"id": "R%d" % i, "kind": "reject if", "check": r, "how": "Confirm this does NOT happen. Try to make it happen."})
    if full:
        rows.append({"id": "S1", "kind": "success", "check": spec.get("success", ""), "how": "Measure or observe it as written."})
        for i, o in enumerate(spec.get("out_of_scope") or [], 1):
            rows.append({"id": "O%d" % i, "kind": "out of scope", "check": o, "how": "Confirm it was not built."})
    return rows


def cmd_accept(a):
    spec = load_json(a.spec, "spec")
    if not isinstance(spec, dict) or not isinstance(spec.get("must_have"), list) or not spec["must_have"] \
            or not all(isinstance(m, dict) and nonempty_str(m.get("item")) and nonempty_str(m.get("accept")) for m in spec["must_have"]):
        raise UserError("spec needs must_have items that each have an accept test (run: check)", 1)
    rows = accept_rows(spec, a.full)
    if a.json:
        print(json.dumps({"title": spec.get("title", ""), "rows": rows, "results": ["pass", "partial", "fail"]}, indent=1, ensure_ascii=False))
        return 0
    print("# Acceptance run: %s" % spec.get("title", "untitled"))
    print("")
    print("| ID | Kind | Check | How to verify | Result |")
    print("|---|---|---|---|---|")
    for r in rows:
        print("| %s | %s | %s | %s | ? |" % (r["id"], r["kind"], md_cell(r["check"]), md_cell(r["how"])))
    print("")
    print("Run every check for real (open it, click it, run it), then replace ? with pass, partial or fail plus one line of evidence.")
    print("Any fail on a must have or reject if means not done: say so first, then fix or ask. Never mark pass from memory of the code.")
    return 0


# ----------------------------------------------------------------- main

def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", help="path to a question bank JSON (default: assets/question-bank.json)")
    sub = ap.add_subparsers(dest="cmd", metavar="command")

    p = sub.add_parser("types", help="list project types, or detect the type from a request")
    p.add_argument("--detect", metavar="TEXT", help='detect the type from request text ("-" reads stdin)')
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_types)

    p = sub.add_parser("ask", help="next questions to ask, highest weight first")
    p.add_argument("--type", default="", help="comma separated type ids (empty: universal questions only)")
    p.add_argument("--known", default="", help="comma separated question ids already answered or known")
    p.add_argument("--skip-areas", default="", help="comma separated areas to leave out")
    p.add_argument("--round", type=int, help="1 foundations, 2 shape, 3 edges (default: first round with weighty questions left)")
    p.add_argument("--n", type=int, help="questions to show (default 5, fast mode 3, max 8)")
    p.add_argument("--extra", type=int, default=0, help="follow-up questions already asked outside the bank (count toward the 15)")
    p.add_argument("--fast", action="store_true", help='user said "just build it": top questions across all rounds')
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("coverage", help="areas settled vs open, weight left, stop advice")
    p.add_argument("--type", default="")
    p.add_argument("--known", default="")
    p.add_argument("--extra", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_coverage)

    p = sub.add_parser("assume", help="default assumptions for the questions not answered")
    p.add_argument("--type", default="")
    p.add_argument("--known", default="")
    p.add_argument("--min-weight", type=int, default=2, help="only questions of at least this weight (default 2)")
    p.add_argument("--json", action="store_true", help="output ready for the spec assumptions field")
    p.set_defaults(fn=cmd_assume)

    p = sub.add_parser("check", help="validate a spec JSON")
    p.add_argument("spec", help='spec path, or "-" for stdin')
    p.add_argument("--strict", action="store_true", help="warnings also fail")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("render", help="render a spec as Markdown or printable HTML")
    p.add_argument("spec")
    p.add_argument("--format", choices=["md", "html"], default="md")
    p.add_argument("--out", help="write to this file instead of stdout")
    p.add_argument("--force", action="store_true", help="render even if check finds errors")
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("accept", help="post-build acceptance checklist from a spec")
    p.add_argument("spec")
    p.add_argument("--full", action="store_true", help="also check success and out of scope")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_accept)
    return ap


def main(argv=None):
    ap = build_parser()

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_usage(sys.stderr)
        return 2
    try:
        return args.fn(args)
    except UserError as ex:
        print("error: %s" % ex, file=sys.stderr)
        return ex.code
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
