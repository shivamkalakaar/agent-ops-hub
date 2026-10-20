#!/usr/bin/env python3
"""Plain-English statistics for spreadsheet data. Standard library only; every formula is checked against scipy in the tests.

  python3 scripts/stats.py compare data.csv --value revenue --group plan
  python3 scripts/stats.py props --a 120/1000 --b 150/1000
  python3 scripts/stats.py props data.csv --group variant --success converted
  python3 scripts/stats.py chi2 data.csv --a region --b churned
  python3 scripts/stats.py corr data.csv --x spend --y sales
  python3 scripts/stats.py trend data.csv --value revenue --date order_date --by month
  python3 scripts/stats.py ci data.csv --value revenue --stat median
  python3 scripts/stats.py power props --p1 0.10 --lift 0.2 --daily 800
  python3 scripts/stats.py anomaly data.csv --value revenue --date order_date --by day
  python3 scripts/stats.py define churn                  # formula, denominator, mistakes, sanity range

Subcommands: compare (Welch t, Mann-Whitney, effect size, CI), props (two-proportion z, Newcombe CI, lift, Fisher when
counts are small), chi2 (independence, Cramer's V), corr (Pearson and Spearman with CI), trend (OLS slope with CI),
ci (seeded bootstrap), power (sample size for proportions and means), anomaly (z-score, robust z, IQR),
define (metric glossary lookup).
Each prints a verdict and CAUTION lines: significance is not importance, watch the sample size, many tests mean false alarms.
Exit codes: 0 ok, 2 usage or data problem (one line message).
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import math
import os
import random
import sys

import _dist as D
import _parse as P
import dataio


class UsageError(Exception):
    pass


# ------------------------------------------------------------------ small helpers
def g4(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    if isinstance(x, float) and math.isinf(x):
        return "inf" if x > 0 else "-inf"
    if x == 0:
        return "0"
    a = abs(x)
    if a >= 1e6 or a < 1e-3:
        return "%.3g" % x
    return ("{:,.%dg}" % 5).format(x) if a >= 1000 else "%.4g" % x


def pfmt(p):
    if p is None or (isinstance(p, float) and math.isnan(p)):
        return "p = n/a"
    return "p < 0.001" if p < 0.001 else "p = %.3f" % p if p >= 0.001 else "p = %.4f" % p


def p_words(p, alpha):
    if p < 0.001:
        return "very unlikely to be chance alone"
    if p < 0.01:
        return "unlikely to be chance alone"
    if p < alpha:
        return "unlikely to be chance alone, though it only just clears the %.2f bar" % alpha
    if p < 0.10:
        return "suggestive but not conclusive"
    return "consistent with chance: no reliable difference detected, which is not proof there is none"


def mean_var(xs):
    n = len(xs)
    m = math.fsum(xs) / n
    v = math.fsum((x - m) ** 2 for x in xs) / (n - 1) if n > 1 else float("nan")
    return m, v


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def quantile(s, p):
    k = (len(s) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def holm(ps):
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    adj, run = [0.0] * len(ps), 0.0
    for rank, i in enumerate(order):
        run = max(run, (len(ps) - rank) * ps[i])
        adj[i] = min(1.0, run)
    return adj


def effect_label(d, cuts=(0.2, 0.5, 0.8)):
    a = abs(d)
    return "negligible" if a < cuts[0] else "small" if a < cuts[1] else "medium" if a < cuts[2] else "large"


def parse_floats(text):
    try:
        return [float(x) for x in text.replace(";", ",").split(",") if x.strip()]
    except ValueError:
        raise UsageError("could not read the numbers in '%s'; give comma-separated numbers" % text[:40])


# ------------------------------------------------------------------ core tests (pure functions)
def welch(x, y, conf=0.95):
    n1, n2 = len(x), len(y)
    if n1 < 2 or n2 < 2:
        raise UsageError("each group needs at least 2 values (got %d and %d)" % (n1, n2))
    m1, v1 = mean_var(x)
    m2, v2 = mean_var(y)
    se2 = v1 / n1 + v2 / n2
    diff = m2 - m1
    if se2 == 0:
        return {"diff": diff, "t": None, "df": None, "p": 1.0 if diff == 0 else 0.0, "ci": (diff, diff), "d": None,
                "m1": m1, "m2": m2, "sd1": 0.0, "sd2": 0.0}
    se = math.sqrt(se2)
    t = diff / se
    df = se2 ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    p = D.t_sf2(t, df)
    tc = D.t_isf((1 - conf) / 2, df)
    sp = math.sqrt(((n1 - 1) * v1 + (n2 - 1) * v2) / (n1 + n2 - 2))
    return {"diff": diff, "t": t, "df": df, "p": p, "ci": (diff - tc * se, diff + tc * se), "se": se,
            "d": (diff / sp) if sp > 0 else None, "m1": m1, "m2": m2, "sd1": math.sqrt(v1), "sd2": math.sqrt(v2)}


def mann_whitney(x, y):
    n1, n2 = len(x), len(y)
    ranks, ties = D.rankdata(list(x) + list(y))
    r1 = math.fsum(ranks[:n1])
    u1 = r1 - n1 * (n1 + 1) / 2.0
    u2 = n1 * n2 - u1
    n = n1 + n2
    mu = n1 * n2 / 2.0
    tie = sum(t ** 3 - t for t in ties)
    s2 = n1 * n2 / 12.0 * ((n + 1) - tie / (n * (n - 1))) if n > 1 else 0
    if s2 <= 0:
        return {"u": u1, "p": 1.0, "prob_b_greater": u2 / (n1 * n2)}
    z = (max(u1, u2) - mu - 0.5) / math.sqrt(s2)
    return {"u": max(u1, u2), "z": z, "p": min(1.0, 2 * D.norm_sf(z)), "prob_b_greater": u2 / (n1 * n2)}


def two_prop(x1, n1, x2, n2, conf=0.95):
    if not (0 <= x1 <= n1 and 0 <= x2 <= n2 and n1 > 0 and n2 > 0):
        raise UsageError("successes must be between 0 and the group size")
    p1, p2 = x1 / n1, x2 / n2
    pool = (x1 + x2) / (n1 + n2)
    se = math.sqrt(pool * (1 - pool) * (1 / n1 + 1 / n2))
    z = (p2 - p1) / se if se > 0 else 0.0
    p_z = 2 * D.norm_sf(abs(z)) if se > 0 else 1.0
    out = {"p1": p1, "p2": p2, "diff": p2 - p1, "z": z, "p_z": p_z, "ci": D.newcombe(x1, n1, x2, n2, conf),
           "ci1": D.wilson(x1, n1, conf), "ci2": D.wilson(x2, n2, conf)}
    exp_min = min((x1 + x2) * n1 / (n1 + n2), (x1 + x2) * n2 / (n1 + n2), (n1 + n2 - x1 - x2) * n1 / (n1 + n2), (n1 + n2 - x1 - x2) * n2 / (n1 + n2))
    out["min_expected"] = exp_min
    out["p_fisher"] = D.fisher_exact(int(x1), int(n1 - x1), int(x2), int(n2 - x2)) if exp_min < 10 and (n1 + n2) < 100000 else None
    out["p"] = out["p_fisher"] if out["p_fisher"] is not None else p_z
    if p1 > 0:
        out["lift"] = p2 / p1 - 1
        if x1 > 0 and x2 > 0:
            se_l = math.sqrt(1 / x1 - 1 / n1 + 1 / x2 - 1 / n2)
            zc = D.norm_ppf(1 - (1 - conf) / 2)
            lr = math.log(p2 / p1)
            out["lift_ci"] = (math.exp(lr - zc * se_l) - 1, math.exp(lr + zc * se_l) - 1)
    return out


def chi2_test(table):
    """table: list of rows of counts. Returns chi2, df, p, Cramer's V, expected, standardised residuals."""
    r, c = len(table), len(table[0])
    rows = [sum(row) for row in table]
    cols = [sum(table[i][j] for i in range(r)) for j in range(c)]
    n = sum(rows)
    if n == 0 or r < 2 or c < 2:
        raise UsageError("need at least 2 categories in each column")
    chi = 0.0
    exp = [[rows[i] * cols[j] / n for j in range(c)] for i in range(r)]
    res = [[0.0] * c for _ in range(r)]
    for i in range(r):
        for j in range(c):
            e = exp[i][j]
            if e > 0:
                chi += (table[i][j] - e) ** 2 / e
                res[i][j] = (table[i][j] - e) / math.sqrt(e * (1 - rows[i] / n) * (1 - cols[j] / n)) if rows[i] < n and cols[j] < n else 0.0
    df = (r - 1) * (c - 1)
    k = min(r - 1, c - 1)
    return {"chi2": chi, "df": df, "p": D.chi2_sf(chi, df), "cramers_v": math.sqrt(chi / (n * k)), "n": n, "k": k,
            "expected": exp, "residuals": res, "small_cells": sum(1 for row in exp for e in row if e < 5),
            "cells": r * c}


