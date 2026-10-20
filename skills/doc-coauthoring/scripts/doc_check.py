#!/usr/bin/env python3
"""
doc_check.py: document blueprints, a mechanical quality check, and a cold-reader test kit.

  python3 scripts/doc_check.py types
  python3 scripts/doc_check.py blueprint decision-doc
  python3 scripts/doc_check.py check draft.md --type decision-doc
  python3 scripts/doc_check.py check draft.md --json
  python3 scripts/doc_check.py kit draft.md --type decision-doc --out reader-test.txt
  python3 scripts/doc_check.py kit draft.md --questions questions.txt

Subcommands
  types       list the 14 document types (keys, aliases, length, purpose)
  blueprint   print one blueprint: sections with word budgets, write order, reader questions, must-have checks
  check       lint a Markdown/plain-text draft: placeholders, bottom line up front, headings vs blueprint,
              word budgets, reading time and grade, long sentences and paragraphs, undefined acronyms,
              vague words and filler, relative dates, action items without owner/date, unsourced statistics
  kit         build ONE paste-ready file for a fresh chat (cold-reader test) plus an author-only answer key

Exit codes: 0 ok, 1 findings of severity error (or any warning with --strict), 2 usage error.
Use `-` as FILE to read standard input. No network; standard library only.
"""
from __future__ import annotations

import argparse
import bisect
import difflib
import json
import os
import re
import sys
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "assets" / "doctypes.json"
READ_WPM = 230
MAX_SENTENCE_WORDS = 35
MAX_PARAGRAPH_SENTENCES = 7
OVER_BUDGET = 1.25
THIN_BUDGET = 0.5
SEV_RANK = {"error": 0, "warn": 1, "info": 2}


class UsageError(Exception):
    pass


def die(msg):
    raise UsageError(msg)


# ------------------------------------------------------------------ input and data


def read_source(path):
    if path == "-":
        data = sys.stdin.buffer.read()
    else:
        p = Path(path)
        if not p.exists():
            die("file not found: %s" % path)
        if p.is_dir():
            die("%s is a directory, give a Markdown or text file" % path)
        try:
            data = p.read_bytes()
        except OSError as ex:
            die("cannot read %s: %s" % (path, ex.strerror or ex))
    if b"\x00" in data:
        die("%s looks like a binary file, not Markdown or text" % ("stdin" if path == "-" else path))
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        sys.stderr.write("doc_check: warning: input is not valid UTF-8, undecodable bytes were replaced\n")
    return text.replace("\r\n", "\n").replace("\r", "\n")


_TYPES_CACHE = {}


def load_types():
    if "types" not in _TYPES_CACHE:
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            _TYPES_CACHE["types"] = data["types"]
        except (OSError, ValueError, KeyError) as ex:
            die("cannot read %s: %s" % (DATA_FILE, ex))
    return _TYPES_CACHE["types"]


def norm_key(s):
    s = re.sub(r"[\s_/]+", "-", s.strip().lower())
    return re.sub(r"-{2,}", "-", s).strip("-")


def resolve_type(name, types=None):
    types = types if types is not None else load_types()
    k = norm_key(name)
    if k in types:
        return k
    for key, bp in types.items():
        if k in [norm_key(a) for a in bp.get("aliases", [])]:
            return key
    names = list(types) + [a for bp in types.values() for a in bp.get("aliases", [])]
    close = difflib.get_close_matches(k, names, n=3, cutoff=0.5)
    hint = " Closest: %s." % ", ".join(close) if close else ""
    die("unknown document type '%s'.%s Run: python3 scripts/doc_check.py types" % (name, hint))


# ------------------------------------------------------------------ text primitives

WORD_RE = re.compile(r"\d+(?:[.,]\d+)+|[^\W_]+(?:['\u2019\-][^\W_]+)*")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
HR_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])[ \t]+(.*)$")
QUOTE_RE = re.compile(r"^ {0,3}>[ \t]?(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
TASK_RE = re.compile(r"^\[( |x|X)\][ \t]+")
FM_KEY_RE = re.compile(r"^[A-Za-z_][\w. -]{0,40}:(?:\s|$)")
WIKI_NOTE_RE = re.compile(r"\[\[.{0,500}?\]\]", re.S)
WIKI_LINE_RE = re.compile(r"\[\[(.{0,300}?)\]\]")

_CODE = re.compile(r"(`+)(.+?)\1")
_IMG = re.compile(r"!\[([^\]\n]{0,300})\]\([^)\n]{0,500}\)")
_LINK = re.compile(r"\[([^\]\n]{0,300})\]\([^)\n]{0,500}\)")
_REFL = re.compile(r"\[([^\]\n]{1,300})\]\[[^\]\n]{0,100}\]")
_URL = re.compile(r"https?://[^\s<>)\]]{1,2000}")
_TAG = re.compile(r"</?[A-Za-z][^>\n]{0,200}>")
_EMPH = re.compile(r"\*\*|__|~~|(?<!\w)_(?=\S)|(?<=\S)_(?!\w)|\*")
_LINKISH = re.compile(r"https?://|\]\(|\[\^\w+\]|\[\d{1,3}\]")


def clean(s, drop_code=False):
    """Strip Markdown syntax so prose analysis sees what a reader sees."""
    if "`" in s:
        s = _CODE.sub(" " if drop_code else r"\2", s)
    if "[" in s:
        s = _IMG.sub(r"\1", s)
        s = _LINK.sub(r"\1", s)
        s = _REFL.sub(r"\1", s)
    if "http" in s:
        s = _URL.sub(" ", s)
    if "<" in s:
        s = _TAG.sub(" ", s)
    return _EMPH.sub("", s)


def count_words(s):
    return len(WORD_RE.findall(s))


_SYL_CACHE = {}


def syllables(word):
    r = _SYL_CACHE.get(word)
    if r is not None:
        return r
    import unicodedata
    w = unicodedata.normalize("NFKD", word.lower())
    w = re.sub(r"[^a-z]", "", w)
    if not w:
        r = 1
    elif len(w) <= 3:
        r = 1
    else:
        w = re.sub(r"(?:[^laeiouy]es|(?<![td])ed|[^laeiouy]e)$", "", w)
        w = re.sub(r"^y", "", w)
        r = max(1, len(re.findall(r"[aeiouy]{1,2}", w)))
    _SYL_CACHE[word] = r
    return r


_SENT_END = re.compile(
    r'[.!?]+["\')\]\u201d\u2019*_]*\s+(?=[A-Z0-9"\'(\[\u201c\u2018\u00c0-\u00d6\u00d8-\u00de])')
_PROTECT = re.compile(
    r"\b(?:e\.g|i\.e|vs|cf|approx|dr|mr|mrs|ms|prof|st|jr|sr|fig|u\.s|u\.k|a\.m|p\.m|inc|ltd|co|corp)\.$", re.I)
_NO_NUM = re.compile(r"\b(?:no|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.$", re.I)


def split_sentences(text):
    """Return [(start_offset, sentence)]; abbreviations, decimals and URLs do not end a sentence."""
    out = []
    start = 0
    for m in _SENT_END.finditer(text):
        end = m.end()
        chunk = text[start:end].rstrip()
        if _PROTECT.search(chunk) or (_NO_NUM.search(chunk) and text[end:end + 1].isdigit()):
            continue
        out.append((start, chunk))
        start = end
    tail = text[start:].strip()
    if tail:
        out.append((start, tail))
    return out


def trunc(s, n=70):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


# ------------------------------------------------------------------ dates

_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|(?-i:May)|june?|july?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
DATE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
    r"|\b\d{1,2}\.\d{1,2}\.\d{4}\b"
    r"|\b" + _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?\b(?:,?\s+\d{4})?"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH + r"\b\.?(?:,?\s+\d{4})?"
    r"|\b" + _MONTH + r"\.?,?\s+\d{4}\b"
    r"|\bQ[1-4][\s']?(?:FY)?\s?(?:20)?\d{2}\b"
    r"|\bFY[\s']?(?:20)?\d{2}\b"
    r"|\bH[12][\s']?(?:20)?\d{2}\b",
    re.I)
