"""Internal helpers for dataio.py: parse numbers and dates, infer column types.

Not a command. Import it (dataio does), do not run it.

Key rules:
  * numbers: "1,234.56", "1.234,56", "1 234,56", "$1,200", "12%", "(500)", "1e5"
  * the decimal separator is decided per COLUMN from evidence, and flagged if it stays ambiguous
  * dates: ISO, D/M/Y, M/D/Y, "Aug 5, 2026", "5 Aug 2026", "2026-08"; DD/MM vs MM/DD is decided
    from evidence in the column (a day above 12) and otherwise reported as AMBIGUOUS, never guessed
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections import Counter

NULL_TOKENS = {
    "", "n/a", "na", "n.a.", "n.a", "-", "--", "---", "\u2013", "\u2014", "null", "none", "nan", "nil",
    "#n/a", "#div/0!", "#value!", "#ref!", "#name?", "#num!", "#null!", "#error!", "missing", "(blank)", "(null)",
}
NULL_VARIANTS = set()
for _t in list(NULL_TOKENS):
    NULL_VARIANTS.update({_t, _t.upper(), _t.capitalize()})
BOOL_TOKENS = {"true": True, "false": False, "yes": True, "no": False, "y": True, "n": False}
ID_NAME = re.compile(r"(^|[\s_\-.])(id|uuid|guid|sku|code|key)$|^id$|[a-z]Id$", re.I)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTHS["sept"] = 9
MONTH_WORDS = dict(MONTHS)
MONTH_WORDS.update({w: i for i, w in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
     "december"], 1)})
NUMERIC_TYPES = ("int", "float", "money", "percent")
SAMPLE_OVER = 400000      # columns longer than this are typed from an even sample
SAMPLE_SIZE = 150000

_CUR = r"(?:[$€£¥₹]|USD|EUR|GBP|CHF|CAD|AUD|JPY|CNY|INR)"
_BODY = r"(?:\d(?:[\d.,'’  ]*\d)?|[.,]\d+)"
_NUM_RE = re.compile(r"^(?P<s1>[-+−]?)\s*(?P<pre>" + _CUR + r")?\s*(?P<s2>[-−]?)\s*(?P<body>" + _BODY
                     + r")\s*(?P<post>%|" + _CUR + r")?$")
_SCI_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?[eE][-+]?\d+$")

_TIME = r"(?:[ T,]+(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<se>\d{2})(?:[.,]\d+)?)?\s*(?P<ap>[AaPp][Mm])?)?\s*(?:Z|[+-]\d{2}:?\d{2})?"
_ISO_RE = re.compile(r"^(?P<y>\d{4})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})" + _TIME + r"$")
_NUMDATE_RE = re.compile(r"^(?P<a>\d{1,2})(?P<sep>[/.\-])(?P<b>\d{1,2})(?P=sep)(?P<y>\d{4}|\d{2})" + _TIME + r"$")
_DMONY_RE = re.compile(r"^(?P<d>\d{1,2})(?:st|nd|rd|th)?[ \-/.]+(?P<mon>[A-Za-z]{3,9})\.?[ \-/.,]+(?P<y>\d{4}|\d{2})" + _TIME + r"$")
_MONDY_RE = re.compile(r"^(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>\d{4})" + _TIME + r"$")
_YM_RE = re.compile(r"^(?P<y>\d{4})-(?P<m>0[1-9]|1[0-2])$")


def norm_key(s: str) -> str:
    """Case, spacing, punctuation and accent-insensitive key: 'Paris ', 'PARIS' and 'paris' share one key."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", s)).strip()


def is_null(s) -> bool:
    return s is None or s.strip().lower() in NULL_TOKENS


# ---------------------------------------------------------------- numbers
def number_shape(s: str):
    """Return (negative, hint, body) if s looks like a number, else None. hint: money, percent, or None."""
    t = s.strip()
    if not t:
        return None
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1].strip()
    if _SCI_RE.match(t):
        return (t.startswith("-"), None, "sci:" + t)
    m = _NUM_RE.match(t)
    if not m:
        return None
    if m.group("s1") in ("-", "−") or m.group("s2"):
        neg = True
    hint = None
    if m.group("post") == "%":
        hint = "percent"
    elif m.group("pre") or m.group("post"):
        hint = "money"
    return (neg, hint, m.group("body"))


