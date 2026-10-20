#!/usr/bin/env python3
"""Profile a CSV/TSV/JSON/XLSX file: what is in it, and what could make an answer wrong. Standard library only.

  python3 scripts/profile.py sales.xlsx
  python3 scripts/profile.py sales.csv --json
  python3 scripts/profile.py sales.csv --write-clean sales_clean.csv --log sales_clean.log.txt

The output starts with a TRUST REPORT: at most 5 ranked issues that could change an answer, each with what
to do. Then shape, per-column statistics and the other findings (duplicates, outliers, impossible values,
near-duplicate categories, mixed types, date gaps, ambiguous dates, totals rows, id columns).
--write-clean applies only conservative fixes (trim, one null style, plain numbers, ISO dates when the
day/month order is unambiguous, merge case/space variants) and logs every change. It never deletes a data row.
Exit codes: 0 fine or minor notes, 1 serious issues found, 2 file or usage problem.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
import sys
from collections import Counter, defaultdict

import _parse as P
from _parse import norm_key
import dataio

STRICT_NONNEG = re.compile(r"\b(units?|qty|quantity|count|counts|age|sessions?|orders?|clicks?|impressions?|visits?|users?|views?|"
                           r"duration|stock|inventory|weight|height|headcount|seats?|installs?|signups?|leads?)\b", re.I)
SOFT_NONNEG = re.compile(r"\b(price|amount|revenue|sales|cost|total|value|spend|fee|salary|income|mrr|arr)\b", re.I)
FUTURE_OK = re.compile(r"(due|expir|renew|deliver|end|deadline|target|plan|forecast|budget|next|valid|until|eta|scheduled)", re.I)


def quantile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def numeric_summary(vals):
    v = [x for x in vals if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if not v:
        return {}
    s = sorted(v)
    n = len(s)
    mean = math.fsum(s) / n
    sd = math.sqrt(math.fsum((x - mean) ** 2 for x in s) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "min": s[0], "max": s[-1], "mean": mean, "median": quantile(s, 0.5), "std": sd,
            "q1": quantile(s, 0.25), "q3": quantile(s, 0.75), "sum": math.fsum(s)}


def fmt(x):
    if x is None:
        return ""
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat(sep=" ") if isinstance(x, dt.datetime) else x.isoformat()
    if isinstance(x, float):
        if x == int(x) and abs(x) < 1e12:
            return "{:,}".format(int(x))
        return "{:,.4g}".format(x) if abs(x) < 1e-3 or abs(x) >= 1e7 else "{:,.2f}".format(x)
    if isinstance(x, int):
        return "{:,}".format(x)
    return str(x)


def duplicate_rows(t):
    """Indexes of rows that repeat an earlier row exactly. Two light passes: a bitmap of hashes, then exact checks
    only for the few rows whose hash bit was hit twice (keeps memory flat on millions of rows)."""
    rows = t.rows
    bits = bytearray(1 << 24)                       # 2^27 bit positions
    twice = set()
    for r in rows:
        h = hash(tuple(r)) & 0x7FFFFFF
        byte, mask = h >> 3, 1 << (h & 7)
        if bits[byte] & mask:
            twice.add(h)
        else:
            bits[byte] |= mask
    if not twice:
        return []
    seen, dupes = {}, []
    for k, r in enumerate(rows):
        if (hash(tuple(r)) & 0x7FFFFFF) in twice:
            key = tuple(r)
            if key in seen:
                dupes.append(k)
            else:
                seen[key] = k
    return dupes


def date_gaps(dates):
    ds = sorted(set(d.date() if isinstance(d, dt.datetime) else d for d in dates if d is not None))
    if len(ds) < 8:
        return None
    diffs = [(b - a).days for a, b in zip(ds, ds[1:])]
    med = sorted(diffs)[len(diffs) // 2]
    if med <= 1:
        grain, thr = "daily", 7
    elif 6 <= med <= 8:
        grain, thr = "weekly", 15
    elif 27 <= med <= 32:
        grain, thr = "monthly", 62
    else:
        return None
    gaps = [(a, b, (b - a).days) for a, b in zip(ds, ds[1:]) if (b - a).days >= thr]
    return {"grain": grain, "count": len(gaps), "biggest": sorted(gaps, key=lambda g: -g[2])[:3]}


def profile(t, today=None):
    today = today or dt.date.today()
    n = t.n_rows
    cols, issues = [], []
    F = {"duplicates": {}, "outliers": [], "impossible": [], "near_duplicates": [], "date_gaps": [], "mixed": [],
         "ids": [], "constant": [], "empty": []}

    def issue(score, kind, col, what, action):
        issues.append({"score": score, "kind": kind, "column": col, "what": what, "action": action})

    m = t.meta
    if m.get("totals"):
        rows = ", ".join(str(x["source_row"]) for x in m["totals"][:4])
        issue(60, "totals", None, "%d total/subtotal row(s) at source row %s were %s." % (
            len(m["totals"]), rows, "KEPT and will double count" if "KEPT" in " ".join(m["warnings"]) else "left out of the rows"),
            "Confirm they are subtotals; never sum them together with the detail rows.")
    if n < 30:
        issue(58, "small", None, "Only %d rows: any percentage or difference here is very noisy." % n,
              "Report counts, avoid strong claims, ask for more data.")

    dupes = duplicate_rows(t)
    if dupes:
        share = len(dupes) / max(n, 1)
        F["duplicates"]["exact_rows"] = {"count": len(dupes), "share": share, "source_rows": [t.src_row(k) for k in dupes[:5]]}
        issue(min(88, 62 + share * 400), "duplicates", None,
              "%d exact duplicate row(s) (%.1f%% of rows), e.g. source row %d." % (len(dupes), share * 100, t.src_row(dupes[0])),
              "Ask whether repeats are real (two identical sales) or an export error; if error, drop them before summing.")

    for j, (h, info) in enumerate(zip(t.header, t.infos)):
        typ = info["type"]
        col = {"name": h, "type": typ, "nonnull": info["nonnull"], "nulls": info["nulls"],
               "null_pct": round(100.0 * info["nulls"] / n, 1) if n else 0.0, "unique": info["unique"],
               "sampled": info.get("sampled", False)}
        vals = t.values(j) if typ in P.NUMERIC_TYPES or typ in ("date", "datetime") else []
        raw = t.raw(j)
        if typ == "empty":
            F["empty"].append(h)
            col["note"] = "all empty"
        elif typ in P.NUMERIC_TYPES:
            s = numeric_summary(vals)
            col.update({k: s[k] for k in ("min", "max", "mean", "median", "std", "sum") if k in s})
            # outliers (IQR)
            if s and s["n"] >= 8:
                iqr = s["q3"] - s["q1"]
                lo, hi = s["q1"] - 1.5 * iqr, s["q3"] + 1.5 * iqr
                far_lo, far_hi = s["q1"] - 3 * iqr, s["q3"] + 3 * iqr
                out = [(k, v) for k, v in enumerate(vals) if v is not None and (v < lo or v > hi)]
                far = [(k, v) for k, v in out if v < far_lo or v > far_hi]
                if iqr > 0 and far:
                    inside = [v for v in vals if v is not None and lo <= v <= hi]
                    mean_in = math.fsum(inside) / len(inside) if inside else 0
                    shift = abs(s["mean"] - mean_in) / abs(mean_in) if mean_in else 0
                    F["outliers"].append({"column": h, "count": len(out), "extreme": len(far), "fences": [lo, hi],
                                          "examples": [{"source_row": t.src_row(k), "value": v} for k, v in far[:5]],
                                          "mean_shift_pct": round(shift * 100, 1)})
                    if len(far) <= max(5, 0.005 * n) and (shift > 0.05 or (s["median"] and s["max"] > 10 * abs(s["median"]))):
                        issue(70 if shift > 0.1 else 50, "outlier", h,
                              "%d extreme value(s), e.g. %s at source row %d; they move the mean by %.0f%%." % (
                                  len(far), fmt(far[0][1]), t.src_row(far[0][0]), shift * 100),
                              "Check they are real; report medians, or show results with and without them.")
            # impossible values
            neg = [(k, v) for k, v in enumerate(vals) if v is not None and v < 0]
            if neg and typ != "percent":
                strict = bool(STRICT_NONNEG.search(h)) and not SOFT_NONNEG.search(h)
                soft = bool(SOFT_NONNEG.search(h))
                if strict or soft:
                    F["impossible"].append({"column": h, "kind": "negative", "count": len(neg), "strict": strict,
                                            "examples": [{"source_row": t.src_row(k), "value": v} for k, v in neg[:5]]})
                    if strict:
                        issue(76, "impossible", h, "%d negative value(s) in a count-like column, e.g. %s at source row %d." % (
                            len(neg), fmt(neg[0][1]), t.src_row(neg[0][0])), "Ask what a negative means here (returns? errors?) and exclude or fix.")
                    else:
                        issue(42, "negative", h, "%d negative value(s), e.g. %s at source row %d (refunds or credits?)." % (
                            len(neg), fmt(neg[0][1]), t.src_row(neg[0][0])), "Decide whether refunds belong in the total; say which in the answer.")
            if re.search(r"\bage\b", h, re.I) and s and s["max"] > 120:
                F["impossible"].append({"column": h, "kind": "too_large", "count": sum(1 for v in vals if v and v > 120)})
                issue(72, "impossible", h, "ages above 120 (max %s)." % fmt(s["max"]), "Treat as typos; exclude or fix.")
            if typ == "percent" and s and (s["max"] > 100 or s["min"] < 0) and not SOFT_NONNEG.search(h):
                F["impossible"].append({"column": h, "kind": "percent_range", "count": 1})
                issue(55, "impossible", h, "percent values outside 0-100 (min %s, max %s)." % (fmt(s["min"]), fmt(s["max"])),
                      "Check whether the column mixes fractions (0.12) with percents (12).")
        elif typ in ("date", "datetime"):
            ds = [v for v in vals if v is not None]
            if ds:
                dd = [d.date() if isinstance(d, dt.datetime) else d for d in ds]
                col.update({"min": min(dd).isoformat(), "max": max(dd).isoformat(), "span_days": (max(dd) - min(dd)).days})
                fut = [(k, v) for k, v in enumerate(vals) if v is not None and (v.date() if isinstance(v, dt.datetime) else v) > today]
                if fut and not FUTURE_OK.search(h):
                    F["impossible"].append({"column": h, "kind": "future_date", "count": len(fut),
                                            "examples": [{"source_row": t.src_row(k), "value": fmt(v)} for k, v in fut[:5]]})
                    issue(74, "impossible", h, "%d date(s) in the future (after %s), e.g. %s at source row %d." % (
                        len(fut), today.isoformat(), fmt(fut[0][1]), t.src_row(fut[0][0])),
                        "Likely typos or a wrong day/month order; fix or exclude before trending.")
                old = [v for v in dd if v.year < 1990]
                if old:
                    F["impossible"].append({"column": h, "kind": "old_date", "count": len(old)})
                    issue(60, "impossible", h, "%d date(s) before 1990, e.g. %s." % (len(old), min(old).isoformat()),
                          "Probably empty-date placeholders (1900/1970); treat as missing.")
                g = date_gaps(ds)
                if g and g["count"]:
                    F["date_gaps"].append(dict(column=h, grain=g["grain"], count=g["count"],
                                               biggest=[[a.isoformat(), b.isoformat(), d] for a, b, d in g["biggest"]]))
                    a, b, d = g["biggest"][0]
                    issue(52, "gaps", h, "%s data has %d gap(s); the largest is %d days (%s to %s)." % (
                        g["grain"], g["count"], d, a.isoformat(), b.isoformat()),
                        "Missing periods look like a drop. Ask if they are real zeros or missing data.")
            if info["date_order"] in ("ambiguous", "mixed"):
                example = next((r.strip() for r in raw if r.strip()), "")
                issue(95, "ambiguous_dates", h, "dates like %s can be day-first or month-first (%s); they are NOT parsed." % (
                    example, info["date_reason"]), "Ask the owner which order the file uses, then re-run with --date-order dmy or mdy.")
        elif typ in ("category", "text", "id", "bool"):
            if info["unique"] > 100000 and typ in ("id", "text"):
                cnt = Counter()                                  # nearly all values are different: nothing useful to count
            else:
                cnt = Counter(v for v in raw if not P.is_null(v))
            top = cnt.most_common(5)
            col["top"] = [[k.strip(), c] for k, c in top]
            if typ == "category" or (typ == "text" and info["unique"] <= 500):
                groups = defaultdict(list)
                for k, c in cnt.items():
                    groups[norm_key(k)].append((k, c))
                var = [sorted(g, key=lambda x: -x[1]) for g in groups.values() if len(g) > 1]
                if var:
                    var.sort(key=lambda g: -sum(c for _, c in g))
                    F["near_duplicates"].append({"column": h, "kind": "case_space", "groups": [[[k, c] for k, c in g] for g in var[:6]]})
                    ex = var[0]
                    issue(72, "near_duplicates", h, "%d value group(s) differ only by case/spacing, e.g. %s. Segment totals are split." % (
                        len(var), ", ".join("%r x%d" % (k, c) for k, c in ex[:4])),
                        "Merge them (--write-clean does) before grouping or ranking.")
                elif typ == "category" and len(groups) <= 200:
                    import difflib
                    keys = sorted(groups, key=lambda k: -sum(c for _, c in groups[k]))
                    typos = []
                    for i, k in enumerate(keys):
                        if i == 0 or len(k) < 4:
                            continue
                        cm = difflib.get_close_matches(k, keys[:i], n=1, cutoff=0.88)
                        if cm:
                            typos.append((groups[k][0][0], groups[cm[0]][0][0]))
                    if typos:
                        F["near_duplicates"].append({"column": h, "kind": "typo", "pairs": typos[:6]})
                        issue(48, "typos", h, "possible typos: %s." % ", ".join("%r ~ %r" % p for p in typos[:3]),
                              "Confirm with the owner; merge if they are the same thing.")
            if typ == "id":
                r = {"column": h, "unique": info["unique"], "rows": n}
                d = info["nonnull"] - info["unique"]
                if 0 < d and d > len(dupes) and info["unique"] >= 0.5 * max(info["nonnull"], 1) and not info.get("sampled"):
                    r["repeated"] = d
                    issue(80, "duplicate_ids", h, "%d repeated value(s) in id column '%s' (%d unique of %d)." % (d, h, info["unique"], info["nonnull"]),
                          "Find out if an id may repeat (many lines per order) or if rows are double counted.")
                F["ids"].append(r)
            if info["unique"] == 1 and n > 1:
                F["constant"].append(h)
        if info["bad_count"]:
            F["mixed"].append({"column": h, "type": typ, "bad": info["bad_count"], "examples": info["bad"][:3]})
            share = info["bad_count"] / max(info["nonnull"], 1)
            issue(min(85, 60 + share * 400), "mixed_types", h, "'%s' reads as %s but %d cell(s) are something else (e.g. %s); they are ignored." % (
                h, typ, info["bad_count"], ", ".join(repr(b[:20]) for b in info["bad"][:3])), "Fix those cells at the source or say they were excluded.")
        if info["sep_ambiguous"]:
            issue(70, "separator", h, "numbers in '%s' could use comma as thousands or as decimal mark; read as thousands." % h,
                  "Confirm the number format with the owner.")
        if typ != "empty" and col["null_pct"] >= 5 and typ in P.NUMERIC_TYPES + ("date", "datetime"):
            issue(min(80, 40 + col["null_pct"] * 0.7), "nulls", h, "%.0f%% of '%s' is empty (%d rows); averages and sums skip them." % (
                col["null_pct"], h, info["nulls"]), "Say how empty cells were treated (skipped, zero?) and whether they cluster in one segment.")
        cols.append(col)

    issues.sort(key=lambda x: -x["score"])
    top = issues[:5]
    for i, x in enumerate(top, 1):
        x["rank"] = i
    serious = [x for x in issues if x["score"] >= 70]
    return {"file": m["source"], "format": m["format"], "sheet": m.get("sheet"),
            "shape": {"rows": n, "columns": t.n_cols, "source_rows": m.get("source_rows")},
            "trust": {"verdict": ("%d serious issue(s): fix or state them before trusting any answer" % len(serious)) if serious
                      else ("minor issues only" if top else "no issues found that would change an answer"),
                      "issues": top, "more_issues": max(0, len(issues) - 5)},
            "columns": cols, "findings": F, "warnings": m["warnings"], "notes": m.get("notes", [])}


def render_text(p):
    out = []
    a = out.append
    a("DATA PROFILE: %s (%s%s)  %d rows x %d columns" % (
        p["file"], p["format"], ", sheet " + p["sheet"] if p["sheet"] else "", p["shape"]["rows"], p["shape"]["columns"]))
    a("")
    a("TRUST REPORT: " + p["trust"]["verdict"])
    for x in p["trust"]["issues"]:
        a("  %d. %s%s" % (x["rank"], "[%s] " % x["column"] if x["column"] else "", x["what"]))
        a("     Do: " + x["action"])
    if p["trust"]["more_issues"]:
        a("  (+%d lower-ranked issue(s) in --json)" % p["trust"]["more_issues"])
    a("")
    a("COLUMNS")
    a("  %-24s %-9s %6s %8s  %s" % ("name", "type", "empty", "unique", "summary"))
    for c in p["columns"]:
        if c["type"] in P.NUMERIC_TYPES and "min" in c:
            s = "min %s  median %s  mean %s  max %s" % (fmt(c["min"]), fmt(c["median"]), fmt(c["mean"]), fmt(c["max"]))
        elif c["type"] in ("date", "datetime") and "min" in c:
            s = "%s to %s (%d days)" % (c["min"], c["max"], c["span_days"])
        elif "top" in c:
            s = ", ".join("%s (%d)" % (k[:18], v) for k, v in c["top"][:4])
        else:
            s = c.get("note", "")
        a("  %-24s %-9s %5.0f%% %7s%s  %s" % (c["name"][:24], c["type"], c["null_pct"], c["unique"], "+" if c["sampled"] else " ", s))
    F = p["findings"]
    a("")
    a("OTHER FINDINGS")
    shown = False
    for o in F["outliers"]:
        shown = True
        a("  outliers in %s: %d beyond 1.5xIQR, %d extreme (fences %s to %s); e.g. %s" % (
            o["column"], o["count"], o["extreme"], fmt(o["fences"][0]), fmt(o["fences"][1]),
            ", ".join("%s (row %d)" % (fmt(e["value"]), e["source_row"]) for e in o["examples"][:3])))
    for g in F["near_duplicates"]:
        shown = True
        if g["kind"] == "case_space":
            a("  same value written differently in %s: %s" % (g["column"], "; ".join(
                "/".join("%r x%d" % (k, c) for k, c in grp[:4]) for grp in g["groups"][:3])))
        else:
            a("  possible typos in %s: %s" % (g["column"], ", ".join("%r~%r" % tuple(x) for x in g["pairs"][:4])))
    for g in F["date_gaps"]:
        shown = True
        a("  gaps in %s (%s): %d, biggest %s to %s" % (g["column"], g["grain"], g["count"], g["biggest"][0][0], g["biggest"][0][1]))
    if F["ids"]:
        shown = True
        a("  id columns (do not sum or average): " + ", ".join(i["column"] for i in F["ids"]))
    if F["constant"]:
        shown = True
        a("  constant columns (one value only): " + ", ".join(F["constant"]))
    if F["empty"]:
        shown = True
        a("  completely empty columns: " + ", ".join(F["empty"]))
    if not shown:
        a("  nothing else notable")
    for w in p["warnings"]:
        a("  note: " + w)
    return "\n".join(out)


# ------------------------------------------------------------------ cleaning
def clean(t, out_path, log_path):
    """Conservative clean-up of table t. Total rows the reader set aside are put back in place. Never removes data rows."""
    log = []
    counts = Counter()
    examples = defaultdict(list)

    def note(kind, col, before, after):
        counts[(kind, col)] += 1
        if len(examples[(kind, col)]) < 3:
            examples[(kind, col)].append((before, after))

    header = list(t.header)
    new_rows = [list(r) for r in t.rows]
    for j, (h, info) in enumerate(zip(header, t.infos)):
        typ = info["type"]
        variants = {}
        if typ in ("category", "text"):
            cnt = Counter(v.strip() for v in (r[j] for r in t.rows) if not P.is_null(v))
            groups = defaultdict(list)
            for k, c in cnt.items():
                groups[norm_key(k)].append((c, k))
            for g in groups.values():
                if len(g) > 1:
                    best = sorted(g, key=lambda x: (-x[0], x[1]))[0][1]
                    for _, k in g:
                        if k != best:
                            variants[k] = best
        conv = P.make_converter(info)
        for r in new_rows:
            v = r[j]
            if P.is_null(v):
                if v != "":
                    note("null token to empty", h, v, "")
                r[j] = ""
                continue
            s = v.strip()
            if s != v:
                note("trimmed whitespace", h, v, s)
            new = s
            if typ in P.NUMERIC_TYPES:
                x = conv(s)
                if x is not None:
                    if typ == "money":
                        txt = "%.2f" % x
                    else:
                        txt = str(int(x)) if x == int(x) and abs(x) < 1e15 else repr(float(x))
                    new = txt + ("%" if typ == "percent" else "")
                    if new != s:
                        note("number normalised (" + typ + ")", h, s, new)
            elif typ in ("date", "datetime") and info["date_order"] not in ("ambiguous", "mixed"):
                x = conv(s)
                if x is not None:
                    new = x.isoformat(sep=" ") if isinstance(x, dt.datetime) else x.isoformat()
                    if new != s:
                        note("date to ISO", h, s, new)
            elif s in variants:
                new = variants[s]
                note("merged spelling variant", h, s, new)
            r[j] = new
    m = t.meta
    kept_totals = [x for x in m.get("totals", []) if "position" in x] if not t.meta.get("totals_kept") else []
    for x in sorted(kept_totals, key=lambda x: x["position"]):      # put total rows back where they were
        new_rows.insert(min(x["position"], len(new_rows)), list(x["cells"]))
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(new_rows)
    log.append("CLEANING LOG for %s -> %s" % (m["source"], out_path))
    log.append("rows in: %d  rows out: %d (no data row is ever deleted)" % (t.n_rows + len(kept_totals), len(new_rows)))
    for w_ in m["warnings"]:
        log.append("read note: " + w_)
    if kept_totals:
        log.append("TOTAL rows kept in place, remove before summing: source rows " + ", ".join(str(x["source_row"]) for x in kept_totals))
    for i in t.infos:
        if i["type"] in ("date", "datetime") and i["date_order"] in ("ambiguous", "mixed"):
            log.append("NOT converted: dates in '%s' are ambiguous (day/month order unknown); left as written." % i["name"])
    if not counts:
        log.append("no cell needed changing")
    for (kind, col), c in sorted(counts.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        ex = "; ".join("%r -> %r" % e for e in examples[(kind, col)])
        log.append("%-34s %-20s %6d cell(s)   e.g. %s" % (kind, col, c, ex))
    with open(log_path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(log) + "\n")
    return sum(counts.values())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="CSV, TSV, JSON, NDJSON or XLSX path, or - for stdin")
    ap.add_argument("--sheet", help="sheet name or number (XLSX)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--date-order", choices=["dmy", "mdy"], help="resolve ambiguous 03/04/2026 style dates")
    ap.add_argument("--keep-totals", action="store_true", help="keep rows recognised as totals in the analysis")
    ap.add_argument("--max-rows", type=int, help="read only the first N data rows")
    ap.add_argument("--today", help="treat this YYYY-MM-DD as today when looking for future dates")
    ap.add_argument("--write-clean", metavar="OUT.csv", help="write a conservatively cleaned copy")
    ap.add_argument("--log", metavar="LOG.txt", help="where to write the cleaning log (default: OUT.csv.log.txt)")
    a = ap.parse_args(argv)
    try:
        today = dt.date.fromisoformat(a.today) if a.today else None
    except ValueError:
        print("error: --today must look like 2026-09-29", file=sys.stderr)
        return 2
    try:
        t = dataio.read_table(a.file, a.sheet, a.date_order, a.keep_totals, a.max_rows)
    except dataio.DataError as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 2
    if a.write_clean:
        log_path = a.log or a.write_clean + ".log.txt"
        try:
            changed = clean(t, a.write_clean, log_path)
        except OSError as ex:
            print("error: cannot write output (%s)" % ex.strerror, file=sys.stderr)
            return 2
        print("wrote %s and %s (%d cell(s) changed, 0 rows deleted)" % (a.write_clean, log_path, changed), file=sys.stderr)
    p = profile(t, today)
    print(json.dumps(p, indent=2, ensure_ascii=False, default=str) if a.json else render_text(p))
    return 1 if any(x["score"] >= 70 for x in p["trust"]["issues"]) else 0


if __name__ == "__main__":
    sys.exit(main())
