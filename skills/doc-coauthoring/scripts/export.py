#!/usr/bin/env python3
"""
export.py: turn a Markdown draft into a Word (.docx) or a print-ready HTML file. Standard library only.

  python3 scripts/export.py draft.md --to docx
  python3 scripts/export.py draft.md --to docx --title "Q4 plan" --author "Ruben Hassid" --theme formal --out plan.docx
  python3 scripts/export.py draft.md --to html --page a4 --out plan.html
  cat draft.md | python3 scripts/export.py - --to docx --out draft.docx

Supported Markdown: title block, headings 1-6, paragraphs, **bold**, *italic*, ~~strike~~, `code`, [links](url),
bullet / numbered / task lists (nested), pipe tables (header shading, alignment), block quotes, fenced code,
horizontal rules. Page numbers ("Page X of Y") are added to Word footers. HTML comments are dropped.
Images are replaced by a visible [Image: alt] marker (and a warning). Optional front matter (title, author, date)
is read from a leading --- block.

Themes: clean (Calibri, blue accents) or formal (Times, black, centered title). Pages: letter (default) or a4.
Output goes to --out; without it, next to the input file if that is inside the current directory tree, else here.
Exit codes: 0 ok, 1 nothing to export (empty input), 2 usage or file error.
"""
from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path

WARNINGS = []

# ------------------------------------------------------------------ input


class UsageError(Exception):
    pass


def die(msg):
    raise UsageError(msg)


def read_source(path):
    if path == "-":
        data = sys.stdin.buffer.read()
    else:
        p = Path(path)
        if not p.exists():
            die("file not found: %s" % path)
        if p.is_dir():
            die("%s is a directory, give a Markdown file" % path)
        try:
            data = p.read_bytes()
        except OSError as ex:
            die("cannot read %s: %s" % (path, ex.strerror or ex))
    if b"\x00" in data:
        die("%s looks like a binary file, not Markdown" % ("stdin" if path == "-" else path))
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        WARNINGS.append("input is not valid UTF-8, undecodable bytes were replaced")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def safe_out(path, src=None):
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


def default_out(src, ext):
    cwd = os.path.realpath(os.getcwd())
    if src == "-":
        return Path(cwd) / ("document." + ext)
    cand = os.path.realpath(str(Path(src).with_suffix("." + ext)))
    if cand.startswith(cwd + os.sep):
        return Path(cand)
    return Path(cwd) / (Path(src).stem + "." + ext)


# ------------------------------------------------------------------ Markdown parsing

FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([^\s`]*)[^`]*$")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
HR_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])[ \t]+(.*)$")
LIST_EMPTY_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])[ \t]*$")
QUOTE_RE = re.compile(r"^ {0,3}>[ \t]?(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
TASK_RE = re.compile(r"^\[( |x|X)\][ \t]+")
FM_KEY_RE = re.compile(r"^([A-Za-z_][\w -]{0,30}):\s*(.*)$")
MAX_DEPTH = 12


def remove_comments(text):
    """Remove <!-- ... --> (and the spaces before it) in linear time. An unclosed comment is left as text."""
    out = []
    pos = 0
    while True:
        i = text.find("<!--", pos)
        if i < 0:
            break
        j = text.find("-->", i + 4)
        if j < 0:
            break
        k = i
        while k > pos and text[k - 1] in " \t":
            k -= 1
        out.append(text[pos:k])
        pos = j + 3
    out.append(text[pos:])
    return "".join(out)


def strip_comments(text):
    """Drop <!-- ... --> outside fenced code."""
    if "<!--" not in text:
        return text
    out = []
    seg = []
    fence = None
    for line in text.split("\n"):
        m = FENCE_RE.match(line)
        if fence is None and m:
            out.append(remove_comments("\n".join(seg)) if seg else None)
            seg = []
            fence = (m.group(1)[0], len(m.group(1)))
            out.append(line)
            continue
        if fence is not None:
            out.append(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not line.strip().strip(fence[0]):
                fence = None
            continue
        seg.append(line)
    if seg:
        out.append(remove_comments("\n".join(seg)))
    return "\n".join(x for x in out if x is not None)


def split_front_matter(text):
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    for j in range(1, min(len(lines), 60)):
        if lines[j].strip() in ("---", "..."):
            meta = {}
            body = [x for x in lines[1:j] if x.strip()]
            pairs = [FM_KEY_RE.match(x) for x in body]
            if body and sum(1 for m in pairs if m) * 2 >= len(body):
                for m in pairs:
                    if m:
                        meta[m.group(1).strip().lower()] = m.group(2).strip().strip("\"'")
                return meta, "\n".join(lines[j + 1:])
            return {}, text
    return {}, text


def split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def is_table_start(lines, i):
    return (i + 1 < len(lines) and "|" in lines[i] and "|" in lines[i + 1]
            and TABLE_SEP_RE.match(lines[i + 1]) is not None)


def is_block_start(lines, i):
    s = lines[i]
    return bool(HEADING_RE.match(s) or FENCE_RE.match(s) or HR_RE.match(s) or QUOTE_RE.match(s)
                or LIST_RE.match(s) or is_table_start(lines, i))


def parse_blocks(lines, depth=0):
    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = FENCE_RE.match(line)
        if m:
            fch, flen, lang = m.group(1)[0], len(m.group(1)), m.group(2)
            indent = len(line) - len(line.lstrip(" "))
            j = i + 1
            body = []
            while j < n:
                cm = FENCE_RE.match(lines[j])
                if cm and cm.group(1)[0] == fch and len(cm.group(1)) >= flen and not lines[j].strip().strip(fch):
                    break
                body.append(lines[j][indent:] if lines[j][:indent].strip() == "" else lines[j].lstrip(" "))
                j += 1
            blocks.append(("code", lang, "\n".join(body)))
            i = j + 1
            continue
        m = HEADING_RE.match(line)
        if m and m.group(2).strip():
            blocks.append(("h", len(m.group(1)), parse_inline(m.group(2).strip())))
            i += 1
            continue
        if HR_RE.match(line):
            blocks.append(("hr",))
            i += 1
            continue
        if is_table_start(lines, i):
            header = split_row(line)
            seps = split_row(lines[i + 1])
            aligns = []
            for k in range(len(header)):
                c = seps[k].strip() if k < len(seps) else ""
                aligns.append("center" if c.startswith(":") and c.endswith(":") and len(c) > 1
                              else "right" if c.endswith(":") else "left")
            rows = []
            j = i + 2
            while j < n and lines[j].strip() and "|" in lines[j]:
                cells = split_row(lines[j])
                cells = (cells + [""] * len(header))[: len(header)]
                rows.append([parse_inline(c) for c in cells])
                j += 1
            blocks.append(("table", aligns, [parse_inline(c) for c in header], rows))
            i = j
            continue
        if QUOTE_RE.match(line):
            inner = []
            j = i
            while j < n:
                qm = QUOTE_RE.match(lines[j])
                if qm:
                    inner.append(qm.group(1))
                elif lines[j].strip() and inner and inner[-1].strip() and not is_block_start(lines, j):
                    inner.append(lines[j])
                else:
                    break
                j += 1
            blocks.append(("quote", parse_blocks(inner, depth + 1) if depth < MAX_DEPTH else [("p", parse_inline(" ".join(inner)))]))
            i = j
            continue
        if LIST_RE.match(line) or LIST_EMPTY_RE.match(line):
            blk, i = parse_list(lines, i, depth)
            blocks.append(blk)
            continue
        para = [line.lstrip() if line.endswith("  ") else line.strip()]
        i += 1
        while i < n and lines[i].strip() and not is_block_start(lines, i):
            para.append(lines[i].lstrip())
            i += 1
        blocks.append(("p", parse_inline("\n".join(para).rstrip())))
    return blocks


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def parse_list(lines, i, depth):
    n = len(lines)
    first = lines[i].expandtabs(4)
    m = LIST_RE.match(first) or LIST_EMPTY_RE.match(first)
    base = len(m.group(1))
    ordered = m.group(2)[0].isdigit()
    start = int(m.group(2)[:-1]) if ordered else 1
    items = []
    cur = None
    j = i
    while j < n:
        line = lines[j].expandtabs(4)
        if not line.strip():
            k = j + 1
            while k < n and not lines[k].strip():
                k += 1
            if k >= n:
                break
            nl = lines[k].expandtabs(4)
            ml = LIST_RE.match(nl) or LIST_EMPTY_RE.match(nl)
            ind = indent_of(nl)
            if ml and ind <= base + 1 and ml.group(2)[0].isdigit() == ordered:
                j = k
                continue
            if ind > base + 1 and cur is not None:
                cur["lines"].append("")
                j += 1
                continue
            break
        ml = LIST_RE.match(line) or LIST_EMPTY_RE.match(line)
        ind = indent_of(line)
        if ml and ind <= base + 1:
            if ml.group(2)[0].isdigit() != ordered:
                break
            body = ml.groups()[2] if len(ml.groups()) > 2 else ""
            cur = {"lines": [body], "cindent": ind + len(ml.group(2)) + 1}
            items.append(cur)
            j += 1
            continue
        if cur is None:
            break
        if ind > base + 1:
            cut = min(ind, cur["cindent"])
            cur["lines"].append(line[cut:])
            j += 1
            continue
        if not is_block_start([line], 0) and lines[j - 1].strip():
            cur["lines"].append(line.strip())
            j += 1
            continue
        break
    out = []
    for it in items:
        ls = it["lines"]
        task = None
        tm = TASK_RE.match(ls[0]) if ls else None
        if tm:
            task = tm.group(1) in ("x", "X")
            ls = [ls[0][tm.end():]] + ls[1:]
        blocks = parse_blocks(ls, depth + 1) if depth < MAX_DEPTH else [("p", parse_inline(" ".join(ls)))]
        out.append({"blocks": blocks, "task": task})
    return ("list", ordered, start, out), j


# ---- inline

_SPECIAL = re.compile(r"[\\`*_~\[!<\n]|https?://")
_ENTITY = re.compile(r"&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
_TAG = re.compile(r"</?([A-Za-z][A-Za-z0-9]*)(?:\s[^>\n]{0,200})?/?>")
_AUTOLINK = re.compile(r"<((?:https?://|mailto:)[^\s<>]+)>")
_BARE_URL = re.compile(r"https?://[^\s<>\[\]()]{0,2000}(?:\([^\s<>()]*\)[^\s<>\[\]()]*)*[^\s<>\[\]().,;:!?'\"*_~]")
_IMAGE = re.compile(r"!\[([^\]\n]{0,300})\]\(\s*([^)\s]{0,1000})(?:\s+\"[^\"\n]{0,300}\")?\s*\)")
_ESCAPABLE = set("\\`*_{}[]()#+-.!|<>~&\"'")


def _unescape(t):
    return _ENTITY.sub(lambda m: html.unescape(m.group(0)), t) if "&" in t else t


def _find_bracket_end(s, i):
    """s[i] == '['. Return index of the matching ']' or -1."""
    depth = 0
    j = i
    n = min(len(s), i + 500)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            k = j
            while k < n and s[k] == "`":
                k += 1
            close = s.find(s[j:k], k)
            j = close + (k - j) if close >= 0 else k
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def _find_paren_end(s, i):
    """s[i] == '('. Return index of the matching ')' (one level of nesting allowed) or -1."""
    depth = 0
    j = i
    n = min(len(s), i + 2000)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
        elif c == "\n" and depth > 0 and s[j + 1: j + 2] == "\n":
            return -1
        j += 1
    return -1


def _find_close(s, start, c, d):
    """Find the closing delimiter of length d for emphasis char c, or -1."""
    j = start
    n = min(len(s), start + 20000)
    while j < n:
        ch = s[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "`":
            k = j
            while k < n and s[k] == "`":
                k += 1
            close = s.find(s[j:k], k)
            j = close + (k - j) if close >= 0 else k
            continue
        if ch == c:
            k = j
            while k < n and s[k] == c:
                k += 1
            run = k - j
            ok = run == d or (d == 3 and run > 3)
            if ok and j > start and not s[j - 1].isspace() and (c != "_" or k >= n or not s[k].isalnum()):
                return j
            j = k
            continue
        j += 1
    return -1


MAX_INLINE_DEPTH = 30


def parse_inline(s, flags="", href=None, _d=0):
    """Return runs: [(text, flags, href)]; text '\\n' is a hard line break."""
    if _d > MAX_INLINE_DEPTH:
        return [(s.replace("\n", " "), flags, href)] if s else []
    runs = []
    buf = []
    nofind = {}
    last_close = s.rfind("]")

    def flush():
        if buf:
            t = _unescape("".join(buf))
            if t:
                runs.append((t, flags, href))
            del buf[:]

    def brk():
        flush()
        runs.append(("\n", "", None))

    i = 0
    n = len(s)
    while i < n:
        m = _SPECIAL.search(s, i)
        if not m:
            buf.append(s[i:])
            break
        if m.start() > i:
            buf.append(s[i: m.start()])
            i = m.start()
        c = s[i]
        if c == "\\":
            if i + 1 < n and s[i + 1] == "\n":
                brk()
                i += 2
            elif i + 1 < n and s[i + 1] in _ESCAPABLE:
                buf.append(s[i + 1])
                i += 2
            else:
                buf.append(c)
                i += 1
            continue
        if c == "\n":
            txt = "".join(buf)
            del buf[:]
            if txt.endswith("  "):
                buf.append(txt.rstrip(" "))
                brk()
            else:
                buf.append(txt.rstrip(" ") + " ")
            i += 1
            while i < n and s[i] == " ":
                i += 1
            continue
        if c == "`":
            k = i
            while k < n and s[k] == "`":
                k += 1
            ticks = s[i:k]
            close = -1
            pos = k
            while True:
                q = s.find(ticks, pos)
                if q < 0:
                    break
                if (q == 0 or s[q - 1] != "`") and s[q + len(ticks): q + len(ticks) + 1] != "`":
                    close = q
                    break
                pos = q + 1
            if close < 0:
                buf.append(ticks)
                i = k
                continue
            code = s[k:close].replace("\n", " ")
            if len(code) > 2 and code[0] == " " and code[-1] == " ":
                code = code[1:-1]
            flush()
            runs.append((code, flags + ("c" if "c" not in flags else ""), href))
            i = close + len(ticks)
            continue
        if c == "!":
            im = _IMAGE.match(s, i)
            if im:
                alt = im.group(1).strip()
                WARNINGS.append("image not embedded, replaced by a marker: %s" % (im.group(2) or alt or "image"))
                flush()
                runs.append(("[Image: %s]" % alt if alt else "[Image]", flags + ("i" if "i" not in flags else ""), href))
                i = im.end()
            else:
                buf.append(c)
                i += 1
            continue
        if c == "[":
            e = _find_bracket_end(s, i) if i < last_close else -1
            if e > 0 and e + 1 < n and s[e + 1] == "(":
                pe = _find_paren_end(s, e + 1)
                if pe > 0:
                    target = s[e + 2: pe].strip()
                    target = re.sub(r"\s+\"[^\"]*\"$", "", target).strip("<>")
                    flush()
                    runs.extend(parse_inline(s[i + 1: e], flags, target if target else href, _d + 1))
                    i = pe + 1
                    continue
            buf.append(c)
            i += 1
            continue
        if c == "<":
            am = _AUTOLINK.match(s, i)
            if am:
                flush()
                runs.append((am.group(1), flags, am.group(1)))
                i = am.end()
                continue
            tm = _TAG.match(s, i)
            if tm:
                if tm.group(1).lower() == "br":
                    brk()
                else:
                    flush()
                i = tm.end()
                continue
            buf.append(c)
            i += 1
            continue
        if c in "*_~":
            k = i
            while k < n and s[k] == c:
                k += 1
            r = k - i
            handled = False
            if c == "~":
                if r == 2 and k < n and not s[k].isspace():
                    cl = s.find("~~", k, k + 20000)
                    if cl > k:
                        flush()
                        runs.extend(parse_inline(s[k:cl], flags + ("s" if "s" not in flags else ""), href, _d + 1))
                        i = cl + 2
                        handled = True
            elif k < n and not s[k].isspace() and (c != "_" or i == 0 or not s[i - 1].isalnum()):
                for d in range(min(r, 3), 0, -1):
                    if nofind.get((c, d), n + 1) <= i + r:
                        continue
                    cl = _find_close(s, i + r, c, d)
                    if cl < 0:
                        nofind[(c, d)] = min(nofind.get((c, d), n + 1), i + r)
                    if cl > 0:
                        flush()
                        if r > d:
                            runs.append((c * (r - d), flags, href))
                        add = "".join(f for f in {1: "i", 2: "b", 3: "bi"}[d] if f not in flags)
                        runs.extend(parse_inline(s[i + r:cl], flags + add, href, _d + 1))
                        i = cl + d
                        handled = True
                        break
            if not handled:
                buf.append(s[i:k])
                i = k
            continue
        if s.startswith("http", i):
            if i == 0 or not s[i - 1].isalnum():
                um = _BARE_URL.match(s, i)
                if um:
                    flush()
                    runs.append((um.group(0), flags, um.group(0)))
                    i = um.end()
                    continue
            buf.append("h")
            i += 1
            continue
        buf.append(c)
        i += 1
    flush()
    return merge_runs(runs)


def merge_runs(runs):
    out = []
    for t, f, h in runs:
        if out and t != "\n" and out[-1][0] != "\n" and out[-1][1] == f and out[-1][2] == h:
            out[-1] = (out[-1][0] + t, f, h)
        else:
            out.append((t, f, h))
    return out


def plain(runs):
    return "".join(t for t, f, h in runs if t != "\n")


def safe_href(url):
    if not url:
        return None
    url = re.sub(r"[\x00-\x20]", "", url)
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", url)
    if m and m.group(1).lower() not in ("http", "https", "mailto", "tel"):
        return None
    return url


# ------------------------------------------------------------------ document model


def build_document(text, title_arg, author_arg, stem):
    meta, body = split_front_matter(strip_comments(text))
    blocks = parse_blocks(body.split("\n"))
    title = (title_arg or meta.get("title") or "").strip()
    if blocks and blocks[0][0] == "h" and blocks[0][1] == 1:
        h1 = plain(blocks[0][2]).strip()
        if not title or title.lower() == h1.lower():
            title = title or h1
            blocks = blocks[1:]
    if not title:
        title = re.sub(r"[-_]+", " ", stem).strip().capitalize() or "Document"
    author = (author_arg or meta.get("author") or "").strip()
    date = meta.get("date", "").strip()
    return {"title": title, "author": author, "date": date, "blocks": blocks}


def count_blocks(blocks):
    n = 0
    for b in blocks:
        n += 1
        if b[0] == "quote":
            n += count_blocks(b[1])
        elif b[0] == "list":
            for it in b[3]:
                n += count_blocks(it["blocks"])
    return n


# ------------------------------------------------------------------ themes and pages

THEMES = {
    "clean": {
        "font": "Calibri", "mono": "Consolas", "size": 22, "line": 276, "after": 120, "color": "222222",
        "head": "1F3864", "accent": "2F5597", "muted": "595959", "th_fill": "DCE6F2", "th_color": "1F3864",
        "border": "BFBFBF", "code_fill": "F3F4F6", "bar": "8EA9C8", "title_size": 52, "h": [34, 28, 24, 22],
        "title_align": "left", "caps": False, "margin": 1440,
    },
    "formal": {
        "font": "Times New Roman", "mono": "Courier New", "size": 24, "line": 288, "after": 140, "color": "000000",
        "head": "000000", "accent": "000000", "muted": "404040", "th_fill": "D9D9D9", "th_color": "000000",
        "border": "7F7F7F", "code_fill": "F2F2F2", "bar": "7F7F7F", "title_size": 40, "h": [30, 26, 24, 24],
        "title_align": "center", "caps": True, "margin": 1440,
    },
}
PAGES = {"letter": (12240, 15840, "Letter"), "a4": (11906, 16838, "A4")}

# ------------------------------------------------------------------ DOCX

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def xesc(s):
    return _XML_BAD.sub("", s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def xattr(s):
    return xesc(s).replace('"', "&quot;")


class Docx(object):
    def __init__(self, theme, page):
        self.t = THEMES[theme]
        self.theme = theme
        self.pw, self.ph, self.page_name = PAGES[page]
        self.width = self.pw - 2 * self.t["margin"]
        self.body = []
        self.rels = []
        self.ordered = []

    # ---- runs
    def _rpr(self, flags, link=False):
        s = ""
        if link:
            s += '<w:rStyle w:val="Hyperlink"/>'
        elif "c" in flags:
            s += '<w:rStyle w:val="CodeChar"/>'
        if link and "c" in flags:
            m = self.t["mono"]
            s += '<w:rFonts w:ascii="%s" w:hAnsi="%s" w:cs="%s"/>' % (m, m, m)
        if "b" in flags:
            s += "<w:b/><w:bCs/>"
        if "i" in flags:
            s += "<w:i/><w:iCs/>"
        if "s" in flags:
            s += "<w:strike/>"
        return "<w:rPr>%s</w:rPr>" % s if s else ""

    def _text_runs(self, text, flags, link=False):
        rpr = self._rpr(flags, link)
        out = []
        for piece in re.split(r"(\n|\t)", text):
            if piece == "\n":
                out.append("<w:r>%s<w:br/></w:r>" % rpr)
            elif piece == "\t":
                out.append("<w:r>%s<w:tab/></w:r>" % rpr)
            elif piece:
                out.append('<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>' % (rpr, xesc(piece)))
        return "".join(out)

    def runs(self, runs, base=""):
        out = []
        for text, flags, href in runs:
            if text == "\n":
                out.append("<w:r><w:br/></w:r>")
                continue
            fl = flags + "".join(f for f in base if f not in flags)
            url = safe_href(href) if href else None
            if url and not url.startswith("#"):
                rid = "rIdL%d" % (len(self.rels) + 1)
                self.rels.append((rid, url))
                out.append('<w:hyperlink r:id="%s" w:history="1">%s</w:hyperlink>' % (rid, self._text_runs(text, fl, True)))
            else:
                out.append(self._text_runs(text, fl))
        return "".join(out)

    # ---- blocks
    def para(self, runs, style=None, ppr="", base=""):
        st = '<w:pStyle w:val="%s"/>' % style if style else ""
        p = "<w:pPr>%s%s</w:pPr>" % (st, ppr) if (st or ppr) else ""
        self.body.append("<w:p>%s%s</w:p>" % (p, self.runs(runs, base)))

    def heading(self, level, runs):
        self.para(runs, "Heading%d" % min(level, 4))

    def code(self, text):
        lines = text.replace("\t", "    ").split("\n") or [""]
        inner = []
        for k, ln in enumerate(lines):
            if k:
                inner.append("<w:r><w:br/></w:r>")
            if ln:
                inner.append('<w:r><w:t xml:space="preserve">%s</w:t></w:r>' % xesc(ln))
        self.body.append('<w:p><w:pPr><w:pStyle w:val="CodeBlock"/></w:pPr>%s</w:p>' % "".join(inner))

    def hr(self):
        self.body.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="%s"/></w:pBdr>'
                         '<w:spacing w:before="120" w:after="120"/></w:pPr></w:p>' % self.t["border"])

    def quote(self, blocks):
        for b in blocks:
            if b[0] == "p":
                self.para(b[1], "Quote")
            elif b[0] == "quote":
                self.quote(b[1])
            else:
                self.block(b)

    def table(self, aligns, header, rows):
        ncols = max(1, len(header))
        weights = []
        for k in range(ncols):
            w = len(plain(header[k])) if k < len(header) else 0
            for r in rows:
                if k < len(r):
                    w = max(w, len(plain(r[k])))
            weights.append(min(40, max(6, w)))
        total = sum(weights)
        floor = min(900, self.width // ncols)
        widths = [max(floor, int(self.width * w / total)) for w in weights]
        over = sum(widths) - self.width
        surplus = [w - floor for w in widths]
        if over > 0 and sum(surplus) > 0:
            widths = [w - int(over * sp / sum(surplus)) for w, sp in zip(widths, surplus)]
        widths[-1] += self.width - sum(widths)
        t = self.t
        borders = "".join('<w:%s w:val="single" w:sz="4" w:space="0" w:color="%s"/>' % (side, t["border"])
                          for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
        x = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="%d" w:type="dxa"/><w:tblBorders>%s</w:tblBorders>'
             '<w:tblLayout w:type="fixed"/><w:tblCellMar><w:top w:w="60" w:type="dxa"/><w:left w:w="100" w:type="dxa"/>'
             '<w:bottom w:w="60" w:type="dxa"/><w:right w:w="100" w:type="dxa"/></w:tblCellMar>'
             '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" w:noHBand="0" w:noVBand="1"/>'
             '</w:tblPr>' % (sum(widths), borders)]
        x.append("<w:tblGrid>%s</w:tblGrid>" % "".join('<w:gridCol w:w="%d"/>' % w for w in widths))

        def cell(runs, k, head):
            al = aligns[k] if k < len(aligns) else "left"
            jc = '<w:jc w:val="%s"/>' % al if al != "left" else ""
            shd = '<w:shd w:val="clear" w:color="auto" w:fill="%s"/>' % t["th_fill"] if head else ""
            base = "b" if head else ""
            return ('<w:tc><w:tcPr><w:tcW w:w="%d" w:type="dxa"/>%s</w:tcPr><w:p><w:pPr><w:pStyle w:val="TableText"/>%s</w:pPr>%s</w:p></w:tc>'
                    % (widths[k], shd, jc, self.runs(runs, base)))

        x.append('<w:tr><w:trPr><w:cantSplit/><w:tblHeader/></w:trPr>%s</w:tr>' % "".join(cell(header[k], k, True) for k in range(len(header))))
        for r in rows:
            x.append('<w:tr><w:trPr><w:cantSplit/></w:trPr>%s</w:tr>' % "".join(cell(r[k] if k < len(r) else [], k, False) for k in range(len(header))))
        x.append("</w:tbl>")
        self.body.append("".join(x))
        self.body.append('<w:p><w:pPr><w:spacing w:before="0" w:after="60" w:line="120" w:lineRule="exact"/></w:pPr></w:p>')

    def new_ordered(self, start, lvl):
        self.ordered.append((start, lvl))
        return len(self.ordered) + 1

    def list(self, block, depth=0):
        _, ordered, start, items = block
        lvl = min(depth, 8)
        num_id = self.new_ordered(start, lvl) if ordered else 1
        for it in items:
            blocks = it["blocks"]
            first = blocks[0][1] if blocks and blocks[0][0] == "p" else []
            rest = blocks[1:] if blocks and blocks[0][0] == "p" else blocks
            if it["task"] is not None:
                glyph = "\u2612" if it["task"] else "\u2610"
                g = ('<w:r><w:rPr><w:rFonts w:ascii="Segoe UI Symbol" w:hAnsi="Segoe UI Symbol" w:eastAsia="Segoe UI Symbol" w:cs="Segoe UI Symbol"/></w:rPr>'
                     '<w:t xml:space="preserve">%s </w:t></w:r>' % glyph)
                ppr = '<w:pStyle w:val="ListParagraph"/><w:ind w:left="%d" w:hanging="360"/>' % (720 + 720 * lvl)
                self.body.append("<w:p><w:pPr>%s</w:pPr>%s%s</w:p>" % (ppr, g, self.runs(first)))
            else:
                ppr = '<w:pStyle w:val="ListParagraph"/><w:numPr><w:ilvl w:val="%d"/><w:numId w:val="%d"/></w:numPr>' % (lvl, num_id)
                self.body.append("<w:p><w:pPr>%s</w:pPr>%s</w:p>" % (ppr, self.runs(first)))
            for b in rest:
                if b[0] == "list":
                    self.list(b, depth + 1)
                elif b[0] == "p":
                    self.para(b[1], "ListParagraph", '<w:ind w:left="%d"/>' % (720 + 720 * lvl))
                else:
                    self.block(b)

    def block(self, b):
        k = b[0]
        if k == "h":
            self.heading(b[1], b[2])
        elif k == "p":
            self.para(b[1])
        elif k == "list":
            self.list(b)
        elif k == "quote":
            self.quote(b[1])
        elif k == "code":
            self.code(b[2])
        elif k == "hr":
            self.hr()
        elif k == "table":
            self.table(b[1], b[2], b[3])

    # ---- parts
    def document_xml(self, doc):
        t = self.t
        head = []
        if doc["title"]:
            head.append('<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr>%s</w:p>' % self._text_runs(doc["title"], ""))
        sub = " \u00b7 ".join(x for x in (doc["author"], doc["date"]) if x)
        if sub:
            head.append('<w:p><w:pPr><w:pStyle w:val="Subtitle"/></w:pPr>%s</w:p>' % self._text_runs(sub, ""))
        for b in doc["blocks"]:
            self.block(b)
        if not self.body:
            self.body.append("<w:p/>")
        sect = ('<w:sectPr><w:footerReference w:type="default" r:id="rIdFooter"/><w:pgSz w:w="%d" w:h="%d"/>'
                '<w:pgMar w:top="%d" w:right="%d" w:bottom="%d" w:left="%d" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
                % (self.pw, self.ph, t["margin"], t["margin"], t["margin"], t["margin"]))
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document xmlns:w="%s" xmlns:r="%s"><w:body>%s%s%s</w:body></w:document>'
                % (NS_W, NS_R, "".join(head), "".join(self.body), sect))

    def styles_xml(self):
        t = self.t
        f, mono = t["font"], t["mono"]
        fonts = lambda name: '<w:rFonts w:ascii="%s" w:hAnsi="%s" w:eastAsia="%s" w:cs="%s"/>' % (name, name, name, name)
        h_caps = "<w:smallCaps/>" if t["caps"] else ""

        def pstyle(sid, name, based="Normal", nxt=None, ppr="", rpr="", q=True, prio=None):
            return ('<w:style w:type="paragraph" w:styleId="%s"><w:name w:val="%s"/>%s%s%s%s%s%s</w:style>'
                    % (sid, name, '<w:basedOn w:val="%s"/>' % based if based else "",
                       '<w:next w:val="%s"/>' % nxt if nxt else "",
                       '<w:uiPriority w:val="%d"/>' % prio if prio else "", "<w:qFormat/>" if q else "",
                       "<w:pPr>%s</w:pPr>" % ppr if ppr else "", "<w:rPr>%s</w:rPr>" % rpr if rpr else ""))

        heads = []
        space = [(360, 120), (280, 100), (200, 80), (160, 60)]
        for k in range(4):
            rpr = fonts(f) + "<w:b/><w:bCs/>" + (h_caps if k == 0 else "") + '<w:color w:val="%s"/><w:sz w:val="%d"/><w:szCs w:val="%d"/>' % (t["head"], t["h"][k], t["h"][k])
            heads.append(pstyle("Heading%d" % (k + 1), "heading %d" % (k + 1), nxt="Normal", prio=9,
                                ppr='<w:keepNext/><w:keepLines/><w:spacing w:before="%d" w:after="%d"/><w:outlineLvl w:val="%d"/>' % (space[k][0], space[k][1], k),
                                rpr=rpr))
        title_ppr = '<w:keepNext/><w:spacing w:before="0" w:after="80"/><w:jc w:val="%s"/>' % t["title_align"]
        if self.theme == "clean":
            title_ppr = '<w:keepNext/><w:pBdr><w:bottom w:val="single" w:sz="8" w:space="4" w:color="%s"/></w:pBdr><w:spacing w:before="0" w:after="120"/><w:jc w:val="left"/>' % t["accent"]
        x = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:styles xmlns:w="%s">' % NS_W,
             '<w:docDefaults><w:rPrDefault><w:rPr>%s<w:color w:val="%s"/><w:sz w:val="%d"/><w:szCs w:val="%d"/><w:lang w:val="en-US" w:eastAsia="en-US" w:bidi="ar-SA"/></w:rPr></w:rPrDefault>'
             '<w:pPrDefault><w:pPr><w:spacing w:after="%d" w:line="%d" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
             % (fonts(f), t["color"], t["size"], t["size"], t["after"], t["line"]),
             '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>',
             '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont"><w:name w:val="Default Paragraph Font"/><w:uiPriority w:val="1"/><w:semiHidden/><w:unhideWhenUsed/></w:style>',
             '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:uiPriority w:val="99"/><w:semiHidden/><w:unhideWhenUsed/>'
             '<w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>',
             '<w:style w:type="numbering" w:default="1" w:styleId="NoList"><w:name w:val="No List"/><w:uiPriority w:val="99"/><w:semiHidden/><w:unhideWhenUsed/></w:style>',
             pstyle("Title", "Title", nxt="Normal", prio=10, ppr=title_ppr,
                    rpr=fonts(f) + '<w:b/><w:bCs/><w:color w:val="%s"/><w:sz w:val="%d"/><w:szCs w:val="%d"/>' % (t["head"], t["title_size"], t["title_size"])),
             pstyle("Subtitle", "Subtitle", nxt="Normal", prio=11,
                    ppr='<w:spacing w:before="0" w:after="280"/><w:jc w:val="%s"/>' % t["title_align"],
                    rpr='<w:color w:val="%s"/><w:sz w:val="%d"/><w:szCs w:val="%d"/>' % (t["muted"], t["size"] + 2, t["size"] + 2))]
        x += heads
        x += [pstyle("Quote", "Quote", nxt="Normal", prio=29,
                     ppr='<w:pBdr><w:left w:val="single" w:sz="18" w:space="10" w:color="%s"/></w:pBdr><w:spacing w:before="120" w:after="120"/><w:ind w:left="540" w:right="360"/>' % t["bar"],
                     rpr='<w:i/><w:iCs/><w:color w:val="%s"/>' % t["muted"]),
              pstyle("CodeBlock", "Code Block", prio=30,
                     ppr='<w:pBdr><w:top w:val="single" w:sz="4" w:space="4" w:color="%s"/><w:left w:val="single" w:sz="4" w:space="6" w:color="%s"/><w:bottom w:val="single" w:sz="4" w:space="4" w:color="%s"/><w:right w:val="single" w:sz="4" w:space="6" w:color="%s"/></w:pBdr>'
                         '<w:shd w:val="clear" w:color="auto" w:fill="%s"/><w:spacing w:before="120" w:after="160" w:line="240" w:lineRule="auto"/><w:ind w:left="140" w:right="140"/>' % (t["border"], t["border"], t["border"], t["border"], t["code_fill"]),
                     rpr=fonts(mono) + '<w:sz w:val="19"/><w:szCs w:val="19"/>'),
              pstyle("ListParagraph", "List Paragraph", prio=34, ppr='<w:spacing w:after="60"/><w:ind w:left="720"/><w:contextualSpacing/>'),
              pstyle("TableText", "Table Text", prio=35, q=False, ppr='<w:spacing w:before="0" w:after="0" w:line="252" w:lineRule="auto"/>',
                     rpr='<w:sz w:val="%d"/><w:szCs w:val="%d"/>' % (t["size"] - 2, t["size"] - 2)),
              pstyle("Footer", "footer", prio=99, q=False,
                     ppr='<w:tabs><w:tab w:val="center" w:pos="4680"/><w:tab w:val="right" w:pos="9360"/></w:tabs><w:spacing w:after="0"/><w:jc w:val="center"/>',
                     rpr='<w:color w:val="7F7F7F"/><w:sz w:val="18"/><w:szCs w:val="18"/>'),
              '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/><w:basedOn w:val="DefaultParagraphFont"/><w:uiPriority w:val="99"/><w:unhideWhenUsed/><w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr></w:style>',
              '<w:style w:type="character" w:customStyle="1" w:styleId="CodeChar"><w:name w:val="Code Char"/><w:basedOn w:val="DefaultParagraphFont"/><w:uiPriority w:val="30"/><w:rPr>%s<w:sz w:val="%d"/><w:szCs w:val="%d"/><w:shd w:val="clear" w:color="auto" w:fill="%s"/></w:rPr></w:style>'
              % (fonts(mono), t["size"] - 2, t["size"] - 2, t["code_fill"]),
              '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/><w:uiPriority w:val="39"/>'
              '<w:tblPr><w:tblBorders>%s</w:tblBorders></w:tblPr></w:style>'
              % "".join('<w:%s w:val="single" w:sz="4" w:space="0" w:color="%s"/>' % (s, t["border"]) for s in ("top", "left", "bottom", "right", "insideH", "insideV")),
              "</w:styles>"]
        return "".join(x)

    def numbering_xml(self):
        bullets = ["\u2022", "\u25e6", "\u25aa"]
        fmts = ["decimal", "lowerLetter", "lowerRoman"]

        def levels(kind, first_lvl=0, first_val=1):
            out = []
            for lv in range(9):
                left = 720 * (lv + 1)
                if kind == "bullet":
                    out.append('<w:lvl w:ilvl="%d"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="%s"/><w:lvlJc w:val="left"/>'
                               '<w:pPr><w:ind w:left="%d" w:hanging="360"/></w:pPr><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial" w:hint="default"/></w:rPr></w:lvl>'
                               % (lv, bullets[lv % 3], left))
                else:
                    out.append('<w:lvl w:ilvl="%d"><w:start w:val="%d"/><w:numFmt w:val="%s"/><w:lvlText w:val="%%%d."/><w:lvlJc w:val="left"/>'
                               '<w:pPr><w:ind w:left="%d" w:hanging="360"/></w:pPr></w:lvl>'
                               % (lv, first_val if lv == first_lvl else 1, fmts[lv % 3], lv + 1, left))
            return "".join(out)

        # one abstract definition per ordered list, so every list restarts in every consumer
        abstracts = ['<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>%s</w:abstractNum>' % levels("bullet")]
        nums = ['<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>']
        for k, (start, lvl) in enumerate(self.ordered):
            abstracts.append('<w:abstractNum w:abstractNumId="%d"><w:multiLevelType w:val="hybridMultilevel"/>%s</w:abstractNum>'
                             % (k + 1, levels("decimal", lvl, start)))
            nums.append('<w:num w:numId="%d"><w:abstractNumId w:val="%d"/></w:num>' % (k + 2, k + 1))
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:numbering xmlns:w="%s">%s%s</w:numbering>'
                % (NS_W, "".join(abstracts), "".join(nums)))

    def footer_xml(self):
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:ftr xmlns:w="%s" xmlns:r="%s"><w:p><w:pPr><w:pStyle w:val="Footer"/><w:jc w:val="center"/></w:pPr>'
                '<w:r><w:t xml:space="preserve">Page </w:t></w:r><w:fldSimple w:instr=" PAGE "><w:r><w:t>1</w:t></w:r></w:fldSimple>'
                '<w:r><w:t xml:space="preserve"> of </w:t></w:r><w:fldSimple w:instr=" NUMPAGES "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p></w:ftr>' % (NS_W, NS_R))

    def rels_xml(self):
        base = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
        x = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
             '<Relationship Id="rIdStyles" Type="%sstyles" Target="styles.xml"/>' % base,
             '<Relationship Id="rIdNumbering" Type="%snumbering" Target="numbering.xml"/>' % base,
             '<Relationship Id="rIdSettings" Type="%ssettings" Target="settings.xml"/>' % base,
             '<Relationship Id="rIdFooter" Type="%sfooter" Target="footer1.xml"/>' % base]
        for rid, url in self.rels:
            x.append('<Relationship Id="%s" Type="%shyperlink" Target="%s" TargetMode="External"/>' % (rid, base, xattr(url)))
        x.append("</Relationships>")
        return "".join(x)