def detect_decimal(bodies):
    """Decide the decimal separator from a set of number bodies. Returns (decimal, ambiguous)."""
    votes = {".": 0, ",": 0}
    for b in bodies:
        if b.startswith("sci:"):
            continue
        has_c, has_d = "," in b, "." in b
        if has_c and has_d:
            votes["," if b.rfind(",") > b.rfind(".") else "."] += 3
        elif has_c:
            if b.count(",") > 1:
                votes["."] += 2
            elif len(b.split(",")[1]) != 3:
                votes[","] += 2
        elif has_d:
            if b.count(".") > 1:
                votes[","] += 2
            elif len(b.split(".")[1]) != 3:
                votes["."] += 2
    if votes[","] > votes["."]:
        return ",", False
    ambiguous = votes["."] == 0 and any(
        (("," in b) != ("." in b)) and not b.startswith("sci:") for b in bodies)
    return ".", ambiguous


def body_to_float(body: str, decimal: str):
    """Parse a number body with a known decimal separator. Returns (float, has_fraction) or None."""
    if body.startswith("sci:"):
        try:
            return float(body[4:]), True
        except ValueError:
            return None
    b = body.replace(" ", " ")
    thousands = "," if decimal == "." else "."
    if b.count(decimal) > 1:
        return None
    ip, _, fp = b.partition(decimal)
    if thousands in fp or " " in fp or "'" in fp:
        return None
    groups = re.split("[" + re.escape(thousands) + r" '’]", ip)
    if len(groups) > 1 and not (1 <= len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:])):
        return None
    ip = "".join(groups) or "0"
    if not ip.isdigit() or (fp and not fp.isdigit()):
        return None
    return float(ip + ("." + fp if fp else "")), bool(fp)


def parse_number(s: str, decimal: str = "."):
    """Parse one cell to (value, hint, has_fraction) or None."""
    sh = number_shape(s)
    if sh is None:
        return None
    neg, hint, body = sh
    r = body_to_float(body, decimal)
    if r is None:
        return None
    v, frac = r
    return (-v if neg and not body.startswith("sci:") else v, hint, frac)


# ---------------------------------------------------------------- dates
def _year(y: str) -> int:
    n = int(y)
    if len(y) == 2:
        n += 2000 if n < 70 else 1900
    return n


def _time(m):
    if m.groupdict().get("h") is None:
        return None
    h, mi, se = int(m.group("h")), int(m.group("mi")), int(m.group("se") or 0)
    ap = m.group("ap")
    if ap:
        h = h % 12 + (12 if ap.lower() == "pm" else 0)
    return (h, mi, se)


def date_parts(s: str):
    """Split a date-like string into parts or None. Keys: form, y, m, d, a, b, sep, time."""
    t = s.strip()
    if len(t) < 6 and not _YM_RE.match(t):
        return None
    m = _ISO_RE.match(t)
    if m:
        return {"form": "iso", "y": int(m.group("y")), "m": int(m.group("m")), "d": int(m.group("d")), "time": _time(m)}
    m = _YM_RE.match(t)
    if m:
        return {"form": "ym", "y": int(m.group("y")), "m": int(m.group("m")), "d": 1, "time": None}
    m = _NUMDATE_RE.match(t)
    if m:
        return {"form": "num", "a": int(m.group("a")), "b": int(m.group("b")), "y": _year(m.group("y")),
                "sep": m.group("sep"), "time": _time(m)}
    for rx in (_DMONY_RE, _MONDY_RE):
        m = rx.match(t)
        if m:
            mon = MONTH_WORDS.get(m.group("mon").lower().rstrip("."))
            if mon is None:
                return None
            return {"form": "text", "y": _year(m.group("y")), "m": mon, "d": int(m.group("d")), "time": _time(m)}
    return None


