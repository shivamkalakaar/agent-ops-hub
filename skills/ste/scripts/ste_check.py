#!/usr/bin/env python3
"""Check text against the mechanical rules of Simplified Technical English (ASD-STE100 style).

  ste_check.py check FILE|- [--mode procedural|descriptive|auto] [--json]
  ste_check.py words [--find WORD]
  ste_check.py examples [--kind procedural|descriptive|warning]

It finds patterns, not meaning. Passing is necessary, not sufficient. Exit codes: 0 clean, 1 findings, 2 usage error.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(os.path.dirname(HERE), "assets")

LIMITS = {"procedural": 20, "descriptive": 25}
MAX_SENTENCES = 6
MODALS = {"should": "must (rule) or can (possibility)", "would": "will, or the simple present",
          "could": "can", "may": "can", "might": "can", "shall": "must"}
CONTRACTION = re.compile(r"\b\w+(?:n't|'re|'ve|'ll|'d|'m)\b|\b(?:it|that|there|let|here|what|who)'s\b", re.I)
PERFECT = re.compile(r"\b(?:has|have|had)\s+(?:not\s+)?(?:been|\w+ed|\w+en|done|made|gone|seen|shown|run|begun|become)\b", re.I)
BE = r"(?:am|is|are|was|were|be|been|being)"
CONTINUOUS = re.compile(r"\b" + BE + r"\s+(?:not\s+)?(\w+ing)\b", re.I)
PASSIVE = re.compile(r"\b(?:is|are|was|were|be|been|being)\s+(?:not\s+)?(?:\w+ed|\w+en|made|done|shown|held|kept|sent|set|put|run|built|found|given|taken|written)\b(?!\s+(?:to|from)\b)", re.I)
LATIN = re.compile(r"\b(?:e\.g\.|i\.e\.|etc\.?|viz\.|cf\.|vs\.?)(?=\W|$)", re.I)
RISK = re.compile(r"\b(?:can cause|can result|risk of|danger|hazard|injur\w+|death|damage|harm)\b", re.I)
IMPERATIVE = set("""open close remove install disconnect connect turn set put push pull press hold lift lower tighten loosen
apply clean examine measure record write read select click enter type save delete copy paste start stop restart use make do not
add attach detach replace repair check find send write fill drain pour wait ensure keep let move insert lock unlock
energize de-energize supply engage disengage release take get go run create update install download upload log sign""".split())
NOTE_INSTRUCTION = re.compile(r"^(?:do not|make sure|" + "|".join(sorted(IMPERATIVE - {"do", "not", "make", "let", "keep"})) + r")\b", re.I)
LABEL = re.compile(r"^\s*\**(WARNING|CAUTION|NOTE)\**\s*:\s*", re.I)
ING_OK = {"during", "morning", "evening", "ceiling", "bring", "thing", "something", "anything", "nothing", "everything",
          "string", "spring", "king", "ring", "wing", "sing", "swing", "sting", "bearing", "housing", "fitting", "building",
          "warning", "setting", "settings", "wiring", "mounting", "logging", "heading", "training", "drawing", "reading",
          "meeting", "pricing", "shipping", "marketing", "planning", "testing", "packaging", "tubing", "plumbing", "casing"}


def load_json(name):
    with open(os.path.join(ASSETS, name), encoding="utf-8") as f:
        return json.load(f)


def mask(text):
    """Blank out code, URLs, quoted strings and inline code with spaces, keeping line numbers."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"```.*?```", blank, text, flags=re.S)
    text = re.sub(r"~~~.*?~~~", blank, text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", blank, text)
    text = re.sub(r"https?://\S+|\b[\w.-]+@[\w.-]+\.\w+\b", blank, text)
    text = re.sub(r"\"[^\"\n]*\"|“[^”\n]*”", blank, text)
    return text


def blocks(text):
    """Yield (line_no, kind, text) per paragraph, list item or heading. kind: para|item|heading."""
    cur = None
    for i, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip()
        if not line.strip():
            if cur:
                yield cur
                cur = None
            continue
        m = re.match(r"^\s*(#{1,6})\s+(.*)$", line)
        item = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$", line)
        if m or item or LABEL.match(line):
            if cur:
                yield cur
            if m:
                yield (i, "heading", m.group(2))
                cur = None
            else:
                cur = (i, "item" if item else "para", item.group(1) if item else line.strip())
        elif re.match(r"^\s*\|", line) or re.match(r"^\s*[-=_*]{3,}\s*$", line):
            if cur:
                yield cur
                cur = None
        else:
            cur = (cur[0], cur[1], cur[2] + " " + line.strip()) if cur else (i, "para", line.strip())
    if cur:
        yield cur


def sentences(s):
    s = re.sub(r"\b(e\.g|i\.e|etc|vs|cf|viz|Mr|Mrs|Dr|No)\.", lambda m: m.group(1) + "․", s)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(\[])", s)
    return [p.replace("․", ".").strip() for p in parts if p.strip()]


def words_of(s):
    s = re.sub(r"\*\*|__|[*_]", "", s)
    return [w for w in s.split() if re.search(r"\w", w)]


def sentence_kind(sent, item_kind, mode):
    if mode != "auto":
        return mode
    first = re.sub(r"[^a-z-]", "", sent.split()[0].lower()) if sent.split() else ""
    if item_kind == "item" and re.match(r"^\d", sent) is None and first in IMPERATIVE:
        return "procedural"
    if first in IMPERATIVE or LABEL.match(sent):
        return "procedural"
    return "descriptive"


def check(text, mode="auto"):
    findings = []
    subs = load_json("word-substitutions.json")["entries"]
    sub_res = [(re.compile(r"(?<![\w-])(?:%s)(?:s|d|ed|es|ing)?(?![\w-])" % "|".join(re.escape(a) for a in e["avoid"]), re.I), e) for e in subs]
    masked = mask(text)
    for line_no, kind, body in blocks(masked):
        sents = sentences(body)
        if kind == "para" and len(sents) > MAX_SENTENCES:
            findings.append((line_no, "paragraph", "error", "%d sentences in one paragraph (maximum %d)" % (len(sents), MAX_SENTENCES), "split by topic"))
        for sent in sents:
            lab = LABEL.match(sent)
            core = sent[lab.end():] if lab else sent
            k = sentence_kind(core, kind, mode)
            n = len(words_of(core))
            lim = LIMITS[k]
            if kind != "heading" and n > lim:
                findings.append((line_no, "length", "error", "%d words in a %s sentence (maximum %d): \"%s\"" % (n, k, lim, core[:60]), "cut it in two, one instruction each"))
            if ";" in core:
                findings.append((line_no, "semicolon", "error", "semicolon", "write two sentences"))
            for m in CONTRACTION.finditer(core):
                findings.append((line_no, "contraction", "error", "contraction \"%s\"" % m.group(0), "write the full form"))
            for w in re.findall(r"[A-Za-z]+", core):
                if w.lower() in MODALS:
                    findings.append((line_no, "modal", "error", "\"%s\"" % w, "use " + MODALS[w.lower()]))
            for m in PERFECT.finditer(core):
                findings.append((line_no, "perfect", "error", "present perfect \"%s\"" % m.group(0), "use the simple past or present"))
            for m in CONTINUOUS.finditer(core):
                if m.group(1).lower() not in ING_OK:
                    findings.append((line_no, "continuous", "error", "continuous form \"%s\"" % m.group(0), "use a simple form"))
            for m in PASSIVE.finditer(core):
                sev = "error" if core[m.end():].lstrip().lower().startswith("by ") else "review"
                findings.append((line_no, "passive", sev, "possible passive \"%s\"" % m.group(0), "name who acts and use active voice"))
            for m in LATIN.finditer(core):
                findings.append((line_no, "latin", "error", "\"%s\"" % m.group(0), "write for example / that is, or name the items"))
            for rx, e in sub_res:
                for m in rx.finditer(core):
                    if m.group(0).lower() == e["use"].lower():
                        continue
                    findings.append((line_no, "word", "error", "\"%s\"" % m.group(0), "use \"%s\"%s" % (e["use"], (" (" + e["note"] + ")") if e["note"] else "")))
            ings = [w for w in re.findall(r"\b[A-Za-z]{4,}ing\b", core) if w.lower() not in ING_OK
                    and not CONTINUOUS.search(core)]
            if ings and kind != "heading":
                findings.append((line_no, "ing-word", "review", "-ing word(s): %s" % ", ".join(sorted(set(ings))[:5]), "fine as a technical name, otherwise use a simple verb"))
            if lab:
                label = lab.group(1).upper()
                first = sents[0][lab.end():] if sent is sents[0] else ""
                if label in ("WARNING", "CAUTION") and sent is sents[0] and RISK.search(core.split(". ")[0]):
                    findings.append((line_no, "warning-order", "error", "%s starts with the risk" % label, "start with the command or condition, then give the risk"))
                if label == "NOTE" and NOTE_INSTRUCTION.match(core.strip()):
                    findings.append((line_no, "note-instruction", "error", "NOTE contains an instruction", "move the instruction to a step, or make it a WARNING or CAUTION"))
    findings.sort(key=lambda f: (f[0], f[1]))
    return findings


def read_input(path):
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        sys.exit_msg = "cannot read %s: %s" % (path, e)
        raise SystemExit(2)


def cmd_check(a):
    try:
        text = read_input(a.file)
    except SystemExit:
        print("ste_check: " + getattr(sys, "exit_msg", "cannot read input"), file=sys.stderr)
        return 2
    fs = check(text, a.mode)
    if a.json:
        print(json.dumps([{"line": l, "rule": r, "severity": s, "message": m, "fix": x} for l, r, s, m, x in fs], indent=1))
    else:
        for l, r, s, m, x in fs:
            print("L%-4d %-8s [%s] %s -> %s" % (l, "REVIEW" if s == "review" else "FIX", r, m, x))
        errs = sum(1 for f in fs if f[2] == "error")
        print("%d to fix, %d to review" % (errs, len(fs) - errs) if fs else "no findings")
    return 1 if any(f[2] == "error" for f in fs) else 0


def cmd_words(a):
    for e in load_json("word-substitutions.json")["entries"]:
        if a.find and a.find.lower() not in " ".join(e["avoid"]).lower() and a.find.lower() != e["use"].lower():
            continue
        print("%s -> %s%s" % (", ".join(e["avoid"]), e["use"], ("  (" + e["note"] + ")") if e["note"] else ""))
    return 0


def cmd_examples(a):
    for p in load_json("examples.json")["pairs"]:
        if a.kind and p["kind"] != a.kind:
            continue
        print("[%s]\n  before: %s\n  after:  %s\n  why:    %s\n" % (p["kind"], p["before"], p["after"], p["why"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="find rule violations in a text file")
    c.add_argument("file", help="path, or - for stdin")
    c.add_argument("--mode", choices=["procedural", "descriptive", "auto"], default="auto")
    c.add_argument("--json", action="store_true")
    w = sub.add_parser("words", help="list unapproved words and replacements")
    w.add_argument("--find", help="filter by word")
    e = sub.add_parser("examples", help="print before/after pairs")
    e.add_argument("--kind", choices=["procedural", "descriptive", "warning"])
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    return {"check": cmd_check, "words": cmd_words, "examples": cmd_examples}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