def build_docx(doc, theme, page):
    d = Docx(theme, page)
    document = d.document_xml(doc)
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
          '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
          '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
          '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
          '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
          '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>')
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<dc:title>%s</dc:title><dc:creator>%s</dc:creator></cp:coreProperties>' % (xesc(doc["title"]), xesc(doc["author"])))
    app = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
           '<Application>How to AI doc export</Application></Properties>')
    settings = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:settings xmlns:w="%s"><w:zoom w:percent="100"/><w:defaultTabStop w:val="720"/>'
                '<w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>' % NS_W)
    parts = [("[Content_Types].xml", ct), ("_rels/.rels", rels), ("docProps/core.xml", core), ("docProps/app.xml", app),
             ("word/document.xml", document), ("word/styles.xml", d.styles_xml()), ("word/numbering.xml", d.numbering_xml()),
             ("word/settings.xml", settings), ("word/footer1.xml", d.footer_xml()), ("word/_rels/document.xml.rels", d.rels_xml())]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts:
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, data.encode("utf-8"))
    return buf.getvalue()


# ------------------------------------------------------------------ HTML

HTML_CSS = """
:root { --fg: #%(color)s; --head: #%(head)s; --accent: #%(accent)s; --muted: #%(muted)s; --line: #%(border)s; --code: #%(code_fill)s; --th: #%(th_fill)s; --bar: #%(bar)s; }
@page { size: %(page)s; margin: 0.9in 1in; @bottom-center { content: "Page " counter(page) " of " counter(pages); font: 9pt %(font_css)s; color: #7f7f7f; } }
* { box-sizing: border-box; }
html { background: #fff; }
body { margin: 0; color: var(--fg); background: #fff; font: %(size)spt/1.5 %(font_css)s; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
main, header { display: block; }
.page { max-width: 46rem; margin: 2.5rem auto; padding: 0 1.25rem; }
.doc-title { font-size: %(title)spt; line-height: 1.15; margin: 0 0 .2em; color: var(--head); text-align: %(title_align)s; %(title_rule)s }
.byline { margin: 0 0 1.8em; color: var(--muted); text-align: %(title_align)s; }
h1, h2, h3, h4, h5, h6 { color: var(--head); line-height: 1.25; margin: 1.5em 0 .5em; break-after: avoid; }
h1 { font-size: %(h1)spt; %(h1_caps)s } h2 { font-size: %(h2)spt; } h3 { font-size: %(h3)spt; } h4, h5, h6 { font-size: %(h4)spt; }
p, ul, ol, pre, table, blockquote { margin: 0 0 .9em; }
ul, ol { padding-left: 1.6em; } li { margin: .2em 0; } li > p { margin: .3em 0; } li > ul, li > ol { margin: .2em 0; }
li.task { list-style: none; margin-left: -1.4em; } li.task .box { display: inline-block; width: 1.4em; }
a { color: #0563c1; }
code { font-family: %(mono_css)s; font-size: .9em; background: var(--code); padding: .05em .3em; border-radius: 3px; }
pre { background: var(--code); border: 1px solid var(--line); padding: .7em .9em; overflow-x: auto; white-space: pre-wrap; break-inside: avoid; }
pre code { background: none; padding: 0; font-size: .85em; }
blockquote { border-left: 4px solid var(--bar); padding: .1em 1em; color: var(--muted); font-style: italic; }
blockquote > :last-child { margin-bottom: 0; }
table { border-collapse: collapse; width: 100%%; font-size: .93em; }
th, td { border: 1px solid var(--line); padding: .35em .6em; vertical-align: top; text-align: left; }
th { background: var(--th); color: #%(th_color)s; } thead { display: table-header-group; } tr { break-inside: avoid; }
hr { border: 0; border-top: 1px solid var(--line); margin: 1.5em 0; }
del { color: var(--muted); }
@media print { .page { max-width: none; margin: 0; padding: 0; } a { color: inherit; text-decoration: none; } a[href^="http"]::after { content: " (" attr(href) ")"; font-size: .8em; color: var(--muted); word-break: break-all; } a.raw::after { content: none !important; } }
"""