def resolve_date(p, order):
    """Turn date parts into a date/datetime. order: dmy, mdy, mixed, ambiguous, None. Returns None if not resolvable."""
    if p["form"] == "num":
        a, b = p["a"], p["b"]
        if order == "dmy" or (order in ("mixed", "ambiguous", None) and a > 12 and b <= 12):
            d, m = a, b
        elif order == "mdy" or (order in ("mixed", "ambiguous", None) and b > 12 and a <= 12):
            m, d = a, b
        else:
            return None
        y = p["y"]
    else:
        y, m, d = p["y"], p["m"], p["d"]
    try:
        day = dt.date(y, m, d)
        if p["time"] is not None:
            h, mi, se = p["time"]
            return dt.datetime(y, m, d, h, mi, se)
        return day
    except ValueError:
        return None


def date_order_from_parts(parts_list):
    """Column-level DD/MM vs MM/DD evidence. Returns (order, reason). order None when no numeric dates."""
    dmy = mdy = weak_dmy = 0
    nnum = 0
    for p in parts_list:
        if p["form"] != "num":
            continue
        nnum += 1
        if p["a"] > 12 and p["b"] <= 12:
            dmy += 1
        elif p["b"] > 12 and p["a"] <= 12:
            mdy += 1
        elif p["a"] <= 12 and p["b"] <= 12 and p["sep"] == ".":
            weak_dmy += 1
    if not nnum:
        return None, ""
    if dmy and mdy:
        return "mixed", "some values have the day first and some the month first"
    if dmy:
        return "dmy", "a value has a day above 12 in the first position"
    if mdy:
        return "mdy", "a value has a day above 12 in the second position"
    if weak_dmy:
        return "dmy", "dotted dates (dd.mm.yyyy) are read day-first"
    return "ambiguous", "every date has both parts 12 or below, so day-first and month-first both fit"


# ---------------------------------------------------------------- inference
def _prime_step(step: int) -> int:
    """Smallest prime >= step, so an even sample does not line up with periodic data (weekly, monthly)."""
    step = max(2, step)
    while any(step % d == 0 for d in range(2, int(step ** 0.5) + 1)):
        step += 1
    return step