def pearson(x, y, conf=0.95):
    n = len(x)
    if n < 3:
        raise UsageError("need at least 3 pairs (got %d)" % n)
    mx, my = math.fsum(x) / n, math.fsum(y) / n
    sxx = math.fsum((a - mx) ** 2 for a in x)
    syy = math.fsum((b - my) ** 2 for b in y)
    sxy = math.fsum((a - mx) * (b - my) for a, b in zip(x, y))
    if sxx == 0 or syy == 0:
        raise UsageError("one of the columns is constant, so a correlation is undefined")
    r = max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))
    return _corr_result(r, n, conf, 1.0)


def _corr_result(r, n, conf, var_factor):
    if abs(r) >= 1.0:
        p = 0.0
    else:
        p = D.t_sf2(r * math.sqrt((n - 2) / (1 - r * r)), n - 2)
    ci = None
    if n > 3 and abs(r) < 1.0:
        z = math.atanh(r)
        se = math.sqrt(var_factor / (n - 3)) if var_factor == 1.0 else math.sqrt((1 + r * r / 2) / (n - 3))
        zc = D.norm_ppf(1 - (1 - conf) / 2)
        ci = (math.tanh(z - zc * se), math.tanh(z + zc * se))
    return {"r": r, "n": n, "p": p, "ci": ci}


def spearman(x, y, conf=0.95):
    n = len(x)
    if n < 3:
        raise UsageError("need at least 3 pairs (got %d)" % n)
    rx, _ = D.rankdata(list(x))
    ry, _ = D.rankdata(list(y))
    r = pearson(rx, ry, conf)["r"]
    return _corr_result(r, n, conf, 0.0)


def ols(xs, ys, conf=0.95):
    n = len(xs)
    if n < 3:
        raise UsageError("need at least 3 periods for a trend (8 or more is better; got %d)" % n)
    mx, my = math.fsum(xs) / n, math.fsum(ys) / n
    sxx = math.fsum((a - mx) ** 2 for a in xs)
    sxy = math.fsum((a - mx) * (b - my) for a, b in zip(xs, ys))
    syy = math.fsum((b - my) ** 2 for b in ys)
    slope = sxy / sxx
    icpt = my - slope * mx
    resid = [b - (icpt + slope * a) for a, b in zip(xs, ys)]
    sse = math.fsum(e * e for e in resid)
    df = n - 2
    s2 = sse / df
    se = math.sqrt(s2 / sxx)
    if se == 0:
        t, p, ci = float("inf") if slope else 0.0, 0.0 if slope else 1.0, (slope, slope)
    else:
        t = slope / se
        p = D.t_sf2(t, df)
        tc = D.t_isf((1 - conf) / 2, df)
        ci = (slope - tc * se, slope + tc * se)
    r2 = 1 - sse / syy if syy > 0 else 0.0
    acf1 = None
    if n >= 6:
        den = math.fsum(e * e for e in resid)
        if den > 0:
            acf1 = math.fsum(resid[i] * resid[i - 1] for i in range(1, n)) / den
    return {"slope": slope, "intercept": icpt, "se": se, "t": t, "p": p, "ci": ci, "r2": r2, "n": n, "resid": resid, "acf1": acf1}


def bootstrap(data, stat="mean", conf=0.95, resamples=5000, seed=1):
    n = len(data)
    limit = 6e6 if stat == "median" else 3e7
    b = int(max(300, min(resamples, limit / max(n, 1))))
    rng = random.Random(seed)
    vals = []
    for _ in range(b):
        s = rng.choices(data, k=n)
        if stat == "mean":
            vals.append(math.fsum(s) / n if n < 50 else sum(s) / n)
        else:
            vals.append(median(s))
    vals.sort()
    a = (1 - conf) / 2
    return {"resamples": b, "ci": (quantile(vals, a), quantile(vals, 1 - a))}