def slugify(text, seen):
    s = re.sub(r"[^\w\s-]", "", text.lower(), flags=re.U).strip()
    s = re.sub(r"[\s_]+", "-", s) or "section"
    k = seen.get(s, 0)
    seen[s] = k + 1
    return s if k == 0 else "%s-%d" % (s, k)


def html_runs(runs):
    out = []
    for text, flags, href in runs:
        if text == "\n":
            out.append("<br>")
            continue
        t = html.escape(text, quote=False)
        if "c" in flags:
            t = "<code>%s</code>" % t
        if "b" in flags:
            t = "<strong>%s</strong>" % t
        if "i" in flags:
            t = "<em>%s</em>" % t
        if "s" in flags:
            t = "<del>%s</del>" % t
        url = safe_href(href) if href else None
        if url:
            t = '<a href="%s"%s>%s</a>' % (html.escape(url, quote=True), ' class="raw"' if text == url else "", t)
        out.append(t)
    return "".join(out)


def html_blocks(blocks, out, seen):
    for b in blocks:
        k = b[0]
        if k == "h":
            lv = b[1]
            out.append('<h%d id="%s">%s</h%d>' % (lv, html.escape(slugify(plain(b[2]), seen)), html_runs(b[2]), lv))
        elif k == "p":
            out.append("<p>%s</p>" % html_runs(b[1]))
        elif k == "hr":
            out.append("<hr>")
        elif k == "code":
            out.append("<pre><code>%s</code></pre>" % html.escape(b[2], quote=False))
        elif k == "quote":
            out.append("<blockquote>")
            html_blocks(b[1], out, seen)
            out.append("</blockquote>")
        elif k == "list":
            _, ordered, start, items = b
            tag = "ol" if ordered else "ul"
            out.append("<%s%s>" % (tag, ' start="%d"' % start if ordered and start != 1 else ""))
            for it in items:
                bl = it["blocks"]
                cls = ' class="task"' if it["task"] is not None else ""
                box = '<span class="box">%s</span>' % ("\u2612" if it["task"] else "\u2610") if it["task"] is not None else ""
                if bl and bl[0][0] == "p":
                    out.append("<li%s>%s%s" % (cls, box, html_runs(bl[0][1])))
                    html_blocks(bl[1:], out, seen)
                else:
                    out.append("<li%s>%s" % (cls, box))
                    html_blocks(bl, out, seen)
                out.append("</li>")
            out.append("</%s>" % tag)
        elif k == "table":
            _, aligns, header, rows = b

            def cell(tag, runs, i):
                al = aligns[i] if i < len(aligns) else "left"
                st = ' style="text-align:%s"' % al if al != "left" else ""
                return "<%s%s>%s</%s>" % (tag, st, html_runs(runs), tag)
            out.append("<table><thead><tr>%s</tr></thead><tbody>" % "".join(cell("th", header[i], i) for i in range(len(header))))
            for r in rows:
                out.append("<tr>%s</tr>" % "".join(cell("td", r[i] if i < len(r) else [], i) for i in range(len(header))))
            out.append("</tbody></table>")