BARE_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_WD = r"(?:mon|tues?|wednes|thurs?|fri|satur|sun)day"
REL_RE = re.compile(
    r"\b(?:next|last|this|coming|previous|past)\s+(?:week|weekend|month|quarter|year|sprint|cycle|half)\b"
    r"|\b(?:next|this|last|coming)\s+" + _WD + r"\b"
    r"|\b(?:today|tomorrow|yesterday|tonight)\b"
    r"|\b(?:recently|soon|shortly|lately)\b"
    r"|\b(?:in|within|over|after)\s+the\s+(?:next|coming|last|past)\s+(?:few\s+|couple\s+of\s+)?(?:\w+\s+)?(?:days?|weeks?|months?|quarters?|years?)\b"
    r"|\b(?:in|within)\s+(?:a\s+)?(?:few|couple\s+of|\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s+(?:business\s+)?(?:days?|weeks?|months?|quarters?|years?)\b"
    r"|\b(?:end|start|beginning|middle)\s+of\s+(?:the\s+)?(?:week|month|quarter|year|day)\b"
    r"|\b(?:later|earlier)\s+this\s+(?:week|month|quarter|year)\b"
    r"|\b(?:eod|eow|eom|eoq|eoy|cob)\b"
    r"|(?<!every )(?<!each )\b" + _WD + r"\b"
    r"|\b(?:asap|as soon as possible|in the near future|at the moment)\b",
    re.I)


def find_dates(text):
    return [m.group(0) for m in DATE_RE.finditer(text)]


def has_date(text):
    return DATE_RE.search(text) is not None


# ------------------------------------------------------------------ language tables

PLACEHOLDER_RES = [
    re.compile(
        r"\[(?:to be (?:written|added|completed|filled|determined|confirmed)|tbw|tbd|tbc|todo|to do|content here|"
        r"insert[^\]\n]{0,40}|fill[ -]?in[^\]\n]{0,40}|placeholder[^\]\n]{0,40}|your [^\]\n]{1,30}|add [^\]\n]{1,30}|"
        r"name|date|owner|number)\](?![(\[:])", re.I),
    re.compile(r"\b(?:TBD|TBC|TODO|FIXME|XXX)\b"),
    re.compile(r"\?{3,}"),
    re.compile(r"\blorem ipsum\b", re.I),
    re.compile(r"<(?:insert|your|add|todo|tbd|placeholder)[^>\n]{0,40}>", re.I),
]

VAGUE = [
    (r"\bvarious\b", "name them or give the number"),
    (r"\ba number of\b", "give the number"),
    (r"\bnumerous\b", "give the number"),
    (r"\ba (?:wide )?(?:variety|range) of\b", "name them"),
    (r"\b(?:a lot|lots) of\b", "give the number"),
    (r"\bquite a few\b", "give the number"),
    (r"\b(?:a|the) (?:vast |large |great )?majority of\b", "give the share, for example 72%"),
    (r"(?<!statistically )\bsignificant(?:ly)?\b", "give the size of the change"),
    (r"\bsubstantial(?:ly)?\b", "give the size"),
    (r"\bconsiderabl[ey]\b", "give the size"),
    (r"\betc\b\.?", "finish the list or cut it"),
    (r"\band so (?:on|forth)\b", "finish the list or cut it"),
]
FILLER = [
    (r"\bin order to\b", "to"),
    (r"\b(?:to|we|will|can|could|should|would|may|might|must|and|or|then|not|also|they|you|i|it)\s+(leverage)\b|\b(leverag(?:es|ed|ing))\b", "use"),
    (r"\butili[sz](?:e|es|ed|ing|ation)\b", "use"),
    (r"\bfacilitat(?:e|es|ed|ing)\b", "help, enable or run"),
    (r"\bdue to the fact that\b", "because"),
    (r"\bin the event that\b", "if"),
    (r"\bat this point in time\b", "now"),
    (r"\bat the end of the day\b", "cut it"),
    (r"\b(?:it is|it's) (?:important|worth) (?:to )?(?:note|noting|mention|mentioning)(?: that)?\b|\bit should be noted that\b|\bneedless to say\b", "cut it and say the thing"),
    (r"\bin terms of\b", "rephrase"),
    (r"\bwith (?:respect|regard) to\b|\bin regard to\b", "about, on"),
    (r"\bprior to\b", "before"),
    (r"\bsubsequent to\b", "after"),
    (r"\b(?:is|are) able to\b|\b(?:has|have) the ability to\b", "can"),
    (r"\bon a (?:regular|daily|weekly|monthly) basis\b", "say how often, for example every Monday"),
    (r"\b(?:going|moving) forward\b", "cut it, or give the date"),
    (r"\b(?:basically|essentially|actually|literally|simply|obviously|of course)\b", "cut it"),
    (r"\b(?:very|really|extremely|quite|fairly|somewhat)\b", "cut it or pick a stronger word"),
    (r"\b(?:circle back|touch base|synerg(?:y|ies)|low[- ]hanging fruit|move the needle|deep dive|game[- ]changer|paradigm shift|best[- ]in[- ]class|world[- ]class|cutting[- ]edge|state[- ]of[- ]the[- ]art|holistic|seamless(?:ly)?|robust)\b", "say what it is or does"),
    (r"\b(?:each and every|first and foremost|one and only)\b", "cut the doubled word"),
    (r"\bwhether or not\b", "whether"),
    (r"\bthe fact that\b", "rephrase"),
    (r"\bas per\b", "per, or as"),
    (r"\bin light of\b", "given"),
    (r"\blearnings\b", "lessons"),
]
VAGUE = [(re.compile(p, re.I), s) for p, s in VAGUE]
FILLER = [(re.compile(p, re.I), s) for p, s in FILLER]

KNOWN_ACRONYMS = set("""
AI API ASAP AM B2B B2C CEO CFO COO CTO CMO CIO CPO CRO CRM CSS CSV CV COVID DIY EU EUR FAQ FTE FY FYI GBP GDP GPS HR
HTML HTTP HTTPS ID IT JSON KPI LLC LLM ML MTD MVP NDA OK OKR PC PDF PM PR PTO QA QTD ROI RSVP SEO SLA SMS SQL TV UI UK
UN URL US USA USD UX VAT VP SVP EVP XML YTD README EOD EOW EOM EOQ EOY ETA
""".split())
STOP_CAPS = set("""
NOT MUST SHALL SHOULD MAY AND OR THE ALL ANY NEW NOTE WARNING CAUTION IMPORTANT CRITICAL DO IF IN ON OF TO IS AT BY NO
YES AS AN OUT TOP OFF ADD END FOR USE ONE TWO NOW PRO CON PROS CONS DONE OPEN FIXED RED GREEN AMBER BLUE YELLOW DRAFT
FINAL PUBLIC INTERNAL CONFIDENTIAL WIP BUT WITH FROM THIS THAT WILL CAN TBD TBC TODO FIXME XXX
""".split())
ACR_RE = re.compile(r"\b([A-Z]{2,8})(?:s|'s|\u2019s)?\b")
ACR_DEF_RES = [
    re.compile(r"\(\s*([A-Z]{2,8})s?\s*\)"),
    re.compile(r"\b([A-Z]{2,8})s?\s*\((?=[^)]*\s)[^)]{4,80}\)"),
    re.compile(r"\b([A-Z]{2,8})s?\s+(?:stands for|means|is short for|is an acronym for|refers to|is defined as)\b"),
    re.compile(r"^[\s|*_-]*([A-Z]{2,8})[\s*_]*(?:[:|=]|\s[-\u2013\u2014]\s)\s*\S"),
]
GLOSSARY_HEAD = re.compile(r"\b(?:glossary|definition|term|abbreviation|acronym|terminology)s?\b")

STAT_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s?%"
    r"|\b\d+(?:[.,]\d+)?\s?(?:percent|per cent|percentage points?)\b"
    r"|\b\d+(?:\.\d+)?x\b"
    r"|\b\d+\s+(?:in|out of)\s+\d+\b"
    r"|\b\d+(?:[.,]\d+)?\s?(?:million|billion|trillion)\b", re.I)
OWN_TARGET_RE = re.compile(
    r"\b(?:target|targets|goal|goals|aim|aims|aiming|objective|objectives|threshold|guardrail|sla|slo|okr|kpi|budget|"
    r"at least|no more than|no less than|up to|under|below|exceeds?|if|we (?:will|want|plan|expect|aim|hope)|should|must|"
    r"need to|needs to|assum\w+|estimat\w+|forecast\w*|project(?:ed|ion|ions)|expect(?:ed|s)?|"
    r"roll(?:ed|ing|s)? ?out|rollout|ship(?:ped|ping)?|launch(?:ed|ing)?|releas(?:e|ed|ing)|deploy(?:ed|ing)?|"
    r"enabl(?:e|ed|ing)|ramp(?:ed|ing)?|raise|increase|reduce|cut|grow|improve|lift|boost|achieve|reach|hit|"
    r"maintain|lower|decrease|keep)\b", re.I)
