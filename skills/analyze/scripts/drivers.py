#!/usr/bin/env python3
"""What drove the change between two periods? Exact breakdown by segment. Standard library only.

  python3 scripts/drivers.py orders.csv --metric revenue --date order_date --a 2026-07 --b 2026-08
  python3 scripts/drivers.py funnel.csv --ratio orders,sessions --date date --auto --dims channel,device
  python3 scripts/drivers.py orders.csv --metric count --date order_date --a 2026-Q1 --b 2026-Q2 --json

Additive metric (revenue, orders, count): per segment delta and its share of the total change.
Ratio metric (--ratio NUM,DEN: conversion, average order value, margin): exact rate/mix split. For each segment
rate = avg weight x change in its rate, mix = change in its weight x (its avg rate minus the overall avg rate);
across segments these add up EXACTLY to the total change. DEN or NUM may be the word count (rows).
Also: dimensions ranked by how concentrated the change is, new/lost segments, a drill into the top segment,
small-sample warnings and a HISTORY CHECK (is this change bigger than normal period-to-period movement?).
Periods: 2026-08, 2026-Q3, 2026-H1, 2026, 2026-W32, 2026-08-05, 2026-08-01..2026-08-31, or --auto
(latest complete month, or week for short files, versus the one before).
Exit codes: 0 ok, 2 usage or data problem (one line message).
"""
from __future__ import annotations

import argparse
import bisect
import calendar
import datetime as dt
import json
import math
import re
import sys
from collections import defaultdict

import _parse as P
import dataio

SMALL_N = 30


class UsageError(Exception):
    pass