def build_html(doc, theme, page):
    t = THEMES[theme]
    serif = theme == "formal"
    font_css = '"Times New Roman", Times, Georgia, serif' if serif else 'Calibri, "Segoe UI", -apple-system, Helvetica, Arial, sans-serif'
    mono_css = '"Courier New", Courier, monospace' if serif else 'Consolas, "SF Mono", Menlo, monospace'
    css = HTML_CSS % {
        "color": t["color"], "head": t["head"], "accent": t["accent"], "muted": t["muted"], "border": t["border"],
        "code_fill": t["code_fill"], "th_fill": t["th_fill"], "th_color": t["th_color"], "bar": t["bar"],
        "page": PAGES[page][2], "font_css": font_css, "mono_css": mono_css, "size": t["size"] // 2,
        "title": t["title_size"] // 2, "title_align": t["title_align"],
        "title_rule": "" if serif else "border-bottom: 2px solid var(--accent); padding-bottom: .15em;",
        "h1": t["h"][0] // 2, "h2": t["h"][1] // 2, "h3": t["h"][2] // 2, "h4": t["h"][3] // 2,
        "h1_caps": "font-variant: small-caps;" if t["caps"] else "",
    }
    body = []
    html_blocks(doc["blocks"], body, {})
    sub = " \u00b7 ".join(x for x in (doc["author"], doc["date"]) if x)
    head = '<header><h1 class="doc-title">%s</h1>%s</header>' % (html.escape(doc["title"], quote=False),
                                                                '<p class="byline">%s</p>' % html.escape(sub, quote=False) if sub else "")
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="generator" content="doc-coauthoring export.py">\n%s<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n<div class="page">\n%s\n<main>\n%s\n</main>\n</div>\n</body>\n</html>\n'
            % ('<meta name="author" content="%s">\n' % html.escape(doc["author"], quote=True) if doc["author"] else "",
               html.escape(doc["title"], quote=False), css, head, "\n".join(body))).encode("utf-8")


