#!/usr/bin/env python3
"""handoff.py: mine a long AI conversation for what must survive, build the handoff, prove it is faithful.

Subcommands
  sweep     list candidate corrections, constraints, decisions, dead ends, open questions and verbatim
            values from a saved conversation, plus (Claude Code) the files really written
  build     render HANDOFF.json into HANDOFF.md (+ resume-prompt.txt for an AI reader, + optional HTML)
  check     lint HANDOFF.json or HANDOFF.md and prove every verbatim value occurs in the transcript
  profiles  show what else to capture for a kind of work (code, writing, research, ...)

Examples
  python3 scripts/handoff.py sweep --latest
  python3 scripts/handoff.py sweep chat.txt --json --limit 20
  python3 scripts/handoff.py build HANDOFF.json --for ai --out .
  python3 scripts/handoff.py check HANDOFF.json --transcript chat.txt

Exit codes: 0 ok, 1 findings in the checked thing, 2 usage or input error.
No network, standard library only. Files are written only by build, only under --out.
"""
from __future__ import annotations

import argparse
import difflib
import html
import json
import os
import re
import shlex
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

RECIPIENTS = ("ai", "human", "self")
RECIPIENT_LABEL = {"ai": "for a new AI session", "human": "for a colleague", "self": "for future me"}
KINDS = ("code", "writing", "research", "analysis", "design", "planning", "sales", "general")
ALL_FIELDS = ("title", "date", "recipient", "kind", "objective", "current_state", "decisions", "dead_ends",
              "artifacts", "verbatim", "preferences", "open_items", "unresolved", "uncertain")
LIST_SECTIONS = ("decisions", "dead_ends", "artifacts", "verbatim", "preferences")
ITEM_KEYS = {"decisions": ("what", "why"), "dead_ends": ("what", "why"),
             "artifacts": ("name", "location", "status"), "verbatim": ("label", "value")}
PRIMARY = {"decisions": "what", "dead_ends": "what", "artifacts": "name", "verbatim": "label"}


class UserError(Exception):
    """A mistake in the input or arguments: one line on stderr, exit code 2."""


# ------------------------------------------------------------------ text helpers
def clip(s: str, n: int = 140) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(" ")
    if sp >= n * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:-") + "..."


def est_tokens(text: str) -> int:
    if not text:
        return 0
    return int(max(len(text) / 4.0, len(text.split()) * 1.3) + 0.5)


def read_secs(text: str) -> int:
    return int(len(text.split()) / 230.0 * 60 + 0.5)


def fmt_secs(s: int) -> str:
    return "%ds" % s if s < 60 else "%dm%02ds" % (s // 60, s % 60)


def read_text(path: str) -> str:
    if path == "-":
        buf = getattr(sys.stdin, "buffer", None)
        data = buf.read() if buf is not None else sys.stdin.read().encode("utf-8")
    else:
        p = Path(path)
        if not p.is_file():
            raise UserError("cannot read %s: not a file" % path)
        try:
            data = p.read_bytes()
        except OSError as e:
            raise UserError("cannot read %s: %s" % (path, e.strerror or e))
    return data.decode("utf-8-sig", errors="replace")


_NORM_MAP = {0x2018: "'", 0x2019: "'", 0x201c: '"', 0x201d: '"', 0x2013: "-", 0x2014: "-", 0xa0: " "}


def norm_match(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_NORM_MAP)
    return re.sub(r"\s+", " ", s).strip()


SECRET_STRONG = [re.compile(p) for p in (
    r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{30,}\b", r"\bxox[abprs]-[A-Za-z0-9-]{10,}", r"\bAIza[0-9A-Za-z_-]{30,}",
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}")]
SECRET_LOOSE = [re.compile(
    r"(?i)\b(?:password|passwd|pwd|secret|api[_ -]?key|access[_ -]?token|auth[_ -]?token|bearer)\b\s*[:=]\s*[\"']?[^\s\"']{8,}")]


def mask_secrets(text: str):
    n = 0
    for rx in SECRET_STRONG + SECRET_LOOSE:
        text, k = rx.subn("[secret]", text)
        n += k
    return text, n


def has_secret(text: str, strong_only: bool = False) -> bool:
    return any(rx.search(text) for rx in (SECRET_STRONG if strong_only else SECRET_STRONG + SECRET_LOOSE))


# ------------------------------------------------------------------ sentences
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)", re.S)
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[`*_#-])|\n+")
_BULLET_RE = re.compile(r"^\s*(?:[-*+>]+|\d{1,3}[.)])\s+")
_HEAD_RE = re.compile(r"^\s*#{1,6}\s*")


def split_sentences(text: str):
    text = _FENCE_RE.sub("\n", text[:200000])
    out = []
    for part in _SENT_RE.split(text):
        s = _HEAD_RE.sub("", _BULLET_RE.sub("", part.strip()))
        s = s.replace("**", "").replace("__", "")
        if 4 <= len(s) <= 700:
            out.append(s)
    return out


def lower_norm(s: str) -> str:
    return s.lower().replace("’", "'").replace("‘", "'")


# ------------------------------------------------------------------ verbatim token extraction
_MONTHS = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
           r"sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
URL_RE = re.compile(r"https?://[^\s<>\"'`)\]]{4,}")
EMAIL_RE = re.compile(r"(?<![\w.%+-])[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,5}")
WINPATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s\"'<>|*?]{2,}")
HOMEPATH_RE = re.compile(r"(?<![\w/.:-])~/[\w.@+~/-]{1,200}")
ABSPATH_RE = re.compile(r"(?<![\w/.:~%&#=?@+-])/(?:[\w.@+~-]{1,80}/){1,15}[\w.@+~-]{1,80}/?")
RELPATH_RE = re.compile(r"(?<![\w/.:@~-])(?:\.{1,2}/)?(?:[\w.@+-]{1,60}/){1,12}[\w@+-]{1,60}\.[A-Za-z][A-Za-z0-9]{0,7}\b")
FILE_RE = re.compile(r"(?<![\w/.@~-])[\w-]{1,60}\.(?:py|js|ts|tsx|jsx|md|json|csv|xlsx|docx|pdf|html|css|txt|yaml|yml|toml|sh|"
                     r"ipynb|sql|png|jpg|jpeg|gif|svg|pptx|zip|mp4|mp3|log|xml|rb|go|rs|java|cpp|swift|php)\b")
ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b")
LONGDATE_RE = re.compile(r"\b" + _MONTHS + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?\b(?:,?\s+\d{4}\b)?", re.I)
DAYMONTH_RE = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTHS + r"\b\.?(?:,?\s+\d{4}\b)?", re.I)
MONTHYEAR_RE = re.compile(r"\b" + _MONTHS + r"\.?\s+\d{4}\b", re.I)
SLASHDATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
QUARTER_RE = re.compile(r"\bQ[1-4]\s?(?:20)?\d{2}\b")
TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?:\s?[ap]m)?\b|\b(?:1[0-2]|[1-9])\s?[ap]m\b", re.I)
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
COLOR_RE = re.compile(r"(?<![\w&])#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?\b")
IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{2,5})?\b")
SHA_RE = re.compile(r"\b(?=(?:[0-9a-f]{7,40}|[0-9a-f]{64})\b)(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}\b")
TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")
ISSUE_RE = re.compile(r"(?<![\w&#/])#\d{1,5}\b")
OPAQUE_RE = re.compile(r"\b(?=[A-Za-z0-9_-]{20,64}\b)(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{20,64}\b")
MONEY_RE = re.compile(r"[$€£¥]\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:[kKmMbB]\b|million\b|billion\b|thousand\b|bn\b))?"
                      r"(?:\s?/\s?(?:mo|month|yr|year|day|week|user|seat|hr|hour)\b)?"
                      r"|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|CAD|AUD|dollars|euros|pounds)\b")
PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?%")
VERSION_RE = re.compile(r"\bv\d+(?:\.\d+){1,3}(?:-[\w.]+)?\b|\b\d+\.\d+\.\d+(?:-[\w.]+)?\b")
_UNITS = (r"(?:kb|mb|gb|tb|ms|secs?|seconds?|mins?|minutes?|hrs?|hours?|days?|weeks?|months?|years?|words?|chars?|"
          r"characters?|lines?|pages?|slides?|items?|rows?|columns?|users?|people|customers?|subscribers?|emails?|"
          r"posts?|tokens?|steps?|calls?|requests?|files?|bullets?|sections?|paragraphs?|examples?|options?|versions?|k|m|bn)")
QTY_RE = re.compile(r"\b\d[\d,]*(?:\.\d+)?\s?" + _UNITS + r"\b", re.I)
BIGNUM_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b(?!(?:19|20)\d\d\b)\d{4,}\b")
QUOTE_RES = (re.compile(r"\"([^\"\n]{6,140})\""), re.compile(r"“([^”\n]{6,140})”"),
             re.compile(r"`([^`\n]{3,120})`"))
_BLOB_RE = re.compile(r"\S{200,}")
_TICKET_DENY = {"UTF", "ISO", "SHA", "MD", "COVID", "AES", "RSA", "TLS", "SSL", "ASCII", "HTTP"}
KIND_ORDER = ("url", "email", "path", "id", "date", "time", "money", "percent", "version", "color", "quantity", "error", "quote")
ERR_HINTS = ("rror", "xception", "Traceback", "fatal:", "panic:", "ERR!", "ENOENT", "EACCES", "ECONN", "ETIMEDOUT",
             "EADDRINUSE", "denied", "not found", "No such file", "Cannot find", "HTTP")
ERR_RE = re.compile(
    r"^\s*(?:[\w.]*(?:Error|Exception)\b[:(]|Traceback \(most recent call last\)|npm ERR!|fatal:|panic:|error(?: TS\d+)?:|"
    r"ERROR[: ]|Uncaught |Unhandled |FAILED |.*\b(?:ENOENT|EACCES|ECONNREFUSED|ETIMEDOUT|EADDRINUSE)\b|"
    r".*\b(?:Permission denied|command not found|No such file or directory|Cannot find module|ModuleNotFoundError)\b|"
    r".*\bHTTP(?:/\d(?:\.\d)?)? [45]\d\d\b)")


def _trim(v: str) -> str:
    return v.rstrip(".,;:!?)]}>'\"")


def _trim_path(v: str) -> str:
    return v.rstrip(".,;:)")


def _clean_file(v: str) -> str:
    stem, ext = v.rsplit(".", 1)
    return "" if ext.lower() in ("js", "ts") and stem[:1].isupper() else v


def _clean_month(v: str) -> str:
    return "" if re.search(r"\bmay\b", v) else v


def _clean_ticket(v: str) -> str:
    return "" if v.split("-")[0] in _TICKET_DENY else v


def _grab(rx, kind, text, out, clean=None):
    def rep(m):
        v = m.group(0)
        if clean is not None:
            v = clean(v)
        if v:
            out.append((kind, v))
        return " " * (m.end() - m.start())
    return rx.sub(rep, text)


def extract_tokens(text: str, quotes: bool = False):
    """Return [(kind, value)] for urls, emails, paths, ids, dates, money, ... found in text."""
    out = []
    text = text[:400000]
    if quotes:
        plain = _FENCE_RE.sub(" ", text)
        for rx in QUOTE_RES:
            for m in rx.finditer(plain):
                v = m.group(1).strip()
                if v:
                    out.append(("quote", v))
    t = _BLOB_RE.sub(" ", text)
    t = _grab(URL_RE, "url", t, out, _trim)
    t = _grab(EMAIL_RE, "email", t, out, _trim_path)
    t = _grab(WINPATH_RE, "path", t, out, _trim_path)
    t = _grab(HOMEPATH_RE, "path", t, out, _trim_path)
    t = _grab(ABSPATH_RE, "path", t, out, _trim_path)
    t = _grab(RELPATH_RE, "path", t, out, _trim_path)
    t = _grab(FILE_RE, "path", t, out, _clean_file)
    t = _grab(ISO_RE, "date", t, out)
    t = _grab(LONGDATE_RE, "date", t, out, _clean_month)
    t = _grab(DAYMONTH_RE, "date", t, out, _clean_month)
    t = _grab(MONTHYEAR_RE, "date", t, out, _clean_month)
    t = _grab(SLASHDATE_RE, "date", t, out)
    t = _grab(QUARTER_RE, "date", t, out)
    t = _grab(TIME_RE, "time", t, out)
    t = _grab(UUID_RE, "id", t, out)
    t = _grab(COLOR_RE, "color", t, out)
    t = _grab(IP_RE, "id", t, out)
    t = _grab(SHA_RE, "id", t, out)
    t = _grab(TICKET_RE, "id", t, out, _clean_ticket)
    t = _grab(ISSUE_RE, "id", t, out)
    t = _grab(OPAQUE_RE, "id", t, out)
    t = _grab(MONEY_RE, "money", t, out, lambda v: v.rstrip(",."))
    t = _grab(PERCENT_RE, "percent", t, out)
    t = _grab(VERSION_RE, "version", t, out)
    t = _grab(QTY_RE, "quantity", t, out)
    t = _grab(BIGNUM_RE, "quantity", t, out)
    return out


