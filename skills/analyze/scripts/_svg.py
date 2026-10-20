"""Internal: tiny SVG toolkit for chart.py (ticks, number and date formatting, canvas). Not a command."""
from __future__ import annotations

import datetime as dt
import math
import re
from xml.sax.saxutils import escape

# Okabe-Ito colour-blind-safe palette (yellow last: weak contrast on white)
PALETTE = ["#0072B2", "#E69F00", "#009E73", "#D55E00", "#56B4E9", "#CC79A7", "#B8A800"]
GREY = "#9aa0a6"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

CSS = """
.cz-bg{fill:#fff}.cz-t{fill:#1f2328}.cz-m{fill:#59636e}.cz-g{stroke:#e6e8eb;stroke-width:1}.cz-ax{stroke:#9aa0a6;stroke-width:1}
.cz-tot{fill:#59636e}.cz-conn{stroke:#9aa0a6;stroke-width:1;stroke-dasharray:3 3}.cz-halo{stroke:#fff;stroke-width:3;paint-order:stroke;fill:none}
text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
@media (prefers-color-scheme: dark){.cz-bg{fill:#14161a}.cz-t{fill:#eceff1}.cz-m{fill:#a6adb4}.cz-g{stroke:#2b3037}.cz-ax{stroke:#6b737b}.cz-tot{fill:#a6adb4}.cz-conn{stroke:#6b737b}.cz-halo{stroke:#14161a}}
"""


def text_w(s: str, size: float) -> float:
    return len(s) * size * 0.56


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[:max(1, n - 1)] + "…"


def wrap(s: str, width: int, lines: int = 2):
    words, out, cur = s.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            out.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = trunc(out[-1] + "…", width)
    return [trunc(x, width) for x in out]


def nice_num(x: float, rnd: bool) -> float:
    if x <= 0:
        return 1.0
    e = math.floor(math.log10(x))
    f = x / 10 ** e
    if rnd:
        nf = 1 if f < 1.5 else 2 if f < 3 else 5 if f < 7 else 10
    else:
        nf = 1 if f <= 1 else 2 if f <= 2 else 5 if f <= 5 else 10
    return nf * 10 ** e


def nice_ticks(lo: float, hi: float, target: int = 5):
    """Round tick values covering [lo, hi]. Returns (ticks, step)."""
    if hi == lo:
        hi = lo + (abs(lo) * 0.1 if lo else 1.0)
    step = nice_num((hi - lo) / max(target - 1, 1), True)
    start = math.floor(lo / step + 1e-9) * step
    end = math.ceil(hi / step - 1e-9) * step
    n = int(round((end - start) / step))
    return [round(start + i * step, 12) for i in range(n + 1)], step


def unit_for(maxabs: float):
    """One K/M/B unit for a whole axis so its labels read 0, 5K, 10K instead of 5,000, 10K."""
    for div, sym, lo in ((1e9, "B", 1e9), (1e6, "M", 1e6), (1e3, "K", 1e4)):
        if maxabs >= lo:
            return (div, sym)
    return (1.0, "")


def _decimals(step):
    for d in range(0, 5):
        if abs(step * 10 ** d - round(step * 10 ** d)) < 1e-9 * max(1.0, step * 10 ** d):
            return d
    return 4


def fmt_num(v: float, step=None, prefix="", suffix="", pct=False, unit=None) -> str:
    """Readable number. step = axis tick step (fixes decimals); unit = (divisor, symbol) shared by an axis."""
    if pct:
        if step is None:
            d = 0 if abs(v) * 100 >= 100 else (1 if abs(v) * 100 >= 10 else 2)
        else:
            d = 0 if step * 100 >= 1 - 1e-9 else _decimals(step * 100)
        r = round(abs(v) * 100, d)
        txt = "%.*f" % (d, r)
        if step is None and "." in txt:
            txt = txt.rstrip("0").rstrip(".")
        return "%s%s%%" % ("-" if v < 0 and r != 0 else "", txt)
    a = abs(v)
    sign = "-" if v < 0 and a != 0 else ""
    if a == 0:
        return prefix + "0" + suffix
    div, sym = unit if unit else unit_for(a)
    x = a / div
    if div != 1.0:
        d = _decimals(step / div) if step else (1 if x < 100 else 0)
        s = "%.*f" % (d, x)
        if "." in s and not step:
            s = s.rstrip("0").rstrip(".")
        return sign + prefix + s + sym + suffix
    d = _decimals(step) if step else (0 if a == int(a) or a >= 100 else (1 if a >= 10 else 2))
    return sign + prefix + format(a, ",.%df" % d) + suffix