SRC_RE = re.compile(
    r"https?://|\]\(|\[\^?\w{1,12}\]|\bsource[sd]?\b|\baccording to\b|\bper (?:the )?[A-Z]|\bbased on\b|\bsurvey\w*\b|"
    r"\bstud(?:y|ies)\b|\breport(?:s|ed)?\b|\bresearch\b|\banalysis\b|\bdashboard\b|\bdata (?:from|shows?|show)\b|"
    r"\b(?:our|internal) (?:data|numbers|metrics|analytics|records)\b|\bas of\b|\bmeasured\b|\bcited\b|\bbenchmark\w*\b|"
    r"\bfrom the\b", re.I)

ACTION_HEAD_RE = re.compile(r"\b(?:next step|action|todo|to do|follow up|followup|task|commitment)s?\b")
ACTION_LEAD_RE = re.compile(r"^(?:action(?: item)?s?|next steps?|follow[- ]?up|todo)\s*[:\-]", re.I)
_NAME = r"[A-Z][\w&/'\u2019.-]*(?: [A-Z][\w&/'\u2019.-]*){0,2}"
_NOT_NAMES = r"(?!(?:We|I|They|It|The|This|That|Team|Someone|Everyone|Anyone|You|There|Each|All|A|An)\b)"
_LABELS = (r"(?!(?:Action|Actions|Note|Next|Todo|Step|Task|Owner|Due|Date|Deadline|Risk|Decision|Status|Update|Result|"
           r"Outcome|Goal|Why|What|How|When|Where|Who|Question|Open|Blocker|TBD|Ask|Follow|Send|Write|Create|Schedule|"
           r"Confirm|Check|Fix|Share|Plan|Test|Run|Ship|Launch|Publish|Prepare|Complete|Add|Remove|Migrate|Build|"
           r"Design|Review|Finalize|Draft|Get|Set|Make|Define|Decide|Agree|Align)\b)")
_PAREN_SKIP = r"(?!Q[1-4]\b|FY|P[0-3]\b|TBD|TBC|ASAP|EOD|EOW|see\b)"
OWNER_RES = [
    re.compile(r"@[\w.-]+"),
    re.compile(r"\b(?:owner|owned by|dri|assignee|assigned to|responsible|accountable|lead|led by)\b", re.I),
    re.compile(r"^" + _NOT_NAMES + _LABELS + _NAME + r"(?: and " + _NAME + r")?\s+(?:will|owns|is responsible|drives|leads|must|should|can|needs to|has to|shall|to(?!\s+(?:the|a|an|all|our|your|my|his|her|their|its|this|that|these|those|each|every|v?\d)\b))\b"),
    re.compile(r"^" + _LABELS + _NAME + r"\s*(?::|\s[-\u2013\u2014]\s)"),
    re.compile(r"\(" + _PAREN_SKIP + _NAME + r"\s*(?:\)|[,;])"),
    re.compile(r"\s[-\u2013\u2014]\s*" + _NAME + r"\s*(?:,.*)?$"),
]
OWNER_COL = re.compile(r"owner|who|assignee|assigned|dri|responsible|lead", re.I)
DATE_COL = re.compile(r"date|due|when|deadline|\bby\b|eta|target", re.I)
EMPTY_CELL = re.compile(r"^\W*(?:tbd|tbc|n/?a|none|\?+)?\W*$", re.I)

SUMMARY_TOKENS = ("summary", "tl dr", "tldr", "bottom line", "key finding", "the ask", "abstract", "headline",
                  "recommendation", "decision needed", "overall status")
CUE_RE = re.compile(
    r"\b(?:recommend\w*|propos\w+|decid\w*|decision|approv\w*|we (?:should|will|need|ask|request|found|are asking|suggest|plan)|"
    r"i (?:recommend|propose|ask|need|suggest|am asking|found)|the ask|bottom line|in short|tl;?dr|in summary|"
    r"to summari[sz]e|conclusion|key (?:finding|takeaway|point)s?|main (?:finding|takeaway|point)s?|the answer|"
    r"status|on track|at risk|off track|blocked|goal is|purpose is|"
    r"this (?:doc|document|memo|brief|proposal|plan|report|policy|guide|procedure)\s+(?:\w+\s+){0,3}?"
    r"(?:is|proposes|recommends|asks|explains|describes|covers|sets out|defines|outlines|summari[sz]es|walks))\b", re.I)


# ------------------------------------------------------------------ document model


class Block(object):
    __slots__ = ("kind", "start", "parts", "ordered", "task", "header", "rows", "row_lines",
                 "text", "offs_pos", "offs_ln", "sents", "linkish", "pw")

    def __init__(self, kind, start):
        self.kind = kind
        self.start = start
        self.parts = []
        self.ordered = False
        self.task = None
        self.header = None
        self.rows = None
        self.row_lines = None
        self.text = ""
        self.offs_pos = []
        self.offs_ln = []
        self.sents = []
        self.linkish = False
        self.pw = []

    def line_at(self, off):
        if not self.offs_pos:
            return self.start
        return self.offs_ln[max(0, bisect.bisect_right(self.offs_pos, off) - 1)]


class Heading(object):
    __slots__ = ("level", "text", "norm", "tokens", "line", "end", "is_title")

    def __init__(self, level, text, line):
        self.level = level
        self.text = text
        self.line = line
        self.norm = norm_heading(text)
        self.tokens = stem_tokens(self.norm)
        self.end = 0
        self.is_title = False


def norm_heading(s):
    s = clean(s.strip())
    s = re.sub(r"^\s*(?:\d+(?:\.\d+)*[.):]?|[IVXLC]{1,5}[.):]|[A-Za-z][.):])\s+", "", s)
    s = s.lower().replace("&", " and ")
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


def stem_tokens(norm):
    out = []
    for t in norm.split():
        if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
            t = t[:-1]
        out.append(t)
    return out


def split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def find_front_matter(lines):
    if not lines or lines[0].strip() != "---":
        return -1
    for j in range(1, min(len(lines), 60)):
        if lines[j].strip() in ("---", "..."):
            body = [x for x in lines[1:j] if x.strip()]
            if body and sum(1 for x in body if FM_KEY_RE.match(x)) * 2 >= len(body):
                return j
            return -1
    return -1


class Doc(object):
    pass