# ------------------------------------------------------------------ transcript model and loaders
class Turn:
    __slots__ = ("n", "role", "text", "_sents")

    def __init__(self, n, role, text):
        self.n, self.role, self.text, self._sents = n, role, text, None

    def sents(self):
        if self._sents is None:
            self._sents = split_sentences(self.text)
        return self._sents


class Transcript:
    def __init__(self, name):
        self.name = name
        self.path = name
        self.format = "text"
        self.turns = []
        self.extra = []          # tool inputs/results, summaries: searchable but not analysed
        self.tools = []          # (turn, tool name, input dict)
        self.tool_errors = []    # (turn, first line)
        self.notes = []
        self.skipped = 0
        self.compactions = 0
        self.cwds, self.branches, self.sessions = [], [], []
        self.first_ts = self.last_ts = self.title_hint = None
        self.conversations = None
        self.picked = None
        self.attachments = []

    def add(self, role, text):
        text = text or ""
        if self.turns and self.turns[-1].role == role:
            t = self.turns[-1]
            if text:
                t.text = (t.text + "\n\n" + text) if t.text else text
            return t.n
        t = Turn(len(self.turns) + 1, role, text)
        self.turns.append(t)
        return t.n

    @property
    def last_n(self):
        return self.turns[-1].n if self.turns else 0

    def note_meta(self, o):
        for key, lst in (("cwd", self.cwds), ("gitBranch", self.branches), ("sessionId", self.sessions)):
            v = o.get(key)
            if isinstance(v, str) and v and v not in lst:
                lst.append(v)
        self.stamp(o.get("timestamp"))

    def stamp(self, v):
        ts = to_iso(v)
        if ts:
            if self.first_ts is None or ts < self.first_ts:
                self.first_ts = ts
            if self.last_ts is None or ts > self.last_ts:
                self.last_ts = ts

    def haystack(self):
        return "\n".join([t.text for t in self.turns] + self.extra)


def to_epoch(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        s = v.strip().replace("Z", "+00:00")
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        try:
            d = datetime.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.timestamp()
        except ValueError:
            return 0.0
    return 0.0


def to_iso(v):
    e = to_epoch(v)
    if not e:
        return None
    try:
        return datetime.fromtimestamp(e, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


_INJECTED = ("system-reminder", "local-command-stdout", "local-command-stderr", "local-command-caveat", "command-message",
             "ide_opened_file", "ide_selection", "ide_diagnostics", "user-prompt-submit-hook", "task-notification",
             "bash-input", "bash-stdout", "bash-stderr")
_INJ_RE = re.compile(r"<(%s)\b[^>]*>.*?</\1>" % "|".join(_INJECTED), re.S)
_CMD_NAME = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
_CMD_ARGS = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.S)
_INTERRUPT = re.compile(r"^\[Request interrupted by user[^\]]*\]$")


def clean_text(t: str) -> str:
    if "<" in t:
        name = _CMD_NAME.search(t)
        if name:
            args = _CMD_ARGS.search(t)
            cmd = name.group(1) + ((" " + args.group(1)) if args and args.group(1) else "")
            t = _CMD_ARGS.sub("", _CMD_NAME.sub("", _INJ_RE.sub("", t)))
            t = cmd + "\n" + t
        else:
            t = _INJ_RE.sub("", t)
    t = t.strip()
    return "" if _INTERRUPT.match(t) else t


def as_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in content if b is not None]
    return []


def flat_strings(obj, depth=0):
    if isinstance(obj, str):
        return [obj]
    if depth > 6:
        return []
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in flat_strings(v, depth + 1)]
    if isinstance(obj, list):
        return [s for v in obj for s in flat_strings(v, depth + 1)]
    return []


def norm_role(r):
    r = str(r or "").strip().lower()
    if r in ("user", "human", "you", "me", "customer"):
        return "user"
    if r in ("assistant", "ai", "claude", "chatgpt", "gpt", "bot", "model", "agent"):
        return "assistant"
    return None


def _tool_result(tr, b):
    c = b.get("content")
    if isinstance(c, list):
        text = "\n".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
    else:
        text = c if isinstance(c, str) else ""
    if text:
        tr.extra.append(text[:200000])
        if b.get("is_error"):
            line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
            if line:
                tr.tool_errors.append((tr.last_n, clip(line, 200)))


def _tool_use(tr, b, n):
    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
    tr.tools.append((n, str(b.get("name") or ""), inp))
    tr.extra.append("\n".join(flat_strings(inp)))


def _load_claude_code(objs, tr):
    tr.format = "claude-code-jsonl"
    for o in objs:
        if not isinstance(o, dict):
            continue
        typ = o.get("type")
        if typ == "summary":
            if isinstance(o.get("summary"), str):
                tr.title_hint = o["summary"]
                tr.extra.append(o["summary"])
            continue
        msg = o.get("message")
        if typ not in ("user", "assistant") or not isinstance(msg, dict):
            continue
        tr.note_meta(o)
        role = "assistant" if (msg.get("role") or typ) == "assistant" else "user"
        texts, uses = [], []
        for b in as_blocks(msg.get("content")):
            bt = b.get("type")
            if bt == "text":
                texts.append(str(b.get("text") or ""))
            elif bt == "tool_use":
                uses.append(b)
            elif bt == "tool_result":
                _tool_result(tr, b)
        text = clean_text("\n\n".join(x for x in (clean_text(t) for t in texts) if x))
        if o.get("isSidechain"):
            for b in uses:
                _tool_use(tr, b, tr.last_n)
            if text:
                tr.extra.append(text)
            continue
        if o.get("isCompactSummary") or text.startswith("This session is being continued from a previous conversation"):
            tr.compactions += 1
            tr.extra.append(text)
            continue
        if o.get("isMeta") or text.startswith("Caveat: The messages below were generated by the user"):
            continue
        if role == "assistant":
            if not text and not uses:
                continue
            n = tr.add("assistant", text)
            for b in uses:
                _tool_use(tr, b, n)
        elif text:
            tr.add("user", text)
    if tr.compactions:
        tr.notes.append("%d compaction summary message(s) skipped; the turns before them are still counted" % tr.compactions)