# ------------------------------------------------------------------ CLI

NOTE_RE = re.compile(r"\[\[.{0,300}?\]\]")
PLACEHOLDER_RE = re.compile(r"\[(?:to be written|tbd|tbc|todo|content here)\]|\b(?:TBD|TODO|FIXME)\b|\?{3,}", re.I)


def leftover_warning(text):
    body = re.sub(r"```.*?```", "", strip_comments(text), flags=re.S)
    notes = len(NOTE_RE.findall(body))
    holes = len(PLACEHOLDER_RE.findall(body))
    if notes or holes:
        WARNINGS.append("%d author note%s [[...]] and %d placeholder%s (TBD, TODO, [To be written]) are still in the text; "
                        "run scripts/doc_check.py check first" % (notes, "" if notes == 1 else "s", holes, "" if holes == 1 else "s"))


def build_parser():
    ap = argparse.ArgumentParser(prog="export.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="Markdown file, or - for stdin")
    ap.add_argument("--to", required=True, choices=["docx", "html"], help="output format")
    ap.add_argument("--title", help="document title (default: front matter title, first # heading, or file name)")
    ap.add_argument("--author", help="author shown under the title and stored in the file properties")
    ap.add_argument("--theme", choices=sorted(THEMES), default="clean", help="clean (default) or formal")
    ap.add_argument("--page", choices=sorted(PAGES), default="letter", help="page size: letter (default) or a4")
    ap.add_argument("--out", help="output path (default: next to the input, or ./document.<ext> for stdin)")
    ap.add_argument("--json", action="store_true", help="print a JSON summary instead of one status line")
    return ap


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    del WARNINGS[:]
    try:
        text = read_source(args.file)
        if not text.strip():
            sys.stderr.write("export: the input is empty, nothing to export\n")
            return 1
        stem = "document" if args.file == "-" else Path(args.file).stem
        doc = build_document(text, args.title, args.author, stem)
        leftover_warning(text)
        out = safe_out(args.out, args.file) if args.out else default_out(args.file, args.to)
        if args.file != "-" and os.path.exists(args.file) and os.path.realpath(str(out)) == os.path.realpath(args.file):
            die("output would overwrite the input file %s" % args.file)
        data = build_docx(doc, args.theme, args.page) if args.to == "docx" else build_html(doc, args.theme, args.page)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
        except OSError as ex:
            die("cannot write %s: %s" % (out, ex.strerror or ex))
    except UsageError as ex:
        sys.stderr.write("export: %s\n" % ex)
        return 2
    except RecursionError:
        sys.stderr.write("export: the document is nested too deeply to convert\n")
        return 2
    seen = []
    for w in WARNINGS:
        if w not in seen:
            seen.append(w)
    for w in seen:
        sys.stderr.write("export: warning: %s\n" % w)
    if args.json:
        print(json.dumps({"out": str(out), "format": args.to, "bytes": len(data), "title": doc["title"], "author": doc["author"],
                          "theme": args.theme, "page": args.page, "blocks": count_blocks(doc["blocks"]), "warnings": seen}, indent=2, ensure_ascii=False))
    else:
        print("export: wrote %s (%s, %d KB, %d blocks)" % (show_path(out), args.to, (len(data) + 1023) // 1024, count_blocks(doc["blocks"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