def n_props(p1, p2, alpha=0.05, power=0.8):
    za, zb = D.norm_ppf(1 - alpha / 2), D.norm_ppf(power)
    pb = (p1 + p2) / 2
    return math.ceil((za * math.sqrt(2 * pb * (1 - pb)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2 / (p1 - p2) ** 2)


def power_props(n, p1, p2, alpha=0.05):
    za = D.norm_ppf(1 - alpha / 2)
    pb = (p1 + p2) / 2
    num = abs(p2 - p1) * math.sqrt(n) - za * math.sqrt(2 * pb * (1 - pb))
    return D.norm_cdf(num / math.sqrt(p1 * (1 - p1) + p2 * (1 - p2)))


def mde_props(n, p1, alpha=0.05, power=0.8):
    lo, hi = p1, 1.0 - 1e-9
    if power_props(n, p1, hi, alpha) < power:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if power_props(n, p1, mid, alpha) < power:
            lo = mid
        else:
            hi = mid
    return hi


def n_means(delta, sd, alpha=0.05, power=0.8):
    n = 2 * ((D.norm_ppf(1 - alpha / 2) + D.norm_ppf(power)) * sd / delta) ** 2
    for _ in range(30):
        df = max(2 * n - 2, 2)
        new = 2 * ((D.t_isf(alpha / 2, df) + D.t_ppf(power, df)) * sd / delta) ** 2
        if abs(new - n) < 1e-9:
            break
        n = new
    return math.ceil(n - 1e-9)


def mde_means(n, sd, alpha=0.05, power=0.8):
    df = 2 * n - 2
    return (D.t_isf(alpha / 2, df) + D.t_ppf(power, df)) * sd * math.sqrt(2.0 / n)


# ------------------------------------------------------------------ data access
def load(a):
    if not getattr(a, "file", None):
        raise UsageError("give a data file (a path, or - for stdin)")
    try:
        return dataio.read_table(a.file, getattr(a, "sheet", None), getattr(a, "date_order", None))
    except dataio.DataError as ex:
        raise UsageError(str(ex))


def numeric(t, name):
    j = t.index(name)
    typ = t.infos[j]["type"]
    if typ not in P.NUMERIC_TYPES:
        raise UsageError("column '%s' is %s, not a number" % (t.header[j], typ))
    return j, t.values(j)


def split_groups(t, vcol, gcol, want=None):
    _, vals = numeric(t, vcol)
    gj = t.index(gcol)
    labels = t.raw(gj)
    canon = {}
    for l in labels:
        canon.setdefault(P.norm_key(l), l.strip())
    groups, dropped = {}, 0
    for v, l in zip(vals, labels):
        if v is None or P.is_null(l):
            dropped += 1
            continue
        groups.setdefault(canon[P.norm_key(l)], []).append(v)
    if want:
        missing = [w for w in want if w not in groups]
        if missing:
            raise UsageError("group(s) %s not found. Groups: %s" % (", ".join(missing), ", ".join(list(groups)[:12])))
        groups = {w: groups[w] for w in want}
    if len(groups) < 2:
        raise UsageError("need at least 2 groups in '%s' (found %d)" % (t.header[gj], len(groups)))
    return groups, dropped


def period_start(d, by):
    if by == "day":
        return d
    if by == "week":
        return d - dt.timedelta(days=d.weekday())
    if by == "month":
        return d.replace(day=1)
    if by == "quarter":
        return dt.date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    return dt.date(d.year, 1, 1)


def period_end(s, by):
    if by == "day":
        return s
    if by == "week":
        return s + dt.timedelta(days=6)
    if by == "month":
        return s.replace(day=calendar.monthrange(s.year, s.month)[1])
    if by == "quarter":
        m = s.month + 2
        return dt.date(s.year, m, calendar.monthrange(s.year, m)[1])
    return dt.date(s.year, 12, 31)


def series(t, vcol, dcol, by, agg, keep_partial=False):
    """Aggregate a value column into periods. Returns (list of (start, value, rows), notes, by)."""
    dj = t.index(dcol)
    if t.infos[dj]["type"] not in ("date", "datetime"):
        raise UsageError("column '%s' is %s, not a date" % (t.header[dj], t.infos[dj]["type"]))
    if t.infos[dj]["date_order"] in ("ambiguous", "mixed"):
        raise UsageError("dates in '%s' are ambiguous; pass --date-order dmy or mdy" % t.header[dj])
    dates = [d.date() if isinstance(d, dt.datetime) else d for d in t.values(dj)]
    if vcol == "count":
        vals = [1.0] * len(dates)
    else:
        vals = numeric(t, vcol)[1]
    known = [d for d in dates if d is not None]
    if not known:
        raise UsageError("no usable dates in '%s'" % dcol)
    dmin, dmax = min(known), max(known)
    if by == "auto":
        span = (dmax - dmin).days
        by = "month" if span >= 150 else "week" if span >= 35 else "day"
    acc = {}
    for d, v in zip(dates, vals):
        if d is None or v is None:
            continue
        k = period_start(d, by)
        a = acc.setdefault(k, [0.0, 0])
        a[0] += v
        a[1] += 1
    keys = sorted(acc)
    notes = []
    if not keep_partial and by != "day" and len(keys) > 2:
        slack = dt.timedelta(days=3 if by in ("month", "quarter", "year") else 1)
        if dmax < period_end(keys[-1], by) - slack:
            notes.append("dropped the last %s (%s): data stops at %s, so it is incomplete (--keep-partial to keep)" % (by, keys[-1], dmax))
            keys = keys[:-1]
        if dmin > keys[0] + slack:
            notes.append("dropped the first %s (%s): data starts at %s, so it is incomplete" % (by, keys[0], dmin))
            keys = keys[1:]
    out = []
    for k in keys:
        s, n = acc[k]
        out.append((k, (s / n) if agg == "mean" else s if agg == "sum" else float(n), n))
    return out, notes, by


# ------------------------------------------------------------------ subcommands
def cmd_compare(a):
    conf = 1 - a.alpha
    if a.a_values or a.b_values:
        if not (a.a_values and a.b_values):
            raise UsageError("give both --a-values and --b-values")
        groups, dropped, label = {"A": parse_floats(a.a_values), "B": parse_floats(a.b_values)}, 0, "values"
    else:
        if not (a.file and a.value and a.group):
            raise UsageError("give FILE --value COL --group COL (or --a-values and --b-values)")
        t = load(a)
        want = [g.strip() for g in a.groups.split(",")] if a.groups else None
        groups, dropped = split_groups(t, a.value, a.group, want)
        label = a.value
    names = list(groups)
    if len(names) > 10:
        raise UsageError("%d groups is too many for pairwise tests; pick some with --groups A,B,C" % len(names))
    summary = []
    for g in names:
        v = groups[g]
        m, var = mean_var(v)
        summary.append({"group": g, "n": len(v), "mean": m, "median": median(v), "sd": math.sqrt(var) if len(v) > 1 else None})
    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            x, y = groups[names[i]], groups[names[j]]
            w, mw = welch(x, y, conf), mann_whitney(x, y)
            pairs.append({"a": names[i], "b": names[j], "welch": w, "mw": mw})
    if len(pairs) > 1:
        adj = holm([p["welch"]["p"] for p in pairs])
        for p, q in zip(pairs, adj):
            p["p_adj"] = q
    res = {"command": "compare", "value": label, "alpha": a.alpha, "groups": summary, "pairs": pairs, "dropped_rows": dropped}
    L = ["COMPARE %s across %d groups" % (label, len(names))]
    for s in summary:
        L.append("  %-16s n=%-6d mean %-10s median %-10s sd %s" % (s["group"][:16], s["n"], g4(s["mean"]), g4(s["median"]), g4(s["sd"])))
    for p in pairs:
        w, mw = p["welch"], p["mw"]
        pv = p.get("p_adj", w["p"])
        L.append("")
        L.append("%s vs %s" % (p["a"], p["b"]))
        if w["t"] is None:
            L.append("  VERDICT: both groups are constant; the difference is %s." % g4(w["diff"]))
        else:
            up = "higher" if w["diff"] > 0 else "lower"
            L.append("  VERDICT: %s averages %s %s than %s (%d%% CI %s to %s). This is %s (%s)." % (
                p["b"], g4(abs(w["diff"])), up, p["a"], round(conf * 100), g4(w["ci"][0]), g4(w["ci"][1]), p_words(pv, a.alpha), pfmt(pv)))
            L.append("  Welch t = %s, df = %s, %s%s" % (g4(w["t"]), g4(w["df"]), pfmt(w["p"]), (", Holm-adjusted %s" % pfmt(pv)) if "p_adj" in p else ""))
            L.append("  Mann-Whitney (rank test, robust to outliers): %s; chance a random %s beats a random %s = %.0f%%" % (
                pfmt(mw["p"]), p["b"], p["a"], mw["prob_b_greater"] * 100))
            if (w["p"] < a.alpha) != (mw["p"] < a.alpha):
                L.append("  NOTE: the two tests disagree. The data are skewed or have outliers; compare medians and trust the rank test.")
            if w["d"] is not None:
                L.append("  effect size: Cohen's d = %s (%s)" % (g4(w["d"]), effect_label(w["d"])))
    ns = [s["n"] for s in summary]
    if min(ns) < 30:
        L.append("CAUTION: a group has only %d values; small samples give fragile results." % min(ns))
    if max(ns) > 10000:
        L.append("CAUTION: with this many rows even trivial differences become significant. Judge the size of the difference, not just p.")
    L.append("CAUTION: significance is not importance. Ask whether a difference of the size shown matters in practice.")
    L.append("CAUTION: run enough tests and about 1 in %d will look significant by chance. %s" % (
        round(1 / a.alpha), "Holm adjustment applied across the pairs above." if len(pairs) > 1 else "Say how many comparisons you made."))
    L.append("CAUTION: a difference between groups is not proof of cause unless the groups were assigned at random.")
    if dropped:
        L.append("note: %d row(s) skipped because the value or group was empty." % dropped)
    return res, L


def _binary(t, col, success_value):
    j = t.index(col)
    raw = t.raw(j)
    if success_value is not None:
        sv = P.norm_key(success_value)
        return [None if P.is_null(v) else (1 if P.norm_key(v) == sv else 0) for v in raw]
    out = []
    for v in raw:
        if P.is_null(v):
            out.append(None)
            continue
        k = v.strip().lower()
        if k in ("1", "true", "yes", "y", "t", "1.0"):
            out.append(1)
        elif k in ("0", "false", "no", "n", "f", "0.0"):
            out.append(0)
        else:
            raise UsageError("column '%s' is not yes/no or 1/0 (found '%s'). Use --success-value to say which value counts as success." % (
                t.header[j], v.strip()[:20]))
    return out


def cmd_props(a):
    conf = 1 - a.alpha
    if a.a or a.b:
        try:
            (x1, n1), (x2, n2) = [tuple(float(z) for z in s.split("/")) for s in (a.a, a.b)]
        except (ValueError, AttributeError):
            raise UsageError("give --a and --b like 120/1000 (successes/total)")
        names = ["A", "B"]
    else:
        if not (a.file and a.group and a.success):
            raise UsageError("give FILE --group COL --success COL (or --a 120/1000 --b 150/1000)")
        t = load(a)
        flags = _binary(t, a.success, a.success_value)
        gj = t.index(a.group)
        labels = t.raw(gj)
        canon = {}
        for l in labels:
            canon.setdefault(P.norm_key(l), l.strip())
        tally = {}
        for f, l in zip(flags, labels):
            if f is None or P.is_null(l):
                continue
            c = tally.setdefault(canon[P.norm_key(l)], [0, 0])
            c[0] += f
            c[1] += 1
        want = [g.strip() for g in a.groups.split(",")] if a.groups else list(tally)
        if len(want) != 2:
            raise UsageError("props compares exactly 2 groups; found %d (%s). Pick two with --groups A,B" % (len(want), ", ".join(want[:8])))
        for w in want:
            if w not in tally:
                raise UsageError("group '%s' not found. Groups: %s" % (w, ", ".join(tally)))
        names = want
        (x1, n1), (x2, n2) = tally[want[0]], tally[want[1]]
    r = two_prop(x1, n1, x2, n2, conf)
    res = {"command": "props", "groups": names, "x": [x1, x2], "n": [n1, n2], "alpha": a.alpha, **{k: v for k, v in r.items()}}
    L = ["PROPORTIONS %s vs %s" % (names[0], names[1]),
         "  %-12s %d of %d = %.2f%% (%d%% CI %.2f%% to %.2f%%)" % (names[0], x1, n1, r["p1"] * 100, round(conf * 100), r["ci1"][0] * 100, r["ci1"][1] * 100),
         "  %-12s %d of %d = %.2f%% (%d%% CI %.2f%% to %.2f%%)" % (names[1], x2, n2, r["p2"] * 100, round(conf * 100), r["ci2"][0] * 100, r["ci2"][1] * 100)]
    up = "higher" if r["diff"] > 0 else "lower"
    L.append("VERDICT: %s is %.2f points %s than %s (%d%% CI %+.2f to %+.2f points). This is %s (%s)." % (
        names[1], abs(r["diff"]) * 100, up, names[0], round(conf * 100), r["ci"][0] * 100, r["ci"][1] * 100, p_words(r["p"], a.alpha), pfmt(r["p"])))
    if "lift" in r:
        lc = ("; %d%% CI %+.1f%% to %+.1f%%" % (round(conf * 100), r["lift_ci"][0] * 100, r["lift_ci"][1] * 100)) if "lift_ci" in r else ""
        L.append("  relative lift: %+.1f%%%s" % (r["lift"] * 100, lc))
    L.append("  z = %s, %s (two-proportion z-test)%s" % (g4(r["z"]), pfmt(r["p_z"]),
             ("; Fisher exact %s used because some expected counts are below 10" % pfmt(r["p_fisher"])) if r["p_fisher"] is not None else ""))
    if min(n1, n2) < 100:
        L.append("CAUTION: a group has fewer than 100 observations; proportions this small move a lot by chance.")
    L.append("CAUTION: significance is not importance: is a %.2f point difference worth acting on?" % (abs(r["diff"]) * 100))
    L.append("CAUTION: if you looked at several variants or metrics, some will look significant by chance. Say how many you tried.")
    L.append("CAUTION: stopping a test the moment it looks significant inflates false positives; fix the sample size first (stats.py power).")
    return res, L


def cmd_chi2(a):
    t = load(a)
    ja, jb = t.index(a.a), t.index(a.b)
    ra, rb = t.raw(ja), t.raw(jb)
    ca, cb = {}, {}
    for l in ra:
        ca.setdefault(P.norm_key(l), l.strip())
    for l in rb:
        cb.setdefault(P.norm_key(l), l.strip())
    counts = {}
    for x, y in zip(ra, rb):
        if P.is_null(x) or P.is_null(y):
            continue
        k = (ca[P.norm_key(x)], cb[P.norm_key(y)])
        counts[k] = counts.get(k, 0) + 1
    rows = sorted({k[0] for k in counts})
    cols = sorted({k[1] for k in counts})
    if len(rows) > 30 or len(cols) > 30:
        raise UsageError("too many categories (%d x %d); group rare values first" % (len(rows), len(cols)))
    table = [[counts.get((r, c), 0) for c in cols] for r in rows]
    r = chi2_test(table)
    labels = t.header[ja], t.header[jb]
    res = {"command": "chi2", "a": labels[0], "b": labels[1], "rows": rows, "cols": cols, "table": table, **{k: v for k, v in r.items() if k not in ("expected", "residuals")}}
    v = r["cramers_v"]
    cuts = tuple(c / math.sqrt(r["k"]) for c in (0.1, 0.3, 0.5))
    strength = "negligible" if v < cuts[0] else "weak" if v < cuts[1] else "moderate" if v < cuts[2] else "strong"
    L = ["CHI-SQUARE test of independence: %s x %s (%d x %d table, n=%d)" % (labels[0], labels[1], len(rows), len(cols), r["n"])]
    L.append("VERDICT: the link between %s and %s is %s (%s). Strength: Cramer's V = %.3f (%s)." % (
        labels[0], labels[1], p_words(r["p"], a.alpha) if r["p"] < a.alpha else "not distinguishable from chance", pfmt(r["p"]), v, strength))
    L.append("  chi-square = %s, df = %d" % (g4(r["chi2"]), r["df"]))
    cells = sorted(((abs(r["residuals"][i][j]), i, j) for i in range(len(rows)) for j in range(len(cols))), reverse=True)[:5]
    L.append("  cells that differ most from independence (observed vs expected):")
    for _, i, j in cells:
        L.append("    %s x %s: %d observed vs %.1f expected (residual %+.1f)" % (rows[i][:18], cols[j][:18], table[i][j], r["expected"][i][j], r["residuals"][i][j]))
    if r["small_cells"] > 0.2 * r["cells"]:
        L.append("CAUTION: %d of %d cells expect fewer than 5 rows, so the p-value is unreliable. Merge rare categories or use props (Fisher) for a 2x2." % (r["small_cells"], r["cells"]))
    L.append("CAUTION: with a large n a tiny link becomes 'significant'. Use Cramer's V, not p, to judge how strong it is.")
    L.append("CAUTION: a link is not a cause. Check whether a third factor drives both columns.")
    return res, L


def cmd_corr(a):
    conf = 1 - a.alpha
    t = load(a)
    jx, x = numeric(t, a.x)
    jy, y = numeric(t, a.y)
    pairs = [(u, v) for u, v in zip(x, y) if u is not None and v is not None]
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    pr, sp = pearson(xs, ys, conf), spearman(xs, ys, conf)
    res = {"command": "corr", "x": t.header[jx], "y": t.header[jy], "pearson": pr, "spearman": sp, "pairs": len(pairs), "dropped": len(x) - len(pairs)}
    r = pr["r"]
    strength = "very weak" if abs(r) < 0.2 else "weak" if abs(r) < 0.4 else "moderate" if abs(r) < 0.6 else "strong" if abs(r) < 0.8 else "very strong"
    L = ["CORRELATION %s vs %s (%d pairs)" % (t.header[jx], t.header[jy], len(pairs))]
    L.append("VERDICT: a %s %s relationship (Pearson r = %.3f; r-squared = %.0f%% of the variation shared). %s (%s)." % (
        strength, "positive" if r > 0 else "negative", r, r * r * 100, p_words(pr["p"], a.alpha).capitalize() if pr["p"] < a.alpha else "Could be chance", pfmt(pr["p"])))
    for name, c in (("Pearson (straight-line)", pr), ("Spearman (rank, any steady curve)", sp)):
        ci = ("%d%% CI %.3f to %.3f" % (round(conf * 100), c["ci"][0], c["ci"][1])) if c["ci"] else "no CI (n too small)"
        L.append("  %-34s r = %+.3f, %s, %s" % (name, c["r"], ci, pfmt(c["p"])))
    if abs(pr["r"] - sp["r"]) > 0.2:
        L.append("NOTE: Pearson and Spearman differ a lot: outliers or a curved relationship. Plot it (chart.py scatter) before trusting r.")
    if len(pairs) < 30:
        L.append("CAUTION: only %d pairs; the interval is wide and one odd point can swing r." % len(pairs))
    L.append("CAUTION: correlation is not causation, and a strong r can come from one cluster or a shared trend over time.")
    L.append("CAUTION: significance is not importance: r = 0.1 can be 'significant' with enough rows and explains 1% of the variation.")
    return res, L


def cmd_trend(a):
    conf = 1 - a.alpha
    t = load(a)
    ser, notes, by = series(t, a.value, a.date, a.by, a.agg, a.keep_partial)
    if len(ser) < 3:
        raise UsageError("only %d complete %s period(s) left; a trend needs at least 3 (8+ is better)" % (len(ser), by))
    xs = list(range(len(ser)))
    ys = [s[1] for s in ser]
    o = ols(xs, ys, conf)
    mean_y = math.fsum(ys) / len(ys)
    res = {"command": "trend", "by": by, "agg": a.agg, "periods": [[s[0].isoformat(), s[1], s[2]] for s in ser], "notes": notes,
           **{k: v for k, v in o.items() if k != "resid"}}
    pct = (o["slope"] / mean_y * 100) if mean_y else None
    res["pct_of_mean_per_period"] = pct
    if all(y > 0 for y in ys):
        lo = ols(xs, [math.log(y) for y in ys], conf)
        res["compound_growth_per_period"] = math.exp(lo["slope"]) - 1
        res["compound_ci"] = (math.exp(lo["ci"][0]) - 1, math.exp(lo["ci"][1]) - 1)
    L = ["TREND of %s (%s per %s), %d periods from %s to %s" % (a.value, a.agg, by, len(ser), ser[0][0], ser[-1][0])]
    sig = o["p"] < a.alpha
    direction = "rising" if o["slope"] > 0 else "falling"
    if sig:
        L.append("VERDICT: %s by about %s per %s (%d%% CI %s to %s), %s of the period average. %s" % (
            direction, g4(abs(o["slope"])), by, round(conf * 100), g4(o["ci"][0]), g4(o["ci"][1]),
            ("%.1f%%" % abs(pct)) if pct is not None else "n/a", pfmt(o["p"])))
    else:
        L.append("VERDICT: no clear trend. The best-fit slope is %s per %s (%d%% CI %s to %s, %s), and the interval includes zero." % (
            g4(o["slope"]), by, round(conf * 100), g4(o["ci"][0]), g4(o["ci"][1]), pfmt(o["p"])))
    L.append("  R-squared = %.2f (share of the ups and downs the straight line explains)" % o["r2"])
    if "compound_growth_per_period" in res:
        L.append("  compound growth (log-linear fit): %+.2f%% per %s (%d%% CI %+.2f%% to %+.2f%%)" % (
            res["compound_growth_per_period"] * 100, by, round(conf * 100), res["compound_ci"][0] * 100, res["compound_ci"][1] * 100))
    L.append("  series: " + ", ".join("%s %s" % (s[0].isoformat()[:7 if by in ("month", "quarter", "year") else 10], g4(s[1])) for s in ser[-8:]) +
             (" (last 8 shown)" if len(ser) > 8 else ""))
    for n_ in notes:
        L.append("note: " + n_)
    if len(ser) < 8:
        L.append("CAUTION: only %d periods; a trend line through so few points is a weak claim." % len(ser))
    if o["acf1"] is not None and abs(o["acf1"]) > 0.3:
        L.append("CAUTION: neighbouring periods move together (residual autocorrelation %.2f), so the p-value is too optimistic. Treat borderline results as unproven." % o["acf1"])
    L.append("CAUTION: a straight line ignores seasonality and one-off shocks; check the chart (chart.py line) before quoting the slope.")
    L.append("CAUTION: a past trend is not a forecast.")
    return res, L


def cmd_ci(a):
    conf = 1 - a.alpha
    if a.values:
        vals, label = parse_floats(a.values), "values"
    else:
        if not (a.file and a.value):
            raise UsageError("give FILE --value COL (or --values 1,2,3)")
        t = load(a)
        vals = [v for v in numeric(t, a.value)[1] if v is not None]
        label = a.value
    n = len(vals)
    if n < 5:
        raise UsageError("need at least 5 values for a bootstrap interval (got %d)" % n)
    est = math.fsum(vals) / n if a.stat == "mean" else median(vals)
    b = bootstrap(vals, a.stat, conf, a.resamples, a.seed)
    res = {"command": "ci", "value": label, "stat": a.stat, "n": n, "estimate": est, "ci": b["ci"], "resamples": b["resamples"], "seed": a.seed}
    L = ["BOOTSTRAP CI for the %s of %s (n=%d, %d resamples, seed %d)" % (a.stat, label, n, b["resamples"], a.seed)]
    L.append("VERDICT: the %s is %s; the true value is plausibly between %s and %s (%d%% interval)." % (
        a.stat, g4(est), g4(b["ci"][0]), g4(b["ci"][1]), round(conf * 100)))
    if a.stat == "mean" and n > 2:
        m, v = mean_var(vals)
        tc = D.t_isf((1 - conf) / 2, n - 1)
        h = tc * math.sqrt(v / n)
        res["t_ci"] = (m - h, m + h)
        L.append("  classical t interval for the mean: %s to %s" % (g4(m - h), g4(m + h)))
    m, v = mean_var(vals)
    sd = math.sqrt(v) if v == v else 0
    if sd > 0 and abs(m - median(vals)) > 0.25 * sd:
        L.append("NOTE: mean and median differ noticeably (skewed data). Report the median, or both.")
    if n < 30:
        L.append("CAUTION: n = %d is small; bootstrap intervals are too narrow with so few values." % n)
    L.append("CAUTION: the interval covers sampling noise only. It does not cover a biased or unrepresentative sample.")
    return res, L


def cmd_power(a):
    alpha, power = a.alpha, a.power
    if not 0 < power < 1:
        raise UsageError("--power must be between 0 and 1 (like 0.8)")
    if a.n is not None and a.n < 2:
        raise UsageError("--n is the sample per group you already have, at least 2")
    if a.kind == "props":
        if a.p1 is None or not 0 < a.p1 < 1:
            raise UsageError("give --p1 as the baseline rate between 0 and 1 (like 0.10)")
        if a.n is not None:
            m = mde_props(int(a.n), a.p1, alpha, power)
            res = {"command": "power", "kind": "props", "n_per_group": int(a.n), "p1": a.p1, "detectable_p2": m}
            L = ["POWER: with %d per group and a baseline of %.2f%%" % (a.n, a.p1 * 100)]
            L.append("VERDICT: you can reliably detect (%d%% power) a move to %s, an absolute change of %.2f points (%+.0f%% relative)." % (
                round(power * 100), ("%.2f%%" % (m * 100)) if m else "n/a", ((m - a.p1) * 100) if m else float("nan"), ((m / a.p1 - 1) * 100) if m else float("nan")))
            L.append("  Smaller true effects will often be missed at this sample size.")
            return res, L
        if a.p2 is not None:
            p2 = a.p2
        elif a.lift is not None:
            p2 = a.p1 * (1 + a.lift)
        elif a.mde is not None:
            p2 = a.p1 + a.mde
        else:
            raise UsageError("give one of --p2, --lift (relative, 0.2 = +20%), --mde (absolute) or --n (per group)")
        if not 0 < p2 < 1 or p2 == a.p1:
            raise UsageError("the target rate must differ from the baseline and lie between 0 and 1")
        n = n_props(a.p1, p2, alpha, power)
        res = {"command": "power", "kind": "props", "p1": a.p1, "p2": p2, "n_per_group": n, "total": 2 * n, "alpha": alpha, "power": power}
        L = ["POWER (two proportions): %.2f%% -> %.2f%%, alpha %.2f, power %d%%" % (a.p1 * 100, p2 * 100, alpha, round(power * 100))]
        L.append("VERDICT: you need about %s per group (%s in total)." % ("{:,}".format(n), "{:,}".format(2 * n)))
    else:
        if a.sd is None or a.sd <= 0:
            raise UsageError("give --sd (the typical spread of the metric, above 0)")
        if a.n is not None:
            d = mde_means(int(a.n), a.sd, alpha, power)
            res = {"command": "power", "kind": "means", "n_per_group": int(a.n), "sd": a.sd, "detectable_delta": d}
            L = ["POWER: with %d per group and sd %s" % (a.n, g4(a.sd)),
                 "VERDICT: you can reliably detect (%d%% power) a difference in means of %s (%.2f standard deviations)." % (round(power * 100), g4(d), d / a.sd)]
            return res, L
        if not a.delta:
            raise UsageError("give --delta (smallest difference in means worth detecting) or --n")
        n = n_means(abs(a.delta), a.sd, alpha, power)
        res = {"command": "power", "kind": "means", "sd": a.sd, "delta": a.delta, "n_per_group": n, "total": 2 * n}
        L = ["POWER (two means): difference %s, sd %s (d = %.2f), alpha %.2f, power %d%%" % (g4(a.delta), g4(a.sd), abs(a.delta) / a.sd, alpha, round(power * 100)),
             "VERDICT: you need about %s per group (%s in total)." % ("{:,}".format(n), "{:,}".format(2 * n))]
    if a.daily:
        days = math.ceil(2 * n / a.daily)
        res["days"] = days
        L.append("  at %s units per day that is about %d day(s); run whole weeks to cover weekday effects." % ("{:,}".format(int(a.daily)), days))
    L.append("CAUTION: this assumes two equal groups, a fixed sample size and a two-sided test. Peeking early and stopping breaks it.")
    L.append("CAUTION: halving the effect you want to detect roughly quadruples the sample needed.")
    return res, L


def cmd_anomaly(a):
    t = load(a)
    if a.date:
        ser, notes, by = series(t, a.value, a.date, a.by, a.agg, a.keep_partial)
        labels = [s[0].isoformat() for s in ser]
        vals = [s[1] for s in ser]
    else:
        vals0 = numeric(t, a.value)[1]
        labels, vals, notes, by = [], [], [], "row"
        for i, v in enumerate(vals0):
            if v is not None:
                labels.append("row %d" % t.src_row(i))
                vals.append(v)
    n = len(vals)
    if n < 8:
        raise UsageError("need at least 8 points to look for anomalies (got %d)" % n)
    base = list(vals)
    if a.detrend:
        o = ols(list(range(n)), vals)
        base = o["resid"]
    m, var = mean_var(base)
    sd = math.sqrt(var)
    med = median(base)
    mad = median([abs(x - med) for x in base])
    s = sorted(base)
    q1, q3 = quantile(s, 0.25), quantile(s, 0.75)
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    flo, fhi = q1 - 3.0 * (q3 - q1), q3 + 3.0 * (q3 - q1)
    flags, mild = [], []
    for i, x in enumerate(base):
        z = (x - m) / sd if sd > 0 else 0.0
        rz = 0.6745 * (x - med) / mad if mad > 0 else 0.0
        why = []
        if abs(z) >= a.threshold:
            why.append("z-score %+.1f" % z)
        if abs(rz) >= 3.5:
            why.append("robust z %+.1f" % rz)
        if x < flo or x > fhi:
            why.append("far outside the IQR fences")
        if why:
            flags.append({"label": labels[i], "value": vals[i], "z": z, "robust_z": rz, "reasons": why, "index": i})
        elif x < lo or x > hi:
            mild.append(labels[i])
    res = {"command": "anomaly", "n": n, "by": by, "detrended": a.detrend, "mean": m, "sd": sd, "median": med, "fences": [lo, hi], "flags": flags, "mild": mild, "notes": notes}
    L = ["ANOMALIES in %s (%d %s points%s)" % (a.value, n, by, ", trend removed" if a.detrend else "")]
    if flags:
        L.append("VERDICT: %d unusual point(s):" % len(flags))
        for f in sorted(flags, key=lambda f: -abs(f["z"]))[:15]:
            L.append("  %-14s %-12s %s" % (f["label"], g4(f["value"]), "; ".join(f["reasons"])))
    else:
        L.append("VERDICT: nothing unusual: no point is far outside the normal spread.")
    if mild:
        L.append("  %d more point(s) sit just outside the usual range (%s) but not far enough to call unusual." % (len(mild), ", ".join(mild[:4])))
    for n_ in notes:
        L.append("note: " + n_)
    L.append("CAUTION: a flag is a lead, not an error. Check the underlying rows (promotion, outage, holiday, typo) before acting.")
    if not a.detrend and n >= 8:
        o = ols(list(range(n)), vals)
        if o["p"] < 0.01 and o["r2"] > 0.5:
            L.append("CAUTION: the series has a strong trend, which inflates z-scores at the ends. Re-run with --detrend.")
    L.append("CAUTION: with %d points, about %.1f are flagged by chance at |z| >= %.1f even if nothing is wrong." % (n, n * 2 * D.norm_sf(a.threshold), a.threshold))
    return res, L


def _norm(x):
    return "".join(ch for ch in str(x).lower() if ch.isalnum())


def cmd_define(a):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "metrics.json")
    try:
        with open(path, encoding="utf-8") as f:
            metrics = json.load(f)["metrics"]
    except (OSError, ValueError, KeyError):
        raise UsageError("cannot read assets/metrics.json; read the file directly")
    if a.list or not a.name:
        return {"metrics": {k: v["name"] for k, v in metrics.items()}}, ["%-22s %s" % (k, v["name"]) for k, v in metrics.items()]
    want = _norm(a.name)
    hits = [k for k, v in metrics.items() if want == _norm(k) or want == _norm(v["name"])]
    if not hits:
        hits = [k for k, v in metrics.items() if want in _norm(k) or want in _norm(v["name"]) or any(want in _norm(x) or _norm(x) in want for x in v.get("aliases", []))]
    if not hits:
        raise UsageError("no metric matches '%s'. Run: stats.py define --list" % a.name)
    L, res = [], {}
    for k in hits[:4]:
        m = metrics[k]
        res[k] = m
        L += ["%s  [%s]" % (m["name"].upper(), k), "  formula:      " + m["formula"], "  denominator:  " + m["denominator"], "  unit:         " + m["unit"],
              "  mistakes:"] + ["    - " + x for x in m["mistakes"]] + ["  sanity range: " + m["sanity"], ""]
    return res, L[:-1]


# ------------------------------------------------------------------ CLI
def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="{compare,props,chi2,corr,trend,ci,power,anomaly,define}")

    def common(p, file=True):
        if file:
            p.add_argument("file", nargs="?", help="CSV, TSV, JSON, NDJSON or XLSX path, or - for stdin")
            p.add_argument("--sheet")
            p.add_argument("--date-order", choices=["dmy", "mdy"])
        p.add_argument("--alpha", type=float, default=0.05, help="significance level (default 0.05, giving 95%% intervals)")
        p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("compare", help="numeric values across groups")
    common(p)
    p.add_argument("--value")
    p.add_argument("--group")
    p.add_argument("--groups", help="only these groups, comma-separated")
    p.add_argument("--a-values", help="or give numbers directly: 1,2,3")
    p.add_argument("--b-values")
    p = sub.add_parser("props", help="two proportions (conversion, churn, ...)")
    common(p)
    p.add_argument("--group")
    p.add_argument("--success", help="yes/no or 1/0 column")
    p.add_argument("--success-value", help="the value that counts as success, if the column is not yes/no")
    p.add_argument("--groups")
    p.add_argument("--a", help="or give counts directly: 120/1000")
    p.add_argument("--b")
    p = sub.add_parser("chi2", help="are two categorical columns linked?")
    common(p)
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p = sub.add_parser("corr", help="correlation of two numeric columns")
    common(p)
    p.add_argument("--x", required=True)
    p.add_argument("--y", required=True)
    p = sub.add_parser("trend", help="is a metric trending up or down over time?")
    common(p)
    p.add_argument("--value", required=True, help="numeric column, or the word count")
    p.add_argument("--date", required=True)
    p.add_argument("--by", choices=["auto", "day", "week", "month", "quarter", "year"], default="auto")
    p.add_argument("--agg", choices=["sum", "mean", "count"], default="sum")
    p.add_argument("--keep-partial", action="store_true", help="keep incomplete first and last periods")
    p = sub.add_parser("ci", help="bootstrap confidence interval for a mean or median")
    common(p)
    p.add_argument("--value")
    p.add_argument("--values", help="or give numbers directly")
    p.add_argument("--stat", choices=["mean", "median"], default="mean")
    p.add_argument("--resamples", type=int, default=5000)
    p.add_argument("--seed", type=int, default=1)
    p = sub.add_parser("power", help="how many observations do I need?")
    common(p, file=False)
    p.add_argument("kind", choices=["props", "means"])
    p.add_argument("--p1", type=float, help="baseline rate, like 0.10")
    p.add_argument("--p2", type=float, help="target rate")
    p.add_argument("--lift", type=float, help="relative improvement to detect, 0.2 = +20%%")
    p.add_argument("--mde", type=float, help="absolute improvement to detect, 0.02 = 2 points")
    p.add_argument("--sd", type=float, help="standard deviation of the metric (means)")
    p.add_argument("--delta", type=float, help="smallest difference in means worth detecting")
    p.add_argument("--n", type=float, help="per-group sample you already have: shows what you can detect")
    p.add_argument("--power", type=float, default=0.8)
    p.add_argument("--daily", type=float, help="total units available per day, to turn n into days")
    p = sub.add_parser("define", help="look up a standard metric: formula, denominator, common mistakes, sanity range")
    common(p, file=False)
    p.add_argument("name", nargs="?", help="metric name or alias, like churn, nrr, cac")
    p.add_argument("--list", action="store_true", help="list all metrics")
    p = sub.add_parser("anomaly", help="flag unusual points in a series")
    common(p)
    p.add_argument("--value", required=True)
    p.add_argument("--date")
    p.add_argument("--by", choices=["auto", "day", "week", "month", "quarter", "year"], default="auto")
    p.add_argument("--agg", choices=["sum", "mean", "count"], default="sum")
    p.add_argument("--keep-partial", action="store_true")
    p.add_argument("--threshold", type=float, default=3.0, help="|z| cut-off (default 3)")
    p.add_argument("--detrend", action="store_true", help="remove a straight-line trend first")
    return ap


COMMANDS = {"compare": cmd_compare, "props": cmd_props, "chi2": cmd_chi2, "corr": cmd_corr, "trend": cmd_trend,
            "ci": cmd_ci, "power": cmd_power, "anomaly": cmd_anomaly, "define": cmd_define}


def main(argv=None):
    a = build_parser().parse_args(argv)
    if not 0 < a.alpha < 1:
        print("error: --alpha must be between 0 and 1", file=sys.stderr)
        return 2
    try:
        res, lines = COMMANDS[a.cmd](a)
    except (UsageError, dataio.DataError) as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    else:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