def _chatgpt_messages(conv):
    mapping = conv.get("mapping") or {}
    cur = conv.get("current_node")
    chain = []
    if cur in mapping:
        seen, node = set(), cur
        while node and node in mapping and node not in seen:
            seen.add(node)
            chain.append(mapping[node])
            node = mapping[node].get("parent")
        chain.reverse()
    else:
        nodes = [n for n in mapping.values() if isinstance(n, dict) and isinstance(n.get("message"), dict)]
        chain = sorted(nodes, key=lambda n: to_epoch(n["message"].get("create_time")))
    out = []
    for node in chain:
        m = node.get("message") if isinstance(node, dict) else None
        if not isinstance(m, dict):
            continue
        role = norm_role((m.get("author") or {}).get("role"))
        if not role or (m.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
            continue
        c = m.get("content") or {}
        if c.get("content_type") not in (None, "text", "multimodal_text"):
            continue
        text = "\n".join(p for p in (c.get("parts") or []) if isinstance(p, str))
        out.append((role, text, m.get("create_time"), []))
    return out


def _claude_ai_messages(conv, tr):
    out = []
    for m in conv.get("chat_messages") or []:
        if not isinstance(m, dict):
            continue
        role = norm_role(m.get("sender") or m.get("role"))
        if not role:
            continue
        parts, tools = [], []
        for b in m.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            elif isinstance(b, dict) and b.get("type") == "tool_use":
                tools.append(b)
            elif isinstance(b, dict) and b.get("type") == "tool_result":
                _tool_result(tr, b)
        text = "\n\n".join(p for p in parts if p) or str(m.get("text") or "")
        for a in m.get("attachments") or []:
            if isinstance(a, dict):
                if a.get("file_name"):
                    tr.attachments.append(str(a["file_name"]))
                if isinstance(a.get("extracted_content"), str):
                    tr.extra.append(a["extracted_content"][:200000])
        out.append((role, text, m.get("created_at"), tools))
    return out


def _generic_messages(msgs):
    out = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = norm_role(m.get("role") or m.get("sender") or m.get("author"))
        if not role:
            continue
        c = m.get("content", m.get("text"))
        text = "\n\n".join(str(b.get("text", "")) for b in as_blocks(c) if b.get("type", "text") == "text")
        out.append((role, text, m.get("created_at") or m.get("timestamp"), []))
    return out


def _conv_title(c):
    return str(c.get("name") or c.get("title") or "")


def _pick_conversation(convs, pick, tr):
    tr.conversations = len(convs)
    if pick:
        if pick.isdigit():
            i = int(pick) - 1
            if not 0 <= i < len(convs):
                raise UserError("--pick %s is out of range (the file has %d conversations)" % (pick, len(convs)))
            idx = i
        else:
            hits = [i for i, c in enumerate(convs) if pick.lower() in _conv_title(c).lower()]
            if not hits:
                raise UserError("no conversation title contains %r" % pick)
            idx = hits[0]
    else:
        def stamp(c):
            return max(to_epoch(c.get(k)) for k in ("updated_at", "update_time", "created_at", "create_time"))
        idx = max(range(len(convs)), key=lambda i: (stamp(convs[i]), i))
    tr.picked = (idx + 1, _conv_title(convs[idx]))
    return convs[idx]


def _load_conversation(conv, tr):
    if "mapping" in conv:
        tr.format = "chatgpt-export"
        msgs = _chatgpt_messages(conv)
        tr.stamp(conv.get("update_time") or conv.get("create_time"))
    elif "chat_messages" in conv:
        tr.format = "claude-ai-export"
        msgs = _claude_ai_messages(conv, tr)
        tr.stamp(conv.get("updated_at") or conv.get("created_at"))
    else:
        tr.format = "messages-json"
        msgs = _generic_messages(conv.get("messages") or [])
    for role, text, ts, tools in msgs:
        tr.stamp(ts)
        text = clean_text(text or "")
        if text or tools:
            n = tr.add(role, text)
            for b in tools:
                _tool_use(tr, b, n)


MARK_WORDS = "user|human|you|me|assistant|ai|claude|chatgpt|gpt|bot|model"
MARK_COLON = re.compile(r"^[ \t>]*(?:#{1,6}[ \t]*)?(?:\*\*|__)?(%s)(?:\*\*|__)?[ \t]*[:：][ \t]*(?:\*\*|__)?[ \t]*(.*)$" % MARK_WORDS, re.I)
MARK_HEAD = re.compile(r"^[ \t>]*(?:#{1,6}[ \t]+|\*\*|__)(user|human|assistant|claude|chatgpt)(?:\*\*|__)?[ \t]*$", re.I)


def _load_text(text, tr):
    cur_role, buf, found = None, [], False

    def flush():
        if cur_role is not None:
            tr.add(cur_role, clean_text("\n".join(buf)))

    for line in text.split("\n"):
        line = line.rstrip("\r")
        m = MARK_COLON.match(line)
        h = None if m else MARK_HEAD.match(line)
        if m or h:
            flush()
            found = True
            role = norm_role((m or h).group(1))
            cur_role = role
            buf = [m.group(2)] if m else []
        else:
            buf.append(line)
    if found:
        flush()
        tr.format = "text-markers"
    else:
        tr.format = "text-unlabeled"
        tr.turns = []
        tr.add("unknown", text.strip())
        tr.notes.append("no User:/Assistant: markers found: treated as one unlabeled block; question tracking is off")


def parse_transcript(text: str, pick=None, name="-") -> Transcript:
    tr = Transcript(name)
    s = text.strip()
    if not s:
        tr.format = "empty"
        tr.notes.append("input is empty: nothing to sweep")
        return tr
    whole = None
    if s[0] in "{[":
        try:
            whole = json.loads(s)
        except (ValueError, RecursionError):
            whole = None
    if whole is not None:
        return _from_json(whole, tr, pick)
    if s[0] == "{":
        objs = []
        for line in s.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                objs.append(json.loads(line))
            except (ValueError, RecursionError):
                tr.skipped += 1
        if objs:
            _from_objs(objs, tr)
            if tr.skipped:
                tr.notes.append("%d line(s) could not be parsed and were skipped" % tr.skipped)
            return tr
        tr.skipped = 0
    _load_text(s, tr)
    return tr


def _is_cc(o):
    return isinstance(o, dict) and "message" in o and o.get("type") in (
        "user", "assistant", "summary", "system", "file-history-snapshot", "queue-operation", "attachment")


def _from_objs(objs, tr):
    if any(_is_cc(o) for o in objs):
        _load_claude_code(objs, tr)
    else:
        tr.format = "messages-json"
        for role, text, ts, _ in _generic_messages(objs):
            tr.stamp(ts)
            if clean_text(text):
                tr.add(role, clean_text(text))


def _from_json(whole, tr, pick):
    if isinstance(whole, dict) and _is_cc(whole):
        _from_objs([whole], tr)
    elif isinstance(whole, dict) and isinstance(whole.get("conversations"), list) and whole["conversations"]:
        _load_conversation(_pick_conversation([c for c in whole["conversations"] if isinstance(c, dict)], pick, tr), tr)
    elif isinstance(whole, dict) and ("mapping" in whole or "chat_messages" in whole or isinstance(whole.get("messages"), list)):
        _load_conversation(whole, tr)
    elif isinstance(whole, list) and whole and all(isinstance(c, dict) for c in whole[:5]) and (
            "mapping" in whole[0] or "chat_messages" in whole[0]):
        _load_conversation(_pick_conversation([c for c in whole if isinstance(c, dict)], pick, tr), tr)
    elif isinstance(whole, list) and any(_is_cc(o) for o in whole):
        _from_objs(whole, tr)
    elif isinstance(whole, list) and whole:
        _from_objs(whole, tr)
    else:
        tr.format = "unknown-json"
        tr.notes.append("JSON structure not recognised: expected messages, a claude.ai export or a ChatGPT export")
    if not tr.turns and tr.format != "unknown-json":
        tr.notes.append("no user or assistant messages found")
    return tr


def load_transcript(path: str, pick=None) -> Transcript:
    tr = parse_transcript(read_text(path), pick=pick, name=os.path.basename(path) if path != "-" else "stdin")
    tr.path = path
    return tr


# ------------------------------------------------------------------ --latest (newest Claude Code session)
def encode_project_dir(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def default_projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def find_latest_session(cwd=None, projects_dir=None) -> Path:
    root = Path(projects_dir) if projects_dir else default_projects_dir()
    if not root.is_dir():
        raise UserError("no Claude Code sessions folder at %s: pass the transcript file instead" % root)
    start = os.path.abspath(cwd) if cwd else os.getcwd()
    starts = [start]
    real = os.path.realpath(start)
    if real != start:
        starts.append(real)
    tried = []
    for s in starts:
        p = Path(s)
        for d in [p] + list(p.parents):
            if d == Path(d.anchor):
                break
            cand = root / encode_project_dir(str(d))
            tried.append(cand.name)
            if not cand.is_dir():
                continue
            files = [f for f in cand.glob("*.jsonl") if f.is_file() and f.stat().st_size > 0 and not f.name.startswith("agent-")]
            if files:
                return max(files, key=lambda f: (f.stat().st_mtime, f.name))
    raise UserError("no session files for %s under %s (looked for %s): pass the transcript file or use --cwd DIR"
                    % (start, root, tried[0] if tried else "?"))


# ------------------------------------------------------------------ sweep: cue tables (matched against lowercase sentences)
def _tbl(rows):
    return [(re.compile(p), w, t) for p, w, t in rows]


CORRECTIONS = _tbl([
    (r"^(?:no|nope|nah|wrong|incorrect)(?:\b[\s,.!:;-]|$)", 3, "no"),
    (r"\bnot (?:what|how) (?:i|we) (?:asked|wanted|meant|said|need|expected)", 3, "not what I asked"),
    (r"\b(?:that|this|it)(?:'s| is| was) (?:not|wrong|incorrect|off|still|too)\b", 3, "that's not"),
    (r"\bi (?:said|meant|asked|told you|wrote|specified|already (?:said|told|mentioned|asked))\b", 3, "I said"),
    (r"\byou (?:forgot|missed|ignored|misunderstood|misread|skipped|left out|keep|still|never|didn'?t|did not|don'?t|do not|removed|changed|broke|deleted|overwrote)\b", 3, "you did/forgot"),
    (r"\bwhy (?:did|are|do|would|is|was) (?:you|it|this|that)\b", 2, "why did you"),
    (r"\b(?:stop|quit) (?:doing|using|adding|writing|saying|being|making|asking|repeating|it)\b", 3, "stop"),
    (r"\btoo \w{3,}", 3, "too X"),
    (r"\b(?:shorter|longer|simpler|clearer|tighter|punchier|more concise|less formal|more formal|more casual|less technical|more technical|less salesy|shorten|simplify|condense)\b", 3, "tone/length"),
    (r"\b(?:instead|rather than)\b", 1, "instead"),
    (r"\bactually\b", 1, "actually"),
    (r"\b(?:don'?t|do not|never|stop)\b", 2, "don't"),
    (r"\b(?:revert|undo|roll ?back|go back to|put (?:it|that) back|restore)\b", 2, "revert"),
    (r"\b(?:redo|rewrite|start over|try again|scrap (?:it|that|this))\b", 2, "redo"),
    (r"\b(?:is|are|was|be|it'?s|due)\s+[\w'\"/-]+(?:\s[\w'\"/-]+)?,?\s+not\s+\w+", 2, "X not Y"),
    (r"^(?:but|wait|hold on|hang on|hmm|however)\b", 2, "but/wait"),
    (r"\b(?:should be|should have been|supposed to|meant to be|needs? to be|change (?:it|this|that) to|make (?:it|this|that))\b", 1, "should be"),
])
CONSTRAINTS = _tbl([
    (r"\b(?:must(?: not|n'?t)?|has to|have to|needs? to|required?|requirements?|mandatory|non-?negotiable|cannot|can'?t|shall|should(?: not|n'?t)?)\b", 2, "must"),
    (r"\b(?:never|always|do not|don'?t|under no circumstances)\b", 2, "never/always"),
    (r"\bonly (?:use|include|send|write|show|keep|do|pick|choose|work|cite|mention|from|in|on|for|if|when|allow)\b|^only\b", 2, "only"),
    (r"\b(?:make sure|be sure|ensure|remember to|don'?t forget|keep in mind|note that)\b", 2, "make sure"),
    (r"\b(?:no more than|at most|at least|not more than|not less than|maximum|minimum|max|min|up to|limit(?:ed)? to|cap(?:ped)? at|under|over|within|exactly|below|above)\s+[$€£]?\d", 3, "limit"),
    (r"\b(?:deadline|due date|due (?:on|by)|no later than|by (?:end of|eod|eow|eom|monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tonight|next week)|by the \d|before (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d))\b", 3, "deadline"),
    (r"\b(?:budget|cost cap|price limit)\b", 2, "budget"),
    (r"\b(?:not allowed|forbidden|prohibited|banned|off[- ]limits|no exceptions)\b", 2, "forbidden"),
    (r"\b(?:exactly|precisely|verbatim|word for word)\b", 2, "exact"),
])
NUMERIC_RE = re.compile(r"[$€£]\s?\d|\d\s?(?:%|words?|characters?|chars|minutes?|hours?|days?|weeks?|pages?|slides?|items?)\b|\b20\d\d-\d\d-\d\d\b")
DEC_BOTH = _tbl([
    (r"\b(?:decided|decision|settled on|settle on|agreed|approved|confirmed|locked in|finali[sz]ed|final (?:answer|version|choice|pick|decision)|sticking with|stick with|opted for|opt for|picked|chose|chosen|went with|from now on|going forward|in favou?r of|the plan is)\b", 2, "decided"),
    (r"\b(?:use|using) .{2,40}? instead of\b", 3, "instead of"),
    (r"\b(?:switch(?:ed)?|migrat(?:e|ed)|mov(?:e|ed)) (?:from|to)\b", 2, "switch"),
    (r"\bship it\b|\bgo(?:ing)? with (?:option|plan|approach|the)\b|\boption [a-z0-9]\b", 2, "option"),
])
DEC_INTENT = _tbl([
    (r"\blet'?s (?:go with|use|do|pick|choose|stick with|start with|keep|switch to|move to|take|build|make|ship|call it|name it)\b", 3, "let's"),
    (r"\b(?:we'?ll|we will|i'?ll|i will|we'?re going to|i'?m going to|going to|gonna) (?:use|go with|pick|choose|build|keep|stick with|switch to|start with|call)\b", 3, "we'll"),
    (r"\bwe should (?:use|go|pick|choose|build|keep|switch|start)\b", 3, "we should"),
])
DEC_PROPOSE = _tbl([
    (r"\b(?:i (?:recommend|suggest|propose)|i'?d (?:go|suggest|recommend)|my (?:recommendation|suggestion)|we could|we should|the best (?:option|approach|way)|proposal)\b", 2, "proposal"),
])
DEAD = _tbl([
    (r"\b(?:ruled out|rule out|scrapp?ed|abandon(?:ed)?|gave up|give up|dead[- ]?end|not viable|no good|out of the question|won'?t work|will not work|doesn'?t work|does not work|didn'?t work|did not work|not working|never worked|wrong approach|bad idea|let'?s not|scratch that|forget (?:it|that|about)|never ?mind|drop (?:it|that|this)|dropped|throw (?:it|that) away)\b", 3, "rejected"),
    (r"\b(?:failed|fails|failing|failure|broke|broken|crash(?:ed|es)?|timed out|timeout|rejected|refused|blocked|denied|unsupported|deprecated|not supported|isn'?t supported|not possible|impossible|can'?t be done)\b", 2, "failed"),
    (r"\btoo (?:slow|expensive|complex|risky|brittle|fragile|heavy|hard|costly|noisy|long|big)\b", 2, "too X"),
    (r"\b(?:instead of|rather than|revert(?:ed)?|rolled? back|switched away from|no longer)\b", 2, "instead of"),
    (r"\b(?:tried|attempted|tested|experimented)\b[^.?!]{0,80}\b(?:but|however|didn'?t|failed|instead|not)\b", 3, "tried, but"),
    (r"\b(?:because|since|due to|the problem is|the issue is|turns out)\b", 1, "reason"),
])
PEND = _tbl([
    (r"\b(?:todo|to-?do|tbd|tbc|to be (?:decided|confirmed|determined)|open question|unresolved|undecided|not (?:yet )?(?:decided|confirmed|verified|tested|sure|clear)|haven'?t (?:decided|confirmed|checked|tested|heard|verified)|still (?:need|have|needs|has) to|we (?:still )?need to|need to (?:decide|figure out|confirm|check|verify|find out|ask)|waiting (?:for|on)|blocked (?:on|by)|pending|depends on|follow[- ]?up|revisit|circle back|come back to|left to do|remaining)\b", 2, "pending"),
    (r"\b(?:next step|next steps|next up|later|eventually|someday|unknown|unclear)\b", 1, "next/later"),
])
ACCEPT_RE = re.compile(r"^\W*(?:yes|yep|yeah|yup|ok|okay|sure|perfect|great|good|agreed|approved|correct|exactly|right|sounds good|that works|works for me|go ahead|do it|go for it|ship it|love it|looks good|lgtm|proceed|let'?s (?:do|go)|that'?s (?:it|right|good|great|perfect|fine))\b")
PUSH_RE = re.compile(r"^\W*(?:no|nope|nah|not|wait|hold on|hang on|actually|instead|but|however|don'?t|rather|hmm|stop)\b")
POLITE_RE = re.compile(r"make sense|sound good|anything else|is that clear|hope that helps|let me know|^why (?:did|would|are|do) you\b")
REQUEST_RE = re.compile(r"^(?:can|could|would|will) (?:you|we)\b|^please\b")
YN_RE = re.compile(r"^(?:do|does|did|can|could|should|would|is|are|was|were|will|have|has|had|shall|may|might)\b")
YESNO_REPLY = re.compile(r"^\W*(?:yes|yep|yeah|yup|no|nope|nah|sure|ok|okay|correct|right|exactly|agreed|please do|go ahead|not really|maybe|probably|i think)\b")
STOP = set("that this with have from your what which when where would could should there their about into then than them they been were will just also please want need like make more some other thing things does doing done here very much many most only over such each both same again still even".split())


def score_of(table, low):
    sc, tags = 0, []
    for rx, w, tag in table:
        if rx.search(low):
            sc += w
            tags.append(tag)
    return sc, tags


def content_words(s):
    return set(re.findall(r"[a-z][a-z0-9'-]{3,}", s.lower())) - STOP


def is_answered(q_role, q, reply):
    if not reply.strip():
        return True
    ql = lower_norm(q).strip()
    if q_role == "user" and REQUEST_RE.match(ql):
        return True
    qw = content_words(q)
    if not qw or qw & content_words(reply):
        return True
    if YN_RE.match(ql) and YESNO_REPLY.match(lower_norm(reply[:80])):
        return True
    return q_role == "assistant" and len(reply.split()) <= 6


def react(turn):
    head = lower_norm(turn.text[:160]).strip()
    if PUSH_RE.match(head):
        return "pushback"
    return "accepted" if ACCEPT_RE.match(head) else "none"


class Ctx:
    def __init__(self, qlen=140):
        self.masked = 0
        self.qlen = qlen

    def q(self, s, n=None):
        s2, k = mask_secrets(s)
        self.masked += k
        return clip(s2, n or self.qlen)


def select(cands, limit):
    total = len(cands)
    if limit and total > limit:
        cands = sorted(cands, key=lambda c: (-c["score"], c["turn"]))[:limit]
    return sorted(cands, key=lambda c: c["turn"]), total


def analyze(tr, ctx, limit):
    turns = tr.turns
    labeled = any(t.role != "unknown" for t in turns)
    first_asst = next((t.n for t in turns if t.role == "assistant"), None)
    found = {k: [] for k in ("corrections", "constraints", "decisions", "dead_ends", "open")}
    seen = {k: set() for k in found}
    questions = []

    def add(cat, t, s, score, tags, **extra):
        key = s.lower()
        if key in seen[cat]:
            return
        seen[cat].add(key)
        d = {"turn": t.n, "role": t.role, "quote": ctx.q(s), "score": score, "tags": tags}
        d.update(extra)
        found[cat].append(d)

    for idx, t in enumerate(turns):
        if not t.text:
            continue
        is_user = t.role in ("user", "unknown")
        gate = is_user and (not labeled or (first_asst is not None and t.n > first_asst))
        nxt = turns[idx + 1] if idx + 1 < len(turns) else None
        near_end = idx >= len(turns) - 4
        for s in t.sents():
            low = lower_norm(s)
            if gate:
                sc, tags = score_of(CORRECTIONS, low)
                if sc >= 2:
                    add("corrections", t, s, sc, tags)
            if is_user:
                sc, tags = score_of(CONSTRAINTS, low)
                if sc and NUMERIC_RE.search(low):
                    sc += 1
                if sc >= 2:
                    add("constraints", t, s, sc, tags)
            sc, tags = score_of(DEC_BOTH, low)
            isc, itags = score_of(DEC_INTENT, low)
            if is_user:
                total = sc + (3 if isc else 0)
                if total >= 2:
                    add("decisions", t, s, total, tags + itags, reaction="n/a")
            else:
                psc, ptags = score_of(DEC_PROPOSE, low)
                total = sc + psc + (1 if isc else 0)
                if total >= 1:
                    reaction = react(nxt) if nxt is not None and nxt.role == "user" else "none"
                    if total >= 2 or reaction == "accepted":
                        add("decisions", t, s, total + (2 if reaction == "accepted" else 0), tags + ptags + itags,
                            reaction=reaction, reply_turn=nxt.n if nxt is not None else None)
            sc, tags = score_of(DEAD, low)
            if sc >= 3:
                add("dead_ends", t, s, sc, tags)
            sc, tags = score_of(PEND, low)
            if sc and near_end:
                sc += 1
            if sc >= 2:
                add("open", t, s, sc, tags, kind="marker")
            if labeled and s.endswith("?") and len(s) >= 15 and len(s.split()) >= 3 and not POLITE_RE.search(low):
                questions.append((idx, t, s))
    for idx, t, s in questions:
        nxt = turns[idx + 1] if idx + 1 < len(turns) else None
        if nxt is None:
            add("open", t, s, 3, ["question: the thread ends here"], kind="question")
        elif not is_answered(t.role, s, nxt.text):
            add("open", t, s, 3, ["question: the next reply does not touch it"], kind="question")
    counts, out = {}, {}
    for cat, lst in found.items():
        out[cat], total = select(lst, limit)
        counts[cat] = {"shown": len(out[cat]), "total": total}
    return out, counts


def collect_verbatim(tr, ctx, limit):
    agg = {}
    for t in tr.turns:
        if not t.text:
            continue
        text = t.text
        if t.role == "assistant" and len(text) > 60000:
            text = text[:40000] + "\n" + text[-20000:]
        is_user = t.role != "assistant"
        toks = extract_tokens(text, quotes=is_user)
        if any(h in text for h in ERR_HINTS):
            for line in text.split("\n"):
                if len(line) <= 300 and ERR_RE.match(line):
                    toks.append(("error", clip(line.strip(), 200)))
        for kind, value in toks:
            if has_secret(value):
                ctx.masked += 1
                continue
            key = (kind, re.sub(r"\d+", "#", value.lower()) if kind == "error" else value)
            rec = agg.setdefault(key, {"kind": kind, "value": value, "count": 0, "user": False, "first_turn": t.n, "last_turn": t.n})
            rec["count"] += 1
            rec["user"] = rec["user"] or is_user
            rec["last_turn"] = t.n
    for n, line in tr.tool_errors:
        key = ("error", re.sub(r"\d+", "#", line.lower()))
        rec = agg.setdefault(key, {"kind": "error", "value": line, "count": 0, "user": False, "first_turn": n, "last_turn": n})
        rec["count"] += 1
        rec["last_turn"] = n
    by_kind, counts = {}, {}
    for kind in KIND_ORDER:
        items = [r for r in agg.values() if r["kind"] == kind]
        if not items:
            continue
        items.sort(key=lambda r: (not r["user"], -r["count"], r["first_turn"]))
        keep = items[:limit] if limit else items
        keep.sort(key=lambda r: (r["first_turn"], r["value"]))
        by_kind[kind] = keep
        counts[kind] = {"shown": len(keep), "total": len(items)}
    return by_kind, counts


# ------------------------------------------------------------------ sweep: artifact inventory (ground truth from tool calls)
WRITE_TOOLS = {"Write": "write", "Edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit"}
SCRATCH_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/var/tmp/")
CODE_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".sh", ".c", ".cpp", ".swift", ".php", ".sql")


def bash_removals(cmd):
    """Return [(verb, path)] for rm / mv / git rm / git mv found in a shell command (best effort)."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return []
    segs, cur = [], []
    for tok in tokens:
        if tok and all(c in ";&|()" for c in tok):
            segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    segs.append(cur)
    out = []
    for seg in segs:
        while seg and seg[0] == "sudo":
            seg = seg[1:]
        if seg and seg[0] == "git" and len(seg) > 1 and seg[1] in ("rm", "mv"):
            seg = seg[1:]
        if not seg or seg[0] not in ("rm", "mv"):
            continue
        args = [a for a in seg[1:] if not a.startswith("-")]
        if seg[0] == "mv":
            args = args[:-1]
        out.extend((seg[0], a) for a in args)
    return out


def inventory(tr):
    files, titles = {}, {}
    for n, name, inp in tr.tools:
        path = action = None
        if name in WRITE_TOOLS:
            path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
            action = WRITE_TOOLS[name]
        elif name in ("str_replace_editor", "str_replace_based_edit_tool"):
            cmd = inp.get("command")
            if cmd in ("create", "str_replace", "insert"):
                path, action = inp.get("path"), ("write" if cmd == "create" else "edit")
        elif name == "artifacts":
            aid = str(inp.get("id") or "")
            if inp.get("title"):
                titles[aid] = str(inp["title"])
            path = titles.get(aid) or aid
            action = "write" if inp.get("command") in ("create", "rewrite") else "edit"
        if not isinstance(path, str) or not path:
            continue
        rec = files.setdefault(path, {"path": path, "writes": 0, "edits": 0, "first_turn": n, "last_turn": n,
                                      "scratch": path.startswith(SCRATCH_PREFIXES)})
        rec["writes" if action == "write" else "edits"] += 1
        rec["last_turn"] = max(rec["last_turn"], n)
    for n, name, inp in tr.tools:
        if name != "Bash" or not isinstance(inp.get("command"), str):
            continue
        for verb, target in bash_removals(inp["command"]):
            t = target.rstrip("/")
            key = t.lstrip("./")
            for rec in files.values():
                p = rec["path"]
                if p == t or (key and p.endswith("/" + key)) or (t and p.startswith(t + "/")):
                    rec["gone_hint"] = {"turn": n, "how": verb}
    inv = sorted(files.values(), key=lambda r: (r["scratch"], -r["last_turn"], r["path"]))
    return inv


def guess_kinds(tr, inv, profiles):
    text = "\n".join(t.text for t in tr.turns).lower()
    scores = {}
    for kind, p in profiles.items():
        scores[kind] = sum(min(text.count(sig), 5) for sig in p.get("signals", []))
    if "code" in scores:
        scores["code"] += min(20, 4 * sum(1 for r in inv if r["path"].lower().endswith(CODE_EXTS)))
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"kind": k, "score": s} for k, s in ranked if s >= 4][:3]


def load_profiles():
    p = Path(__file__).resolve().parent.parent / "assets" / "profiles.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data.get("profiles", {}) if isinstance(data, dict) else {}


def sweep_result(tr, limit=12, only=None, qlen=140):
    ctx = Ctx(qlen)
    lim = limit
    cats, counts = analyze(tr, ctx, lim)
    verb, vcounts = collect_verbatim(tr, ctx, 10 if limit else 0)
    counts["verbatim"] = vcounts
    inv = inventory(tr)
    shown_inv = inv[:25] if limit else inv
    counts["artifacts"] = {"shown": len(shown_inv), "total": len(inv)}
    turns = tr.turns
    users = [t for t in turns if t.role in ("user", "unknown")]
    asst = [t for t in turns if t.role == "assistant"]
    res = {
        "source": {"name": tr.name, "path": tr.path, "format": tr.format, "turns": len(turns), "user_turns": len(users),
                   "assistant_turns": len(asst), "first_timestamp": tr.first_ts, "last_timestamp": tr.last_ts,
                   "cwd": tr.cwds, "branch": tr.branches, "session": tr.sessions, "title_hint": tr.title_hint,
                   "conversations_in_file": tr.conversations, "picked": tr.picked, "attachments": tr.attachments,
                   "skipped_lines": tr.skipped, "notes": tr.notes},
        "first_request": {"turn": users[0].n, "quote": ctx.q(users[0].text, 300)} if users else None,
        "last_turns": [{"turn": t.n, "role": t.role, "quote": ctx.q(t.text, 300)}
                       for t in ([users[-1]] if users else []) + ([asst[-1]] if asst else [])
                       if t.text],
        "kind_guess": guess_kinds(tr, inv, load_profiles()),
    }
    res["last_turns"].sort(key=lambda d: d["turn"])
    res.update(cats)
    res["verbatim"] = verb
    res["artifacts"] = shown_inv
    res["counts"] = counts
    res["secrets_masked"] = ctx.masked
    if only:
        for k in list(res):
            if k in ("corrections", "constraints", "decisions", "dead_ends", "open", "verbatim", "artifacts") and k not in only:
                res[k] = {} if k == "verbatim" else []
    return res


# ------------------------------------------------------------------ sweep: text report
def render_sweep(res):
    L = []
    s = res["source"]
    L.append("HANDOFF SWEEP  %s  [%s]" % (s["name"], s["format"]))
    line = "turns %d (user %d, assistant %d)" % (s["turns"], s["user_turns"], s["assistant_turns"])
    if s["first_timestamp"]:
        line += "  |  %s to %s" % (s["first_timestamp"], s["last_timestamp"])
    L.append(line)
    if s["cwd"] or s["branch"]:
        L.append("cwd %s  |  branch %s" % (", ".join(s["cwd"][-2:]) or "?", ", ".join(s["branch"][-2:]) or "?"))
    if s["picked"]:
        L.append("conversation %d of %d: %r (use --pick to change)" % (s["picked"][0], s["conversations_in_file"], s["picked"][1]))
    if s["title_hint"]:
        L.append("session title: %s" % clip(s["title_hint"], 100))
    if res["kind_guess"]:
        best = res["kind_guess"][0]["kind"]
        L.append("kind guess: %s  ->  python3 scripts/handoff.py profiles %s"
                 % (", ".join("%s (%d)" % (g["kind"], g["score"]) for g in res["kind_guess"]), best))
    for n in s["notes"]:
        L.append("note: " + n)
    if res["secrets_masked"]:
        L.append("note: %d secret-like string(s) masked below; never copy secrets into a handoff" % res["secrets_masked"])
    if res["first_request"]:
        L += ["", "FIRST REQUEST", "  T%d user: %s" % (res["first_request"]["turn"], res["first_request"]["quote"])]
    if res["last_turns"]:
        L += ["", "WHERE THE THREAD STOPPED"]
        L += ["  T%d %s: %s" % (d["turn"], d["role"], d["quote"]) for d in res["last_turns"]]
    titles = (("corrections", "CORRECTIONS (user pushback: each is a preference already paid for)"),
              ("constraints", "CONSTRAINTS (must / never / always / only / deadline / budget)"),
              ("decisions", "DECISIONS (who said it; assistant proposals show the user's reaction)"),
              ("dead_ends", "DEAD ENDS AND REJECTIONS"),
              ("open", "OPEN (questions the next reply skipped, pending and blocked markers)"))
    for key, title in titles:
        c = res["counts"].get(key, {"shown": 0, "total": 0})
        if not res[key] and not c["total"]:
            continue
        head = "%s  %d" % (title, c["shown"])
        if c["total"] > c["shown"]:
            head += " of %d (--limit 0 for all)" % c["total"]
        L += ["", head]
        for d in res[key]:
            extra = ""
            if d.get("reaction") and d["reaction"] != "n/a":
                extra = "   [user replied T%s: %s]" % (d.get("reply_turn") or "?", d["reaction"])
            if d.get("kind") == "question":
                extra = "   [%s]" % d["tags"][0]
            L.append("  T%-3d %-9s %s%s" % (d["turn"], d["role"], d["quote"], extra))
    if any(res["verbatim"].values()):
        L += ["", "VERBATIM CANDIDATES (copy exactly; check proves each one occurs in the thread)"]
        for kind in KIND_ORDER:
            items = res["verbatim"].get(kind)
            if not items:
                continue
            c = res["counts"]["verbatim"][kind]
            L.append("  %s%s" % (kind, "" if c["total"] == c["shown"] else "  (%d of %d)" % (c["shown"], c["total"])))
            for r in items:
                L.append("    %s   x%d T%d%s" % (r["value"], r["count"], r["first_turn"],
                                                 "" if r["last_turn"] == r["first_turn"] else "-T%d" % r["last_turn"]))
    if res["artifacts"]:
        c = res["counts"]["artifacts"]
        L += ["", "FILES WRITTEN IN THIS SESSION (ground truth from tool calls)  %d of %d" % (c["shown"], c["total"])]
        for r in res["artifacts"]:
            gone = "   [maybe %s by shell at T%d]" % ("removed" if r["gone_hint"]["how"] == "rm" else "moved", r["gone_hint"]["turn"]) \
                if r.get("gone_hint") else ""
            L.append("  %s   writes %d, edits %d, last T%d%s%s" % (r["path"], r["writes"], r["edits"], r["last_turn"],
                                                                   "  (scratch)" if r["scratch"] else "", gone))
    L += ["", "These are leads, not facts. Open the turn and confirm each one before it goes into HANDOFF.json."]
    return "\n".join(L)


# ------------------------------------------------------------------ handoff document model
SYN = {
    "decisions": {"reason": "why", "rationale": "why", "reasoning": "why", "because": "why", "decision": "what", "item": "what", "choice": "what"},
    "dead_ends": {"reason": "why", "rationale": "why", "reasoning": "why", "because": "why", "approach": "what", "attempt": "what", "tried": "what", "item": "what"},
    "artifacts": {"file": "name", "title": "name", "path": "location", "url": "location", "where": "location", "link": "location", "state": "status"},
    "verbatim": {"name": "label", "key": "label", "field": "label", "text": "value", "content": "value", "string": "value", "exact": "value"},
    "blocked": {"what": "item", "task": "item", "blocker": "unblocks", "unblocked_by": "unblocks", "waiting_on": "unblocks", "needs": "unblocks", "until": "unblocks"},
}


def norm_recipient(raw: str):
    r = (raw or "").strip().lower()
    if not r:
        return ""
    if r in RECIPIENTS:
        return r
    if re.search(r"human|colleague|teammate|team|person|client|manager", r):
        return "human"
    if re.search(r"self|\bme\b|future|myself", r):
        return "self"
    if re.search(r"\bai\b|claude|chat|session|model|assistant|llm", r):
        return "ai"
    return None


def _text(v):
    if v is None:
        return ""
    return v.strip() if isinstance(v, str) else str(v).strip()


def _strings(v, where, problems):
    if v is None:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, list):
        out = []
        for i, x in enumerate(v):
            if isinstance(x, (dict, list)):
                problems.append(("E", "S04", "%s[%d]" % (where, i), "expected a text line, found %s" % type(x).__name__))
            elif _text(x):
                out.append(_text(x))
        return out
    problems.append(("E", "S04", where, "expected a list of text lines, found %s" % type(v).__name__))
    return []


def _objects(v, sect, keys, problems):
    out = []
    if v is None:
        return out
    if not isinstance(v, list):
        problems.append(("E", "S04", sect, "expected a list, found %s" % type(v).__name__))
        return out
    syn = SYN.get(sect, {})
    for i, x in enumerate(v):
        where = "%s[%d]" % (sect, i)
        if isinstance(x, str):
            d = {k: "" for k in keys}
            d[PRIMARY.get(sect, keys[0])] = x.strip()
            out.append(d)
        elif isinstance(x, dict):
            d = {k: "" for k in keys}
            for k, val in x.items():
                tgt = k if k in keys else syn.get(k)
                if tgt and not d[tgt]:
                    d[tgt] = _text(val)
                    if tgt != k:
                        problems.append(("W", "S02", "%s.%s" % (where, k), "key %r was read as %r: use the schema names" % (k, tgt)))
                elif not tgt:
                    problems.append(("W", "S02", "%s.%s" % (where, k), "unknown key %r (expected %s)" % (k, ", ".join(keys))))
            out.append(d)
        else:
            problems.append(("E", "S04", where, "expected an object with %s" % ", ".join(keys)))
    return out


def normalize_doc(obj):
    """Coerce a loaded JSON object into the canonical shape. Returns (doc, present, problems)."""
    problems = []
    present = set(k for k in obj if not str(k).startswith("_"))
    doc = {}
    for k in ("title", "date", "recipient", "kind", "objective", "current_state"):
        v = obj.get(k)
        if isinstance(v, (dict, list)):
            problems.append(("E", "S04", k, "expected text, found %s" % type(v).__name__))
            v = None
        doc[k] = _text(v)
    r = norm_recipient(doc["recipient"])
    if r is None:
        problems.append(("E", "S07", "recipient", "%r is not ai, human or self" % doc["recipient"]))
        r = ""
    doc["recipient"] = r
    for sect in LIST_SECTIONS[:4]:
        doc[sect] = _objects(obj.get(sect), sect, ITEM_KEYS[sect], problems)
    doc["preferences"] = _strings(obj.get("preferences"), "preferences", problems)
    doc["unresolved"] = _strings(obj.get("unresolved"), "unresolved", problems)
    doc["uncertain"] = _strings(obj.get("uncertain"), "uncertain", problems)
    oi = obj.get("open_items")
    if oi is not None and not isinstance(oi, dict):
        problems.append(("E", "S04", "open_items", "expected an object with next, then, blocked"))
        oi = None
    oi = oi or {}
    nxt = oi.get("next")
    if isinstance(nxt, list):
        problems.append(("E", "N01", "open_items.next", "must be ONE step (a single text line), found a list of %d" % len(nxt)))
        nxt = nxt[0] if nxt else ""
    doc["open_items"] = {"next": _text(nxt), "then": _strings(oi.get("then"), "open_items.then", problems),
                         "blocked": _objects(oi.get("blocked"), "blocked", ("item", "unblocks"), problems)}
    for k in obj:
        if not str(k).startswith("_") and k not in ALL_FIELDS:
            close = difflib.get_close_matches(str(k), ALL_FIELDS, n=1, cutoff=0.6)
            problems.append(("W", "S02", str(k), "unknown field %r%s" % (k, "; did you mean %r?" % close[0] if close else "")))
    doc["_open_present"] = set(oi.keys()) if isinstance(oi, dict) else set()
    return doc, present, problems


# ------------------------------------------------------------------ markdown rendering
HEADINGS = {
    "ai": {"objective": "Objective", "current_state": "Current state", "decisions": "Decisions (settled, do not reopen)",
           "dead_ends": "Dead ends (do not retry)", "artifacts": "Artifacts", "verbatim": "Verbatim essentials (use exactly)",
           "preferences": "Working preferences (follow these)", "open_items": "Open items",
           "unresolved": "Unresolved (ask before assuming)", "uncertain": "Unconfirmed (do not rely on these)"},
    "human": {"objective": "Objective", "current_state": "Where things stand", "decisions": "Decisions and the reasons for them",
              "dead_ends": "Dead ends: tried and rejected", "artifacts": "Artifacts and their status", "verbatim": "Exact details (verbatim)",
              "preferences": "Working preferences", "open_items": "Open items and next step",
              "unresolved": "Unresolved questions", "uncertain": "Unconfirmed details"},
    "self": {"objective": "Objective (why you started)", "current_state": "Current state (you are here)",
             "decisions": "Decisions (and why you made them)", "dead_ends": "Dead ends (do not retry)", "artifacts": "Artifacts",
             "verbatim": "Verbatim essentials", "preferences": "Working preferences (learned the hard way)", "open_items": "Open items",
             "unresolved": "Unresolved", "uncertain": "Unconfirmed (do not trust these)"},
}
INTRO = {"ai": "State snapshot for a new AI session. Trust it over assumptions; settled decisions stay settled.",
         "human": "Prepared so you can pick up this work without reading the original conversation.",
         "self": "Note to future me. Half of this will be forgotten by then, so the reasons are written down."}
HEAD_KEYS = [
    (r"opening prompt|resume prompt|verify.?back", "prompt"),
    (r"dead end|did not work|didn'?t work|tried and rejected|ruled out", "dead_ends"),
    (r"decision", "decisions"), (r"artifact|deliverable", "artifacts"), (r"verbatim|exact detail", "verbatim"),
    (r"preference", "preferences"), (r"unresolved|open question", "unresolved"), (r"unconfirmed|uncertain", "uncertain"),
    (r"open item|next step|what happens next", "open_items"),
    (r"current state|where things stand|where we are|you are here", "current_state"), (r"objective|goal", "objective"),
]
NONE_TEXT = re.compile(r"^\W*(?:none|n/a|nothing|no \w+ (?:recorded|yet)|not applicable)\b\W*$", re.I)
RESUME_HEAD = (
    "You are taking over work from a previous chat. Read the handoff between the markers fully before you do anything.\n"
    "It is a state snapshot, not a story. Treat Decisions as settled and Dead ends as closed unless I say otherwise. "
    "Follow Working preferences from your first reply. Copy Verbatim essentials exactly, character for character. "
    "Do not rely on anything listed as Unresolved or Unconfirmed without asking me first.\n")
RESUME_TAIL = (
    "VERIFY-BACK (do this before anything else)\n"
    "Reply with exactly these three things, in your own words, then stop and wait for me to say \"go\":\n"
    "1. The goal.\n"
    "2. The single next step.\n"
    "3. Two things you must not do (take them from Dead ends, Working preferences or Decisions).\n"
    "If something in the handoff looks wrong, missing or contradictory, say so now instead of guessing.\n")


def code_span(s):
    n = max([len(r) for r in re.findall(r"`+", s)] or [0])
    pad = " " if s.startswith("`") or s.endswith("`") else ""
    return "%s%s%s%s%s" % ("`" * (n + 1), pad, s, pad, "`" * (n + 1))


def uncode(s):
    m = re.match(r"^(`+)(.*?)\1$", s.strip(), re.S)
    if not m:
        return s.strip()
    inner = m.group(2)
    if len(inner) > 2 and inner.startswith(" ") and inner.endswith(" ") and inner.strip():
        inner = inner[1:-1]
    return inner


def bullet(text, indent=""):
    lines = text.split("\n")
    return indent + "- " + lines[0] + "".join("\n" + indent + "  " + ln for ln in lines[1:])


def render_sections(doc, rcpt):
    """Return [(key, heading, body)] in reading order; key 'head' is the title block."""
    H = HEADINGS[rcpt]
    date = doc["date"] or "(no date)"
    head = "%s · %s%s\n\n_%s_" % (date, RECIPIENT_LABEL[rcpt], " · kind: " + doc["kind"] if doc["kind"] else "", INTRO[rcpt])
    out = [("head", "# Handoff: %s" % (doc["title"] or "(untitled)"), head)]
    out.append(("objective", H["objective"], doc["objective"] or "(not provided)"))
    out.append(("current_state", H["current_state"], doc["current_state"] or "(not provided)"))
    out.append(("decisions", H["decisions"], "\n".join(
        bullet("**%s**%s" % (d["what"], ": " + d["why"] if d["why"] else "")) for d in doc["decisions"]) or "None."))
    out.append(("dead_ends", H["dead_ends"], "\n".join(
        bullet("**%s**%s" % (d["what"], ": " + d["why"] if d["why"] else "")) for d in doc["dead_ends"]) or "None."))
    arts = []
    for a in doc["artifacts"]:
        loc = " at " + code_span(a["location"]) if a["location"] else ""
        arts.append(bullet("**%s**%s%s" % (a["name"], loc, ": " + a["status"] if a["status"] else "")))
    out.append(("artifacts", H["artifacts"], "\n".join(arts) or "None."))
    ver = []
    for v in doc["verbatim"]:
        val = v["value"]
        if "\n" in val:
            fence = "`" * max(3, max([len(r) for r in re.findall(r"`+", val)] or [0]) + 1)
            ver.append("- **%s**:\n  %s\n%s\n  %s" % (v["label"], fence, "\n".join("  " + ln for ln in val.split("\n")), fence))
        else:
            ver.append("- **%s**: %s" % (v["label"], code_span(val)))
    out.append(("verbatim", H["verbatim"], "\n".join(ver) or "None."))
    out.append(("preferences", H["preferences"], "\n".join(bullet(p) for p in doc["preferences"]) or "None."))
    oi = doc["open_items"]
    lines = ["- **Next step:** %s" % (oi["next"] or "(not provided)")]
    lines.append("- **Then:**" + ("" if oi["then"] else " none"))
    lines += [bullet(t, "  ") for t in oi["then"]]
    lines.append("- **Blocked:**" + ("" if oi["blocked"] else " none"))
    lines += [bullet("%s%s" % (b["item"], " (unblocks: %s)" % b["unblocks"] if b["unblocks"] else ""), "  ") for b in oi["blocked"]]
    out.append(("open_items", H["open_items"], "\n".join(lines)))
    if doc["unresolved"]:
        out.append(("unresolved", H["unresolved"], "\n".join(bullet(x) for x in doc["unresolved"])))
    if doc["uncertain"]:
        out.append(("uncertain", H["uncertain"], "\n".join(bullet(x) for x in doc["uncertain"])))
    return out


def join_sections(sections):
    parts = []
    for key, heading, body in sections:
        if key == "head":
            parts.append(heading + "\n" + body)
        else:
            parts.append("## %s\n%s" % (heading, body))
    return "\n\n".join(parts) + "\n"


def render_resume(md):
    return "%s\n===== HANDOFF START =====\n%s===== HANDOFF END =====\n\n%s" % (RESUME_HEAD, md, RESUME_TAIL)


def _paras(text):
    return "".join("<p>%s</p>" % html.escape(p).replace("\n", "<br>") for p in re.split(r"\n\s*\n", text) if p.strip())


def _ul(items):
    return "<ul>%s</ul>" % "".join("<li>%s</li>" % i for i in items) if items else "<p>None.</p>"


CSS = """:root{color-scheme:light}
body{font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:#1a1a1a;background:#fff;max-width:760px;margin:2rem auto;padding:0 1.25rem}
h1{font-size:1.6rem;margin:0 0 .25rem}.meta{color:#555;margin:0 0 1.25rem;font-size:.9rem}
.next{border-left:4px solid #1a1a1a;background:#f4f4f4;padding:.75rem 1rem;margin:1.25rem 0}
h2{font-size:1.05rem;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid #ddd;padding-bottom:.2rem;margin:1.75rem 0 .6rem}
table{border-collapse:collapse;width:100%;font-size:.95rem}th,td{text-align:left;vertical-align:top;border-bottom:1px solid #e5e5e5;padding:.35rem .5rem}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.88em;background:#f4f4f4}
pre{padding:.6rem .8rem;white-space:pre-wrap;overflow-wrap:anywhere;margin:.2rem 0}li{margin:.25rem 0}
@media print{body{margin:0;max-width:none}h2{break-after:avoid}li,tr{break-inside:avoid}}@page{margin:18mm}"""


def render_html(doc, rcpt):
    H, e = HEADINGS[rcpt], html.escape
    oi = doc["open_items"]
    P = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
         "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
         "<title>Handoff: %s</title><style>%s</style></head><body>" % (e(doc["title"] or "untitled"), CSS),
         "<h1>Handoff: %s</h1>" % e(doc["title"] or "(untitled)"),
         "<p class=\"meta\">%s · %s%s</p>" % (e(doc["date"] or "no date"), e(RECIPIENT_LABEL[rcpt]),
                                                    " · kind: " + e(doc["kind"]) if doc["kind"] else ""),
         "<div class=\"next\"><strong>Next step:</strong> %s</div>" % e(oi["next"] or "(not provided)"),
         "<h2>%s</h2>%s" % (e(H["objective"]), _paras(doc["objective"] or "(not provided)")),
         "<h2>%s</h2>%s" % (e(H["current_state"]), _paras(doc["current_state"] or "(not provided)")),
         "<h2>%s</h2>%s" % (e(H["decisions"]), _ul(["<strong>%s</strong>%s" % (e(d["what"]), ": " + e(d["why"]) if d["why"] else "") for d in doc["decisions"]])),
         "<h2>%s</h2>%s" % (e(H["dead_ends"]), _ul(["<strong>%s</strong>%s" % (e(d["what"]), ": " + e(d["why"]) if d["why"] else "") for d in doc["dead_ends"]]))]
    if doc["artifacts"]:
        rows = "".join("<tr><td>%s</td><td><code>%s</code></td><td>%s</td></tr>" % (e(a["name"]), e(a["location"]), e(a["status"])) for a in doc["artifacts"])
        P.append("<h2>%s</h2><table><tr><th>Name</th><th>Location</th><th>Status</th></tr>%s</table>" % (e(H["artifacts"]), rows))
    else:
        P.append("<h2>%s</h2><p>None.</p>" % e(H["artifacts"]))
    if doc["verbatim"]:
        rows = "".join("<tr><td>%s</td><td><pre>%s</pre></td></tr>" % (e(v["label"]), e(v["value"])) for v in doc["verbatim"])
        P.append("<h2>%s</h2><table>%s</table>" % (e(H["verbatim"]), rows))
    else:
        P.append("<h2>%s</h2><p>None.</p>" % e(H["verbatim"]))
    P.append("<h2>%s</h2>%s" % (e(H["preferences"]), _ul([e(p) for p in doc["preferences"]])))
    P.append("<h2>%s</h2><h3>Then</h3>%s<h3>Blocked</h3>%s" % (
        e(H["open_items"]), _ul([e(t) for t in oi["then"]]),
        _ul(["%s%s" % (e(b["item"]), " (unblocks: %s)" % e(b["unblocks"]) if b["unblocks"] else "") for b in oi["blocked"]])))
    if doc["unresolved"]:
        P.append("<h2>%s</h2>%s" % (e(H["unresolved"]), _ul([e(x) for x in doc["unresolved"]])))
    if doc["uncertain"]:
        P.append("<h2>%s</h2>%s" % (e(H["uncertain"]), _ul([e(x) for x in doc["uncertain"]])))
    P.append("</body></html>\n")
    return "\n".join(P)


# ------------------------------------------------------------------ markdown -> doc (for check on HANDOFF.md)
def heading_field(text):
    t = text.lower()
    for pat, field in HEAD_KEYS:
        if re.search(pat, t):
            return field
    return None


_FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})")


def _bullets(lines):
    """Group lines into top-level bullets: {text, fence, kids}. Nested bullets become kids."""
    items, fence = [], None
    for ln in lines:
        m = _FENCE_OPEN.match(ln)
        if fence is None and m:
            fence = (m.group(1)[0], len(m.group(1)))
            if items:
                items[-1]["fence"] = []
            continue
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not ln.strip().strip(fence[0]):
                fence = None
            elif items is not None and items and items[-1]["fence"] is not None:
                items[-1]["fence"].append(re.sub(r"^ {1,2}", "", ln))
            continue
        b = re.match(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$", ln)
        if b and len(b.group(1)) < 2:
            items.append({"text": b.group(2).rstrip(), "fence": None, "kids": []})
        elif b and items:
            items[-1]["kids"].append(b.group(2).rstrip())
        elif ln.strip() and items:
            items[-1]["text"] += "\n" + ln.strip()
    return items


def _split_what_why(t):
    t = t.strip()
    m = re.match(r"^\*\*(.+?):\*\*\s*(.*)$", t, re.S) or re.match(r"^\*\*(.+?)\*\*\s*[:.]?\s*(.*)$", t, re.S)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    if ": " in t:
        a, b = t.split(": ", 1)
        return a.strip(), b.strip()
    m = re.match(r"^(.*?)\s+because\s+(.*)$", t, re.S)
    return (m.group(1).strip(), m.group(2).strip()) if m else (t, "")


def _md_artifact(t):
    m = re.match(r"^\*\*(.+?)\*\*\s*(?:at|\()?\s*(`+)(.+?)\2\)?\s*[:.]?\s*(.*)$", t, re.S)
    if m:
        return {"name": m.group(1).strip(), "location": m.group(3).strip(), "status": m.group(4).strip()}
    t = t.replace("**", "")
    if ": " in t:
        a, b = t.rsplit(": ", 1)
        return {"name": a.strip(), "location": "", "status": b.strip()}
    return {"name": t.strip(), "location": "", "status": ""}


def _md_verbatim(it):
    t = it["text"].strip()
    m = re.match(r"^\*\*(.+?)\*\*\s*:?\s*(.*)$", t, re.S)
    label, val = (m.group(1), m.group(2)) if m else (t.split(": ", 1) + [""])[:2]
    if it["fence"] is not None:
        return {"label": label.strip(), "value": "\n".join(it["fence"]).strip("\n")}
    return {"label": label.strip(), "value": uncode(val)}


def md_to_doc(md):
    problems = []
    lines = md.replace("\r\n", "\n").split("\n")
    doc = {"title": "", "date": "", "recipient": "", "kind": "", "objective": "", "current_state": "",
           "decisions": [], "dead_ends": [], "artifacts": [], "verbatim": [], "preferences": [], "unresolved": [],
           "uncertain": [], "open_items": {"next": "", "then": [], "blocked": []}, "_open_present": set()}
    present, sections, cur, fence = set(), [], None, None
    for ln in lines:
        fm = _FENCE_OPEN.match(ln)
        if fence is None and fm:
            fence = (fm.group(1)[0], len(fm.group(1)))
        elif fence is not None and fm and fm.group(1)[0] == fence[0] and len(fm.group(1)) >= fence[1] and not ln.strip().strip(fence[0]):
            fence = None
        h = None if fence is not None else re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", ln)
        if h:
            level, text = len(h.group(1)), h.group(2)
            if level == 1 and not doc["title"]:
                doc["title"] = re.sub(r"^handoff\s*[:\-]\s*", "", text, flags=re.I).strip()
                present.add("title")
                cur = ("head", [])
            else:
                field = heading_field(text)
                if field is None:
                    problems.append(("W", "S02", text, "unrecognised section heading %r" % text))
                cur = (field or "?", [])
            sections.append(cur)
        elif cur is not None:
            cur[1].append(ln)
    for field, body in sections:
        text = "\n".join(body).strip()
        if field == "head":
            d = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
            if d:
                doc["date"] = d.group(0)
                present.add("date")
            low = text.lower()
            for r, label in RECIPIENT_LABEL.items():
                if label.lower() in low:
                    doc["recipient"] = r
                    present.add("recipient")
            k = re.search(r"kind:\s*(\w+)", low)
            if k:
                doc["kind"] = k.group(1)
                present.add("kind")
            continue
        if field in (None, "?", "prompt"):
            if field == "prompt":
                doc["_prompt"] = text
            continue
        present.add(field)
        empty = not text or NONE_TEXT.match(text) is not None
        if field in ("objective", "current_state"):
            doc[field] = "" if empty else text
        elif field == "open_items":
            _md_open_items(doc, body)
        elif empty:
            continue
        else:
            items = _bullets(body)
            if field in ("decisions", "dead_ends"):
                doc[field] = [dict(zip(("what", "why"), _split_what_why(i["text"]))) for i in items]
            elif field == "artifacts":
                doc[field] = [_md_artifact(i["text"]) for i in items]
            elif field == "verbatim":
                doc[field] = [_md_verbatim(i) for i in items]
            else:
                doc[field] = [i["text"].replace("**", "").strip() for i in items]
    return doc, present, problems


def _md_open_items(doc, body):
    items = _bullets(body)
    oi = doc["open_items"]
    for it in items:
        t = it["text"].strip()
        m = re.match(r"^\**\s*next(?: step)?\s*:?\s*\**\s*:?\s*(.*)$", t, re.I | re.S)
        if m:
            oi["next"] = m.group(1).strip()
            continue
        m = re.match(r"^\**\s*then\s*:?\s*\**\s*:?\s*(.*)$", t, re.I | re.S)
        if m:
            rest = [m.group(1).strip()] if m.group(1).strip() and m.group(1).strip().lower() != "none" else []
            oi["then"] += rest + it["kids"]
            continue
        m = re.match(r"^\**\s*blocked\s*:?\s*\**\s*:?\s*(.*)$", t, re.I | re.S)
        if m:
            rest = [m.group(1).strip()] if m.group(1).strip() and m.group(1).strip().lower() != "none" else []
            for b in rest + it["kids"]:
                mm = re.match(r"^(.*?)\s*\((?:unblocked by|unblocks?|until|needs?)[:\s]+(.*)\)\s*$", b)
                if mm:
                    oi["blocked"].append({"item": mm.group(1).strip(), "unblocks": mm.group(2).strip()})
                elif " - " in b:
                    a, c = b.split(" - ", 1)
                    oi["blocked"].append({"item": a.strip(), "unblocks": c.strip()})
                else:
                    oi["blocked"].append({"item": b.strip(), "unblocks": ""})


# ------------------------------------------------------------------ check
VERBS = frozenset("""add ask approve archive assign attach audit back begin book bring build call cancel change check choose clean clear close
collect commit compare complete confirm connect contact continue convert copy create cut decide define delete deliver deploy design detect
diagnose discuss document double-check download draft edit email enable ensure enter estimate evaluate examine execute export extract file fill
finalise finalize find finish fix follow forward generate get give go handle identify implement import improve include inspect install
integrate invite keep launch list load log look make map measure merge migrate monitor move name note notify open order organise organize
outline override pay pick ping plan polish post prepare present print prioritise prioritize profile prove publish pull push put read reach
rebuild record reduce refactor register reject release remove rename reorder repeat replace reply report request rerun re-run research
reserve reset resolve restart restore retry review revise revert rewrite run sample save schedule scope search select send set share shortlist
sign sketch split start submit summarise summarize switch sync take test thank tidy track translate trim try tweak undo update upgrade upload
use validate verify view visit wait walk watch write proofread chase nudge draw""".split())
NOT_VERB_START = frozenset("""the a an we i you they he she it this that these those then next after once when while if and but so also still
maybe probably possibly todo tbd none nothing n/a my our your their his her some any all both more another one first finally need needs
should must will would could can may might to ready""".split())
GERUND_OK = frozenset(("bring", "string", "spring"))
REL_DATE_RE = re.compile(
    r"\b(today|tomorrow|yesterday|tonight|this (?:week|month|year|morning|afternoon|evening|weekend)|"
    r"next (?:week|month|year|weekend|quarter|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"last (?:week|month|year|night|weekend|quarter|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"later today|earlier today|the other day|recently|"
    r"in (?:a|an|one|two|three|four|five|\d+) (?:few )?(?:days?|weeks?|months?|hours?)|"
    r"(?:\d+|a|an|one|two|three|a few|several) (?:days?|weeks?|months?) ago)\b", re.I)
WEEKDAY_RE = re.compile(r"\b(?:mon|tues?|wednes|thurs?|fri|satur|sun)day\b", re.I)
DATE_NEAR_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}\b", re.I)
DANGLING_RE = re.compile(
    r"\b(as above|see above|as mentioned|as discussed|as we discussed|as i (?:said|mentioned)|as you (?:said|mentioned)|"
    r"(?:mentioned|discussed|noted) (?:earlier|before|above)|the (?:above|previous message|last message|earlier message)|"
    r"(?:earlier|previous|above) (?:chat|thread|conversation|session)|in the (?:chat|thread|conversation) above|"
    r"same as before|as before|like before|as usual|the usual|what we (?:said|discussed))\b", re.I)
NARRATIVE_RE = re.compile(r"\b(first we|then we|we started|we began|after that|next we|at first|initially we|we then|and then we|later we|eventually we)\b", re.I)
MULTI_STEP_RE = re.compile(r";|\band then\b|\bthen\b|\bafter that\b|\bafterwards\b|\b1[.)]\s.*\b2[.)]\s")
BLANK_REASONS = {"", "n/a", "na", "tbd", "todo", "none", "-", "because", "unknown", "?"}
PLACEHOLDER_VALUES = {"none", "n/a", "na", "unknown", "-", "not applicable", "no"}


def prose_fields(doc):
    yield "title", doc["title"]
    yield "objective", doc["objective"]
    yield "current_state", doc["current_state"]
    for sect in ("decisions", "dead_ends"):
        for i, d in enumerate(doc[sect]):
            yield "%s[%d].what" % (sect, i), d["what"]
            yield "%s[%d].why" % (sect, i), d["why"]
    for i, a in enumerate(doc["artifacts"]):
        yield "artifacts[%d].status" % i, a["status"]
    for sect in ("preferences", "unresolved", "uncertain"):
        for i, x in enumerate(doc[sect]):
            yield "%s[%d]" % (sect, i), x
    oi = doc["open_items"]
    yield "open_items.next", oi["next"]
    for i, x in enumerate(oi["then"]):
        yield "open_items.then[%d]" % i, x
    for i, b in enumerate(oi["blocked"]):
        yield "open_items.blocked[%d].item" % i, b["item"]
        yield "open_items.blocked[%d].unblocks" % i, b["unblocks"]


def covered_by_artifacts(path, arts):
    p = path.lower()
    for a in arts:
        for s in (a["location"], a["name"]):
            s = s.strip().lower().lstrip("./").rstrip("/")
            if not s:
                continue
            if p.endswith(s) or s.endswith(p) or p.startswith(s + "/") or os.path.basename(p) == os.path.basename(s):
                return True
    return False


def in_haystack(value, hay, state):
    """Return 'exact', 'normalized', 'case' or None. state caches the normalised haystack."""
    if value in hay:
        return "exact"
    if "norm" not in state:
        state["norm"] = norm_match(hay)
        state["low"] = state["norm"].lower()
    nv = norm_match(value)
    if nv and nv in state["norm"]:
        return "normalized"
    if nv and nv.lower() in state["low"]:
        return "case"
    return None


def run_checks(doc, present, pre, fmt, md_text, tr, budget, kind, profiles, md_dir=None):
    F = []

    def add(level, code, where, msg):
        F.append({"level": level, "code": code, "where": where, "message": msg})

    for lv, code, where, msg in pre:
        add(lv, code, where, msg)
    for k in ("title", "objective", "current_state"):
        if k not in present:
            add("E", "S03", k, "missing: add %s" % k)
        elif not doc[k]:
            add("E", "S05", k, "is empty")
    for sect in LIST_SECTIONS + ("open_items",):
        if sect not in present:
            add("E", "S03", sect, "section missing: add it, or write [] if there was nothing (an explicit empty is fine)")
    if not doc["date"]:
        add("W", "S06", "date", "no date: add the handoff date as YYYY-MM-DD")
    elif not re.match(r"^\d{4}-\d{2}-\d{2}$", doc["date"]):
        add("W", "S06", "date", "use YYYY-MM-DD, found %r" % doc["date"])
    if not doc["recipient"] and not any(f["code"] == "S07" for f in F):
        add("W", "S07", "recipient", "no recipient: set ai, human or self (build defaults to ai)")
    if kind and kind not in KINDS:
        add("E", "K02", "kind", "%r is not one of %s" % (kind, ", ".join(KINDS)))
    elif not kind:
        add("W", "K01", "kind", "no kind: set one of %s so the type-specific fields can be checked" % ", ".join(KINDS))
    for sect in ("decisions", "dead_ends"):
        code = "D" if sect == "decisions" else "X"
        for i, d in enumerate(doc[sect]):
            w = "%s[%d]" % (sect, i)
            if not d["what"]:
                add("E", code + "01", w + ".what", "entry has no text")
            if d["why"].strip().lower() in BLANK_REASONS or len(d["why"].strip()) < 4:
                add("E", code + "02", w + ".why", "no reason given for %r: %s" % (
                    clip(d["what"], 50), "without it the next session reopens the decision" if sect == "decisions"
                    else "say why it failed or was rejected, and whether the reason is fixable"))
    seen_art = {}
    for i, a in enumerate(doc["artifacts"]):
        w = "artifacts[%d]" % i
        if not a["name"]:
            add("E", "A01", w + ".name", "artifact has no name")
        if not a["status"]:
            add("E", "A02", w + ".status", "%r has no status: say final, draft, or superseded by X" % a["name"])
        elif a["status"].lower().startswith("superseded") and not re.search(r"\bby\b", a["status"], re.I):
            add("W", "A04", w + ".status", "superseded by what? name the file that replaced it")
        if not a["location"]:
            add("W", "A03", w + ".location", "%r has no location" % a["name"])
        key = (a["location"] or a["name"]).lower()
        if key and key in seen_art:
            add("W", "A05", w, "%r is listed twice (see artifacts[%d]): which one is live?" % (a["name"], seen_art[key]))
        seen_art.setdefault(key, i)
    for i, v in enumerate(doc["verbatim"]):
        w = "verbatim[%d]" % i
        if not v["label"] or not v["value"]:
            add("E", "V01", w, "needs both a label and a value")
        if len(v["value"]) > 1200:
            add("W", "V03", w, "very long value (%d chars): keep it in a file and list the file as an artifact" % len(v["value"]))
    oi = doc["open_items"]
    nxt = oi["next"].strip()
    if not nxt:
        add("E", "N01", "open_items.next", "missing: give the ONE concrete next step")
    else:
        first = re.sub(r"[^\w'-]", "", nxt.split()[0].lower())
        if first in NOT_VERB_START or (first.endswith("ing") and len(first) > 4 and first not in GERUND_OK):
            add("E", "N02", "open_items.next", "starts with %r, not a verb: write it as an action such as 'Send ...'" % nxt.split()[0])
        elif first not in VERBS:
            add("W", "N02", "open_items.next", "starts with %r; is that a verb? Start with an action word such as Draft, Send, Run" % nxt.split()[0])
        if "\n" in nxt or MULTI_STEP_RE.search(nxt) or len(re.findall(r"[.!?]\s+[A-Z]", nxt)) >= 1:
            add("W", "N03", "open_items.next", "looks like more than one step: keep ONE here and move the rest to 'then'")
    for i, b in enumerate(oi["blocked"]):
        if not b["item"] or not b["unblocks"]:
            add("W", "N04", "open_items.blocked[%d]" % i, "say what is blocked AND what unblocks it")
    if len(doc["objective"]) > 500:
        add("W", "T02", "objective", "objective is %d chars: keep it to 1-2 sentences" % len(doc["objective"]))
    for where, text in prose_fields(doc):
        if not text:
            continue
        for m in REL_DATE_RE.finditer(text):
            add("W", "R01", where, "relative date %r: write the calendar date (YYYY-MM-DD); it will be wrong when read later" % m.group(0))
        spans = [m.span() for m in REL_DATE_RE.finditer(text)]
        for wm in WEEKDAY_RE.finditer(text):
            if not DATE_NEAR_RE.search(text) and not any(a <= wm.start() < b for a, b in spans):
                add("W", "R02", where, "weekday %r without a date: add the calendar date" % wm.group(0))
                break
        m = DANGLING_RE.search(text)
        if m:
            add("W", "L01", where, "dangling reference %r: the reader has no thread to look back at, say it outright" % m.group(0))
    for where in ("objective", "current_state"):
        if len({m.group(0).lower() for m in NARRATIVE_RE.finditer(doc[where])}) >= 2:
            add("W", "T01", where, "reads like a story (first... then...): state where things stand, not the order things happened")
    scan = [(w, t) for w, t in prose_fields(doc)] + [("verbatim[%d]" % i, v["value"]) for i, v in enumerate(doc["verbatim"])]
    for where, text in scan:
        if has_secret(text, strong_only=True):
            add("E", "V02", where, "looks like a secret or key: replace it with a placeholder and say where the real one lives")
        elif has_secret(text):
            add("W", "V02", where, "may contain a password or token: never put credentials in a handoff")
    tokens = est_tokens(md_text)
    if tokens > 2 * budget:
        add("E", "B01", "document", "about %d tokens (budget %d): you are narrating, not snapshotting" % (tokens, budget))
    elif tokens > budget:
        add("W", "B01", "document", "about %d tokens (budget %d): trim, or raise --budget on purpose" % (tokens, budget))
    if kind in profiles:
        blob = " ".join([t for _, t in prose_fields(doc)] + ["%s %s" % (v["label"], v["value"]) for v in doc["verbatim"]]
                        + ["%s %s %s" % (a["name"], a["location"], a["status"]) for a in doc["artifacts"]]).lower()
        for cap in profiles[kind].get("capture", []):
            if not any(k in blob for k in cap.get("keys", [])):
                add("W", "K03", "kind:" + kind, "nothing about '%s' (%s); add it under %s, or state that it does not apply"
                    % (cap["field"], cap.get("why", ""), cap.get("where", "the right section")))
    if fmt == "md" and doc["recipient"] == "ai":
        if "_prompt" in doc:
            if not re.search(r"restate|verify|before (?:doing|you do) anything", doc["_prompt"], re.I):
                add("W", "M01", "prompt", "the opening prompt has no VERIFY-BACK step (restate goal, next step, two must-not-dos)")
        elif not (md_dir and (Path(md_dir) / "resume-prompt.txt").is_file()):
            add("W", "M01", "prompt", "no Suggested opening prompt section and no resume-prompt.txt next to this file")
    fid = None
    if tr is not None:
        fid = fidelity(doc, tr, add)
    return F, fid


def fidelity(doc, tr, add):
    hay = tr.haystack()
    state = {}
    stats = {"transcript": tr.path, "checked": 0, "exact": 0, "normalized": 0, "case": 0, "missing": 0}
    if not hay.strip():
        add("W", "F00", "transcript", "the transcript is empty: nothing can be verified")
    for i, v in enumerate(doc["verbatim"]):
        val = v["value"].strip()
        if not val or val.lower() in PLACEHOLDER_VALUES:
            continue
        stats["checked"] += 1
        how = in_haystack(val, hay, state)
        w = "verbatim[%d]" % i
        if how == "exact":
            stats["exact"] += 1
        elif how == "normalized":
            stats["normalized"] += 1
            add("W", "F02", w, "%r matches only after normalising quotes, dashes or spacing: copy it exactly" % clip(val, 60))
        elif how == "case":
            stats["case"] += 1
            add("W", "F03", w, "%r matches only ignoring case: copy it exactly" % clip(val, 60))
        else:
            stats["missing"] += 1
            add("E", "F01", w, "%r (%s) does not occur in the transcript: possibly invented or misquoted" % (clip(val, 60), v["label"]))
    skip = {doc["date"]}
    reported = set()
    for where, text in prose_fields(doc):
        for kind, val in extract_tokens(text):
            if kind not in ("url", "email", "path", "id", "date", "money", "percent", "color", "version") or val in skip or (kind, val) in reported:
                continue
            reported.add((kind, val))
            if in_haystack(val, hay, state) is None:
                add("W", "F04", where, "%s %r appears here but never in the transcript" % (kind, clip(val, 60)))
    for i, a in enumerate(doc["artifacts"]):
        if a["location"] and "/" in a["location"] and in_haystack(a["location"], hay, state) is None:
            add("W", "F04", "artifacts[%d].location" % i, "path %r never appears in the transcript" % clip(a["location"], 60))
    inv = [r for r in inventory(tr) if not r["scratch"]]
    if inv:
        miss = [r for r in inv if not covered_by_artifacts(r["path"], doc["artifacts"])]
        for r in miss[:8]:
            add("W", "F05", "artifacts", "written in the session but not listed: %s (last touched T%d)" % (r["path"], r["last_turn"]))
        if len(miss) > 8:
            add("W", "F05", "artifacts", "and %d more files written in the session are not listed" % (len(miss) - 8))
        stats["files_written"] = len(inv)
    return stats


# ------------------------------------------------------------------ commands
def emit_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def resolve_source(args):
    """Return a Transcript from a path or --latest, or None."""
    latest = getattr(args, "latest", False)
    path = getattr(args, "transcript", None)
    if latest and path:
        raise UserError("give a transcript file or --latest, not both")
    if latest:
        path = str(find_latest_session(getattr(args, "cwd", None), getattr(args, "projects_dir", None)))
    if not path:
        return None
    return load_transcript(path, getattr(args, "pick", None))


def cmd_sweep(args):
    tr = resolve_source(args)
    if tr is None:
        raise UserError("give a transcript file (or - for stdin), or use --latest")
    if args.limit < 0:
        raise UserError("--limit must be 0 or more")
    only = None
    if args.only:
        only = {x.strip() for x in args.only.split(",") if x.strip()}
        bad = only - {"corrections", "constraints", "decisions", "dead_ends", "open", "verbatim", "artifacts"}
        if bad:
            raise UserError("--only: unknown section(s) %s" % ", ".join(sorted(bad)))
    res = sweep_result(tr, limit=args.limit, only=only)
    if args.json:
        emit_json(res)
    else:
        print(render_sweep(res))
    return 0


def resolve_out(out):
    p = Path(out)
    if not p.is_absolute():
        cwd = Path.cwd().resolve()
        target = (cwd / p).resolve()
        if target != cwd and cwd not in target.parents:
            raise UserError("--out %s is outside the current folder: pass an absolute path if that is intended" % out)
        p = target
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise UserError("cannot create %s: %s" % (p, e.strerror or e))
    return p


def show_path(p):
    try:
        return str(Path(p).resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(p)


def load_json_doc(path):
    text = read_text(path)
    if not text.strip():
        raise UserError("%s is empty" % path)
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError) as e:
        raise UserError("%s is not valid JSON (%s)" % (path, e))
    if not isinstance(obj, dict):
        raise UserError("%s: the top level must be a JSON object" % path)
    return obj


def cmd_build(args):
    doc, present, problems = normalize_doc(load_json_doc(args.file))
    rcpt = args.rcpt or doc["recipient"] or "ai"
    out = resolve_out(args.out)
    sections = render_sections(doc, rcpt)
    md = join_sections(sections)
    files = [("HANDOFF.md", md)]
    if rcpt == "ai":
        files.append(("resume-prompt.txt", render_resume(md)))
    if args.html:
        files.append(("HANDOFF.html", render_html(doc, rcpt)))
    written = []
    for name, content in files:
        target = out / name
        try:
            with open(str(target), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
        except OSError as e:
            raise UserError("cannot write %s: %s" % (target, e.strerror or e))
        written.append({"path": show_path(target), "tokens": est_tokens(content), "words": len(content.split()),
                        "read_seconds": read_secs(content)})
    secs = [{"section": (h.lstrip("# ").replace("Handoff: ", "Title: ") if k == "head" else h), "tokens": est_tokens(h + "\n" + b),
             "words": len((h + " " + b).split()), "read_seconds": read_secs(h + " " + b)} for k, h, b in sections]
    total = {"tokens": est_tokens(md), "words": len(md.split()), "read_seconds": read_secs(md)}
    if args.json:
        emit_json({"recipient": rcpt, "files": written, "sections": secs, "total": total,
                   "problems": [{"level": a, "code": b, "where": c, "message": d} for a, b, c, d in problems]})
        return 0
    print("built for %s from %s" % (rcpt, args.file))
    for w in written:
        print("  wrote %-22s ~%d tokens, ~%s to read" % (w["path"], w["tokens"], fmt_secs(w["read_seconds"])))
    print("\n  %-44s %7s %6s %7s" % ("section", "tokens", "words", "read"))
    for s in secs:
        print("  %-44s %7d %6d %7s" % (clip(s["section"], 44), s["tokens"], s["words"], fmt_secs(s["read_seconds"])))
    print("  %-44s %7d %6d %7s" % ("TOTAL (HANDOFF.md)", total["tokens"], total["words"], fmt_secs(total["read_seconds"])))
    if problems:
        print("\nnote: %d problem(s) in the JSON; run check for details" % len(problems))
    print("next: run  python3 scripts/handoff.py check %s  (add --latest or --transcript FILE to verify the quotes)" % args.file)
    return 0


def load_doc_file(path):
    text = read_text(path)
    if not text.strip():
        raise UserError("%s is empty" % path)
    ext = Path(path).suffix.lower() if path != "-" else ""
    if ext == ".json" or (ext not in (".md", ".markdown", ".txt") and text.lstrip()[:1] == "{"):
        try:
            obj = json.loads(text)
        except (ValueError, RecursionError) as e:
            raise UserError("%s is not valid JSON (%s)" % (path, e))
        if not isinstance(obj, dict):
            raise UserError("%s: the top level must be a JSON object" % path)
        doc, present, problems = normalize_doc(obj)
        return doc, present, problems, "json", text
    doc, present, problems = md_to_doc(text)
    return doc, present, problems, "md", text


def cmd_check(args):
    doc, present, pre, fmt, raw = load_doc_file(args.file)
    tr = resolve_source(args)
    profiles = load_profiles()
    kind = args.profile or doc["kind"]
    if args.profile and args.profile not in KINDS:
        raise UserError("--profile must be one of %s" % ", ".join(KINDS))
    if args.budget < 1:
        raise UserError("--budget must be a positive number of tokens")
    md = raw if fmt == "md" else join_sections(render_sections(doc, doc["recipient"] or "ai"))
    md_dir = os.path.dirname(os.path.abspath(args.file)) if (fmt == "md" and args.file != "-") else None
    findings, fid = run_checks(doc, present, pre, fmt, md, tr, args.budget, kind, profiles, md_dir)
    errs = sum(1 for f in findings if f["level"] == "E")
    warns = len(findings) - errs
    tokens, secs = est_tokens(md), read_secs(md)
    if args.json:
        emit_json({"file": args.file, "format": fmt, "recipient": doc["recipient"], "kind": kind, "tokens": tokens,
                   "read_seconds": secs, "errors": errs, "warnings": warns, "ok": errs == 0, "findings": findings,
                   "fidelity": fid})
        return 1 if errs else 0
    print("handoff check: %s  (%s, ~%d tokens, ~%s to read)" % (args.file, fmt, tokens, fmt_secs(secs)))
    for f in findings:
        print("  %s %s  %-28s %s" % (f["level"], f["code"], f["where"], f["message"]))
    if fid is not None:
        print("fidelity vs %s: %d verbatim value(s) checked, %d exact, %d near, %d NOT found"
              % (fid["transcript"], fid["checked"], fid["exact"], fid["normalized"] + fid["case"], fid["missing"]))
    else:
        print("fidelity: not checked (add --transcript FILE or --latest to prove the verbatim values occur in the thread)")
    print("ok: no errors, no warnings" if not findings else "result: %d error(s), %d warning(s)" % (errs, warns))
    return 1 if errs else 0


def cmd_profiles(args):
    profiles = load_profiles()
    if not profiles:
        raise UserError("assets/profiles.json is missing or unreadable")
    if args.kind is None:
        if args.json:
            emit_json({k: v.get("title", k) for k, v in profiles.items()})
        else:
            for k, v in profiles.items():
                print("%-10s %s (%d fields)" % (k, v.get("title", ""), len(v.get("capture", []))))
            print("general    anything else: no extra fields")
        return 0
    if args.kind == "general":
        print("general: no extra fields beyond the schema")
        return 0
    if args.kind not in profiles:
        raise UserError("unknown kind %r: choose one of %s" % (args.kind, ", ".join(list(profiles) + ["general"])))
    p = profiles[args.kind]
    if args.json:
        emit_json(p)
        return 0
    print("%s (kind: %s)" % (p.get("title", args.kind), args.kind))
    print("Also capture, in the section shown in brackets:")
    for c in p.get("capture", []):
        print("  - %s [%s]: %s" % (c["field"], c.get("where", "verbatim"), c.get("why", "")))
    for t in p.get("traps", []):
        print("Watch out: " + t)
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="handoff.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="{sweep,build,check,profiles}")
    raw = argparse.RawDescriptionHelpFormatter

    def source_args(p):
        p.add_argument("--latest", action="store_true", help="use the newest Claude Code session for the current folder")
        p.add_argument("--cwd", metavar="DIR", help="folder whose sessions --latest should look up (default: current folder)")
        p.add_argument("--projects-dir", metavar="DIR", help="Claude Code projects folder (default: ~/.claude/projects)")
        p.add_argument("--pick", metavar="N|TEXT", help="for exports with many conversations: number or title text (default: newest)")

    p = sub.add_parser("sweep", formatter_class=raw, help="list what must survive, with turn numbers",
                       description="Read a saved conversation and list candidates: corrections, constraints, decisions, dead ends,\n"
                                   "open questions, verbatim values and (Claude Code) the files really written.\n"
                                   "Input: Claude Code .jsonl, claude.ai or ChatGPT export .json, or text with User:/Assistant: lines.",
                       epilog="examples:\n  handoff.py sweep --latest\n  handoff.py sweep chat.txt --limit 20\n  handoff.py sweep export.json --pick \"pricing\" --json\n  cat chat.txt | handoff.py sweep - --only verbatim,open")
    p.add_argument("transcript", nargs="?", help="transcript file, or - for stdin")
    source_args(p)
    p.add_argument("--limit", type=int, default=12, help="max candidates per list (default 12, 0 = all)")
    p.add_argument("--only", metavar="LIST", help="comma list of: corrections,constraints,decisions,dead_ends,open,verbatim,artifacts")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("build", formatter_class=raw, help="render HANDOFF.json into HANDOFF.md (+ resume prompt, + HTML)",
                       description="Render HANDOFF.json (see assets/handoff.schema.json) into HANDOFF.md.\n"
                                   "For an AI reader also writes resume-prompt.txt, one paste-ready block that ends with a VERIFY-BACK step.\n"
                                   "Reports approximate tokens and reading time per section.",
                       epilog="examples:\n  handoff.py build HANDOFF.json\n  handoff.py build HANDOFF.json --for human --html --out briefs")
    p.add_argument("file", help="HANDOFF.json, or - for stdin")
    p.add_argument("--for", dest="rcpt", choices=RECIPIENTS, help="recipient (default: the recipient field, else ai)")
    p.add_argument("--out", default=".", metavar="DIR", help="output folder (default: current folder; must be inside it unless absolute)")
    p.add_argument("--html", action="store_true", help="also write a clean printable HANDOFF.html")
    p.add_argument("--json", action="store_true", help="machine-readable report")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("check", formatter_class=raw, help="lint a handoff and prove its verbatim values are real",
                       description="Check structure and quality of HANDOFF.json or HANDOFF.md. With a transcript, also check fidelity:\n"
                                   "every verbatim value must occur literally in the thread.\n"
                                   "Exit 0 = no errors, 1 = errors found, 2 = could not run.",
                       epilog="examples:\n  handoff.py check HANDOFF.json --latest\n  handoff.py check HANDOFF.md --transcript chat.txt --json\n  handoff.py check HANDOFF.json --profile code --budget 1200")
    p.add_argument("file", help="HANDOFF.json or HANDOFF.md, or - for stdin")
    p.add_argument("--transcript", metavar="FILE", help="conversation to verify against (any format sweep reads)")
    source_args(p)
    p.add_argument("--profile", metavar="KIND", help="check the fields of this kind of work (default: the kind field)")
    p.add_argument("--budget", type=int, default=1500, metavar="TOKENS", help="token budget before warning (default 1500)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("profiles", formatter_class=raw, help="what else to capture for a kind of work",
                       description="List the kinds of work, or show the extra fields a handoff of that kind must capture.",
                       epilog="examples:\n  handoff.py profiles\n  handoff.py profiles code")
    p.add_argument("kind", nargs="?", help="code, writing, research, analysis, design, planning, sales")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_profiles)
    return ap


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UserError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except BrokenPipeError:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        return 0
    except (RecursionError, MemoryError):
        print("error: the input is too large or too deeply nested to process", file=sys.stderr)
        return 2
    except OSError as e:
        print("error: %s" % (e.strerror or e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
