#!/usr/bin/env python3
"""Clean, colour-blind-safe SVG charts from a spreadsheet, CSV or drivers.py output. Standard library only.

  python3 scripts/chart.py line   sales.csv --x order_date --y revenue --by month --out revenue.svg
  python3 scripts/chart.py line   sales.csv --x order_date --y revenue --group region --by month --out by_region.svg
  python3 scripts/chart.py bar    sales.csv --x product --y revenue --out products.svg
  python3 scripts/chart.py hbar   sales.csv --x channel --y revenue --top 8 --out channels.svg
  python3 scripts/chart.py scatter sales.csv --x units --y revenue --fit --out scatter.svg
  python3 scripts/chart.py hist   sales.csv --x revenue --out dist.svg
  python3 scripts/chart.py waterfall --from-drivers drivers.json --out bridge.svg
  python3 scripts/chart.py waterfall --steps "Jul=100,New=+20,Churn=-15" --out bridge.svg

One idea per chart. Titles state the takeaway (--title), a subtitle gives units and period, --source names the file.
Direct labels instead of legends, Okabe-Ito palette, zero-based bars, dates formatted by span, viewBox-responsive.
Without --out the SVG goes to stdout. Exit codes: 0 ok, 2 usage or data problem.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import datetime as dt

import _parse as P
import _svg as S
import dataio
import stats

GREY = S.GREY


class UsageError(Exception):
    pass


def load(a):
    try:
        return dataio.read_table(a.file, a.sheet, a.date_order)
    except dataio.DataError as ex:
        raise UsageError(str(ex))


def canvas(a, default_title, subtitle_default="", height=None, desc=""):
    src = a.source
    if src is None and getattr(a, "file", None) and a.file != "-":
        src = "Source: " + os.path.basename(a.file)
    return S.Canvas(a.width, height or a.height, a.title if a.title is not None else default_title,
                    a.subtitle if a.subtitle is not None else subtitle_default, src or "", desc)


def agg_value(vals, agg):
    if agg == "count":
        return float(len(vals))
    s = math.fsum(vals)
    return s / len(vals) if agg == "mean" else s


def ycol(t, name):
    if name == "count":
        return [1.0] * t.n_rows
    j = t.index(name)
    if t.infos[j]["type"] not in P.NUMERIC_TYPES:
        raise UsageError("column '%s' is %s, not a number. Use a numeric column, or the word count." % (t.header[j], t.infos[j]["type"]))
    return t.values(j)


def fmt_of(a):
    return dict(prefix=a.prefix, suffix=a.suffix, pct=a.pct)


def write(a, svg):
    if a.out and a.out != "-":
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(svg)
        print("wrote %s (%d bytes)" % (a.out, len(svg.encode("utf-8"))), file=sys.stderr)
    else:
        sys.stdout.write(svg)


def y_range(lo, hi, zero):
    if zero:
        lo = min(lo, 0.0)
        hi = max(hi, 0.0)
    ticks, step = S.nice_ticks(lo, hi, 5)
    return ticks, step


def draw_y_axis(c, ticks, step, x0, x1, ytop, ybot, fmt):
    unit = S.unit_for(max(abs(ticks[0]), abs(ticks[-1]))) if not fmt["pct"] else None
    lo, hi = ticks[0], ticks[-1]
    ys = lambda v: ybot - (v - lo) / (hi - lo) * (ybot - ytop)
    for v in ticks:
        c.line(x0, ys(v), x1, ys(v), cls="g" if v != 0 else "ax")
        c.text(x0 - 8, ys(v) + 4, S.fmt_num(v, step, unit=unit, **fmt), 11, "end", "m")
    return ys


def left_margin(ticks, step, fmt):
    unit = S.unit_for(max(abs(ticks[0]), abs(ticks[-1]))) if not fmt["pct"] else None
    return 16 + max(S.text_w(S.fmt_num(v, step, unit=unit, **fmt), 11) for v in ticks) + 8


# ------------------------------------------------------------------ line
def cmd_line(a):
    t = load(a)
    xj = t.index(a.x)
    xtype = t.infos[xj]["type"]
    ynames = [y.strip() for y in a.y.split(",") if y.strip()]
    if a.group and len(ynames) != 1:
        raise UsageError("--group works with a single --y")
    if xtype in ("date", "datetime"):
        if t.infos[xj]["date_order"] in ("ambiguous", "mixed"):
            raise UsageError("dates in '%s' are ambiguous; pass --date-order dmy or mdy" % t.header[xj])
        xs = [d.date() if isinstance(d, dt.datetime) else d for d in t.values(xj)]
        kind = "date"
        known = [d for d in xs if d is not None]
        if not known:
            raise UsageError("no usable dates in '%s'" % a.x)
        by = a.by
        if by == "auto":
            span = (max(known) - min(known)).days
            by = "month" if span >= 150 else "week" if span >= 35 else "day"
        keyf = lambda d: stats.period_start(d, by)
    elif xtype in P.NUMERIC_TYPES:
        xs, kind, by = t.values(xj), "num", None
        keyf = lambda v: v
    else:
        raw = t.raw(xj)
        xs, kind, by = [None if P.is_null(v) else v.strip() for v in raw], "cat", None
        keyf = lambda v: v
    groups = {}
    if a.group:
        gl = [P.norm_key(v) for v in t.raw(t.index(a.group))]
        canon = {}
        for k, v in zip(gl, t.raw(t.index(a.group))):
            canon.setdefault(k, v.strip())
        yv = ycol(t, ynames[0])
        buckets = {}
        for x, v, g in zip(xs, yv, gl):
            if x is None or v is None or not g:
                continue
            buckets.setdefault(canon[g], {}).setdefault(keyf(x), []).append(v)
        order = sorted(buckets, key=lambda k: -sum(abs(math.fsum(v)) for v in buckets[k].values()))
        if len(order) > 6 and not a.top_groups:
            raise UsageError("%d groups is too many for one line chart (max 6). Filter first or use --top-groups 5." % len(order))
        for k in order[:min(a.top_groups or 6, 6)]:
            groups[k] = {x: agg_value(v, a.agg) for x, v in buckets[k].items()}
    else:
        for name in ynames:
            yv = ycol(t, name)
            b = {}
            for x, v in zip(xs, yv):
                if x is None or v is None:
                    continue
                b.setdefault(keyf(x), []).append(v)
            groups[name] = {x: agg_value(v, a.agg) for x, v in b.items()}
    if not any(groups.values()):
        raise UsageError("nothing to draw: no rows with both x and y values")
    # drop an incomplete last period so the line does not fall off a cliff
    notes = []
    if kind == "date" and by in ("month", "week", "quarter") and not a.keep_partial:
        allx = sorted({x for g in groups.values() for x in g})
        if len(allx) > 3:
            slack = dt.timedelta(days=3 if by != "week" else 1)
            if min(known) > allx[0] + slack:
                for g in groups.values():
                    g.pop(allx[0], None)
                notes.append("first %s dropped (incomplete)" % by)
            if max(known) < stats.period_end(allx[-1], by) - slack:
                for g in groups.values():
                    g.pop(allx[-1], None)
                notes.append("last %s dropped (incomplete)" % by)
    allx = sorted({x for g in groups.values() for x in g}, key=lambda v: v)
    if kind == "cat":
        allx = list(dict.fromkeys(x for x in xs if x is not None and any(x in g for g in groups.values())))
    vals = [v for g in groups.values() for v in g.values()]
    fmt = fmt_of(a)
    zero = min(vals) >= 0 and (min(vals) / max(max(vals), 1e-12) < 0.5) and not a.tight
    ticks, step = y_range(min(vals), max(vals), zero)
    single = len(groups) == 1
    name0 = list(groups)[0]
    c = canvas(a, "%s by %s" % (a.y if not a.group else ynames[0], by or a.x),
               "%s%s" % ({"sum": "Total", "mean": "Average", "count": "Count"}[a.agg] if a.y != "count" else "Count", (" per " + by) if by else ""),
               desc="Line chart of %s" % ", ".join(groups))
    lm = left_margin(ticks, step, fmt)
    end_txt = [(S.fmt_num(list(g.values())[-1], step, **fmt) if single else "%s  %s" % (S.trunc(k, 22), S.fmt_num(list(g.values())[-1], step, **fmt)))
               for k, g in groups.items()]
    x0, x1 = lm, a.width - 16 - max(S.text_w(x, 12.5) for x in end_txt) - 26
    ytop, ybot = c.top + 6, a.height - c.bottom - 26
    ys = draw_y_axis(c, ticks, step, x0, x1, ytop, ybot, fmt)
    if kind == "date":
        d0, d1 = allx[0], allx[-1]
        span = max((d1 - d0).days, 1)
        xsc = lambda d: x0 + 12 + (d - d0).days / span * (x1 - x0 - 24)
        for d, lab in S.date_ticks(d0, d1, max(3, int((x1 - x0) / 80))):
            c.text(xsc(d), ybot + 20, lab, 11, "middle", "m")
    elif kind == "num":
        xt, xstep = S.nice_ticks(allx[0], allx[-1], 6)
        xsc = lambda v: x0 + 12 + (v - xt[0]) / (xt[-1] - xt[0]) * (x1 - x0 - 24)
        for v in xt:
            c.text(xsc(v), ybot + 20, S.fmt_num(v, xstep), 11, "middle", "m")
    else:
        idx = {x: i for i, x in enumerate(allx)}
        xsc = lambda v: x0 + 12 + idx[v] / max(len(allx) - 1, 1) * (x1 - x0 - 24)
        skip = max(1, math.ceil(len(allx) / max(3, int((x1 - x0) / 70))))
        for i, v in enumerate(allx):
            if i % skip == 0:
                c.text(xsc(v), ybot + 20, S.trunc(str(v), 12), 11, "middle", "m")
    ends = []
    for i, (name, g) in enumerate(groups.items()):
        pts = [(xsc(x), ys(g[x])) for x in allx if x in g]
        col = S.PALETTE[i % len(S.PALETTE)]
        if a.highlight and name != a.highlight:
            col = GREY
        c.polyline(pts, col, 2.6 if not (a.highlight and name != a.highlight) else 1.8)
        if len(pts) <= 40:
            for p in pts:
                c.circle(p[0], p[1], 2.8, col)
        c.circle(pts[-1][0], pts[-1][1], 4, col)
        ends.append((pts[-1][1], name, col, g[[x for x in allx if x in g][-1]]))
    if single:
        y, name, col, v = ends[0]
        c.text(xsc(allx[-1]) + 10, y + 4, S.fmt_num(v, step, **fmt), 13, "start", fill=col, weight="700")
    else:
        pos = S.spread_labels([(e[0], e[1]) for e in ends], 15, ytop + 4, ybot)
        for y, name, col, v in ends:
            c.text(xsc(allx[-1]) + 10, pos[name] + 4, "%s  %s" % (S.trunc(name, 22), S.fmt_num(v, step, **fmt)), 12, "start", fill=col, weight="600")
    for i, n in enumerate(notes):
        c.text(a.width - 16, a.height - 10 - 13 * i, n, 11, "end", "m")
    write(a, c.render())


# ------------------------------------------------------------------ bar / hbar
def bar_data(a, t):
    xj = t.index(a.x)
    typ = t.infos[xj]["type"]
    yv = ycol(t, a.y)
    order_by_x = False
    if typ in ("date", "datetime"):
        ds = [d.date() if isinstance(d, dt.datetime) else d for d in t.values(xj)]
        known = [d for d in ds if d is not None]
        by = a.by
        if by == "auto":
            span = (max(known) - min(known)).days
            by = "month" if span >= 150 else "week" if span >= 35 else "day"
        keys = [stats.period_start(d, by) if d else None for d in ds]
        order_by_x = True
        if by in ("month", "week", "quarter", "year"):
            ks = sorted({k for k in keys if k})
            slack = dt.timedelta(days=3 if by != "week" else 1)
            drop = set()
            if len(ks) > 3 and min(known) > ks[0] + slack:
                drop.add(ks[0])
            if len(ks) > 3 and max(known) < stats.period_end(ks[-1], by) - slack:
                drop.add(ks[-1])
            keys = [None if k in drop else k for k in keys]
    else:
        raw = t.raw(xj)
        canon = {}
        for v in raw:
            canon.setdefault(P.norm_key(v), v.strip())
        keys = [canon[P.norm_key(v)] if not P.is_null(v) else None for v in raw]
    b = {}
    for k, v in zip(keys, yv):
        if k is None or v is None:
            continue
        b.setdefault(k, []).append(v)
    items = [(k, agg_value(v, a.agg)) for k, v in b.items()]
    if not items:
        raise UsageError("nothing to draw: no rows with both x and y values")
    if order_by_x:
        items.sort(key=lambda kv: kv[0])
        items = [(k.strftime("%b %Y") if a.by in ("month", "auto") else k.isoformat(), v) for k, v in items]
    else:
        items.sort(key=lambda kv: -kv[1])
        if a.top and len(items) > a.top:
            rest = items[a.top:]
            items = items[:a.top]
            if a.agg != "mean":
                items.append(("%d others" % len(rest), math.fsum(v for _, v in rest)))
    return items


def cmd_bar(a, horizontal=False):
    t = load(a)
    items = bar_data(a, t)
    fmt = fmt_of(a)
    vals = [v for _, v in items]
    title = "%s by %s" % (a.y, a.x)
    sub = {"sum": "Total", "mean": "Average", "count": "Count"}[a.agg]
    if horizontal:
        h = max(a.height if a.height != 420 else 0, 90 + 30 * len(items))
        c = canvas(a, title, sub, height=h, desc="Horizontal bar chart of %s" % a.y)
        lab_w = min(200, max(S.text_w(S.trunc(k, 28), 12) for k, _ in items) + 10)
        ticks, step = y_range(min(vals), max(vals), True)
        unit = S.unit_for(max(abs(ticks[0]), abs(ticks[-1]))) if not a.pct else None
        vlab_w = max(S.text_w(S.fmt_num(v, **fmt), 12) for v in vals) + 12
        x0, x1 = 16 + lab_w, a.width - 16 - vlab_w
        lo, hi = ticks[0], ticks[-1]
        xs = lambda v: x0 + (v - lo) / (hi - lo) * (x1 - x0)
        row_h = (h - c.top - c.bottom - 6) / len(items)
        for i, (k, v) in enumerate(items):
            y = c.top + 4 + i * row_h
            col = S.PALETTE[0] if not a.highlight else (S.PALETTE[3] if k == a.highlight else GREY)
            c.text(x0 - 8, y + row_h / 2 + 4, S.trunc(k, 28), 12, "end", "t")
            xa, xb = sorted((xs(0), xs(v)))
            c.rect(xa, y + row_h * 0.15, xb - xa, row_h * 0.7, fill=col, rx=2)
            c.text(xb + 6 if v >= 0 else xa - 6, y + row_h / 2 + 4, S.fmt_num(v, **fmt), 12, "start" if v >= 0 else "end", "t", weight="600")
        c.line(xs(0), c.top, xs(0), h - c.bottom, cls="ax")
        write(a, c.render())
        return
    c = canvas(a, title, sub, desc="Bar chart of %s" % a.y)
    ticks, step = y_range(min(vals), max(vals), True)
    fm = fmt_of(a)
    lm = left_margin(ticks, step, fm)
    x0, x1 = lm, a.width - 16
    ytop, ybot = c.top + 16, a.height - c.bottom - 40
    ys = draw_y_axis(c, ticks, step, x0, x1, ytop, ybot, fm)
    slot = (x1 - x0) / len(items)
    bw = min(slot * 0.68, 70)
    wrap_w = max(6, int(slot / 6.4))
    for i, (k, v) in enumerate(items):
        cx = x0 + slot * (i + 0.5)
        col = S.PALETTE[0] if not a.highlight else (S.PALETTE[3] if k == a.highlight else GREY)
        y0, y1 = sorted((ys(0), ys(v)))
        c.rect(cx - bw / 2, y0, bw, y1 - y0, fill=col, rx=2)
        if slot >= 46 or i == len(items) - 1 or v in (max(vals), min(vals)):
            c.text(cx, (y0 - 6) if v >= 0 else (y1 + 15), S.fmt_num(v, **fm), 12, "middle", "t", weight="600")
        for li, line in enumerate(S.wrap(k, wrap_w, 2)):
            c.text(cx, ybot + 18 + li * 14, line, 11, "middle", "m")
    write(a, c.render())


# ------------------------------------------------------------------ scatter
def cmd_scatter(a):
    t = load(a)
    xj, yj = t.index(a.x), t.index(a.y)
    for j in (xj, yj):
        if t.infos[j]["type"] not in P.NUMERIC_TYPES:
            raise UsageError("column '%s' is %s, not a number" % (t.header[j], t.infos[j]["type"]))
    xv, yv = t.values(xj), t.values(yj)
    grp = [None] * t.n_rows
    if a.color:
        raw = t.raw(t.index(a.color))
        canon = {}
        for v in raw:
            canon.setdefault(P.norm_key(v), v.strip())
        grp = [canon[P.norm_key(v)] if not P.is_null(v) else None for v in raw]
    pts = [(x, y, g) for x, y, g in zip(xv, yv, grp) if x is not None and y is not None]
    if len(pts) < 3:
        raise UsageError("need at least 3 rows with both values")
    sampled = len(pts) > 1500
    plot = pts[::math.ceil(len(pts) / 1500)] if sampled else pts
    fmt = fmt_of(a)
    c = canvas(a, "%s vs %s" % (a.y, a.x), "Each dot is one row%s" % (" (%d of %d shown)" % (len(plot), len(pts)) if sampled else ""),
               desc="Scatter plot of %s against %s" % (a.y, a.x))
    xt, xstep = S.nice_ticks(min(p[0] for p in pts), max(p[0] for p in pts), 6)
    yt, ystep = S.nice_ticks(min(p[1] for p in pts), max(p[1] for p in pts), 5)
    lm = left_margin(yt, ystep, dict(prefix="", suffix="", pct=False))
    x0, x1 = lm, a.width - 24 - (90 if a.color else 0)
    ytop, ybot = c.top + 12, a.height - c.bottom - 40
    ys = draw_y_axis(c, yt, ystep, x0, x1, ytop, ybot, dict(prefix="", suffix="", pct=False))
    xs = lambda v: x0 + (v - xt[0]) / (xt[-1] - xt[0]) * (x1 - x0)
    xunit = S.unit_for(max(abs(xt[0]), abs(xt[-1])))
    for v in xt:
        c.text(xs(v), ybot + 18, S.fmt_num(v, xstep, unit=xunit), 11, "middle", "m")
    c.text((x0 + x1) / 2, ybot + 34, a.x, 12, "middle", "m")
    c.text(x0, ytop - 6, a.y, 12, "start", "m")
    names = list(dict.fromkeys(g for _, _, g in plot if g is not None))[:6]
    for x, y, g in plot:
        col = S.PALETTE[names.index(g) % 7] if g in names else (GREY if names else S.PALETTE[0])
        c.circle(xs(x), ys(y), 3.2, col, 0.55)
    if a.fit:
        r = stats.ols([p[0] for p in pts], [p[1] for p in pts])
        xa, xb = min(p[0] for p in pts), max(p[0] for p in pts)
        c.line(xs(xa), ys(r["intercept"] + r["slope"] * xa), xs(xb), ys(r["intercept"] + r["slope"] * xb), stroke="#1f2328", width=2, dash="6 4")
        rr = stats.pearson([p[0] for p in pts], [p[1] for p in pts])["r"]
        c.text(x1, ytop + 12, "r = %.2f (n = %d)" % (rr, len(pts)), 12, "end", "t", weight="600", halo=True)
    if names:
        want = []
        for i, n in enumerate(names):
            gp = [(xs(x), ys(y)) for x, y, g in plot if g == n]
            want.append((sum(p[1] for p in gp) / len(gp), n))
        pos = S.spread_labels(want, 15, ytop + 10, ybot - 4)
        for i, n in enumerate(names):
            c.text(x1 + 10, pos[n] + 4, S.trunc(n, 14), 12, "start", fill=S.PALETTE[i % 7], weight="700")
    write(a, c.render())


# ------------------------------------------------------------------ histogram
def cmd_hist(a):
    t = load(a)
    j = t.index(a.x)
    if t.infos[j]["type"] not in P.NUMERIC_TYPES:
        raise UsageError("column '%s' is %s, not a number" % (t.header[j], t.infos[j]["type"]))
    vals = sorted(v for v in t.values(j) if v is not None)
    if len(vals) < 5:
        raise UsageError("need at least 5 values for a histogram")
    n = len(vals)
    q1, q3 = vals[n // 4], vals[(3 * n) // 4]
    lo, hi = vals[0], vals[-1]
    if a.bins:
        nb = a.bins
    else:
        w = 2 * (q3 - q1) * n ** (-1 / 3)
        nb = int(min(40, max(6, math.ceil((hi - lo) / w)))) if w > 0 else 10
    ticks, step = S.nice_ticks(lo, hi, nb + 1)
    step = (ticks[-1] - ticks[0]) / nb if a.bins else step
    edges = [ticks[0] + i * step for i in range((int(round((ticks[-1] - ticks[0]) / step))) + 1)]
    counts = [0] * (len(edges) - 1)
    for v in vals:
        k = min(int((v - edges[0]) / step), len(counts) - 1)
        counts[k] += 1
    fmt = fmt_of(a)
    c = canvas(a, "Distribution of %s" % a.x, "Number of rows in each range (n = %d)" % n, desc="Histogram of %s" % a.x)
    yt, ystep = y_range(0, max(counts), True)
    lm = left_margin(yt, ystep, dict(prefix="", suffix="", pct=False))
    x0, x1 = lm, a.width - 24
    ytop, ybot = c.top + 12, a.height - c.bottom - 30
    ys = draw_y_axis(c, yt, ystep, x0, x1, ytop, ybot, dict(prefix="", suffix="", pct=False))
    xs = lambda v: x0 + (v - edges[0]) / (edges[-1] - edges[0]) * (x1 - x0)
    for i, cnt in enumerate(counts):
        c.rect(xs(edges[i]) + 0.5, ys(cnt), xs(edges[i + 1]) - xs(edges[i]) - 1, ybot - ys(cnt), fill=S.PALETTE[0], rx=1)
    unit = S.unit_for(max(abs(edges[0]), abs(edges[-1])))
    every = max(1, math.ceil(len(edges) / max(3, int((x1 - x0) / 60))))
    for i, e in enumerate(edges):
        if i % every == 0:
            c.text(xs(e), ybot + 18, S.fmt_num(e, step, unit=unit, **fmt), 11, "middle", "m")
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    c.line(xs(med), ytop, xs(med), ybot, stroke="#D55E00", width=2, dash="5 4")
    c.text(xs(med) + 6, ytop + 10, "median %s" % S.fmt_num(med, **fmt), 12, "start", fill="#D55E00", weight="700", halo=True)
    write(a, c.render())


# ------------------------------------------------------------------ waterfall
def parse_steps(spec):
    items = []
    for part in spec.split(","):
        if "=" not in part:
            raise UsageError("--steps items look like Label=+5 (first item is the starting total)")
        k, v = part.rsplit("=", 1)
        try:
            items.append((k.strip(), float(v)))
        except ValueError:
            raise UsageError("cannot read the number in '%s'" % part)
    if len(items) < 2:
        raise UsageError("--steps needs a starting total and at least one step")
    return items


def cmd_waterfall(a):
    if a.from_drivers:
        try:
            with open(a.from_drivers, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            raise UsageError("cannot read drivers JSON: %s" % a.from_drivers)
        br = d.get("bridge") if "bridge" in d else d
        if not br or "steps" not in br:
            raise UsageError("that JSON has no bridge (run drivers.py --json on a file with at least one dimension)")
        start, end = br["start"], br["end"]
        labels = [(br["start_label"], start, "total")] + [(s["label"], s["value"], "step") for s in br["steps"]] + [(br["end_label"], end, "total")]
        title = "What moved %s: %s to %s" % (br["metric"], br["start_label"], br["end_label"])
        sub = "Change by %s" % br["dimension"]
        pct = br.get("pct", False) or a.pct
        src = a.source if a.source is not None else "Source: drivers.py"
    else:
        if not a.steps:
            raise UsageError("give --from-drivers FILE.json or --steps \"Start=100,Up=+20,Down=-15\"")
        it = parse_steps(a.steps)
        start = it[0][1]
        end = start + sum(v for _, v in it[1:])
        labels = [(it[0][0], start, "total")] + [(k, v, "step") for k, v in it[1:]] + [("End", end, "total")]
        title, sub, pct, src = "Bridge from %s" % it[0][0], "", a.pct, a.source
    a.source = src
    fmt = dict(prefix=a.prefix, suffix=a.suffix, pct=pct)
    c = canvas(a, a.title if a.title is not None else title, sub, desc="Waterfall chart")
    levels, cur = [], start
    for lab, v, kind in labels:
        if kind == "total":
            levels.append((lab, 0.0, v, kind, v))
            cur = v
        else:
            levels.append((lab, cur, cur + v, kind, v))
            cur += v
    hi = max(max(l[1], l[2]) for l in levels)
    lo = min((min(l[1], l[2]) if l[3] == "step" else l[2]) for l in levels)
    biggest = max(abs(v) for _, v, k in labels if k == "step") if len(labels) > 2 else 0
    truncated = start > 0 and end > 0 and biggest < 0.12 * min(start, end) and lo > 0
    base = 0.0
    if truncated:
        base = max(0.0, min(lo, start, end) - 2.5 * biggest)
        ticks, step = S.nice_ticks(base, hi, 5)
        base = ticks[0]
        c.subtitle = (c.subtitle + " " if c.subtitle else "") + "(axis starts at %s, not zero)" % S.fmt_num(base, step, **fmt)
    else:
        ticks, step = y_range(min(lo, 0), hi, True)
    lm = left_margin(ticks, step, fmt)
    x0, x1 = lm, a.width - 16
    ytop, ybot = c.top + 22, a.height - c.bottom - 44
    ys = draw_y_axis(c, ticks, step, x0, x1, ytop, ybot, fmt)
    slot = (x1 - x0) / len(levels)
    bw = min(slot * 0.7, 74)
    wrap_w = max(6, int(slot / 6.4))
    prev_level = start
    for i, (lab, a0, a1, kind, v) in enumerate(levels):
        cx = x0 + slot * (i + 0.5)
        ya, yb = sorted((ys(max(a0, ticks[0])), ys(max(a1, ticks[0]))))
        if kind == "total":
            c.rect(cx - bw / 2, ya, bw, yb - ya, cls="tot", rx=2)
            c.text(cx, ya - 7, S.fmt_num(v, **fmt), 12, "middle", "t", weight="700")
        else:
            col = "#0072B2" if v >= 0 else "#D55E00"
            c.rect(cx - bw / 2, ya, bw, max(yb - ya, 1.5), fill=col, rx=2)
            sign = "+" if v > 0 else ("−" if v < 0 else "")
            if pct:
                r = round(abs(v) * 100, 2)
                txt = ("" if r == 0 else sign) + "%.2f pts" % r
            else:
                txt = sign + S.fmt_num(abs(v), **fmt)
            c.text(cx, (ya - 7) if v >= 0 else (yb + 15), txt, 12, "middle", "t", weight="600")
        if i > 0:
            c.line(x0 + slot * (i - 0.5) + bw / 2, ys(prev_level), cx - bw / 2, ys(prev_level), cls="conn")
        prev_level = v if kind == "total" else a1
        for li, line in enumerate(S.wrap(lab, wrap_w, 2)):
            c.text(cx, ybot + 18 + li * 14, line, 11, "middle", "m")
    write(a, c.render())


# ------------------------------------------------------------------ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="{line,bar,hbar,scatter,hist,waterfall}")

    def common(p, file=True):
        if file:
            p.add_argument("file", nargs="?", help="CSV, TSV, JSON, NDJSON or XLSX path")
            p.add_argument("--sheet")
            p.add_argument("--date-order", choices=["dmy", "mdy"])
        p.add_argument("--out", help="SVG file to write (default: stdout)")
        p.add_argument("--title", help="say the takeaway, not the column names")
        p.add_argument("--subtitle", help="units and period")
        p.add_argument("--source", help="source line (default: the file name)")
        p.add_argument("--width", type=int, default=720)
        p.add_argument("--height", type=int, default=420)
        p.add_argument("--prefix", default="", help="text before numbers, like $")
        p.add_argument("--suffix", default="", help="text after numbers, like %%")
        p.add_argument("--pct", action="store_true", help="show values 0.12 as 12%%")
        p.add_argument("--highlight", help="series or bar to emphasise; the rest turn grey")

    p = sub.add_parser("line", help="values over time or over an ordered axis")
    common(p)
    p.add_argument("--x", required=True)
    p.add_argument("--y", required=True, help="numeric column(s), comma-separated, or the word count")
    p.add_argument("--group", help="one line per value of this column (max 6)")
    p.add_argument("--top-groups", type=int, help="keep only the N biggest groups")
    p.add_argument("--by", choices=["auto", "day", "week", "month", "quarter", "year"], default="auto")
    p.add_argument("--agg", choices=["sum", "mean", "count"], default="sum")
    p.add_argument("--keep-partial", action="store_true")
    p.add_argument("--tight", action="store_true", help="do not force the y axis to include zero")
    for name, hlp in (("bar", "compare categories"), ("hbar", "compare many or long-named categories")):
        p = sub.add_parser(name, help=hlp)
        common(p)
        p.add_argument("--x", required=True, help="category column (or a date column, then --by)")
        p.add_argument("--y", required=True, help="numeric column or the word count")
        p.add_argument("--agg", choices=["sum", "mean", "count"], default="sum")
        p.add_argument("--by", choices=["auto", "day", "week", "month", "quarter", "year"], default="auto")
        p.add_argument("--top", type=int, default=12, help="show the top N, lump the rest (default 12)")
    p = sub.add_parser("scatter", help="relationship between two numeric columns")
    common(p)
    p.add_argument("--x", required=True)
    p.add_argument("--y", required=True)
    p.add_argument("--color", help="category column to colour by (max 6 shown)")
    p.add_argument("--fit", action="store_true", help="add a straight-line fit and r")
    p = sub.add_parser("hist", help="distribution of one numeric column")
    common(p)
    p.add_argument("--x", required=True)
    p.add_argument("--bins", type=int)
    p = sub.add_parser("waterfall", help="bridge from A to B (feeds on drivers.py --json)")
    common(p, file=False)
    p.add_argument("--from-drivers", metavar="FILE.json")
    p.add_argument("--steps", help='hand-made bridge: "Start=100,Up=+20,Down=-15"')
    a = ap.parse_args(argv)
    if not hasattr(a, "file"):
        a.file = None
    try:
        if a.cmd in ("line", "bar", "hbar", "scatter", "hist") and not a.file:
            raise UsageError("give a data file")
        {"line": cmd_line, "bar": cmd_bar, "hbar": lambda x: cmd_bar(x, True), "scatter": cmd_scatter, "hist": cmd_hist, "waterfall": cmd_waterfall}[a.cmd](a)
    except (UsageError, dataio.DataError, stats.UsageError) as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 2
    except OSError as ex:
        print("error: cannot write output (%s)" % ex.strerror, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
