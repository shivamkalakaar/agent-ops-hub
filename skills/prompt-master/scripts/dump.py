#!/usr/bin/env python3
"""Brain-dump toolkit for prompt-master: score, atoms, slots, check, render.

Everything printed here is a heuristic SUGGESTION for the model to judge; nothing is rewritten.

Examples:
  python3 scripts/dump.py score dump.txt
  python3 scripts/dump.py atoms dump.txt --json > atoms.json
  python3 scripts/dump.py slots --detect --text dump.txt
  python3 scripts/dump.py slots --type email --text dump.txt --json
  python3 scripts/dump.py check atoms.json ledger.json --spec spec.txt
  python3 scripts/dump.py render spec.json --style readback

Use '-' as a file name to read stdin. Exit codes: 0 ok, 1 findings (check only: something was lost),
2 usage error (bad file, bad JSON, unknown type).
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TYPES_PATH = HERE.parent / "assets" / "task_types.json"


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- input helpers

def read_text(path: str) -> str:
    try:
        if path == "-":
            data = sys.stdin.buffer.read()
        else:
            data = Path(path).read_bytes()
    except OSError as ex:
        raise UsageError("cannot read %s: %s" % (path, ex.strerror or ex))
    text = data.decode("utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_json(path: str, what: str):
    raw = read_text(path)
    try:
        return json.loads(raw)
    except ValueError as ex:
        raise UsageError("%s is not valid JSON: %s" % (what, ex))


WORD_RE = re.compile(r"\w[\w'\u2019-]*")


def count_words(text: str) -> int:
    return len(WORD_RE.findall(text))


# ---------------------------------------------------------------- atomizer

PROT = "\x00"
ABBR = re.compile(r"\b(?:e\.g|i\.e|vs|mr|mrs|ms|dr|a\.m|p\.m)\.", re.I)
SENT_SPLIT = re.compile(r"(?<=[.!?\u2026])\s+(?=[\"'\u201c(\[]?\w)")
CLAUSE_SPLIT = re.compile(r";\s+|\s+[\u2014\u2013]\s+|\s+--\s+|\s+-\s+")
MARK_SPLIT = re.compile(
    r",?\s+(?=(?:oh,?\s+(?:and|also|yeah)|anyway|btw|by the way|one more thing|another thing|and also|on top of that)\b)",
    re.I)
CONJ_SPLIT = re.compile(r"\s+(?=(?:and also|and then|and|but|so|because|which means|plus|while)\b)", re.I)
BULLET_RE = re.compile(r"^\s*(?:[-*\u2022\u25e6]|\d{1,3}[.)]|#{1,6})\s+")


def _merge_short(parts, joiner=", ", min_words=3):
    out = []
    carry = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if carry:
            p = carry + joiner + p
            carry = ""
        if count_words(p) < min_words:
            if out:
                out[-1] = out[-1] + joiner + p
            else:
                carry = p
        else:
            out.append(p)
    if carry:
        if out:
            out[-1] += joiner + carry
        else:
            out.append(carry)
    return out


def _split_long(chunk: str):
    parts = [chunk]
    if count_words(chunk) > 10:
        parts = _merge_short(MARK_SPLIT.split(chunk), joiner=" ")
    res = []
    for p in parts:
        if count_words(p) > 25:
            for q in _merge_short(re.split(r",\s+", p)):
                if count_words(q) > 30:
                    res.extend(_merge_short(CONJ_SPLIT.split(q), joiner=" ", min_words=6))
                else:
                    res.append(q)
        else:
            res.append(p)
    return res


def _split_sentences(line: str):
    s = ABBR.sub(lambda m: m.group(0).replace(".", PROT), line)
    out = []
    for sent in SENT_SPLIT.split(s):
        for chunk in CLAUSE_SPLIT.split(sent):
            chunk = chunk.replace(PROT, ".").strip()
            if chunk:
                out.extend(_split_long(chunk))
    return out


def atomize(text: str):
    atoms = []
    para = 0
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            para += 1
            continue
        bullet = bool(BULLET_RE.match(line))
        if bullet:
            line = BULLET_RE.sub("", line, count=1)
        for sent in _split_sentences(line):
            atoms.append({"id": len(atoms) + 1, "text": sent, "para": para, "bullet": bullet})
    return atoms


# ---------------------------------------------------------------- entities

DAY = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tues|thurs)"
MONTH_FULL = r"(?:january|february|march|april|june|july|august|september|october|november|december)"
MONTH_ANY = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)"
DATE_RE = re.compile("|".join("(?:%s)" % p for p in [
    r"\b\d{4}-\d{2}-\d{2}\b",
    r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b",
    r"\b\d{1,2}/\d{1,2}\b",
    r"\b(?:%s|%s\.?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b" % (MONTH_FULL, MONTH_ANY),
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:%s|%s\.?)(?:,?\s+\d{4})?\b" % (MONTH_FULL, MONTH_ANY),
    r"\b%s(?:\s+\d{4})?\b" % MONTH_FULL,
    r"\b(?:(?:next|this|last)\s+)?%s\b" % DAY,
    r"\b(?:today|tonight|tomorrow|yesterday|eod|eow|eom|asap|next (?:week|month|quarter|year)"
    r"|this (?:week|month|quarter|year|morning|afternoon|evening)|end of (?:the )?(?:day|week|month|quarter|year)"
    r"|q[1-4](?:\s+\d{4})?|in \d+ (?:days?|weeks?|months?|hours?))\b",
    r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b",
]), re.I)
DATE_ALIAS = {"tonight": "today", "eod": "today", "end of day": "today", "end of the day": "today",
              "this evening": "today", "this morning": "today", "this afternoon": "today", "eow": "end of week",
              "end of the week": "end of week", "eom": "end of month", "end of the month": "end of month"}

UNITS = (r"(?:hours?|hrs?|h|minutes?|mins?|days?|d|weeks?|wks?|w|months?|years?|yrs?|words?|pages?|slides?|lines?"
         r"|people|users?|customers?|clients?|items?|bullets?|paragraphs?|sentences?|tickets?|emails?|options?"
         r"|ideas?|examples?|steps?|points?|characters?|chars?|rows?|columns?|leads?|calls?|posts?|seats?"
         r"|employees?|reps?)")
NUM_RE = re.compile(r"(?<![\w.$])(?P<cur>[$\u20ac\u00a3])?(?P<num>\d[\d,]*(?:\.\d+)?)(?P<suf>%|k\b|m\b|bn\b|x\b)?"
                    r"(?:\s?(?P<unit>" + UNITS + r")\b)?", re.I)
NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
            "fifty": 50, "hundred": 100}
NUMWORD_RE = re.compile(r"\b(?P<w>" + "|".join(NUMWORDS) + r")\s+(?P<unit>" + UNITS + r")\b", re.I)
QUOTE_RE = re.compile(r"\"([^\"\n]{3,120})\"|\u201c([^\u201d\n]{3,120})\u201d")
REF_RE = re.compile(r"https?://\S+|[\w.+-]+@[\w-]+\.[\w.-]+")
EMPHASIS = {"NOT", "NEVER", "NO", "DON'T", "DONT", "DO", "MUST", "ONLY", "ALWAYS", "NOTHING", "IMPORTANT",
            "CRITICAL", "URGENT", "ASAP", "EXACTLY", "NONE"}
CAP_RE = re.compile(r"\b[A-Z][\w'\u2019-]*(?:\s+[A-Z][\w'\u2019-]*)*")
STOPCAP = set("""i i'm i'd i'll i've it's that's don't can't won't let's we're we've we'd we'll you're they're he's
she's there's here's what's the a an this that these those there here we you he she they it my our your his her their
its ok okay oh yes no thanks thank hi hello hey dear sorry anyway just maybe let help need want and but so then also
please can could would will do does did if when what why how who where which while after before since because though
although for from with without on in at by to of as is are was were be have has had not never always still now first
second next last one all some any every each both another other such only very really actually honestly basically
tell ask send write make give get put take turn build create draft find fix check see show use try keep add remove
update review prepare summarize summarise rewrite list plan design outline convert schedule generate compare""".split())
NAME_VERB = re.compile(r"\s+(?:wants|said|says|thinks|asked|told|needs|hates|loves|mentioned|suggested|prefers|likes|"
                       r"expects|is|was|will|has|had|can|should|would|also|and|from|at)\b")
LOWER_NAME_STOP = set("""he she they we i you it who that which everyone someone nobody people client boss team legal
marketing sales customer customers user users them him her us me this what one someone anyone the a an our my your
finance ops support hr management everybody folks""".split())
LOWER_NAME_1 = re.compile(r"(?<![\w-])(?:tell|ask|email|cc|ping|remind|thank)\s+([a-z]{3,})\b")
LOWER_NAME_2 = re.compile(r"\b([a-z]{3,})\s+(?:wants|said|says|thinks|asked|told|needs|hates|loves|mentioned|"
                          r"suggested|prefers|likes|expects)\b")
ARTICLE_BEFORE = re.compile(r"\b(?:the|a|an|my|our|your|their|his|her|this|that|every|each)\s+$")
NEG_RE = re.compile(r"\b(?P<neg>do\s+not|don['\u2019]?t|does\s+not|doesn['\u2019]?t|never|must\s+not|mustn['\u2019]?t|"
                    r"should\s+not|shouldn['\u2019]?t|cannot|can['\u2019]?t|won['\u2019]?t|will\s+not|no|not|without|"
                    r"avoid|nothing\s+about|leave\s+out|leave\s+off|skip|exclude|omit)\b(?P<rest>[^.,;:!?\n]{0,60})",
                    re.I)
NEG_SKIP_NEXT = {"sure", "really", "important", "problem", "worries", "worry", "idea", "just", "quite", "wait",
                 "exactly", "necessarily", "only", "a", "i"}
STOPW = set("""a an the and or of to in on at by for with from up down out off over into onto about that this these
those it its is are was were be been being as if so than then them they he she we you i my our your their his her
have has had do does did not no yet also very too just any some more most much many can could would should will
shall may might must""".split())


def _mask(text: str, spans):
    if not spans:
        return text
    chars = list(text)
    for s, e in spans:
        for i in range(s, e):
            chars[i] = " "
    return "".join(chars)


def norm_date(t: str) -> str:
    t = re.sub(r"\s+", " ", t.strip().lower())
    t = re.sub(r"^(?:next|this|last)\s+(?=%s$)" % DAY, "", t)
    return DATE_ALIAS.get(t, t)


def unit_key(u: str) -> str:
    u = u.lower()
    if u.endswith("s") and len(u) > 3:
        u = u[:-1]
    return {"h": "hour", "hr": "hour", "d": "day", "w": "week", "wk": "week", "min": "minute", "yr": "year",
            "char": "character", "rep": "rep"}.get(u, u)


def extract_entities(text: str):
    ents = []
    spans = []
    for m in QUOTE_RE.finditer(text):
        ents.append({"kind": "quote", "text": (m.group(1) or m.group(2)).strip()})
        spans.append(m.span())
    for m in REF_RE.finditer(text):
        ents.append({"kind": "ref", "text": m.group(0).rstrip(".,;)")})
        spans.append(m.span())
    masked = _mask(text, spans)
    dspans = []
    for m in DATE_RE.finditer(masked):
        ents.append({"kind": "date", "text": m.group(0).strip(), "norm": norm_date(m.group(0)), "pos": m.start()})
        dspans.append(m.span())
    masked = _mask(masked, dspans)
    nspans = []
    for m in NUM_RE.finditer(masked):
        core = m.group("num").rstrip(",").replace(",", "")
        if not core:
            continue
        e = {"kind": "number", "text": m.group(0).strip().rstrip(","), "norm": core, "pos": m.start()}
        if m.group("unit"):
            e["unit"] = unit_key(m.group("unit"))
        ents.append(e)
        nspans.append(m.span())
    for m in NUMWORD_RE.finditer(masked):
        ents.append({"kind": "number", "text": m.group(0), "norm": str(NUMWORDS[m.group("w").lower()]),
                     "unit": unit_key(m.group("unit")), "pos": m.start()})
        nspans.append(m.span())
    masked = _mask(masked, nspans)
    emph = []
    for m in re.finditer(r"\b[A-Z][A-Z']+\b", masked):
        if m.group(0) in EMPHASIS:
            emph.append(m.group(0))
    for w in sorted(set(emph)):
        ents.append({"kind": "emphasis", "text": w})
    names = {}
    for m in CAP_RE.finditer(masked):
        words = m.group(0).split()
        lo = 0
        hi = len(words)
        while lo < hi and words[lo].lower().strip("'\u2019") in STOPCAP:
            lo += 1
        while hi > lo and words[hi - 1].lower().strip("'\u2019") in STOPCAP:
            hi -= 1
        words = [w for w in words[lo:hi]]
        if not words:
            continue
        if any(w in EMPHASIS for w in words) and len(words) == 1:
            continue
        if len(words) == 1 and words[0].isupper() and not 2 <= len(words[0]) <= 6:
            continue
        if m.start() == 0 and lo == 0 and len(words) == 1 and not words[0].isupper():
            if not NAME_VERB.match(masked[m.end():]):
                continue
        phrase = " ".join(words)
        names.setdefault(phrase.lower(), phrase)
    lowered = masked
    for rx in (LOWER_NAME_1, LOWER_NAME_2):
        for m in rx.finditer(lowered):
            w = m.group(1)
            if ARTICLE_BEFORE.search(lowered[max(0, m.start(1) - 8):m.start(1)]):
                continue
            if w not in LOWER_NAME_STOP and w not in STOPW and w not in STOPCAP:
                names.setdefault(w, w)
    for key in sorted(names):
        ents.append({"kind": "name", "text": names[key]})
    seen = set()
    for m in NEG_RE.finditer(text):
        rest = m.group("rest").split()
        if not rest or rest[0].lower().strip("'\u2019") in NEG_SKIP_NEXT:
            continue
        window = rest[:5]
        while window and window[-1].lower().strip("'\u2019\"()") in STOPW:
            window = window[:-1]
        keys = [w.lower().strip("'\u2019\"()") for w in window
                if w.lower().strip("'\u2019\"()") not in STOPW and len(w.strip("'\u2019\"()")) >= 3][:3]
        if not keys:
            continue
        phrase = (m.group("neg") + " " + " ".join(window)).strip()
        if phrase.lower() in seen:
            continue
        seen.add(phrase.lower())
        strong = m.group("neg").isupper() or any(w.isupper() and len(w) > 1 for w in m.group("neg").split())
        ents.append({"kind": "negation", "text": phrase, "keys": keys, "emphasis": bool(strong)})
    return ents


# ---------------------------------------------------------------- tags

STRONG_ASK = [re.compile(p, re.I) for p in [
    r"\b(?:can|could|would|will)\s+you\b",
    r"\bi\s+(?:(?:really|actually|just|also|still|only)\s+)*(?:need|want|require)\b|\bi(?:'d|\u2019d|\s+would)\s+(?:really\s+)?like\b|\bwanna\b",
    r"\bplease\b(?!\s+(?:do\s+not|don['\u2019]?t|never)\b)",
    r"\bhelp\s+me\b",
    r"^(?:so\s+|ok\s+|okay\s+|and\s+|now\s+)?(?:write|draft|make|create|build|send|summari[sz]e|fix|find|give|prepare|"
    r"put\s+together|turn|rewrite|analy[sz]e|plan|list|compare|review|check|generate|design|outline|update|convert|"
    r"clean\s+up|translate|schedule|tell|show|explain|pull|figure)\b",
    r"\bsomething\s+(?:i|we)\s+can\b",
    r"\b(?:looking|trying)\s+(?:for|to)\b",
    r"\bget\s+me\b",
]]
WEAK_ASK = [re.compile(p, re.I) for p in [
    r"\b(?:wants?|needs?)\s+(?:a|an|the|to|some)\b",
    r"\bneed\s+to\b",
]]
IMPERATIVES = ("write|draft|make|create|build|send|summari[sz]e|fix|find|give|prepare|put together|turn|rewrite|analy[sz]e|"
               "plan|list|compare|review|check|generate|design|outline|update|convert|clean up|translate|schedule|tell|"
               "show|explain|pull|figure")
ASK_KEY_RES = [re.compile(p, re.I) for p in [
    r"(?:can|could|would|will)\s+you\s+(?:(?:please|also|just|maybe)\s+)*(\w+)",
    r"\bi\s+(?:(?:really|actually|just|also|still)\s+)*(?:need|want|require)\s+(?:you\s+to\s+|to\s+)?(?:(?:a|an|the|some|my|our)\s+)?(\w+)",
    r"\bwhat\s+i\s+(?:actually\s+|really\s+)?(?:need|want)\s+is\s+(?:(?:a|an|the|some)\s+)?(\w+)",
    r"please\s+(?:(?:also|just)\s+)*(\w+)",
    r"help\s+me\s+(\w+)",
    r"something\s+(?:i|we)\s+can\s+(\w+)",
    r"(?:looking|trying)\s+(?:for|to)\s+(\w+)",
    r"get\s+me\s+(\w+)",
    r"^(?:so\s+|ok\s+|okay\s+|and\s+|now\s+)?(%s)\b" % IMPERATIVES,
    r"(?:wants?|needs?)\s+(?:to\s+)?(?:(?:a|an|the|some)\s+)?(\w+)",
]]
ASK_STOPKEYS = {"this", "that", "it", "as", "to", "a", "an", "the", "some", "you", "my", "our", "them", "us", "me", "up",
                "out", "be", "and", "so", "is", "of", "for", "in", "on", "with"}
CONSTRAINT_RE = re.compile(
    r"\b(?:must|only|at\s+most|at\s+least|no\s+more\s+than|no\s+less\s+than|make\s+sure|has\s+to|have\s+to|"
    r"needs?\s+to\s+be|should\s+(?:be|not|have|include|sound|feel|use|cover|mention|keep|stay|fit|look)|not\s+too|keep\s+it|limit|maximum|minimum|max|min|under\s+\$?\d|over\s+\$?\d|"
    r"within\s+\d|exactly|deadline|due|by\s+(?:end|eod|tonight|tomorrow)|before\s+(?:end|tonight|tomorrow))\b", re.I)
DATE_CONSTRAINT_RE = re.compile(r"\b(?:by|before|until|due|no\s+later\s+than)\s+$", re.I)
QUESTION_START = re.compile(r"^(?:what|why|how|who|where|when|which|should\s+(?:we|i)|is\s+(?:it|this|that)|are\s+(?:we|they)|"
                            r"do\s+(?:we|you|they)|does\s+(?:it|this)|did\s+(?:we|you|they))\b", re.I)
EMOTION_RE = re.compile(r"\b(?:upset|angry|furious|frustrat\w*|annoy\w*|stress\w*|worried|worry|hate[sd]?|love[sd]?|ugh+|"
                        r"nightmare|terrible|awful|panic\w*|excited|nervous|sick\s+of|fed\s+up|drives?\s+me|"
                        r"anxious|disappointed|thrilled|scared|afraid)\b", re.I)
ASIDE_RE = re.compile(r"^(?:btw|by\s+the\s+way|side\s+note|unrelated|off\s+topic|tangent|random\s+thought|not\s+important|"
                      r"whatever|never\s*mind|nvm|p\.?s\.?)\b|^\(.*\)$", re.I)


def ask_keys(text: str):
    keys = set()
    for rx in ASK_KEY_RES:
        for m in rx.finditer(text):
            k = m.group(1).lower()
            if k not in ASK_STOPKEYS:
                keys.add(k)
    return sorted(keys) or ["ask"]


def annotate(a: dict):
    text = a["text"]
    ents = extract_entities(text)
    tags = []
    strong = any(rx.search(text) for rx in STRONG_ASK)
    weak = any(rx.search(text) for rx in WEAK_ASK)
    is_question = text.rstrip().endswith("?") or bool(QUESTION_START.match(text))
    if strong or weak:
        tags.append("ask")
    elif is_question:
        tags.append("question")
    constraint = any(e["kind"] == "negation" for e in ents) or bool(CONSTRAINT_RE.search(text))
    if not constraint:
        for e in ents:
            if e["kind"] == "date" and DATE_CONSTRAINT_RE.search(text[:e.get("pos", 0)]):
                constraint = True
    if constraint and not (is_question and not strong):
        tags.append("constraint")
    if ASIDE_RE.search(text.strip()) and "ask" not in tags:
        tags.append("aside")
    if EMOTION_RE.search(text):
        tags.append("emotion")
    if not any(t in tags for t in ("ask", "constraint", "question", "aside")):
        tags.insert(0, "context")
    a["tags"] = tags
    a["entities"] = ents
    a["strong_ask"] = strong
    a["ask_keys"] = ask_keys(text) if (strong or weak) else []
    return a


# ---------------------------------------------------------------- contradictions

_NOT_LOOK = r"(?![a-z]*\s+(?:notice|term|story|list|cut|on|of|staffed|time|ago|way|run))"
POLES = {
    "length": {"short": r"short(?:er)?%s|brief|concise|quick|tight|terse|succinct|one[- ]liner|a few lines|snappy|compact" % _NOT_LOOK,
               "long": r"long(?:er)?%s|detailed|thorough|comprehensive|in[- ]depth|in detail|extensive|exhaustive|elaborate|deep dive" % _NOT_LOOK},
    "tone": {"formal": r"formal|professional|corporate|polished|serious|stiff|business[- ]like",
             "casual": r"casual|friendly|relaxed|conversational|playful|informal|chatty|laid[- ]back|human"},
    "depth": {"simple": r"simple|plain|basic|beginner|non[- ]?technical|eli5|layman|easy",
              "technical": r"technical|advanced|expert|jargon|nuanced|sophisticated"},
}
POLE_RES = {dim: {pole: re.compile(r"\b(?:%s)\b" % rx, re.I) for pole, rx in poles.items()} for dim, poles in POLES.items()}
FLIP = {"short": "long", "long": "short", "formal": "casual", "casual": "formal", "simple": "technical",
        "technical": "simple"}
NEG_BEFORE = re.compile(r"(?:\bnot|\bno|\bisn['\u2019]?t|\baren['\u2019]?t|\bnever|\bdon['\u2019]?t|\bnor|\bwithout|\bless|"
                        r"\bhates?|\bavoid)\s+(?:(?:too|so|very|overly|that|really|be|sound(?:ing)?|feel|come across as|"
                        r"it|look|make it|as)\s+){0,3}$", re.I)
FORMATS = {
    "deck": r"deck|slides?|presentation|powerpoint|keynote",
    "one-pager": r"one[- ]?pager|one page|1[- ]?pager|1 page|single page",
    "document": r"doc|document|memo|write[- ]?up|white ?paper",
    "spreadsheet": r"spreadsheet|excel|google sheets?|csv",
}
FORMAT_RES = {k: re.compile(r"\b(?:%s)\b" % v, re.I) for k, v in FORMATS.items()}
LAYOUTS = {"bullets": re.compile(r"\bbullet(?:ed)?(?:\s+points?)?\b|\bbullets\b", re.I),
           "prose": re.compile(r"\bprose\b|\bparagraphs?\b|\bessay\b|\bnarrative\b", re.I)}
OVERRIDE_RE = re.compile(r"\b(?:actually|instead|rather|scratch\s+that|forget\s+(?:that|what)|never\s*mind|"
                         r"on\s+second\s+thought|is\s+better|would\s+be\s+better|works\s+better|prefer(?:red)?|"
                         r"changed\s+my\s+mind|no\s+wait|correction)\b", re.I)
ANCHORS = [
    ("meeting", re.compile(r"\b(?:meeting|call|sync|demo|presentation|event|launch|release|review|interview|workshop)\b", re.I)),
    ("deadline", re.compile(r"\b(?:deadline|due|by|before|until|no later than|need it|ready|send|submit|deliver|finish|ship)\b", re.I)),
]
TRANSITION_RE = re.compile(r"\b(?:from|went|dropped|down|up|increase[sd]?|decrease[sd]?|grew|cut|reduced|vs|versus|"
                           r"before|after|was|used\s+to|now|to)\b", re.I)
REJECT_BEFORE = re.compile(r"(?:forget|scrap|ignore|drop|not|instead of|rather than|no longer|never ?mind|cancel|skip)\s+(?:about\s+|the\s+|that\s+|it\s+)*$", re.I)
KEYWORD_BEFORE = re.compile(r"\b(budget|price|cost|limit|cap|target|goal|max|min|deadline|quota)\W+(?:\w+\W+){0,2}$", re.I)


def _pole_hits(text: str):
    hits = []
    for dim, poles in POLE_RES.items():
        for pole, rx in poles.items():
            for m in rx.finditer(text):
                eff = pole
                if NEG_BEFORE.search(text[max(0, m.start() - 40):m.start()]):
                    eff = FLIP[pole]
                hits.append((dim, eff, m.group(0), m.start()))
    return hits


def find_contradictions(atoms):
    out = []
    by_dim = {}
    fmt_hits = []
    lay_hits = []
    date_hits = {}
    num_hits = {}
    for a in atoms:
        t = a["text"]
        for dim, pole, term, pos in _pole_hits(t):
            by_dim.setdefault(dim, []).append((a["id"], pos, pole, term))
        for k, rx in FORMAT_RES.items():
            for m in rx.finditer(t):
                if NEG_BEFORE.search(t[max(0, m.start() - 25):m.start()]):
                    continue
                fmt_hits.append((a["id"], m.start(), k, m.group(0)))
        for k, rx in LAYOUTS.items():
            for m in rx.finditer(t):
                lay_hits.append((a["id"], m.start(), k, m.group(0)))
        for e in a["entities"]:
            if e["kind"] == "date":
                pre = t[:e.get("pos", 0)]
                for group, arx in ANCHORS:
                    last = None
                    for am in arx.finditer(pre):
                        last = am
                    if last is not None and len(pre[last.end():].split()) <= 6:
                        date_hits.setdefault(group, []).append((a["id"], e["pos"], e["norm"], e["text"]))
                        break
            elif e["kind"] == "number":
                key = e.get("unit")
                if not key:
                    km = KEYWORD_BEFORE.search(t[max(0, e.get("pos", 0) - 40):e.get("pos", 0)])
                    key = km.group(1).lower() if km else None
                if key:
                    num_hits.setdefault(key, []).append((a["id"], e["pos"], e["norm"], e["text"]))

    by_text = {a["id"]: a["text"] for a in atoms}

    def rejected(dim, h):
        if dim in ("length", "tone", "depth"):
            return False
        return bool(REJECT_BEFORE.search(by_text.get(h[0], "")[max(0, h[1] - 30):h[1]]))

    def add(dim, label, hits, extra=None):
        sides = {}
        for aid, pos, val, term in hits:
            s = sides.setdefault(val, {"value": val, "atoms": [], "terms": []})
            if aid not in s["atoms"]:
                s["atoms"].append(aid)
            if term not in s["terms"]:
                s["terms"].append(term)
        live = [h for h in hits if not rejected(dim, h)] or hits
        last = sorted(live, key=lambda h: (h[0], h[1]))[-1]
        involved = {h[0] for h in hits}
        override = any(OVERRIDE_RE.search(a["text"]) for a in atoms if a["id"] in involved)
        rec = {"id": "c%d" % (len(out) + 1), "dimension": dim, "label": label,
               "sides": sorted(sides.values(), key=lambda s: s["atoms"][0]), "suggested_winner": last[2],
               "later_atom": last[0], "override_cue": override,
               "material": dim in ("format", "date", "number") and not override,
               "note": "heuristic: the later statement usually wins; state it as a decision the user can veto"}
        if extra:
            rec.update(extra)
        out.append(rec)

    for dim in ("length", "tone", "depth"):
        hits = by_dim.get(dim, [])
        if len({h[2] for h in hits}) >= 2:
            add(dim, dim, [(h[0], h[1], h[2], h[3]) for h in hits])
    vals = {h[2] for h in fmt_hits}
    if len(vals) >= 2 and vals != {"one-pager", "document"}:
        add("format", "deliverable format", fmt_hits)
    if len({h[2] for h in lay_hits}) >= 2:
        add("layout", "bullets vs prose", lay_hits)
    for group, hits in date_hits.items():
        if len({h[2] for h in hits}) >= 2 and len({h[0] for h in hits}) >= 2:
            add("date", "two dates for the %s" % group, hits, {"anchor": group})
    by_atom = {a["id"]: a["text"] for a in atoms}
    for key, hits in num_hits.items():
        good = [h for h in hits if not TRANSITION_RE.search(by_atom[h[0]])]
        if len({h[2] for h in good}) >= 2 and len({h[0] for h in good}) >= 2:
            add("number", "two numbers for '%s'" % key, good, {"noun": key})
    return out


# ---------------------------------------------------------------- hard tokens

def hard_tokens(atoms):
    toks = {}
    for a in atoms:
        for e in a["entities"]:
            k = e["kind"]
            if k in ("emphasis",):
                continue
            norm = e.get("norm") or e["text"].lower()
            key = (k, norm)
            t = toks.get(key)
            if t is None:
                t = {"kind": k, "text": e["text"], "norm": norm, "atoms": []}
                if k == "negation":
                    t["keys"] = e["keys"]
                toks[key] = t
            if a["id"] not in t["atoms"]:
                t["atoms"].append(a["id"])
    return sorted(toks.values(), key=lambda t: (t["atoms"][0], t["kind"], t["norm"]))


def analyze(text: str) -> dict:
    atoms = [annotate(a) for a in atomize(text)]
    return {"words": count_words(text), "atoms": atoms, "contradictions": find_contradictions(atoms),
            "hard_tokens": hard_tokens(atoms)}


# ---------------------------------------------------------------- score

APOLOGY_RE = re.compile(r"\b(?:sorry|this is (?:a )?(?:mess|messy|rambl\w*|long)|bear with me|i know this is|excuse the|"
                        r"apolog\w+|hope this makes sense|long story|rambling|word ?vomit|brain ?dump|stream of consciousness|"
                        r"not sure (?:if|how) (?:this|i)|hard to explain)\b", re.I)
FILLER_RE = re.compile(r"\b(?:um+|uh+|erm|you know|i mean|kind of|sort of|like,)\b", re.I)
SPOKEN_PUNCT_RE = re.compile(r"\b(?:new line|new paragraph|full stop|question mark|comma)\b", re.I)


ORDINAL_RE = re.compile(r"^\W*(?:(?:so|and|ok|okay|then|oh)\s+)*(first(?:ly)?|second(?:ly)?|third(?:ly)?|fourth|finally|lastly)\b", re.I)
COUNT_TASKS_RE = re.compile(r"\b(?:two|three|four|five|2|3|4|5)\s+(?:things|tasks|asks|requests|jobs|items|projects|deliverables)\b", re.I)


def compute_score(text: str, an: dict) -> dict:
    words = an["words"]
    atoms = an["atoms"]
    n = len(atoms)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    fmt_lines = sum(1 for ln in lines if BULLET_RE.match(ln))
    formatted = bool(lines) and fmt_lines / float(len(lines)) >= 0.2
    sig = {}
    sig["long_unformatted"] = {"hit": words > 150 and not formatted,
                               "evidence": "%d words (needs >150), %d of %d lines are bullets or headings" % (words, fmt_lines, len(lines))}
    ask_atoms = [a for a in atoms if "ask" in a["tags"]]
    keys = sorted({k for a in ask_atoms for k in a["ask_keys"] if k != "ask"}) or (["ask"] if ask_atoms else [])
    sig["multiple_asks"] = {"hit": len(keys) >= 2,
                            "evidence": "%d distinct ask verbs %s in atoms %s" % (len(keys), keys[:6], [a["id"] for a in ask_atoms][:8])}
    cons = an["contradictions"]
    sig["contradiction"] = {"hit": bool(cons),
                            "evidence": ", ".join("%s (%s)" % (c["id"], c["dimension"]) for c in cons) or "none found"}
    strong = [a for a in atoms if a["strong_ask"]]
    buried = False
    ev = "fewer than 6 atoms, position not meaningful"
    if n >= 6:
        pos = [(a["id"] - 1) / float(n - 1) for a in strong]
        if not strong:
            buried, ev = True, "no explicit request found in %d atoms (the ask is implicit)" % n
        elif all(0.25 < p < 0.75 for p in pos):
            buried, ev = True, "every explicit request sits in the middle half (atoms %s of %d)" % ([a["id"] for a in strong], n)
        else:
            ev = "an explicit request is near the start or end (atom %d of %d)" % (strong[0]["id"], n)
    sig["buried_ask"] = {"hit": buried, "evidence": ev}
    am = APOLOGY_RE.search(text)
    sig["apologetic_framing"] = {"hit": bool(am), "evidence": ('"%s"' % am.group(0)) if am else "none found"}
    starts = [a["text"] for a in atoms if a["text"][:1].isalpha()]
    upper = sum(1 for s in starts if s[:1].isupper())
    ratio = upper / float(len(starts)) if starts else 1.0
    fillers = len(FILLER_RE.findall(text))
    spoken = bool(SPOKEN_PUNCT_RE.search(text))
    sentences = [s for s in re.split(r"[.!?]+\s+|\n+", text) if s.strip()]
    avg = words / float(len(sentences)) if sentences else 0
    reasons = []
    if fillers >= 2:
        reasons.append("%d filler words" % fillers)
    if spoken:
        reasons.append("spoken punctuation")
    if words >= 40 and ratio < 0.2:
        reasons.append("almost no capitalised sentence starts")
    if words >= 60 and avg >= 40:
        reasons.append("%.0f words per sentence on average" % avg)
    sig["dictation_artifacts"] = {"hit": bool(reasons), "evidence": "; ".join(reasons) or "none found"}
    count = sum(1 for s in sig.values() if s["hit"])
    ords = sorted({re.sub(r"ly$", "", ORDINAL_RE.match(a["text"]).group(1).lower()) for a in atoms if ORDINAL_RE.match(a["text"])})
    counted = COUNT_TASKS_RE.search(text)
    multi = len(ords) >= 2 or bool(counted)
    multi_ev = ("ordinals %s" % ords) if len(ords) >= 2 else ('"%s"' % counted.group(0) if counted else "none found")
    material = [c["id"] for c in cons if c["material"]]
    notes = []
    if count < 2:
        rec, why = "skip", "fewer than 2 signals: one clear ask, even with lots of clean context, is a well-fed prompt"
        if cons:
            notes.append("a contradiction exists: resolve it in one line of the answer, do not restructure")
    elif material:
        rec, why = "confirm", "material contradiction %s (no stated preference settles it)" % ", ".join(material)
    elif multi:
        rec, why = "confirm", "multi-task dump (%s): confirm the task list and its order first" % multi_ev
    elif count >= 5:
        rec, why = "confirm", "5 or more signals: the dump is too tangled to guess"
    else:
        rec, why = "light", "%d signals, nothing that blocks a compact readback" % count
    for c in cons:
        if c["override_cue"]:
            notes.append("%s (%s) is settled by a stated preference; suggested winner: %s" % (c["id"], c["dimension"], c["suggested_winner"]))
    strong_keys = {k for a in atoms if a["strong_ask"] for k in a["ask_keys"] if k != "ask"}
    if len(strong_keys) >= 3:
        keys = sorted(strong_keys)
        notes.append("%d distinct requests: if they are separate deliverables this is a multi-task dump, so list and order them in confirm mode" % len(keys))
    if rec != "skip" and (rec == "confirm" or words >= 250):
        atoms_check = "required"
    elif rec == "light":
        atoms_check = "optional"
    else:
        atoms_check = "not needed"
    return {"words": words, "atoms": n, "signals": sig, "signal_count": count,
            "multi_task": {"hit": multi, "evidence": multi_ev}, "recommendation": rec,
            "reason": why, "atoms_and_check": atoms_check, "notes": notes,
            "judge": "heuristic only: you decide effort-to-redo and whether two readings of the goal diverge badly"}


def public(an: dict) -> dict:
    return {"words": an["words"],
            "atoms": [{"id": a["id"], "text": a["text"], "tags": a["tags"], "entities": a["entities"]} for a in an["atoms"]],
            "contradictions": an["contradictions"], "hard_tokens": an["hard_tokens"]}


def fmt_score(sc: dict) -> str:
    label = {"skip": "SKIP (answer directly, no restructuring)",
             "light": "LIGHT (compact readback, then proceed)",
             "confirm": "CONFIRM (full structure, wait for a yes)"}[sc["recommendation"]]
    lines = ["brain-dump score: %d of 6 signals -> %s" % (sc["signal_count"], label),
             "  why: %s" % sc["reason"],
             "  words %d | atoms %d | atoms + check: %s" % (sc["words"], sc["atoms"], sc["atoms_and_check"])]
    for name, s in sc["signals"].items():
        lines.append("  [%s] %s: %s" % ("x" if s["hit"] else " ", name, s["evidence"]))
    if sc["multi_task"]["hit"]:
        lines.append("  multi-task cue: " + sc["multi_task"]["evidence"])
    for n in sc["notes"]:
        lines.append("  note: " + n)
    lines.append("  " + sc["judge"])
    return "\n".join(lines)


def fmt_atoms(an: dict) -> str:
    atoms = an["atoms"]
    lines = ["%d atoms from %d words" % (len(atoms), an["words"])]
    for a in atoms:
        lines.append("a%-3d [%s] %s" % (a["id"], ",".join(a["tags"]), a["text"]))
        ents = ["%s %s" % (e["kind"], e["text"]) for e in a["entities"]]
        if ents:
            lines.append("       " + " | ".join(ents))
    if an["contradictions"]:
        lines.append("possible contradictions (heuristic, you judge):")
        for c in an["contradictions"]:
            sides = " vs ".join('"%s" (%s)' % (s["terms"][0], ",".join("a%d" % i for i in s["atoms"])) for s in c["sides"])
            lines.append("  %s %s: %s; later statement: %s%s" % (
                c["id"], c["label"], sides, c["suggested_winner"],
                "; a stated preference settles it" if c["override_cue"] else ""))
    else:
        lines.append("possible contradictions: none found")
    lines.append("hard tokens to carry into the spec: %d (numbers, dates, names, quotes, negations). Save --json for check." % len(an["hard_tokens"]))
    return "\n".join(lines)


def cmd_score(args) -> int:
    text = read_text(args.file)
    sc = compute_score(text, analyze(text))
    print(json.dumps(sc, ensure_ascii=False, indent=1) if args.json else fmt_score(sc))
    return 0


def cmd_atoms(args) -> int:
    an = analyze(read_text(args.file))
    print(json.dumps(public(an), ensure_ascii=False, indent=1) if args.json else fmt_atoms(an))
    return 0


# ---------------------------------------------------------------- slots

def load_types() -> dict:
    try:
        return json.loads(TYPES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        raise UsageError("cannot load assets/task_types.json: %s" % ex)


def resolve_slots(tdef: dict, common: dict) -> dict:
    res = {"required": [], "optional": []}
    for grp in res:
        for entry in tdef["slots"].get(grp, []):
            if isinstance(entry, str):
                base = dict(common[entry])
                base["id"] = entry
            else:
                ref = entry.get("ref")
                base = dict(common[ref]) if ref else {}
                if ref:
                    base["id"] = ref
                base.update({k: v for k, v in entry.items() if k != "ref"})
            base.setdefault("detect", [])
            base.setdefault("default_assumption", None)
            res[grp].append(base)
    return res


def _rx(pattern: str):
    try:
        return re.compile(pattern, re.I)
    except re.error as ex:
        raise UsageError("bad pattern in task_types.json: %s (%s)" % (pattern, ex))


def cue_regex(cue: str):
    parts = [re.escape(p) for p in cue.strip().split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"s?(?!\w)", re.I)


def detect_type(text: str, types: dict) -> list:
    ranked = []
    for order, (tid, tdef) in enumerate(types.items()):
        score = 0
        hit = []
        for cue in tdef["cues"]:
            if cue_regex(cue).search(text):
                score += len(cue.split())
                hit.append(cue)
        if score:
            ranked.append({"type": tid, "score": score, "cues": hit, "_o": order})
    ranked.sort(key=lambda r: (-r["score"], r["_o"]))
    for r in ranked:
        del r["_o"]
    return ranked


def confidence(ranked: list) -> str:
    if not ranked:
        return "none"
    if len(ranked) == 1 or ranked[0]["score"] >= 2 * ranked[1]["score"]:
        return "high" if ranked[0]["score"] >= 2 else "medium"
    return "ambiguous" if ranked[0]["score"] == ranked[1]["score"] else "medium"


def snippet(text: str, s: int, e: int) -> str:
    out = text[max(0, s - 30):e + 30].strip()
    if s - 30 > 0:
        out = "..." + out
    if e + 30 < len(text):
        out += "..."
    return out


def slot_report(tid: str, tdef: dict, common: dict, atoms: list) -> dict:
    slots = resolve_slots(tdef, common)
    answered, blockers, assumptions = [], [], []
    for grp in ("required", "optional"):
        for s in slots[grp]:
            found = None
            pats = [_rx(p) for p in s["detect"]]
            for a in atoms:
                for rx in pats:
                    m = rx.search(a["text"])
                    if m:
                        found = {"atom": a["id"], "snippet": snippet(a["text"], m.start(), m.end())}
                        break
                if found:
                    break
            base = {"id": s["id"], "label": s.get("label", s["id"]), "required": grp == "required"}
            if found:
                base.update(found)
                answered.append(base)
            elif grp == "required" and s["default_assumption"] is None:
                base["question"] = s.get("blocker_question") or "What is: %s?" % base["label"]
                blockers.append(base)
            elif s["default_assumption"] is not None:
                base["assume"] = s["default_assumption"]
                assumptions.append(base)
    overflow = blockers[3:]
    for b in overflow:
        b["assume"] = "Unstated: proceed on the most likely reading and flag it in the answer"
    return {"type": tid, "label": tdef["label"], "spec_hint": tdef["spec_hint"], "answered": answered,
            "blockers": blockers[:3], "overflow_blockers": overflow, "assumptions": assumptions + overflow}


def fmt_slots(rep: dict, ranked) -> str:
    lines = []
    if ranked is not None:
        if ranked:
            lines.append("detected: " + ", ".join("%s (%d: %s)" % (r["type"], r["score"], "/".join(r["cues"][:4])) for r in ranked[:3])
                         + " | confidence: " + confidence(ranked))
        else:
            lines.append("detected: no task type matched the cues")
    lines.append("type: %s (%s)" % (rep["type"], rep["label"]))
    lines.append("spec hint: " + rep["spec_hint"])
    lines.append("already answered (do NOT ask):")
    lines.extend('  %s%s: a%d "%s"' % (s["id"], "" if s["required"] else " (optional)", s["atom"], s["snippet"]) for s in rep["answered"])
    if not rep["answered"]:
        lines.append("  none")
    lines.append("blockers (ask these, at most 3):")
    lines.extend("  %d. %s: %s" % (i, b["id"], b["question"]) for i, b in enumerate(rep["blockers"], 1))
    if not rep["blockers"]:
        lines.append("  none: proceed")
    lines.append("assumptions (state them, do not ask):")
    lines.extend("  %s%s: %s" % (s["id"], "" if s["required"] else " (optional)", s["assume"]) for s in rep["assumptions"])
    if not rep["assumptions"]:
        lines.append("  none")
    if rep["overflow_blockers"]:
        lines.append("note: %d more blockers were demoted to assumptions to respect the 3-question cap" % len(rep["overflow_blockers"]))
    return "\n".join(lines)


def cmd_slots(args) -> int:
    data = load_types()
    types, common = data["types"], data["common"]
    if args.list:
        for tid, t in types.items():
            print("%-18s %s" % (tid, t["label"]))
        return 0
    if not args.text:
        raise UsageError("slots needs --text FILE|- (or --list)")
    if not args.type and not args.detect:
        raise UsageError("slots needs --type ID or --detect (see --list)")
    text = read_text(args.text)
    ranked = detect_type(text, types) if args.detect else None
    tid = args.type
    if not tid:
        if not ranked:
            raise UsageError("no task type matched the text; pass --type ID (see --list)")
        tid = ranked[0]["type"]
    if tid not in types:
        close = difflib.get_close_matches(tid, list(types), n=1)
        raise UsageError("unknown type '%s'%s; valid: %s" % (tid, " (did you mean %s?)" % close[0] if close else "", ", ".join(types)))
    rep = slot_report(tid, types[tid], common, atomize(text))
    if args.json:
        if ranked is not None:
            rep["detected"] = {"ranking": ranked[:3], "confidence": confidence(ranked)}
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(fmt_slots(rep, ranked))
    return 0


# ---------------------------------------------------------------- check

BUCKETS = ("goal", "deliverable", "context", "constraints", "tasks")
LEDGER_KEYS = BUCKETS + ("parked", "dropped", "resolved", "preserve")
NEG_CUE = re.compile(r"\b(?:no|not|never|don['\u2019]?t|do not|without|avoid|exclude|skip|omit|leave out|leave off|nothing|"
                     r"cannot|can['\u2019]?t|won['\u2019]?t|off[- ]limits|hold off|wait|until|forbid\w*|banned?)\b")


def parse_id(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        m = re.fullmatch(r"\s*a?(\d+)\s*", x, re.I)
        return int(m.group(1)) if m else None
    if isinstance(x, dict):
        return parse_id(x.get("id"))
    return None


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def all_strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            for s in all_strings(x):
                yield s
    elif isinstance(v, list):
        for x in v:
            for s in all_strings(x):
                yield s


def token_found(t: dict, hay: str) -> bool:
    kind, norm, text = t["kind"], t["norm"], t["text"].lower()
    if text and text in hay:
        return True
    if kind == "number":
        return re.search(r"(?<![\d.])" + re.escape(norm) + r"(?!\d)", hay) is not None
    if kind == "date":
        group = {a for a, b in DATE_ALIAS.items() if b == norm} | {norm}
        return any(g in hay for g in group)
    if kind in ("name", "quote", "ref"):
        return re.search(r"(?<!\w)" + re.escape(norm) + r"(?!\w)", hay) is not None
    if kind == "negation":
        for k in t.get("keys", []):
            for m in re.finditer(re.escape(k), hay):
                if NEG_CUE.search(hay[max(0, m.start() - 80):m.end() + 40]):
                    return True
        return False
    return False


def do_check(atoms_doc: dict, ledger: dict, spec: str) -> dict:
    if not isinstance(atoms_doc, dict) or not isinstance(atoms_doc.get("atoms"), list):
        raise UsageError("ATOMS.json must be the output of: dump.py atoms FILE --json")
    if not isinstance(ledger, dict):
        raise UsageError("LEDGER.json must be a JSON object with buckets (goal, deliverable, context, constraints, tasks, parked, dropped, resolved)")
    ids = {a["id"] for a in atoms_doc["atoms"]}
    tags = {a["id"]: a.get("tags", []) for a in atoms_doc["atoms"]}
    text_of = {a["id"]: a["text"] for a in atoms_doc["atoms"]}
    res = {"unknown_keys": [], "unknown_ids": [], "missing_reasons": [], "unmapped": [], "lost_tokens": [],
           "unresolved_contradictions": [], "warnings": []}
    for k in ledger:
        if k not in LEDGER_KEYS:
            close = difflib.get_close_matches(k, LEDGER_KEYS, n=1)
            res["unknown_keys"].append("%s%s" % (k, " (did you mean %s?)" % close[0] if close else ""))
    mapped, exempt = set(), set()
    for b in BUCKETS + ("parked", "dropped"):
        for item in as_list(ledger.get(b)):
            i = parse_id(item)
            if i is None:
                res["unknown_ids"].append("%s: cannot read an atom id from %s" % (b, json.dumps(item, ensure_ascii=False)[:60]))
                continue
            if i not in ids:
                res["unknown_ids"].append("%s: a%d does not exist" % (b, i))
                continue
            mapped.add(i)
            if b in ("parked", "dropped"):
                reason = item.get("reason") if isinstance(item, dict) else None
                if not (isinstance(reason, str) and reason.strip()):
                    res["missing_reasons"].append("%s a%d needs a reason" % (b, i))
                exempt.add(i)
                if "constraint" in tags[i]:
                    res["warnings"].append("a%d is %s but tagged constraint: check it does not apply (%s)" % (i, b, text_of[i][:60]))
    res["unmapped"] = [{"id": i, "text": text_of[i]} for i in sorted(ids - mapped)]
    strings = list(all_strings(ledger))
    hay = (spec + "\n" + "\n".join(strings)).lower()
    hay = re.sub(r"(?<=\d),(?=\d)", "", hay)
    for t in atoms_doc.get("hard_tokens", []):
        live = [i for i in t.get("atoms", []) if i not in exempt]
        if live and not token_found(t, hay):
            res["lost_tokens"].append({"kind": t["kind"], "text": t["text"], "atoms": live})
    resolved = [r for r in as_list(ledger.get("resolved")) if isinstance(r, dict)]
    for r in resolved:
        if not str(r.get("decision", "")).strip():
            res["missing_reasons"].append("resolved entry %s has no decision" % json.dumps(r.get("contradiction") or r.get("atoms"), ensure_ascii=False))
    for c in atoms_doc.get("contradictions", []):
        involved = {i for s in c.get("sides", []) for i in s.get("atoms", [])}
        ok = False
        for r in resolved:
            if str(r.get("contradiction", "")).lower() == str(c.get("id", "")).lower():
                ok = True
            elif involved & {parse_id(x) for x in as_list(r.get("atoms"))}:
                ok = True
        if not ok:
            res["unresolved_contradictions"].append({"id": c.get("id"), "label": c.get("label")})
    res["spec_given"] = bool(spec.strip())
    if not spec.strip():
        res["warnings"].append("no --spec given: tokens were searched in ledger notes and preserve only")
    res["ok"] = not any(res[k] for k in ("unknown_keys", "unknown_ids", "missing_reasons", "unmapped", "lost_tokens", "unresolved_contradictions"))
    res["counts"] = {"atoms": len(ids), "mapped": len(mapped), "hard_tokens": len(atoms_doc.get("hard_tokens", []))}
    return res


def fmt_check(r: dict) -> str:
    lines = ["check: %s (%d of %d atoms mapped, %d hard tokens)" % (
        "OK, nothing lost" if r["ok"] else "FAIL, something was lost", r["counts"]["mapped"], r["counts"]["atoms"], r["counts"]["hard_tokens"])]
    for u in r["unmapped"]:
        lines.append('  unmapped a%d: "%s" (map it to a bucket, or park it with a reason)' % (u["id"], u["text"][:90]))
    for t in r["lost_tokens"]:
        lines.append('  lost %s "%s" from %s: add it to the spec or ledger.preserve' % (t["kind"], t["text"], ",".join("a%d" % i for i in t["atoms"])))
    for c in r["unresolved_contradictions"]:
        lines.append("  unresolved %s (%s): add a resolved entry with a decision, or say why it is not a conflict" % (c["id"], c["label"]))
    for m in r["missing_reasons"] + r["unknown_ids"] + r["unknown_keys"]:
        lines.append("  fix: " + m)
    for w in r["warnings"]:
        lines.append("  warning: " + w)
    return "\n".join(lines)


def spec_text(arg) -> str:
    if arg is None:
        return ""
    if arg == "-":
        return read_text("-")
    try:
        p = Path(arg)
        if p.is_file():
            return read_text(arg)
    except (OSError, ValueError):
        pass
    return arg


def cmd_check(args) -> int:
    if [args.atoms, args.ledger, args.spec].count("-") > 1:
        raise UsageError("only one input can read stdin")
    atoms_doc = read_json(args.atoms, "ATOMS.json")
    ledger = read_json(args.ledger, "LEDGER.json")
    res = do_check(atoms_doc, ledger, spec_text(args.spec))
    print(json.dumps(res, ensure_ascii=False, indent=1) if args.json else fmt_check(res))
    return 0 if res["ok"] else 1


# ---------------------------------------------------------------- render

def _items(spec: dict, key: str) -> list:
    v = spec.get(key)
    if v is None:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    raise UsageError("spec field '%s' must be a string or a list of strings" % key)


def _one(spec: dict, key: str) -> str:
    v = spec.get(key, "")
    if isinstance(v, list):
        v = "; ".join(str(x) for x in v)
    return str(v).strip()


def _clean(s: str) -> str:
    return s.strip().rstrip(".;, ")


def _join(items) -> str:
    return "; ".join(_clean(i) for i in items)


def _bullets(items) -> str:
    return "\n".join("- " + i for i in items)


def render_readback(spec: dict) -> str:
    goal, deliv = _clean(_one(spec, "goal")), _clean(_one(spec, "deliverable"))
    cons = _items(spec, "constraints")
    out = "Taking from this: %s as %s" % (goal, deliv)
    out += ", constraints: %s." % _join(cons) if cons else "."
    assume = _items(spec, "decisions") + _items(spec, "assumptions")
    if assume:
        out += "\nAssuming %s." % _join(assume)
    parked = _items(spec, "parked")
    if parked:
        out += (" " if assume else "\n") + "Parked: %s." % _join(parked)
    qs = _items(spec, "questions")
    if qs:
        out += (" " if (assume or parked) else "\n") + "Before I proceed: %s" % " ".join(q if q.endswith("?") else q + "?" for q in qs)
    else:
        out += (" " if (assume or parked) else "\n") + "Proceeding."
    return out


def render_confirm(spec: dict) -> str:
    qs = _items(spec, "questions")
    if len(qs) > 3:
        raise UsageError("more than 3 open questions (%d): keep only true blockers and move the rest to assumptions" % len(qs))

    def block(label, items):
        return "%s:\n%s" % (label, _bullets(items)) if items else "%s: none" % label

    tasks = _items(spec, "tasks")
    order = "Task order:\n" + "\n".join("%d. %s" % (i, t) for i, t in enumerate(tasks, 1)) if tasks else "Task order: single task"
    parts = ["## What I'm reading in this", "Goal: " + _one(spec, "goal"), "Deliverable: " + _one(spec, "deliverable"),
             block("Key context", _items(spec, "context")), block("Constraints", _items(spec, "constraints")),
             block("Interpreted decisions", _items(spec, "decisions") + _items(spec, "assumptions")),
             block("Open questions", qs), block("Parked", _items(spec, "parked")), order]
    return "\n".join(parts)


def render_prompt(spec: dict) -> str:
    goal, deliv = _one(spec, "goal"), _one(spec, "deliverable")
    tasks = _items(spec, "tasks")
    task = goal + "\nDeliverable: " + deliv
    if tasks:
        task += "\nDo these in order:\n" + "\n".join("%d. %s" % (i, t) for i, t in enumerate(tasks, 1))
    secs = []
    if _one(spec, "role"):
        secs.append(("role", _one(spec, "role")))
    secs.append(("task", task))
    for tag, items in (("context", _items(spec, "context")), ("constraints", _items(spec, "constraints")),
                       ("assumptions", _items(spec, "decisions") + _items(spec, "assumptions"))):
        if items:
            secs.append((tag, _bullets(items)))
    secs.append(("output_format", _one(spec, "output_format") or deliv))
    if _items(spec, "success"):
        secs.append(("success_criteria", _bullets(_items(spec, "success"))))
    return "\n\n".join("<%s>\n%s\n</%s>" % (t, body, t) for t, body in secs)


def cmd_render(args) -> int:
    spec = read_json(args.spec, "SPEC.json")
    if not isinstance(spec, dict):
        raise UsageError("SPEC.json must be a JSON object with goal, deliverable, context, constraints, ...")
    for k in ("goal", "deliverable"):
        if not _one(spec, k):
            raise UsageError("SPEC.json needs a non-empty '%s'" % k)
    out = {"readback": render_readback, "confirm": render_confirm, "prompt": render_prompt}[args.style](spec) + "\n"
    if args.out:
        try:
            Path(args.out).write_text(out, encoding="utf-8")
        except OSError as ex:
            raise UsageError("cannot write %s: %s" % (args.out, ex.strerror or ex))
    else:
        sys.stdout.write(out)
    return 0


# ---------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dump.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("score", help="objective brain-dump score and a skip/light/confirm recommendation")
    p.add_argument("file", help="text file, or - for stdin")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_score)
    p = sub.add_parser("atoms", help="split into numbered atoms with tags, entities and possible contradictions")
    p.add_argument("file", help="text file, or - for stdin")
    p.add_argument("--json", action="store_true", help="machine output; save it as ATOMS.json for check")
    p.set_defaults(fn=cmd_atoms)
    p = sub.add_parser("slots", help="which slots of a task type are answered, blocking or assumed")
    p.add_argument("--type", help="task type id (see --list)")
    p.add_argument("--text", help="the dump: file, or - for stdin")
    p.add_argument("--detect", action="store_true", help="guess the type from cues (used when --type is absent)")
    p.add_argument("--list", action="store_true", help="list task type ids")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_slots)
    p = sub.add_parser("check", help="prove nothing was lost: every atom mapped, every hard token kept")
    p.add_argument("atoms", help="ATOMS.json from: atoms --json")
    p.add_argument("ledger", help="LEDGER.json: buckets of atom ids, parked/dropped with reasons, resolved decisions")
    p.add_argument("--spec", help="the structured spec: file, - for stdin, or literal text")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("render", help="print a spec as readback, confirm block or copy-ready prompt")
    p.add_argument("spec", help="SPEC.json")
    p.add_argument("--style", choices=("readback", "confirm", "prompt"), required=True)
    p.add_argument("--out", help="write here instead of stdout")
    p.set_defaults(fn=cmd_render)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 2
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    try:
        return args.fn(args)
    except UsageError as ex:
        print("dump.py: %s" % ex, file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