def infer_column(name: str, values, date_order=None):
    """Infer one column. values: list of str|None. Returns an info dict (see keys below)."""
    total = len(values)
    sampled = total > SAMPLE_OVER
    src = values[::_prime_step(total // SAMPLE_SIZE)] if sampled else values
    cnt = Counter()
    nulls = 0
    null_raw = set()
    for v in src:
        if is_null(v):
            nulls += 1
            null_raw.add(v)
        else:
            cnt[v.strip()] += 1
    n = sum(cnt.values())
    unique = len(cnt)
    if sampled:                       # exact null count, estimated everything else (marked in info["sampled"])
        nulls = sum(values.count(x) for x in (null_raw | NULL_VARIANTS) if x is not None) + values.count(None)
        n_all = total - nulls
        unique = n_all if unique >= 0.98 * n else unique
    info = {"name": name, "type": "empty", "nonnull": n_all if sampled else n, "nulls": nulls, "unique": unique,
            "sampled": sampled,
            "decimal": ".", "sep_ambiguous": False, "date_order": None, "date_reason": "", "bad": [], "bad_count": 0}
    if n == 0:
        return info
    idish = bool(ID_NAME.search(name.strip()))
    uniq = unique
    denom = n_all if sampled else n

    if all(k.lower() in BOOL_TOKENS for k in cnt) and uniq <= 2:
        info["type"] = "bool"
        return info

    digits_only = all(k.isdigit() for k in cnt)
    if digits_only and any(len(k) > 1 and k.startswith("0") for k in cnt):
        info["type"] = "id"
        return info

    # numeric attempt
    shapes = {k: number_shape(k) for k in cnt}
    good = {k: s for k, s in shapes.items() if s is not None}
    if good:
        decimal, amb = detect_decimal([s[2] for s in good.values()])
        parsed = {k: body_to_float(s[2], decimal) for k, s in good.items()}
        ok_w = sum(cnt[k] for k, r in parsed.items() if r is not None)
        if ok_w >= 0.9 * n:
            bad = [k for k in cnt if shapes[k] is None or parsed.get(k) is None]
            hints = Counter()
            frac = False
            for k, r in parsed.items():
                if r is not None:
                    hints[good[k][1]] += cnt[k]
                    frac = frac or r[1]
            if hints["percent"] >= 0.5 * ok_w:
                t = "percent"
            elif hints["money"] >= 0.5 * ok_w:
                t = "money"
            else:
                t = "float" if frac else "int"
            info.update(type=t, decimal=decimal, sep_ambiguous=amb and any("," in k or "." in k for k in cnt),
                        bad=bad[:5], bad_count=sum(cnt[k] for k in bad))
            if t == "int" and uniq >= 0.9 * denom and (idish or _is_sequence(cnt, n)):
                info["type"] = "id"
            elif t == "float" and idish and uniq >= 0.9 * denom:
                info["type"] = "id"
            return info

    # date attempt
    parts = {k: date_parts(k) for k in cnt}
    pgood = {k: p for k, p in parts.items() if p is not None}
    if sum(cnt[k] for k in pgood) >= 0.9 * n:
        order, reason = date_order_from_parts(list(pgood.values()))
        if date_order in ("dmy", "mdy"):
            order, reason = date_order, "set by the user"
        res = {k: resolve_date(p, order) for k, p in pgood.items()}
        ok_w = sum(cnt[k] for k, r in res.items() if r is not None)
        amb_w = sum(cnt[k] for k, p in pgood.items() if res[k] is None and p["form"] == "num" and order in ("ambiguous", "mixed"))
        if ok_w + amb_w >= 0.9 * n:
            has_time = any(p["time"] is not None for p in pgood.values())
            bad = [k for k in cnt if k not in pgood or (res[k] is None and not (pgood[k]["form"] == "num" and order in ("ambiguous", "mixed")))]
            info.update(type="datetime" if has_time else "date", date_order=order, date_reason=reason,
                        bad=bad[:5], bad_count=sum(cnt[k] for k in bad))
            return info

    # text-like
    avg_len = sum(len(k) * c for k, c in cnt.items()) / n
    spaced = sum(c for k, c in cnt.items() if " " in k) / n
    if (idish and uniq >= 0.9 * denom and n >= 3) or (uniq >= 0.95 * denom and n >= 20 and avg_len <= 40 and spaced <= 0.1):
        info["type"] = "id"
    elif uniq <= 50 and uniq <= max(2, denom / 2) and avg_len <= 60:
        info["type"] = "category"
    else:
        info["type"] = "text"
    return info


def _is_sequence(cnt, n) -> bool:
    if n < 20:
        return False
    try:
        vals = sorted(int(k) for k in cnt)
    except ValueError:
        return False
    return vals[-1] - vals[0] == len(vals) - 1


def make_converter(info):
    """Return f(raw) -> typed value (None for null or unparseable) for a column described by info."""
    t = info["type"]
    cache = {}
    if t in NUMERIC_TYPES:
        dec = info["decimal"]

        def conv(raw):
            if is_null(raw):
                return None
            p = parse_number(raw, dec)
            if p is None:
                return None
            v = p[0]
            return int(v) if t == "int" and v == int(v) else v
    elif t in ("date", "datetime"):
        order = info.get("date_order")

        def conv(raw):
            if is_null(raw):
                return None
            p = date_parts(raw)
            if p is None:
                return None
            v = resolve_date(p, order)
            if v is not None and t == "date" and isinstance(v, dt.datetime):
                v = v.date()
            if v is not None and t == "datetime" and not isinstance(v, dt.datetime):
                v = dt.datetime(v.year, v.month, v.day)
            return v
    elif t == "bool":
        def conv(raw):
            return None if is_null(raw) else BOOL_TOKENS.get(raw.strip().lower())
    else:
        def conv(raw):
            return None if is_null(raw) else raw.strip()

    def cached(raw):
        if raw is None:
            return None
        try:
            return cache[raw]
        except KeyError:
            v = cache[raw] = conv(raw)
            return v
    return cached