def parse_doc(text):
    d = Doc()
    lines = text.split("\n")
    n = len(lines)
    kind = ["text"] * n
    vis = list(lines)
    notes = []
    fm_end = find_front_matter(lines)
    for i in range(fm_end + 1):
        kind[i] = "front"
    fence = None
    in_comment = False
    c_start = 0
    c_buf = []
    for i in range(fm_end + 1, n):
        line = lines[i]
        if fence:
            kind[i] = "code"
            m = FENCE_RE.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not m.group(2).strip():
                fence = None
            continue
        if not in_comment:
            m = FENCE_RE.match(line)
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = (m.group(1)[0], len(m.group(1)))
                kind[i] = "code"
                continue
        if in_comment or "<!--" in line:
            was_in = in_comment
            out = []
            pos = 0
            while pos < len(line):
                if in_comment:
                    j = line.find("-->", pos)
                    if j < 0:
                        c_buf.append(line[pos:])
                        pos = len(line)
                        break
                    c_buf.append(line[pos:j])
                    notes.append((c_start, "comment", " ".join(c_buf).strip()))
                    in_comment = False
                    c_buf = []
                    pos = j + 3
                else:
                    j = line.find("<!--", pos)
                    if j < 0:
                        out.append(line[pos:])
                        break
                    out.append(line[pos:j])
                    in_comment = True
                    c_start = i + 1
                    c_buf = []
                    pos = j + 4
            vis[i] = "".join(out)
            if (was_in or in_comment or vis[i] != line) and not vis[i].strip():
                kind[i] = "comment"
                continue
        if "[[" in vis[i]:
            def _wiki(m, _i=i):
                notes.append((_i + 1, "note", m.group(1).strip()))
                return ""
            vis[i] = WIKI_LINE_RE.sub(_wiki, vis[i])
            if not vis[i].strip():
                kind[i] = "comment"
    d.lines, d.vis, d.kind, d.n, d.notes = lines, vis, kind, n, notes
    d.fm_end = fm_end
    d.code_lines = sum(1 for k in kind if k == "code")

    blocks = []
    headings = []
    cur = None
    i = 0

    def finalize(b):
        pos = 0
        texts = []
        for ln, t in b.parts:
            if b.task is not None and not b.offs_pos:
                t = TASK_RE.sub("", t, count=1)
            c = clean(t)
            b.offs_pos.append(pos)
            b.offs_ln.append(ln)
            texts.append(c)
            pos += len(c) + 1
        b.text = " ".join(texts)
        b.pw = [count_words(c) for c in texts]
        b.linkish = any(_LINKISH.search(t) for _, t in b.parts)
        if b.kind in ("para", "item", "quote"):
            for off, s in split_sentences(b.text):
                b.sents.append((off, s, count_words(s)))

    def flush():
        nonlocal cur
        if cur is not None:
            finalize(cur)
            blocks.append(cur)
            cur = None

    while i < n:
        if kind[i] not in ("text",):
            flush()
            i += 1
            continue
        s = vis[i]
        if not s.strip():
            flush()
            i += 1
            continue
        m = HEADING_RE.match(s)
        if m and m.group(2).strip():
            flush()
            h = Heading(len(m.group(1)), m.group(2).strip(), i + 1)
            headings.append(h)
            b = Block("heading", i + 1)
            b.parts.append((i + 1, h.text))
            finalize(b)
            blocks.append(b)
            i += 1
            continue
        if HR_RE.match(s):
            flush()
            i += 1
            continue
        if "|" in s and i + 1 < n and kind[i + 1] == "text" and "|" in vis[i + 1] and TABLE_SEP_RE.match(vis[i + 1]):
            flush()
            b = Block("table", i + 1)
            b.header = split_row(s)
            b.rows = []
            b.row_lines = []
            b.parts.append((i + 1, " | ".join(b.header)))
            j = i + 2
            while j < n and kind[j] == "text" and vis[j].strip() and "|" in vis[j]:
                cells = split_row(vis[j])
                b.rows.append(cells)
                b.row_lines.append(j + 1)
                b.parts.append((j + 1, " | ".join(cells)))
                j += 1
            finalize(b)
            blocks.append(b)
            i = j
            continue
        mq = QUOTE_RE.match(s)
        if mq:
            content = re.sub(r"^(?:>\s?)+", "", mq.group(1))
            if cur is not None and cur.kind == "quote":
                cur.parts.append((i + 1, content))
            else:
                flush()
                cur = Block("quote", i + 1)
                cur.parts.append((i + 1, content))
            i += 1
            continue
        ml = LIST_RE.match(s)
        if ml:
            flush()
            cur = Block("item", i + 1)
            cur.ordered = ml.group(2)[0].isdigit()
            body = ml.group(3)
            mt = TASK_RE.match(body)
            if mt:
                cur.task = mt.group(1) in ("x", "X")
            cur.parts.append((i + 1, body))
            i += 1
            continue
        if cur is not None and cur.kind in ("para", "item", "quote"):
            cur.parts.append((i + 1, s.strip()))
        else:
            cur = Block("para", i + 1)
            cur.parts.append((i + 1, s.strip()))
        i += 1
    flush()

    stack = []
    for k, h in enumerate(headings):
        while stack and headings[stack[-1]].level >= h.level:
            headings[stack.pop()].end = h.line
        stack.append(k)
    for k in stack:
        headings[k].end = n + 1
    h1 = [h for h in headings if h.level == 1]
    if headings and len(h1) == 1 and h1[0] is headings[0]:
        headings[0].is_title = True
    d.blocks = blocks
    d.headings = headings
    d.sections = [h for h in headings if not h.is_title]
    d.sec_level = min([h.level for h in d.sections]) if d.sections else 0

    lw = [0] * (n + 1)
    hw = 0
    for b in blocks:
        if b.kind == "heading":
            hw += b.pw[0]
            continue
        for (ln, _), w in zip(b.parts, b.pw):
            lw[ln - 1] = w
    pre = [0]
    for x in lw:
        pre.append(pre[-1] + x)
    d.prefix = pre
    d.total_words = pre[-1] + hw
    return d


def words_between(d, first_line, end_line):
    """Words in body lines first_line..end_line-1 (1-based, end exclusive)."""
    a = max(0, first_line - 1)
    b = min(len(d.prefix) - 1, end_line - 1)
    return d.prefix[b] - d.prefix[a] if b > a else 0


# ------------------------------------------------------------------ blueprint matching


def score_heading(h_tokens, cand_tokens):
    if not h_tokens or not cand_tokens:
        return 0
    if h_tokens == cand_tokens:
        return 100
    lh, lc = len(h_tokens), len(cand_tokens)
    if lc < lh:
        for i in range(lh - lc + 1):
            if h_tokens[i:i + lc] == cand_tokens:
                return 60 + 5 * lc
    elif lh < lc:
        for i in range(lc - lh + 1):
            if cand_tokens[i:i + lh] == h_tokens:
                return 40 + 5 * lh
    return 0


def match_sections(d, bp):
    """One-to-one match of document headings to blueprint sections. Returns {section_index: Heading}."""
    cands = []
    for si, sec in enumerate(bp["sections"]):
        names = [sec["name"]] + list(sec.get("aka", []))
        toks = [stem_tokens(norm_heading(x)) for x in names]
        for hi, h in enumerate(d.sections):
            best = max(score_heading(h.tokens, t) for t in toks)
            if best:
                cands.append((-best, h.level, h.line, si, hi))
    cands.sort()
    used_s, used_h, out = set(), set(), {}
    for _, _, _, si, hi in cands:
        if si in used_s or hi in used_h:
            continue
        used_s.add(si)
        used_h.add(hi)
        out[si] = d.sections[hi]
    return out


# ------------------------------------------------------------------ findings


def F(sev, code, line, msg, **extra):
    d = {"severity": sev, "code": code, "line": line, "message": msg}
    d.update(extra)
    return d


def find_placeholders(d):
    out = []
    for i in range(d.n):
        if d.kind[i] not in ("text", "front"):
            continue
        s = d.vis[i]
        if not s.strip():
            continue
        if "`" in s:
            s = _CODE.sub(lambda m: " " * len(m.group(0)), s)
        spans = []
        for rx in PLACEHOLDER_RES:
            for m in rx.finditer(s):
                spans.append((m.start(), m.end()))
        spans.sort()
        last = -1
        for a, b in spans:
            if a < last:
                continue
            last = b
            out.append(F("error", "placeholder", i + 1, "Unfinished placeholder: %s" % trunc(s[a:b], 40), snippet=trunc(s[a:b], 40)))
    return out


def check_notes(d):
    out = []
    for ln, kind, text in d.notes:
        if kind == "note":
            out.append(F("warn", "author-note", ln, "Author note [[...]] still in the text: %s. Resolve it or delete it before sharing." % trunc(text, 50)))
        else:
            out.append(F("info", "author-note", ln, "Hidden comment <!-- ... --> still in the file: %s. Readers who paste raw text will see it." % trunc(text, 50)))
    return out


def first_words_text(d, limit):
    parts = []
    acc = 0
    for b in d.blocks:
        if acc >= limit:
            break
        t = b.text
        toks = list(WORD_RE.finditer(t))
        if acc + len(toks) > limit:
            take = limit - acc
            t = t[: toks[take - 1].end()] if take > 0 else ""
            acc = limit
        else:
            acc += len(toks)
        parts.append(t)
    return " ".join(parts)


def check_bottom_line(d):
    if d.total_words == 0:
        return []
    head = first_words_text(d, 120)
    if CUE_RE.search(head):
        return []
    for idx, h in enumerate(d.sections):
        if any(tok in h.norm for tok in SUMMARY_TOKENS):
            if idx <= 1:
                return []
            return [F("info", "summary-late", h.line, "The summary section '%s' is section %d. Move it to the top so the reader gets the bottom line first." % (trunc(h.text, 40), idx + 1))]
    return [F("warn", "no-bottom-line", None, "No bottom line in the first 120 words. Lead with the recommendation, the decision needed or the one-sentence answer, or add a Summary section at the top.")]