def date_ticks(d0: dt.date, d1: dt.date, max_ticks: int = 8):
    """Ticks for a date axis, formatted by span. Returns [(date, label)]."""
    span = (d1 - d0).days
    out = []
    if span <= 21:
        step = 1 if span <= max_ticks else 2 if span <= 2 * max_ticks else 7
        d = d0
        while d <= d1:
            out.append((d, "%s %d" % (MONTHS[d.month - 1], d.day)))
            d += dt.timedelta(days=step)
    elif span <= 100:
        d = d0 + dt.timedelta(days=(7 - d0.weekday()) % 7)
        step = 7 if span / 7 <= max_ticks else 14
        while d <= d1:
            out.append((d, "%s %d" % (MONTHS[d.month - 1], d.day)))
            d += dt.timedelta(days=step)
    elif span <= 1100:
        months = max(1, math.ceil((span / 30.4) / max_ticks))
        months = 1 if months == 1 else 2 if months == 2 else 3 if months <= 3 else 6 if months <= 6 else 12
        d = dt.date(d0.year, d0.month, 1)
        if d < d0:
            d = dt.date(d.year + (d.month // 12), d.month % 12 + 1, 1)
        first = True
        while d <= d1:
            if d.month % months == (1 % months) or months == 1:
                lab = MONTHS[d.month - 1]
                if first or d.month == 1:
                    lab += " %d" % d.year
                out.append((d, lab))
                first = False
            d = dt.date(d.year + (d.month // 12), d.month % 12 + 1, 1)
    else:
        years = max(1, math.ceil((span / 365.25) / max_ticks))
        years = 1 if years == 1 else 2 if years == 2 else 5 if years <= 5 else 10
        y = d0.year + (0 if dt.date(d0.year, 1, 1) >= d0 else 1)
        while dt.date(y, 1, 1) <= d1:
            if y % years == 0 or years == 1:
                out.append((dt.date(y, 1, 1), str(y)))
            y += 1
    if not out:
        out = [(d0, "%s %d" % (MONTHS[d0.month - 1], d0.day)), (d1, "%s %d" % (MONTHS[d1.month - 1], d1.day))]
    return out


class Canvas:
    def __init__(self, width=720, height=420, title="", subtitle="", source="", desc=""):
        self.w, self.h = width, height
        self.title, self.subtitle, self.source, self.desc = title, subtitle, source, desc
        self.el = []
        self.top = 22
        if title:
            self.top += 26
        if subtitle:
            self.top += 20
        self.top += 12
        self.bottom = 16 + (18 if source else 0)

    # primitives -----------------------------------------------------
    def text(self, x, y, s, size=12, anchor="start", cls="t", weight=None, fill=None, halo=False, dy=None):
        attrs = 'x="%.1f" y="%.1f" font-size="%s" text-anchor="%s"' % (x, y, size, anchor)
        if weight:
            attrs += ' font-weight="%s"' % weight
        if dy is not None:
            attrs += ' dy="%s"' % dy
        col = ('fill="%s"' % fill) if fill else ('class="%s"' % cls)
        if halo:
            self.el.append('<text %s class="halo">%s</text>' % (attrs, escape(s)))
        self.el.append('<text %s %s>%s</text>' % (attrs, col, escape(s)))

    def line(self, x1, y1, x2, y2, cls="g", stroke=None, width=None, dash=None):
        a = ('stroke="%s"' % stroke) if stroke else ('class="%s"' % cls)
        if width:
            a += ' stroke-width="%s"' % width
        if dash:
            a += ' stroke-dasharray="%s"' % dash
        self.el.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" %s/>' % (x1, y1, x2, y2, a))

    def rect(self, x, y, w, h, fill=None, cls=None, rx=2, opacity=None):
        a = ('fill="%s"' % fill) if fill else ('class="%s"' % cls)
        if opacity:
            a += ' fill-opacity="%s"' % opacity
        self.el.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="%s" %s/>' % (x, y, max(w, 0), max(h, 0), rx, a))

    def circle(self, x, y, r, fill, opacity=None):
        self.el.append('<circle cx="%.1f" cy="%.1f" r="%s" fill="%s"%s/>' % (x, y, r, fill, (' fill-opacity="%s"' % opacity) if opacity else ""))

    def polyline(self, pts, stroke, width=2.5):
        self.el.append('<polyline fill="none" stroke="%s" stroke-width="%s" stroke-linejoin="round" stroke-linecap="round" points="%s"/>' % (
            stroke, width, " ".join("%.1f,%.1f" % p for p in pts)))

    # frame ------------------------------------------------------------
    def render(self):
        head = []
        y = 30
        if self.title:
            head.append('<text x="16" y="%d" font-size="18" font-weight="700" class="t">%s</text>' % (y, escape(self.title)))
            y += 22
        if self.subtitle:
            head.append('<text x="16" y="%d" font-size="13" class="m">%s</text>' % (y, escape(self.subtitle)))
        foot = ""
        if self.source:
            foot = '<text x="16" y="%d" font-size="11" class="m">%s</text>' % (self.h - 10, escape(self.source))
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" role="img" aria-label="%s" '
                'style="width:100%%;max-width:%dpx;height:auto;display:block">\n<title>%s</title><desc>%s</desc>\n<style>%s</style>\n'
                '<rect class="bg" x="0" y="0" width="%d" height="%d"/>\n%s\n%s\n%s\n</svg>\n') % (
            self.w, self.h, escape(self.title or "chart", {'"': "&quot;"}), self.w, escape(self.title or "chart"), escape(self.desc),
            CSS, self.w, self.h, "\n".join(head), "\n".join(self.el), foot)
        return re.sub(r'class="(bg|t|m|g|ax|tot|conn|halo)"', r'class="cz-\1"', svg)


def spread_labels(items, min_gap=14.0, lo=0.0, hi=1e9):
    """items: [(y, key)] -> {key: y} nudged so labels do not overlap."""
    order = sorted(items)
    ys = [y for y, _ in order]
    for i in range(1, len(ys)):
        if ys[i] - ys[i - 1] < min_gap:
            ys[i] = ys[i - 1] + min_gap
    over = ys[-1] - hi if ys else 0
    if over > 0:
        ys = [y - over for y in ys]
        for i in range(len(ys) - 2, -1, -1):
            if ys[i + 1] - ys[i] < min_gap:
                ys[i] = ys[i + 1] - min_gap
    return {k: max(lo, y) for y, (_, k) in zip(ys, order)}