# ------------------------------------------------------------------ periods
def _add_months(d, n):
    m = d.year * 12 + d.month - 1 + n
    return dt.date(m // 12, m % 12 + 1, 1)


def _month_end(d):
    return dt.date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


class Period:
    def __init__(self, kind, start, end, label):
        self.kind, self.start, self.end, self.label = kind, start, end, label

    def key(self):
        return (self.start, self.end)

    def shift(self, k):
        """The same kind of period, k periods earlier (k may be negative for later)."""
        if self.kind in ("month", "quarter", "half"):
            step = {"month": 1, "quarter": 3, "half": 6}[self.kind] * k
            s = _add_months(self.start, -step)
            n = {"month": 1, "quarter": 3, "half": 6}[self.kind]
            e = _month_end(_add_months(s, n - 1))
        elif self.kind == "year":
            s, e = dt.date(self.start.year - k, 1, 1), dt.date(self.start.year - k, 12, 31)
        else:
            length = (self.end - self.start).days + 1
            s, e = self.start - dt.timedelta(days=k * length), self.end - dt.timedelta(days=k * length)
        return Period(self.kind, s, e, _label(self.kind, s, e))


def _label(kind, s, e):
    if kind == "month":
        return "%04d-%02d" % (s.year, s.month)
    if kind == "quarter":
        return "%04d-Q%d" % (s.year, (s.month - 1) // 3 + 1)
    if kind == "half":
        return "%04d-H%d" % (s.year, (s.month - 1) // 6 + 1)
    if kind == "year":
        return "%04d" % s.year
    if s == e:
        return s.isoformat()
    return "%s..%s" % (s.isoformat(), e.isoformat())


def parse_period(text):
    t = text.strip()
    try:
        m = re.match(r"^(\d{4})-(\d{1,2})$", t)
        if m:
            s = dt.date(int(m.group(1)), int(m.group(2)), 1)
            return Period("month", s, _month_end(s), _label("month", s, None))
        m = re.match(r"^(\d{4})-?Q([1-4])$", t, re.I) or re.match(r"^Q([1-4])[ -]?(\d{4})$", t, re.I)
        if m:
            y, q = (int(m.group(1)), int(m.group(2))) if len(m.group(1)) == 4 else (int(m.group(2)), int(m.group(1)))
            s = dt.date(y, 3 * q - 2, 1)
            return Period("quarter", s, _month_end(_add_months(s, 2)), _label("quarter", s, None))
        m = re.match(r"^(\d{4})-?H([12])$", t, re.I)
        if m:
            s = dt.date(int(m.group(1)), 6 * int(m.group(2)) - 5, 1)
            return Period("half", s, _month_end(_add_months(s, 5)), _label("half", s, None))
        if re.match(r"^\d{4}$", t):
            y = int(t)
            return Period("year", dt.date(y, 1, 1), dt.date(y, 12, 31), t)
        m = re.match(r"^(\d{4})-?W(\d{1,2})$", t, re.I)
        if m:
            s = dt.date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
            return Period("range", s, s + dt.timedelta(days=6), _label("range", s, s + dt.timedelta(days=6)))
        m = re.match(r"^(\d{4}-\d{2}-\d{2})$", t)
        if m:
            s = dt.date.fromisoformat(t)
            return Period("day", s, s, t)
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\s*\.\.\s*(\d{4}-\d{2}-\d{2})$", t)
        if m:
            s, e = dt.date.fromisoformat(m.group(1)), dt.date.fromisoformat(m.group(2))
            if e < s:
                raise UsageError("period '%s' ends before it starts" % text)
            return Period("range", s, e, _label("range", s, e))
    except ValueError:
        pass
    raise UsageError("cannot read period '%s'. Use 2026-08, 2026-Q3, 2026-H1, 2026, 2026-W32, 2026-08-05 or 2026-08-01..2026-08-31" % text)


def auto_periods(dmin, dmax):
    """Latest complete period versus the one before. Returns (A, B, note)."""
    if (dmax - dmin).days >= 45:
        cm = dt.date(dmax.year, dmax.month, 1)
        complete = dmax.day >= calendar.monthrange(dmax.year, dmax.month)[1] - 3
        b = parse_period("%04d-%02d" % (cm.year, cm.month))
        note = "latest month looks complete (data reaches %s)" % dmax.isoformat()
        if not complete:
            b = b.shift(1)
            note = "%s is incomplete (data stops at %s), so the last complete month is used" % (
                "%04d-%02d" % (dmax.year, dmax.month), dmax.isoformat())
        a = b.shift(1)
        if a.start < dmin - dt.timedelta(days=3):
            raise UsageError("--auto needs two complete months of data; the file covers %s to %s" % (dmin, dmax))
        return a, b, note
    end = dmax - dt.timedelta(days=(dmax.weekday() + 1) % 7)       # latest Sunday on or before dmax
    b = Period("range", end - dt.timedelta(days=6), end, "")
    b.label = _label("range", b.start, b.end)
    a = b.shift(1)
    if a.start < dmin:
        raise UsageError("--auto needs two complete weeks of data; the file covers %s to %s" % (dmin, dmax))
    return a, b, "short file: last complete week (Mon-Sun) versus the week before"


# ------------------------------------------------------------------ core maths
def decompose_ratio(seg):
    """seg: {label: {'A': (num, den, rows), 'B': (num, den, rows)}} -> list of per-segment dicts + totals.

    R = sum_v w_v r_v with w_v = den share.  For every segment (midpoint weights):
        rate_v = wbar_v * (rB_v - rA_v)          mix_v = (wB_v - wA_v) * (rbar_v - Rbar)
    and sum_v (rate_v + mix_v) == R_B - R_A exactly."""
    dA = sum(v["A"][1] for v in seg.values())
    dB = sum(v["B"][1] for v in seg.values())
    nA = sum(v["A"][0] for v in seg.values())
    nB = sum(v["B"][0] for v in seg.values())
    RA, RB = nA / dA, nB / dB
    Rbar = (RA + RB) / 2
    out = []
    for label, v in seg.items():
        wA, wB = v["A"][1] / dA, v["B"][1] / dB
        rA = v["A"][0] / v["A"][1] if v["A"][1] else None
        rB = v["B"][0] / v["B"][1] if v["B"][1] else None
        if rA is None and rB is None:
            continue
        if rA is None:
            rA = rB
        if rB is None:
            rB = rA
        wbar, rbar = (wA + wB) / 2, (rA + rB) / 2
        rate, mix = wbar * (rB - rA), (wB - wA) * (rbar - Rbar)
        out.append({"label": label, "rA": rA, "rB": rB, "wA": wA, "wB": wB, "rate": rate, "mix": mix,
                    "delta": rate + mix, "A": v["A"][0], "B": v["B"][0], "denA": v["A"][1], "denB": v["B"][1],
                    "rowsA": v["A"][2], "rowsB": v["B"][2]})
    return out, RA, RB


def analyze_dim(name, seg, ratio):
    """One dimension: per segment deltas, shares, concentration, new and lost segments."""
    if ratio:
        segs, RA, RB = decompose_ratio(seg)
        total = RB - RA
        for s in segs:
            s["status"] = "new" if s["denA"] == 0 else ("lost" if s["denB"] == 0 else "both")
    else:
        segs = []
        for label, v in seg.items():
            segs.append({"label": label, "A": v["A"][0], "B": v["B"][0], "delta": v["B"][0] - v["A"][0],
                         "rowsA": v["A"][2], "rowsB": v["B"][2],
                         "status": "new" if v["A"][2] == 0 else ("lost" if v["B"][2] == 0 else "both")})
        total = sum(s["delta"] for s in segs)
        RA = RB = None
    ref = max(abs(RA), abs(RB)) if ratio else max(abs(sum(v["A"][0] for v in seg.values())), abs(sum(v["B"][0] for v in seg.values())))
    negligible = abs(total) <= 0.001 * ref or abs(total) < 1e-12          # a near-zero total makes shares meaningless
    for s in segs:
        s["share"] = None if negligible else s["delta"] / total
    segs.sort(key=lambda s: -abs(s["delta"]))
    absum = sum(abs(s["delta"]) for s in segs)
    k = len(segs)
    if k > 1 and absum > 0:
        hhi = sum((abs(s["delta"]) / absum) ** 2 for s in segs)
        conc = (hhi - 1.0 / k) / (1 - 1.0 / k)
    else:
        conc = 0.0
    res = {"dimension": name, "segments_count": k, "concentration": round(conc, 4), "segments": segs,
           "new": [s["label"] for s in segs if s["status"] == "new"], "lost": [s["label"] for s in segs if s["status"] == "lost"],
           "top_share": segs[0]["share"] if segs else None}
    if ratio:
        res["rate_total"] = sum(s["rate"] for s in segs)
        res["mix_total"] = sum(s["mix"] for s in segs)
    return res


# ------------------------------------------------------------------ data passes
class Data:
    """Row-level view prepared once: dates, metric parts and canonical dimension labels."""

    def __init__(self, table, metric, ratio, date_col, dims_req, today=None):
        t = table
        self.t = t
        j = t.index(date_col)
        info = t.infos[j]
        if info["type"] not in ("date", "datetime"):
            raise UsageError("column '%s' is %s, not a date. Pick the date column with --date." % (t.header[j], info["type"]))
        if info["date_order"] in ("ambiguous", "mixed"):
            raise UsageError("dates in '%s' are ambiguous (day/month order unknown). Ask which is right, then pass --date-order dmy or mdy."
                             % t.header[j])
        self.date_name = t.header[j]
        self.dates = [d.date() if isinstance(d, dt.datetime) else d for d in t.values(j)]
        self.ratio = bool(ratio)
        used = {j}
        self.num = self._part(ratio[0] if ratio else metric, used)
        self.den = self._part(ratio[1], used) if ratio else None
        self.metric_name = ("%s/%s" % ratio) if ratio else (metric if metric != "count" else "count")
        # dimensions
        cand = []
        if dims_req:
            for d in dims_req:
                k = t.index(d)
                if t.infos[k]["type"] in ("date", "datetime", "float", "money", "percent"):
                    raise UsageError("'%s' is %s; a dimension must be a category-like column" % (t.header[k], t.infos[k]["type"]))
                cand.append(k)
        else:
            cand = [k for k, i in enumerate(t.infos) if i["type"] in ("category", "bool") and k not in used]
        self.dims = {}
        self.skipped_dims = []
        for k in cand:
            labels = self._canon(t.raw(k))
            u = len(set(labels))
            if not dims_req and not (2 <= u <= 50):
                continue
            if u < 2:
                self.skipped_dims.append("%s (only one value)" % t.header[k])
                continue
            self.dims[t.header[k]] = labels
        self.no_date = sum(1 for d in self.dates if d is None)

    def _part(self, name, used):
        n = self.t.n_rows
        if name == "count":
            return [1.0] * n
        k = self.t.index(name)
        typ = self.t.infos[k]["type"]
        if typ not in P.NUMERIC_TYPES:
            raise UsageError("column '%s' is %s, not a number, so it cannot be summed. Use --metric count to count rows." % (self.t.header[k], typ))
        used.add(k)
        return [None if v is None else float(v) for v in self.t.values(k)]

    @staticmethod
    def _canon(raw):
        cnt = defaultdict(lambda: defaultdict(int))
        for v in raw:
            s = v.strip()
            cnt[P.norm_key(s)][s] += 1
        best = {k: max(c.items(), key=lambda kv: (kv[1], kv[0]))[0] for k, c in cnt.items()}
        return [best[P.norm_key(v.strip())] if not P.is_null(v) else "(empty)" for v in raw]

    def collect(self, A, B, only=None):
        """One pass: totals per side and per-dimension segments. only = (dim name, label) row filter."""
        tot = {"A": [0.0, 0.0, 0], "B": [0.0, 0.0, 0]}
        segs = {d: defaultdict(lambda: {"A": [0.0, 0.0, 0], "B": [0.0, 0.0, 0]}) for d in self.dims}
        flt = self.dims[only[0]] if only else None
        skipped = 0
        dates, num, den, dimlists = self.dates, self.num, self.den, list(self.dims.items())
        aS, aE, bS, bE = A.start, A.end, B.start, B.end
        for i, d in enumerate(dates):
            if d is None:
                continue
            if aS <= d <= aE:
                side = "A"
            elif bS <= d <= bE:
                side = "B"
            else:
                continue
            if flt is not None and flt[i] != only[1]:
                continue
            n = num[i]
            dv = den[i] if den is not None else 1.0
            if n is None or dv is None:
                skipped += 1
                continue
            t_ = tot[side]
            t_[0] += n
            t_[1] += dv
            t_[2] += 1
            for name, lab in dimlists:
                s = segs[name][lab[i]][side]
                s[0] += n
                s[1] += dv
                s[2] += 1
        return tot, segs, skipped

    def period_values(self, periods):
        """Metric value per period key, for the history check."""
        plist = sorted(periods, key=lambda p: p.start)
        starts = [p.start for p in plist]
        acc = {p.key(): [0.0, 0.0, 0] for p in plist}
        for i, d in enumerate(self.dates):
            if d is None:
                continue
            n = self.num[i]
            dv = self.den[i] if self.den is not None else 1.0
            if n is None or dv is None:
                continue
            k = bisect.bisect_right(starts, d) - 1
            for q in range(k, max(-1, k - 4), -1):
                p = plist[q]
                if p.start <= d <= p.end:
                    a = acc[p.key()]
                    a[0] += n
                    a[1] += dv
                    a[2] += 1
        return acc


def value_of(acc, ratio):
    n, dn, rows = acc
    if rows == 0:
        return None
    if ratio:
        return n / dn if dn else None
    return n


def history_check(data, A, B, ratio, dmin):
    ps, pairs = {A.key(): A, B.key(): B}, []
    for k in range(1, 61):
        a, b = A.shift(k), B.shift(k)
        if a.start < dmin - dt.timedelta(days=3):
            break
        ps[a.key()], ps[b.key()] = a, b
        pairs.append((a, b))
    vals = data.period_values(list(ps.values()))
    cur_a, cur_b = value_of(vals[A.key()], ratio), value_of(vals[B.key()], ratio)
    if not cur_a:
        return {"status": "none", "text": "no history check: the baseline value is zero or empty"}
    cur = (cur_b - cur_a) / abs(cur_a) if cur_b is not None else None
    past = []
    for a, b in pairs:
        va, vb = value_of(vals[a.key()], ratio), value_of(vals[b.key()], ratio)
        if va and vb is not None:
            past.append((vb - va) / abs(va))
    if cur is None:
        return {"status": "none", "text": "no history check: the comparison period is empty"}
    if len(past) < 5:
        return {"status": "insufficient", "past_changes": len(past), "change_pct": cur * 100,
                "text": "HISTORY CHECK: only %d earlier comparable change(s) in the file (need 5+), so I cannot say whether %+.1f%% is unusual." % (
                    len(past), cur * 100)}
    mean = math.fsum(past) / len(past)
    sd = math.sqrt(math.fsum((x - mean) ** 2 for x in past) / (len(past) - 1))
    z = (cur - mean) / sd if sd > 0 else None
    bigger = sum(1 for x in past if abs(x) < abs(cur)) / len(past)
    if z is None:
        verdict = "history is perfectly flat, so any change is unusual"
    elif abs(z) < 1 and bigger < 0.8:
        verdict = "within normal wobble"
    elif abs(z) < 2:
        verdict = "larger than a typical period but not extreme"
    else:
        verdict = "unusual: outside normal movement"
    text = ("HISTORY CHECK: this change is %+.1f%%. Over %d earlier comparable periods the change averaged %+.1f%% (typical swing +/-%.1f%%). "
            "z = %s; bigger than %.0f%% of past moves. Verdict: %s. (Ignores seasonality: for seasonal data compare with the same period last year.)" % (
                cur * 100, len(past), mean * 100, sd * 100, ("%.1f" % z) if z is not None else "n/a", bigger * 100, verdict))
    return {"status": "ok", "verdict": verdict, "z": z, "change_pct": cur * 100, "past_changes": len(past),
            "past_mean_pct": mean * 100, "past_sd_pct": sd * 100, "share_of_past_moves_smaller": bigger, "text": text}


# ------------------------------------------------------------------ orchestration
def rank_dims(data, segs, ratio):
    res = [analyze_dim(name, dict(s), ratio) for name, s in segs.items() if len(s) > 1]
    res.sort(key=lambda r: (-r["concentration"], -abs(r["top_share"] or 0)))
    return res


def run(table, metric, ratio, date_col, A, B, dims=None, drill=1, today=None):
    data = Data(table, metric, ratio, date_col, dims, today)
    known = [d for d in data.dates if d is not None]
    if not known:
        raise UsageError("no usable dates in '%s'" % data.date_name)
    tot, segs, skipped = data.collect(A, B)
    if tot["A"][2] == 0 or tot["B"][2] == 0:
        raise UsageError("no rows in period %s (dates run %s to %s)" % (
            A.label if tot["A"][2] == 0 else B.label, min(known), max(known)))
    if ratio and (tot["A"][1] == 0 or tot["B"][1] == 0):
        raise UsageError("the denominator '%s' sums to zero in one of the periods" % ratio[1])
    va = tot["A"][0] / tot["A"][1] if ratio else tot["A"][0]
    vb = tot["B"][0] / tot["B"][1] if ratio else tot["B"][0]
    change = vb - va
    out = {"metric": data.metric_name, "kind": "ratio" if ratio else "additive", "date_column": data.date_name,
           "A": {"label": A.label, "start": A.start.isoformat(), "end": A.end.isoformat(), "value": va, "rows": tot["A"][2]},
           "B": {"label": B.label, "start": B.start.isoformat(), "end": B.end.isoformat(), "value": vb, "rows": tot["B"][2]},
           "change": change, "change_pct": (change / abs(va) * 100) if va else None, "warnings": [], "dimensions": []}
    w = out["warnings"]
    if ratio:
        out["A"]["num"], out["A"]["den"] = tot["A"][0], tot["A"][1]
        out["B"]["num"], out["B"]["den"] = tot["B"][0], tot["B"][1]
    if skipped:
        w.append("%d row(s) in the two periods were skipped because the metric cell is empty." % skipped)
    if data.no_date:
        w.append("%d row(s) have no usable date and are not in either period." % data.no_date)
    for lab, side in ((A.label, "A"), (B.label, "B")):
        if tot[side][2] < SMALL_N:
            w.append("period %s has only %d rows: results are noisy." % (lab, tot[side][2]))
    if max(known) < B.end - dt.timedelta(days=3):
        w.append("data stops at %s, before the end of %s (%s): B may be incomplete, which looks like a drop." % (
            max(known), B.label, B.end))
    if min(known) > A.start + dt.timedelta(days=3):
        w.append("data starts at %s, after the start of %s: A may be incomplete." % (min(known), A.label))
    if abs(change) <= 0.01 * abs(va or 1):
        w.append("the total change is under 1%: there is little to explain; segment movements mostly offset each other.")
    if data.skipped_dims:
        w.append("dimensions not used: " + "; ".join(data.skipped_dims))
    if not data.dims:
        w.append("no category-like columns to break down by (use --dims to force some).")
    ranked = rank_dims(data, segs, ratio)
    for r in ranked:
        small = [s["label"] for s in r["segments"][:3] if min(s["rowsA"], s["rowsB"]) < SMALL_N and s["status"] == "both"]
        r["small_n"] = small
    out["dimensions"] = ranked
    out["history"] = history_check(data, A, B, ratio, min(known))
    if ranked:
        top = ranked[0]
        top_seg = _lead_segment(top, change)
        out["bridge"] = _bridge(out, top)
        out["drill"] = []
        cur_segs = [s for s in top["segments"] if not change or s["delta"] * change > 0][:max(1, drill)] or top["segments"][:1]
        for seg_ in cur_segs:
            t2, s2, _ = data.collect(A, B, (top["dimension"], seg_["label"]))
            rest = {n: v for n, v in s2.items() if n != top["dimension"]}
            r2 = rank_dims(data, rest, ratio)
            out["drill"].append({"within": "%s = %s" % (top["dimension"], seg_["label"]), "delta": seg_["delta"], "share": seg_["share"],
                                 "rows": [t2["A"][2], t2["B"][2]], "dimensions": r2[:3]})
    else:
        out["bridge"] = None
        out["drill"] = []
    out["headline"] = headline(out)
    return out


def _lead_segment(dim, change):
    for s in dim["segments"]:
        if change == 0 or s["delta"] * change > 0:
            return s
    return dim["segments"][0] if dim["segments"] else None


def _bridge(out, dim, keep=6):
    segs = dim["segments"]
    steps = [{"label": s["label"], "value": s["delta"]} for s in segs[:keep]]
    rest = segs[keep:]
    if rest:
        steps.append({"label": "%d other" % len(rest), "value": sum(s["delta"] for s in rest)})
    pct = out["kind"] == "ratio" and 0 <= out["A"]["value"] <= 1.5 and 0 <= out["B"]["value"] <= 1.5
    return {"dimension": dim["dimension"], "start_label": out["A"]["label"], "end_label": out["B"]["label"],
            "start": out["A"]["value"], "end": out["B"]["value"], "steps": steps, "metric": out["metric"], "pct": pct}


def _pts(x):
    v = round(x * 100, 2)
    return "0.00" if v == 0 else "%+.2f" % v


def _sg(x):
    return ("+" if x > 0 else "") + _num(x)


def _share(x):
    v = round(x * 100)
    return "0%" if v == 0 else "%d%%" % v


def _num(x, pct=False):
    if x is None:
        return "n/a"
    if pct:
        return "%.2f%%" % (x * 100)
    if abs(x) >= 1000:
        return "{:,.0f}".format(x)
    return "{:,.4g}".format(x) if abs(x) < 10 else "{:,.1f}".format(x)


def headline(out):
    pct = out["kind"] == "ratio" and 0 <= out["A"]["value"] <= 1.5 and 0 <= out["B"]["value"] <= 1.5
    ch = out["change"]
    flat = out["change_pct"] is not None and abs(out["change_pct"]) < 0.5
    verb = "was flat" if flat else "rose" if ch > 0 else "fell" if ch < 0 else "did not change"
    s = "%s %s from %s to %s (%s to %s" % (out["metric"], verb, out["A"]["label"], out["B"]["label"],
                                          _num(out["A"]["value"], pct), _num(out["B"]["value"], pct))
    if out["change_pct"] is not None:
        s += ", %+.1f%%" % out["change_pct"]
    s += ")."
    if flat:
        s += " Nothing meaningful changed, so there is no driver to find."
    elif out["dimensions"]:
        top = out["dimensions"][0]
        lead = _lead_segment(top, ch)
        if lead and lead["share"] is not None and abs(lead["share"]) >= 0.3 and top["concentration"] >= 0.3:
            s += " %.0f%% of the change sits in %s = %s." % (lead["share"] * 100, top["dimension"], lead["label"])
            if out["drill"] and out["drill"][0]["dimensions"]:
                d2 = out["drill"][0]["dimensions"][0]
                l2 = _lead_segment(d2, ch)
                if l2 and l2["share"] is not None and abs(l2["share"]) >= 0.3:
                    s += " Inside it, %s = %s holds %.0f%% of that segment's change." % (d2["dimension"], l2["label"], l2["share"] * 100)
        else:
            s += " No single segment explains it: the change is spread out."
    h = out["history"]
    if h.get("verdict"):
        s += " Versus history: " + h["verdict"] + "."
    return s


def render_text(out, top_dims=3, rows_per_dim=6):
    pct = out["kind"] == "ratio" and 0 <= out["A"]["value"] <= 1.5 and 0 <= out["B"]["value"] <= 1.5
    L = []
    a = L.append
    a("WHAT DROVE THE CHANGE: %s, %s -> %s" % (out["metric"], out["A"]["label"], out["B"]["label"]))
    a("  A %-12s %s  (%d rows)" % (out["A"]["label"], _num(out["A"]["value"], pct), out["A"]["rows"]))
    a("  B %-12s %s  (%d rows)" % (out["B"]["label"], _num(out["B"]["value"], pct), out["B"]["rows"]))
    chs = ("%+.2f pts" % (out["change"] * 100)) if pct else _sg(out["change"])
    a("  change %s%s" % (chs, (" (%+.1f%%)" % out["change_pct"]) if out["change_pct"] is not None else ""))
    a("")
    a("HEADLINE: " + out["headline"])
    a("")
    a(out["history"]["text"])
    a("")
    if not out["dimensions"]:
        a("No dimension to break the change down by.")
    for r in out["dimensions"][:top_dims]:
        a("BY %s   (%d segments, concentration %.2f: 0 = spread evenly, 1 = one segment)" % (r["dimension"].upper(), r["segments_count"], r["concentration"]))
        head = "  %-22s %12s %12s %12s %8s" % ("segment", "A", "B", "change", "share")
        if out["kind"] == "ratio":
            head += " %10s %10s" % ("rate part", "mix part") if not pct else " %10s %10s" % ("rate (pts)", "mix (pts)")
        a(head)
        for s in r["segments"][:rows_per_dim]:
            fm = (lambda x: _num(x, pct)) if out["kind"] == "ratio" else _num
            va, vb = (s["rA"], s["rB"]) if out["kind"] == "ratio" else (s["A"], s["B"])
            line = "  %-22s %12s %12s %12s %8s" % (
                (s["label"] + ("*" if s["status"] != "both" else ""))[:22], fm(va) if not (out["kind"] == "ratio" and s["denA"] == 0) else "-",
                fm(vb) if not (out["kind"] == "ratio" and s["denB"] == 0) else "-",
                (("%s pts" % _pts(s["delta"])) if pct else _sg(s["delta"])),
                _share(s["share"]) if s["share"] is not None else "n/a")
            if out["kind"] == "ratio":
                line += " %10s %10s" % (_pts(s["rate"]) if pct else "%+.4g" % s["rate"], _pts(s["mix"]) if pct else "%+.4g" % s["mix"])
            a(line)
        if r["segments_count"] > rows_per_dim:
            a("  ... %d more segment(s) in --json" % (r["segments_count"] - rows_per_dim))
        if out["kind"] == "ratio":
            a("  totals: rate effect %s + mix effect %s = %s" % (
                ("%s pts" % _pts(r["rate_total"])) if pct else "%+.4g" % r["rate_total"],
                ("%s pts" % _pts(r["mix_total"])) if pct else "%+.4g" % r["mix_total"], chs))
            a("  (rate = the segment itself got better or worse; mix = more or less traffic in high or low performing segments)")
        if r["new"] or r["lost"]:
            a("  * new segments: %s   lost segments: %s" % (", ".join(r["new"]) or "none", ", ".join(r["lost"]) or "none"))
        if r["small_n"]:
            a("  small sample: %s has fewer than %d rows in one period, so read its rate with care." % (", ".join(r["small_n"]), SMALL_N))
        a("")
    if len(out["dimensions"]) > top_dims:
        a("Other dimensions, by concentration: " + ", ".join("%s (%.2f)" % (r["dimension"], r["concentration"]) for r in out["dimensions"][top_dims:]))
        a("")
    for dr in out["drill"]:
        a("DRILL INTO %s (change %s, rows %d -> %d)" % (dr["within"], _num(dr["delta"], False) if out["kind"] == "additive" else ("%s pts" % _pts(dr["delta"]) if pct else "%+.4g" % dr["delta"]), dr["rows"][0], dr["rows"][1]))
        if not dr["dimensions"]:
            a("  no further dimension to split by")
        for r in dr["dimensions"][:2]:
            segs = ", ".join("%s %s (%s)" % (s["label"], ("%s pts" % _pts(s["delta"])) if pct else _sg(s["delta"]),
                                             _share(s["share"]) if s["share"] is not None else "n/a") for s in r["segments"][:3])
            a("  by %s (concentration %.2f): %s" % (r["dimension"], r["concentration"], segs))
        a("")
    a("Shares can exceed 100% or be negative when other segments moved the other way. A segment with * exists in only one period.")
    for x in out["warnings"]:
        a("warning: " + x)
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="CSV, TSV, JSON, NDJSON or XLSX path, or - for stdin")
    ap.add_argument("--metric", help="numeric column to sum, or the word count to count rows")
    ap.add_argument("--ratio", metavar="NUM,DEN", help="ratio metric, e.g. orders,sessions or revenue,count")
    ap.add_argument("--date", required=True, help="date column")
    ap.add_argument("--a", help="baseline period (see above)")
    ap.add_argument("--b", help="comparison period")
    ap.add_argument("--auto", action="store_true", help="latest complete period versus the one before")
    ap.add_argument("--dims", help="comma-separated columns to break down by (default: auto-pick categories)")
    ap.add_argument("--drill", type=int, default=1, help="how many top segments to drill into (default 1)")
    ap.add_argument("--sheet", help="sheet name or number (XLSX)")
    ap.add_argument("--date-order", choices=["dmy", "mdy"], help="resolve ambiguous 03/04/2026 style dates")
    ap.add_argument("--keep-totals", action="store_true")
    ap.add_argument("--json", action="store_true", help="machine-readable output (includes the waterfall bridge)")
    a = ap.parse_args(argv)
    try:
        if not a.metric and not a.ratio:
            raise UsageError("give --metric COL (or the word count) or --ratio NUM,DEN")
        ratio = None
        if a.ratio:
            parts = [p.strip() for p in a.ratio.split(",")]
            if len(parts) != 2 or not all(parts):
                raise UsageError("--ratio needs two names, like orders,sessions")
            ratio = (parts[0], parts[1])
        if not a.auto and not (a.a and a.b):
            raise UsageError("give --a and --b periods, or --auto")
        t = dataio.read_table(a.file, a.sheet, a.date_order, a.keep_totals)
        note = None
        if a.auto:
            di = t.index(a.date)
            ds = [d for d in t.values(di) if d is not None]
            if not ds:
                raise UsageError("no usable dates in '%s'" % a.date)
            ds = [d.date() if isinstance(d, dt.datetime) else d for d in ds]
            A, B, note = auto_periods(min(ds), max(ds))
        else:
            A, B = parse_period(a.a), parse_period(a.b)
        dims = [d.strip() for d in a.dims.split(",") if d.strip()] if a.dims else None
        out = run(t, a.metric, ratio, a.date, A, B, dims, a.drill)
        if note:
            out["warnings"].insert(0, "--auto chose A=%s, B=%s: %s." % (A.label, B.label, note))
        out["warnings"] = t.meta["warnings"][:3] + out["warnings"] if t.meta["warnings"] else out["warnings"]
    except (UsageError, dataio.DataError) as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str) if a.json else render_text(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