def check_sections(d, bp):
    """Headings vs blueprint, word budgets. Returns (findings, section_rows)."""
    out = []
    matched = match_sections(d, bp) if bp else {}
    by_heading = {id(h): si for si, h in matched.items()}
    rows = []
    for h in d.sections:
        row = {"heading": h.text, "level": h.level, "line": h.line,
               "words": words_between(d, h.line + 1, h.end), "matches": None, "budget": None}
        si = by_heading.get(id(h))
        if si is not None:
            row["matches"] = bp["sections"][si]["name"]
            row["budget"] = bp["sections"][si]["words"]
        rows.append(row)
    # empty sections: no words up to the next heading of any level, and no child heading
    hs = d.headings
    for k, h in enumerate(hs):
        if h.is_title:
            continue
        nxt = hs[k + 1] if k + 1 < len(hs) else None
        stop = nxt.line if nxt else d.n + 1
        if words_between(d, h.line + 1, stop) == 0 and not (nxt and nxt.level > h.level):
            out.append(F("warn", "empty-section", h.line, "Section '%s' has no text." % trunc(h.text, 40)))
    if not bp:
        return out, rows
    secs = bp["sections"]
    if not d.sections:
        out.append(F("warn", "no-headings", None, "No headings found, so the %s blueprint cannot be compared. Use one heading per section (## Name)." % bp["name"]))
        return out, rows
    missing = [sec for si, sec in enumerate(secs) if si not in matched and not sec.get("optional")]
    for sec in missing:
        out.append(F("warn", "missing-section", None, "Missing section '%s': %s" % (sec["name"], sec["purpose"])))
    required = [x for x in secs if not x.get("optional")]
    if len(missing) >= 3 and len(missing) * 10 >= len(required) * 6:
        out.append(F("warn", "wrong-type", None, "Only %d of %d expected sections found. Is '%s' the right type? Run: python3 scripts/doc_check.py types" % (len(required) - len(missing), len(required), bp["name"])))
    top_si, top_name = -1, ""
    for si in sorted(matched, key=lambda k: matched[k].line):
        if si < top_si:
            out.append(F("info", "section-order", matched[si].line, "Section '%s' comes after '%s'; the blueprint reading order puts it earlier." % (secs[si]["name"], top_name)))
        else:
            top_si, top_name = si, secs[si]["name"]
    for si, h in matched.items():
        sec = secs[si]
        lo, hi = sec["words"]
        w = words_between(d, h.line + 1, h.end)
        if hi and w > hi * OVER_BUDGET:
            out.append(F("warn", "section-over-budget", h.line, "Section '%s' is %d words; the budget is %d to %d. Cut to what the reader needs to act." % (trunc(h.text, 40), w, lo, hi)))
        elif w > 0 and lo and w < lo * THIN_BUDGET:
            out.append(F("warn", "section-thin", h.line, "Section '%s' is only %d word%s; the budget is %d to %d. Good looks like: %s" % (trunc(h.text, 40), w, "" if w == 1 else "s", lo, hi, sec["good_looks_like"])))
    used = set(id(h) for h in matched.values())
    extra = [h.text for h in d.sections if h.level == d.sec_level and id(h) not in used]
    if extra:
        more = " and %d more" % (len(extra) - 8) if len(extra) > 8 else ""
        out.append(F("info", "extra-sections", None, "Sections outside the blueprint: %s%s" % (", ".join("'%s'" % trunc(x, 30) for x in extra[:8]), more)))
    lo, hi = bp["length_words"]
    if d.total_words > hi * OVER_BUDGET:
        out.append(F("warn", "doc-long", None, "The document is %d words; a %s runs %d to %d. Cut, or move detail to an appendix." % (d.total_words, bp["name"].lower(), lo, hi)))
    elif d.total_words < lo * THIN_BUDGET:
        out.append(F("info", "doc-short", None, "The document is %d words; a %s usually runs %d to %d." % (d.total_words, bp["name"].lower(), lo, hi)))
    return out, rows


# ---- readability


def fk_grade(words, sentences, syl):
    """Flesch-Kincaid grade level, clamped at 0 and rounded to one decimal."""
    if not words or not sentences:
        return None
    return round(max(0.0, 0.39 * words / sentences + 11.8 * syl / words - 15.59), 1)


def readability(d):
    sent_n = 0
    word_n = 0
    syl_n = 0
    long_sents = []
    long_paras = []
    for b in d.blocks:
        if b.kind not in ("para", "item", "quote"):
            continue
        cnt = 0
        for off, s, wc in b.sents:
            if wc == 0:
                continue
            cnt += 1
            sent_n += 1
            word_n += wc
            for w in WORD_RE.findall(s):
                syl_n += syllables(w)
            if wc > MAX_SENTENCE_WORDS:
                long_sents.append((b.line_at(off), wc, s))
        if cnt > MAX_PARAGRAPH_SENTENCES:
            long_paras.append((b.start, cnt))
    grade = fk_grade(word_n, sent_n, syl_n) if word_n >= 30 else None
    return {"sentences": sent_n, "words": word_n, "grade": grade, "long_sents": long_sents, "long_paras": long_paras}


def check_readability(d, rd, grade_max):
    out = []
    for ln, wc, s in rd["long_sents"]:
        out.append(F("warn", "long-sentence", ln, "%d-word sentence (limit %d): \"%s\". Split it or cut it." % (wc, MAX_SENTENCE_WORDS, trunc(s, 60)), words=wc))
    for ln, cnt in rd["long_paras"]:
        out.append(F("warn", "long-paragraph", ln, "%d sentences in one paragraph (limit %d). Split it, or turn parallel points into a list." % (cnt, MAX_PARAGRAPH_SENTENCES), sentences=cnt))
    if rd["grade"] is not None and rd["grade"] > grade_max:
        out.append(F("warn", "grade-high", None, "Reading grade %.1f is above the target of %d. Prefer short sentences and common words." % (rd["grade"], grade_max)))
    return out


# ---- acronyms


def check_acronyms(d, known):
    known = KNOWN_ACRONYMS | set(known)
    gloss_spans = [(h.line, h.end) for h in d.sections if GLOSSARY_HEAD.search(h.norm)]
    uses = {}
    defs = {}
    for b in d.blocks:
        if b.kind == "heading":
            continue
        for ln, t in b.parts:
            c = clean(t, drop_code=True)
            in_gloss = any(a <= ln < e for a, e in gloss_spans)
            for rx in ACR_DEF_RES:
                for m in rx.finditer(c):
                    a = m.group(1)
                    if a not in defs or ln < defs[a]:
                        defs[a] = ln
            letters = sum(1 for ch in c if ch.isalpha())
            if letters >= 8 and sum(1 for ch in c if ch.isupper()) > 0.7 * letters:
                continue
            for m in ACR_RE.finditer(c):
                a = m.group(1)
                if in_gloss:
                    if a not in defs or ln < defs[a]:
                        defs[a] = ln
                if a in known or a in STOP_CAPS:
                    continue
                uses.setdefault(a, []).append(ln)
    out = []
    for a in sorted(uses, key=lambda k: uses[k][0]):
        first = uses[a][0]
        if a not in defs:
            out.append(F("warn", "undefined-acronym", first, "%s is never defined (%d use%s). Spell it out once, for example 'full name (%s)', or add it to a glossary." % (a, len(uses[a]), "" if len(uses[a]) == 1 else "s", a), acronym=a))
        elif defs[a] > first:
            out.append(F("warn", "undefined-acronym", first, "%s is used before it is defined (defined on line %d). Define it at first use." % (a, defs[a]), acronym=a))
    return out


# ---- vague words, filler, relative dates, statistics, actions


def check_language(d):
    groups = {}
    for b in d.blocks:
        if b.kind not in ("para", "item", "quote"):
            continue
        for table, sev, code in ((VAGUE, "warn", "vague-quantifier"), (FILLER, "info", "filler")):
            for rx, sug in table:
                for m in rx.finditer(b.text):
                    hit = m.group(m.lastindex) if m.lastindex else m.group(0)
                    key = (code, sev, " ".join(hit.lower().split()), sug)
                    groups.setdefault(key, []).append(b.line_at(m.start()))
    out = []
    for (code, sev, phrase, sug), lns in sorted(groups.items(), key=lambda kv: (kv[0][1] != "warn", kv[1][0], kv[0][2])):
        where = ", ".join("L%d" % x for x in lns[:4]) + (" +%d" % (len(lns) - 4) if len(lns) > 4 else "")
        what = "Vague" if code == "vague-quantifier" else "Filler"
        out.append(F(sev, code, lns[0], "%s: '%s' x%d (%s). Fix: %s." % (what, phrase, len(lns), where, sug), phrase=phrase, count=len(lns)))
    return out


def _units(d):
    """Sentence-sized units: (line, text, block)."""
    for b in d.blocks:
        if b.kind in ("para", "item", "quote"):
            for off, s, wc in b.sents:
                yield b.line_at(off), s, b
        elif b.kind == "table":
            for ln, cells in zip(b.row_lines, b.rows):
                yield ln, " ".join(clean(c) for c in cells), b


def has_owner(t):
    t = t.strip()
    return any(rx.search(t) for rx in OWNER_RES)


def find_actions(d):
    """Return (findings, set_of_lines_missing_date)."""
    spans = [(h.line, h.end) for h in d.sections if ACTION_HEAD_RE.search(h.norm)]
    in_action = lambda ln: any(a <= ln < e for a, e in spans)
    out = []
    no_date_lines = set()

    def report(ln, text, owner_ok, date_ok):
        if not owner_ok:
            out.append(F("warn", "action-no-owner", ln, "Action without a clear owner: \"%s\". Name the person or team, for example 'Priya to ...'." % trunc(text, 55)))
        if not date_ok:
            no_date_lines.add(ln)
            out.append(F("warn", "action-no-date", ln, "Action without a calendar date: \"%s\". Add a date such as 2026-10-15." % trunc(text, 55)))

    for b in d.blocks:
        if b.kind == "item":
            if b.task is True:
                continue
            if b.task is False or in_action(b.start) or ACTION_LEAD_RE.match(b.text):
                report(b.start, b.text, has_owner(b.text), has_date(b.text))
        elif b.kind == "para" and ACTION_LEAD_RE.match(b.text):
            report(b.start, b.text, has_owner(b.text), has_date(b.text))
        elif b.kind == "table" and in_action(b.start):
            oc = [i for i, c in enumerate(b.header) if OWNER_COL.search(c)]
            dc = [i for i, c in enumerate(b.header) if DATE_COL.search(c)]
            if not oc and not dc:
                out.append(F("warn", "action-table-columns", b.start, "Action table has no Owner or Due column. Add both."))
                continue
            if not oc:
                out.append(F("warn", "action-table-columns", b.start, "Action table has no Owner column."))
            if not dc:
                out.append(F("warn", "action-table-columns", b.start, "Action table has no Due date column."))
            for ln, cells in zip(b.row_lines, b.rows):
                label = trunc(cells[0] if cells else "", 40)
                o_ok = True if not oc else all(i < len(cells) and not EMPTY_CELL.match(clean(cells[i])) for i in oc)
                d_ok = True if not dc else all(i < len(cells) and has_date(clean(cells[i])) for i in dc)
                report(ln, label, o_ok, d_ok)
    return out, no_date_lines


def check_relative_dates(d, skip_lines):
    out = []
    for ln, s, b in _units(d):
        if ln in skip_lines:
            continue
        m = REL_RE.search(s)
        if not m or has_date(s) or BARE_YEAR.search(s):
            continue
        out.append(F("warn", "relative-date", ln, "Relative date '%s' with no calendar date. Readers open this later; write the date, for example 'week of 2026-10-05'." % m.group(0).lower(), phrase=m.group(0).lower()))
    return out


def check_stats(d):
    out = []
    blocks = d.blocks
    for bi, b in enumerate(blocks):
        if b.kind in ("para", "item", "quote"):
            sents = b.sents
            for k, (off, s, wc) in enumerate(sents):
                m = STAT_RE.search(s)
                if not m or OWN_TARGET_RE.search(s):
                    continue
                nxt = sents[k + 1][1] if k + 1 < len(sents) else ""
                if b.linkish or SRC_RE.search(s) or (nxt and not STAT_RE.search(nxt) and SRC_RE.search(nxt)):
                    continue
                out.append(F("warn", "unsourced-stat", b.line_at(off + m.start()), "Statistic without a source: '%s'. Say where it comes from (link, report, date) or mark it as an estimate." % m.group(0).strip(), stat=m.group(0).strip()))
        elif b.kind == "table":
            txt = b.text
            if not STAT_RE.search(txt) or OWN_TARGET_RE.search(txt):
                continue
            around = txt + " " + (blocks[bi - 1].text if bi > 0 else "") + " " + (blocks[bi + 1].text if bi + 1 < len(blocks) else "")
            if b.linkish or SRC_RE.search(around):
                continue
            out.append(F("warn", "unsourced-stat", b.start, "Table contains statistics but no source. Add a 'Source:' line under it."))
    return out


# ---- blueprint must-haves

_RX_CACHE = {}


def rx_of(p):
    r = _RX_CACHE.get(p)
    if r is None:
        try:
            r = re.compile(p, re.I)
        except re.error as ex:
            die("bad regex in %s: %s" % (DATA_FILE.name, ex))
        _RX_CACHE[p] = r
    return r


class Scope(object):
    """Units of text for blueprint checks, addressable by blueprint section name."""

    def __init__(self, d, bp):
        self.d = d
        self.bp = bp
        self.matched = match_sections(d, bp)
        self.by_name = {bp["sections"][si]["name"]: h for si, h in self.matched.items()}
        units = []
        for i in range(d.fm_end + 1):
            if d.lines[i].strip():
                units.append((i + 1, d.lines[i], "front"))
        for b in d.blocks:
            if b.kind == "table":
                for ln, cells in zip(b.row_lines, b.rows):
                    units.append((ln, " ".join(clean(c) for c in cells), "row"))
            elif b.kind == "heading":
                units.append((b.start, b.text, "heading"))
            else:
                units.append((b.start, b.text, b.kind))
        units.sort(key=lambda u: u[0])
        self.units = units

    def spans(self, names):
        sp = [(self.by_name[n].line, self.by_name[n].end) for n in names if n in self.by_name]
        return sp

    def select(self, test):
        units = self.units
        if test.get("first"):
            limit = test["first"]
            acc = 0
            sel = []
            for ln, t, k in units:
                if k == "front":
                    continue
                if acc >= limit:
                    break
                toks = list(WORD_RE.finditer(t))
                if acc + len(toks) > limit:
                    take = limit - acc
                    t = t[: toks[take - 1].end()] if take > 0 else ""
                    acc = limit
                else:
                    acc += len(toks)
                sel.append((ln, t, k))
            return sel
        if test.get("in"):
            sp = self.spans(test["in"])
            if sp:
                return [u for u in units if any(a <= u[0] < e for a, e in sp)]
        return units


def run_test(test, scope):
    d = scope.d
    if "re" in test:
        rx = rx_of(test["re"])
        found = []
        for ln, t, k in scope.select(test):
            for m in rx.finditer(t):
                if test.get("date") and not has_date(t):
                    continue
                found.append(" ".join(m.group(0).lower().split()))
        return len(set(found)) >= test.get("n", 1) and len(found) >= test.get("total", 0)
    if "dates" in test:
        found = set()
        for ln, t, k in scope.select(test):
            for x in find_dates(t):
                found.add(x.lower())
        return len(found) >= test["dates"]
    if "ol" in test:
        cnt = sum(1 for b in d.blocks if b.kind == "item" and b.ordered)
        return cnt >= test["ol"]
    if "items" in test:
        best = 0
        for name in test["items"]:
            h = scope.by_name.get(name)
            if not h:
                continue
            kids = [x for x in d.headings if h.line < x.line < h.end and x.level == h.level + 1]
            items = [b for b in d.blocks if b.kind == "item" and h.line < b.start < h.end]
            rows = [r for b in d.blocks if b.kind == "table" and h.line < b.start < h.end for r in b.rows]
            best = max(best, len(kids) if kids else max(len(items), len(rows)))
        return best >= test["n"]
    return False


def check_musts(d, bp):
    scope = Scope(d, bp)
    out = []
    for chk in bp.get("checks", []):
        sev = chk.get("sev", "warn")
        if chk.get("when") and not any(rx_of(chk["when"]).search(t) for _, t, k in scope.units if k != "front"):
            continue
        if "none" in chk:
            hits = []
            for ln, t, k in scope.units:
                if k == "front":
                    continue
                for p in chk["none"]:
                    m = rx_of(p).search(t)
                    if m:
                        hits.append((ln, m.group(0)))
            if hits:
                shown = ", ".join("'%s' (L%d)" % (h, ln) for ln, h in hits[:3])
                out.append(F(sev, "must-have", hits[0][0], "%s. Found: %s." % (chk["label"], shown), check=chk["id"]))
            continue
        if not any(run_test(t, scope) for t in chk.get("any", [])):
            out.append(F(sev, "must-have", None, "Missing: %s." % chk["label"], check=chk["id"]))
    return out


# ------------------------------------------------------------------ analysis driver


def analyze(text, btype=None, bp=None, known=()):
    d = parse_doc(text)
    findings = []
    if d.total_words == 0:
        findings.append(F("error", "empty-doc", None, "The document has no text."))
    findings += find_placeholders(d)
    findings += check_notes(d)
    findings += check_bottom_line(d)
    sec_findings, rows = check_sections(d, bp)
    findings += sec_findings
    rd = readability(d)
    grade_max = int(bp["grade_max"]) if bp else 12
    findings += check_readability(d, rd, grade_max)
    findings += check_acronyms(d, known)
    findings += check_language(d)
    act, no_date = find_actions(d)
    findings += act
    findings += check_relative_dates(d, no_date)
    findings += check_stats(d)
    if bp:
        findings += check_musts(d, bp)
    findings.sort(key=lambda f: (SEV_RANK[f["severity"]], f["line"] if f["line"] is not None else 0, f["code"]))
    counts = {"error": 0, "warn": 0, "info": 0}
    for f in findings:
        counts[f["severity"]] += 1
    words = d.total_words
    stats = {
        "words": words,
        "reading_minutes": max(1, int(words / READ_WPM + 0.5)) if words else 0,
        "sentences": rd["sentences"],
        "avg_sentence_words": round(rd["words"] / rd["sentences"], 1) if rd["sentences"] else 0,
        "fk_grade": rd["grade"],
        "sections": len([h for h in d.sections if h.level == d.sec_level]),
        "code_lines": d.code_lines,
    }
    return {"stats": stats, "sections": rows, "findings": findings, "counts": counts}


def render_text(res, show_all=False, per_code=6):
    st = res["stats"]
    head = []
    if res.get("type"):
        head.append("type %s" % res["type"])
    head.append("%d words" % st["words"])
    head.append("~%d min read" % st["reading_minutes"])
    if st["fk_grade"] is not None:
        head.append("grade %.1f" % st["fk_grade"])
    head.append("%d section%s" % (st["sections"], "" if st["sections"] == 1 else "s"))
    out = ["doc_check: %s" % res["file"], " | ".join(head)]
    top = min([x["level"] for x in res["sections"]] or [0])
    secs = [r for r in res["sections"] if r["matches"] or r["level"] == top]
    if secs:
        out.append("")
        out.append("Sections (words / budget)")
        for r in secs:
            tag = ""
            if r["budget"]:
                lo, hi = r["budget"]
                tag = "  budget %d-%d" % (lo, hi)
                if r["words"] > hi * OVER_BUDGET:
                    tag += "  OVER"
                elif r["words"] < lo * THIN_BUDGET:
                    tag += "  THIN"
                tag += "  = %s" % r["matches"]
            out.append("  L%-4d %-32s %5d%s" % (r["line"], trunc(r["heading"], 32), r["words"], tag))
    labels = (("error", "ERRORS"), ("warn", "WARNINGS"), ("info", "INFO"))
    for sev, title in labels:
        items = [f for f in res["findings"] if f["severity"] == sev]
        if not items:
            continue
        out.append("")
        out.append("%s (%d)" % (title, len(items)))
        seen = {}
        hidden = {}
        for f in items:
            c = seen.get(f["code"], 0)
            if c >= per_code and not show_all:
                hidden[f["code"]] = hidden.get(f["code"], 0) + 1
                continue
            seen[f["code"]] = c + 1
            where = "L%-4d" % f["line"] if f["line"] is not None else "doc  "
            out.append("  %s %-18s %s" % (where, f["code"], f["message"]))
        for code, n in sorted(hidden.items()):
            out.append("        %-18s (+%d more, use --all or --json)" % (code, n))
    c = res["counts"]
    out.append("")
    if not res["findings"]:
        out.append("No findings. Now run the cold-reader test (kit) before you send it.")
    else:
        out.append("%d error%s, %d warning%s, %d info. Fix errors first; treat warnings as review prompts, verify each by eye." % (
            c["error"], "" if c["error"] == 1 else "s", c["warn"], "" if c["warn"] == 1 else "s", c["info"]))
    return "\n".join(out)


# ------------------------------------------------------------------ kit


def iter_notes(text):
    """Yield (start, end, body) for <!-- comments --> and [[notes]] in order. Linear time; an unclosed comment is skipped."""
    spans = []
    i = text.find("<!--")
    while i >= 0:
        j = text.find("-->", i + 4)
        if j < 0:
            break
        spans.append((i, j + 3, "c"))
        i = text.find("<!--", j + 3)
    if "[[" in text:
        for m in WIKI_NOTE_RE.finditer(text):
            if not any(a <= m.start() < b for a, b, _ in spans):
                spans.append((m.start(), m.end(), "n"))
    spans.sort()
    for a, b, _ in spans:
        yield a, b, text[a:b]


def strip_notes(text):
    """Remove <!-- ... --> and [[...]] outside fenced code. Returns (clean_text, removed, warnings)."""
    lines = text.split("\n")
    segs = []
    cur = []
    cur_start = 1
    fence = None
    for i, line in enumerate(lines):
        m = FENCE_RE.match(line)
        if fence is None:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                if cur:
                    segs.append((False, cur_start, cur))
                cur, cur_start = [line], i + 1
                fence = (m.group(1)[0], len(m.group(1)))
                continue
            if not cur:
                cur_start = i + 1
            cur.append(line)
        else:
            cur.append(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not m.group(2).strip():
                segs.append((True, cur_start, cur))
                cur, cur_start = [], i + 2
                fence = None
    if cur:
        segs.append((fence is not None, cur_start, cur))
    removed = []
    warns = []
    out_lines = []
    for is_code, start, seg in segs:
        if is_code:
            out_lines.extend(seg)
            continue
        joined = "\n".join(seg)
        if "<!--" not in joined and "[[" not in joined:
            out_lines.extend(seg)
            continue
        offs = [0]
        for s in seg:
            offs.append(offs[-1] + len(s) + 1)

        pieces = []
        last = 0
        for a, b, body in iter_notes(joined):
            removed.append({"line": start + bisect.bisect_right(offs, a) - 1,
                            "kind": "comment" if body.startswith("<!--") else "note", "text": trunc(body, 80)})
            b0 = joined.rfind("\n", 0, a) + 1
            a0 = joined.find("\n", b)
            a0 = len(joined) if a0 < 0 else a0
            before_blank = not joined[b0:a].strip()
            after_blank = not joined[b:a0].strip()
            cs, ce = a, b
            if before_blank and after_blank:
                cs, ce = b0, min(a0 + 1, len(joined))
            elif before_blank:
                while ce < a0 and joined[ce] in " \t":
                    ce += 1
            elif after_blank:
                while cs > b0 and joined[cs - 1] in " \t":
                    cs -= 1
            elif joined[cs - 1] in " \t":
                cs -= 1
            cs = max(cs, last)
            pieces.append(joined[last:cs])
            last = max(last, ce)
        pieces.append(joined[last:])
        new = re.sub(r"\n{3,}", "\n\n", "".join(pieces))
        if "<!--" in new:
            warns.append("an unclosed <!-- comment was left in place (line %d)" % (start + new[: new.find("<!--")].count("\n")))
        out_lines.extend(new.split("\n"))
    return "\n".join(out_lines), removed, warns


def parse_questions(text):
    qs = []
    for line in text.split("\n"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        s = re.sub(r"^(?:[-*+]\s+|\(?\d{1,3}[.):]\s+)", "", s).strip()
        if s:
            qs.append(s)
    return qs


KIT_INSTRUCTIONS = """You are a cold reader testing a document. You know nothing beyond what is written below: nothing about the author, the company, the project or the people named.

RULES
1. Answer only from the document. If it does not contain the answer, write exactly: NOT IN THE DOC.
2. Do not guess. Do not fill gaps with general knowledge or with what would be sensible.
3. Quote or name the part you relied on.
4. If two parts of the document disagree, say so instead of picking one.
5. Do not be polite about weak spots. The author wants to find them."""

KIT_FORMAT = """ANSWER FORMAT
For each question, reply with exactly these four lines:
Q<number>: <answer in one to three sentences, or NOT IN THE DOC>
Evidence: <short quote or section name>
Assumed: <anything you had to assume or already know to answer, or "nothing">
Confidence: <high, medium or low>

After the last question, add these four sections:
AMBIGUOUS: every sentence or term that can be read two ways (quote it and give both readings).
CONTRADICTS: every place where the document disagrees with itself (quote both sides).
ASSUMED KNOWLEDGE: terms, people, systems, dates or history the document expects you to know but never explains.
ASK THE AUTHOR: the three questions you would ask before acting on this document."""


def build_kit(doc_text, questions):
    parts = [KIT_INSTRUCTIONS, "", "=== DOCUMENT START ===", doc_text.strip("\n"), "=== DOCUMENT END ===", "", "QUESTIONS"]
    for i, q in enumerate(questions, 1):
        parts.append("%d. %s" % (i, q))
    parts += ["", KIT_FORMAT, ""]
    return "\n".join(parts)


def build_key(name, questions):
    parts = ["# Answer key (author only, never paste this into the reader chat)", "",
             "Document: %s" % name,
             "How to use: write the answer you INTENDED before you read the reader's reply, then compare.",
             "A miss means the document is unclear, not that the reader is dumb. Fix the document, rebuild the kit, test again.", ""]
    for i, q in enumerate(questions, 1):
        parts += ["## Q%d. %s" % (i, q), "Intended answer:", "Where it lives in the document:", "Reader's answer:",
                  "Verdict: [ ] correct  [ ] partly  [ ] wrong  [ ] NOT IN THE DOC", "Fix needed:", ""]
    parts += ["## Cold-reader extras", "Ambiguities reported (quote, then how you will fix each):", "",
              "Contradictions reported:", "", "Assumed knowledge reported (define it, link it, or cut it):", "",
              "Pass rule: every question answered correctly, and no new ambiguity, contradiction or assumption surfaces.", ""]
    return "\n".join(parts)


def safe_out(path, src=None):
    """Relative paths must stay inside the current directory tree; absolute paths are the user's explicit choice."""
    p = Path(path)
    if not p.is_absolute():
        cwd = os.path.realpath(os.getcwd())
        target = os.path.realpath(str(Path(cwd) / p))
        if target != cwd and not target.startswith(cwd + os.sep):
            die("refusing to write outside the current directory: %s (use an absolute path if you mean it)" % path)
        p = Path(target)
    if src and src != "-" and os.path.exists(src) and os.path.realpath(str(p)) == os.path.realpath(src):
        die("--out would overwrite the input file %s" % src)
    return p


def show_path(p):
    """Path as the user would type it: relative when inside the current directory."""
    cwd = os.path.realpath(os.getcwd())
    full = os.path.realpath(str(p))
    return full[len(cwd) + 1:] if full.startswith(cwd + os.sep) else str(p)


def write_text(p, text):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    except OSError as ex:
        die("cannot write %s: %s" % (p, ex.strerror or ex))


# ------------------------------------------------------------------ commands


def cmd_types(args):
    types = load_types()
    if args.json:
        rows = [{"key": k, "name": v["name"], "aliases": v["aliases"], "length_words": v["length_words"], "purpose": v["purpose"]} for k, v in types.items()]
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    print("Document types (pass the key or any alias to --type):")
    for k, v in types.items():
        lo, hi = v["length_words"]
        print("  %-14s %4d-%-4d words  %s" % (k, lo, hi, v["purpose"]))
        print("  %-14s also: %s" % ("", ", ".join(v["aliases"])))
    return 0


def cmd_blueprint(args):
    types = load_types()
    key = resolve_type(args.type, types)
    bp = types[key]
    if args.json:
        out = dict(bp)
        out["key"] = key
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    lo, hi = bp["length_words"]
    L = ["%s  (%s)" % (bp["name"], key), "also: %s" % ", ".join(bp["aliases"]),
         "Purpose: %s" % bp["purpose"], "Reader:  %s" % bp["reader"],
         "Length:  %d-%d words, reading grade target %d or lower" % (lo, hi, bp["grade_max"]), "", "Sections in reading order"]
    for i, s in enumerate(bp["sections"], 1):
        L.append(" %d. %s  [%d-%d words]%s" % (i, s["name"], s["words"][0], s["words"][1], "  (optional)" if s.get("optional") else ""))
        L.append("    purpose: %s" % s["purpose"])
        L.append("    good:    %s" % s["good_looks_like"])
        L.append("    failure: %s" % s["common_failure"])
    L += ["", "Write order (start where the unknowns are, framing sections last): " + " > ".join(bp["write_order"]), "", "Reader questions (seed for the cold-reader test)"]
    L += [" %d. %s" % (i, q) for i, q in enumerate(bp["reader_questions"], 1)]
    L += ["", "Must-have checks (run by `check --type %s`)" % key]
    L += ["  [%s] %s: %s" % (c.get("sev", "warn"), c["id"], c["label"]) for c in bp["checks"]]
    print("\n".join(L))
    return 0


def parse_known(s):
    return {x.strip().upper() for x in re.split(r"[,\s]+", s or "") if x.strip()}


def cmd_check(args):
    types = load_types()
    key = resolve_type(args.type, types) if args.type else None
    text = read_source(args.file)
    res = analyze(text, key, types[key] if key else None, parse_known(args.known))
    res["file"] = "<stdin>" if args.file == "-" else args.file
    res["type"] = key
    if args.json:
        ordered = {"file": res["file"], "type": key, "stats": res["stats"], "counts": res["counts"], "sections": res["sections"], "findings": res["findings"]}
        print(json.dumps(ordered, indent=2, ensure_ascii=False))
    else:
        print(render_text(res, args.all))
    c = res["counts"]
    return 1 if c["error"] or (args.strict and c["warn"]) else 0


def cmd_kit(args):
    if not args.questions and not args.type:
        die("give --questions FILE and/or --type TYPE so the kit has questions (see `types`)")
    if args.questions == "-" and args.file == "-":
        die("FILE and --questions cannot both read standard input")
    types = load_types()
    key = resolve_type(args.type, types) if args.type else None
    text = read_source(args.file)
    if not WORD_RE.search(text):
        sys.stderr.write("doc_check: the document is empty, nothing to test\n")
        return 1
    qs = []
    if args.questions:
        qs += parse_questions(read_source(args.questions))
    if key:
        qs += types[key]["reader_questions"]
    seen = set()
    questions = []
    for q in qs:
        k = " ".join(q.lower().split())
        if k not in seen:
            seen.add(k)
            questions.append(q)
    if not questions:
        die("no questions found in %s" % args.questions)
    clean_text, removed, warns = strip_notes(text)
    kit = build_kit(clean_text, questions)
    name = "<stdin>" if args.file == "-" else Path(args.file).name
    to_stdout = args.out == "-"
    key_path = None
    if to_stdout:
        sys.stdout.write(kit)
        if args.key:
            key_path = safe_out(args.key, args.file)
    else:
        out_path = safe_out(args.out, args.file)
        write_text(out_path, kit)
        key_path = safe_out(args.key, args.file) if args.key else out_path.with_name(out_path.stem + "-key.md")
    if key_path is not None:
        write_text(key_path, build_key(name, questions))
    log = sys.stderr if to_stdout else sys.stdout
    if args.json and not to_stdout:
        print(json.dumps({"kit": str(out_path), "key": str(key_path), "questions": len(questions), "stripped": removed,
                          "warnings": warns, "words": count_words(clean_text)}, indent=2, ensure_ascii=False))
        return 0
    if not to_stdout:
        log.write("kit: wrote %s (%d words, %d questions)\n" % (show_path(out_path), count_words(clean_text), len(questions)))
    if key_path is not None:
        log.write("kit: wrote %s (author only, do not paste)\n" % show_path(key_path))
    if removed:
        log.write("kit: stripped %d author note%s so the reader never sees them:\n" % (len(removed), "" if len(removed) == 1 else "s"))
        for r in removed[:12]:
            log.write("  L%-4d %-8s %s\n" % (r["line"], r["kind"], r["text"]))
        if len(removed) > 12:
            log.write("  ... and %d more\n" % (len(removed) - 12))
    else:
        log.write("kit: no author notes found to strip\n")
    for w in warns:
        log.write("kit: warning: %s\n" % w)
    if len(questions) > 15:
        log.write("kit: warning: %d questions; more than 15 makes a cold reader lose focus\n" % len(questions))
    log.write("next: paste the kit into a NEW chat with no memory and no files, then compare the reply with the key.\n")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="doc_check.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    p = sub.add_parser("types", help="list document types", description="List the document types and their aliases.")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_types)

    p = sub.add_parser("blueprint", help="print one blueprint", description="Print a blueprint: sections, budgets, write order, reader questions, must-have checks.\n  python3 scripts/doc_check.py blueprint prd", formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("type", help="type key or alias, for example decision-doc, prd, sop, post-mortem")
    p.add_argument("--json", action="store_true", help="print the raw blueprint as JSON")
    p.set_defaults(func=cmd_blueprint)

    p = sub.add_parser("check", help="lint a draft", description="Mechanical quality check with line numbers.\n  python3 scripts/doc_check.py check draft.md --type proposal\n  cat draft.md | python3 scripts/doc_check.py check - --json", formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("file", help="Markdown or text file, or - for stdin")
    p.add_argument("--type", help="blueprint key or alias: adds heading, word-budget and must-have checks")
    p.add_argument("--json", action="store_true", help="machine-readable output (all findings)")
    p.add_argument("--all", action="store_true", help="do not cap repeated findings in the text report")
    p.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    p.add_argument("--known", help="comma-separated acronyms your readers already know, for example CAC,ARR")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("kit", help="build the cold-reader test file", description="Build one paste-ready file for a fresh chat plus an author-only answer key.\n  python3 scripts/doc_check.py kit draft.md --type decision-doc --out reader-test.txt\n  python3 scripts/doc_check.py kit draft.md --questions q.txt --type prd", formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("file", help="Markdown or text file, or - for stdin")
    p.add_argument("--questions", help="text file with one question per line (numbering and # comments ignored)")
    p.add_argument("--type", help="also use this blueprint's reader-question seeds")
    p.add_argument("--out", default="reader-test.txt", help="paste-ready file to write (default reader-test.txt, - for stdout)")
    p.add_argument("--key", help="answer-key file (default: <out>-key.md next to --out)")
    p.add_argument("--json", action="store_true", help="print a JSON summary instead of the log lines")
    p.set_defaults(func=cmd_kit)
    return ap


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except UsageError as ex:
        sys.stderr.write("doc_check: %s\n" % ex)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
