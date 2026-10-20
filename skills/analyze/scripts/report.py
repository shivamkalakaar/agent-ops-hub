#!/usr/bin/env python3
"""Build one self-contained, print-friendly HTML report from a small JSON file. Standard library only.

  python3 scripts/report.py --example > report.json      # a filled-in starting point
  python3 scripts/report.py report.json --check           # validate without writing
  python3 scripts/report.py report.json --out report.html

The page leads with the headline finding, states confidence (high, medium or low, always with the reason),
then KPI cards, sections with charts (SVG files from chart.py are inlined) and tables, the method, what could
make it wrong (caveats, data-quality notes) and the next questions. No scripts, no web fonts, works offline,
follows the reader's light or dark setting, prints cleanly. Fields are documented in assets/report.schema.json.
Exit codes: 0 ok, 1 problems found by --check, 2 usage or file problem.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys

LEVELS = ("high", "medium", "low")
NUM_RE = re.compile(r"^[\s$€£¥+\-−(]*[\d.,]+\s*[%KMBkmb)]*\s*(pts?|pp)?$")

EXAMPLE = {
    "title": "Why August revenue fell",
    "subtitle": "Orders export, July vs August 2026",
    "date": "2026-09-29",
    "headline": "Revenue fell 17% in August, and 85% of the drop is Paid Search, almost all of it in EMEA.",
    "confidence": {"level": "high", "reason": "The gap is 3 standard deviations outside normal month-to-month movement and the pattern repeats in the drill-down."},
    "kpis": [
        {"label": "Revenue, Aug", "value": "$54.8K", "delta": "-17.3% vs Jul", "direction": "down"},
        {"label": "Orders, Aug", "value": "254", "delta": "-12.7% vs Jul", "direction": "down"},
        {"label": "EMEA Paid Search", "value": "-$8.1K", "delta": "83% of the drop", "direction": "down"}
    ],
    "sections": [
        {"heading": "What changed", "text": "Paid Search revenue fell from $24.3K to $14.6K. **Inside it, EMEA holds 83% of the change.** Every other channel moved by less than normal wobble.",
         "chart": {"svg_file": "bridge.svg", "alt": "Waterfall from July to August revenue by channel", "caption": "Revenue bridge by channel, USD"},
         "table": {"caption": "Change by channel", "columns": ["Channel", "Jul", "Aug", "Change", "Share"],
                   "rows": [["Paid Search", "24,312", "14,553", "-9,758", "85%"], ["Email", "10,702", "8,505", "-2,197", "19%"]]}}
    ],
    "method": "drivers.py exact breakdown by segment; history check against 18 earlier month-to-month changes. Orders export, 5,760 rows, no rows dropped.",
    "caveats": ["Revenue in the export is order value before refunds.", "One month of change; it may not persist."],
    "data_quality": ["1% of region labels differed by case or spacing and were merged before grouping."],
    "next_questions": ["Did EMEA Paid Search spend change in August?", "Are the missing orders leads that never converted, or a tracking problem?"],
    "sources": ["orders.csv (5,760 rows)"]
}


def esc(x):
    return html.escape("" if x is None else str(x), quote=True)


def rich(text):
    """Escape, then allow **bold** and blank-line paragraphs."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", str(text)) if p.strip()]
    out = []
    for p in paras:
        e = esc(p).replace("\n", " ")
        e = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", e)
        out.append("<p>%s</p>" % e)
    return "\n".join(out)


def check(doc):
    """Return a list of problems (empty when the document is fine)."""
    bad = []
    if not isinstance(doc, dict):
        return ["the JSON must be an object"]
    if not str(doc.get("title", "")).strip():
        bad.append("title is missing")
    if not str(doc.get("headline", "")).strip():
        bad.append("headline is missing: state the finding in one sentence")
    c = doc.get("confidence")
    if not isinstance(c, dict) or c.get("level") not in LEVELS:
        bad.append("confidence.level must be one of high, medium, low")
    elif not str(c.get("reason", "")).strip():
        bad.append("confidence.reason is missing: say why you are that confident")
    for i, k in enumerate(doc.get("kpis", []) or []):
        if not isinstance(k, dict) or "label" not in k or "value" not in k:
            bad.append("kpis[%d] needs label and value" % i)
    for i, s in enumerate(doc.get("sections", []) or []):
        if not isinstance(s, dict) or not (s.get("heading") or s.get("text") or s.get("chart") or s.get("table")):
            bad.append("sections[%d] is empty" % i)
            continue
        t = s.get("table")
        if t:
            w = len(t.get("columns", []))
            if not w or any(len(r) != w for r in t.get("rows", [])):
                bad.append("sections[%d].table: every row needs %d cells (one per column)" % (i, w))
        ch = s.get("chart")
        if ch and not (ch.get("svg") or ch.get("svg_file")):
            bad.append("sections[%d].chart needs svg_file or svg" % i)
    if not doc.get("caveats") and not doc.get("data_quality"):
        bad.append("add at least one caveat or data-quality note: what could make this wrong?")
    return bad


def load_svg(ch, base):
    if ch.get("svg"):
        raw = ch["svg"]
    else:
        path = ch["svg_file"] if os.path.isabs(ch["svg_file"]) else os.path.join(base, ch["svg_file"])
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            raise ValueError("cannot read chart file %s" % ch["svg_file"])
    raw = re.sub(r"^\s*<\?xml[^>]*\?>", "", raw).strip()
    if not raw.startswith("<svg"):
        raise ValueError("chart %s is not an SVG" % (ch.get("svg_file") or "(inline)"))
    if re.search(r"<script|onload=|onerror=|javascript:", raw, re.I):
        raise ValueError("chart %s contains scripts and was refused" % (ch.get("svg_file") or "(inline)"))
    return raw


def render_table(t):
    cols = t.get("columns", [])
    aligns = t.get("align") or []
    rows = t.get("rows", [])
    emph = set(t.get("emphasis", []) or [])
    numeric = []
    for j in range(len(cols)):
        vals = [str(r[j]) for r in rows if str(r[j]).strip()]
        numeric.append(bool(vals) and all(NUM_RE.match(v) for v in vals))
    def al(j):
        return aligns[j] if j < len(aligns) and aligns[j] in ("left", "right", "center") else ("right" if numeric[j] else "left")
    out = ['<div class="tablewrap"><table>']
    if t.get("caption"):
        out.append("<caption>%s</caption>" % esc(t["caption"]))
    out.append("<thead><tr>%s</tr></thead><tbody>" % "".join('<th scope="col" style="text-align:%s">%s</th>' % (al(j), esc(c)) for j, c in enumerate(cols)))
    for i, r in enumerate(rows):
        out.append("<tr%s>%s</tr>" % (' class="em"' if i in emph else "", "".join('<td style="text-align:%s">%s</td>' % (al(j), esc(v)) for j, v in enumerate(r))))
    out.append("</tbody></table></div>")
    return "".join(out)


ARROW = {"up": "▲", "down": "▼", "flat": "■"}

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1f2328;--mut:#59636e;--line:#d8dce1;--acc:#0072B2;--hi:#00795a;--mid:#8a5a00;--lo:#b23c00;--hib:#e3f4ee;--midb:#fbf0d9;--lob:#fbe6dc}
@media (prefers-color-scheme: dark){:root{--bg:#101215;--card:#181b20;--fg:#eceff1;--mut:#a6adb4;--line:#2d3138;--acc:#56B4E9;--hi:#5fd3ad;--mid:#f0c060;--lo:#ff9d6b;--hib:#12332a;--midb:#3a2f12;--lob:#3d2116}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:880px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:1.9rem;line-height:1.2;margin:0 0 4px}
h2{font-size:1.2rem;margin:36px 0 10px;padding-top:6px;border-top:1px solid var(--line)}
.sub{color:var(--mut);margin:0 0 24px}
.headline{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--acc);border-radius:8px;padding:18px 20px;font-size:1.3rem;line-height:1.35;font-weight:650;margin:0 0 14px}
.conf{display:inline-block;border-radius:999px;padding:3px 12px;font-size:.85rem;font-weight:700;margin-right:8px;white-space:nowrap}
.conf.high{background:var(--hib);color:var(--hi)}.conf.medium{background:var(--midb);color:var(--mid)}.conf.low{background:var(--lob);color:var(--lo)}
.confrow{margin:0 0 22px;color:var(--mut)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:0 0 8px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.kpi .l{color:var(--mut);font-size:.82rem}.kpi .v{font-size:1.65rem;font-weight:700;line-height:1.2}.kpi .d{font-size:.85rem;font-weight:600}
.kpi .d.good{color:var(--hi)}.kpi .d.bad{color:var(--lo)}.kpi .d.flat{color:var(--mut)}.kpi .n{color:var(--mut);font-size:.78rem}
figure{margin:14px 0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px;break-inside:avoid}
figure svg{width:100%!important;max-width:none!important;height:auto;display:block}figcaption{color:var(--mut);font-size:.85rem;padding:6px 6px 2px}
.tablewrap{overflow-x:auto;margin:14px 0}table{border-collapse:collapse;width:100%;font-size:.93rem;background:var(--card)}
caption{text-align:left;color:var(--mut);font-size:.85rem;padding:0 0 6px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}th{font-size:.8rem;color:var(--mut);text-transform:none;border-bottom:2px solid var(--line)}
tr.em td{font-weight:700;background:var(--midb)}
ul{padding-left:1.2rem}li{margin:4px 0}
.small{color:var(--mut);font-size:.85rem}
footer{margin-top:40px;color:var(--mut);font-size:.8rem;border-top:1px solid var(--line);padding-top:10px}
@media print{body{background:#fff;color:#000}main{max-width:none;padding:0}.headline,.kpi,figure,table{break-inside:avoid;border-color:#bbb}h2{break-after:avoid}:root{--card:#fff;--fg:#000;--mut:#444;--line:#bbb}}
"""


def build(doc, base="."):
    parts = []
    a = parts.append
    a('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
      "<title>%s</title><style>%s</style></head><body><main>" % (esc(doc["title"]), CSS))
    a("<h1>%s</h1>" % esc(doc["title"]))
    sub = " · ".join(x for x in (doc.get("subtitle"), doc.get("prepared_by"), doc.get("date")) if x)
    if sub:
        a('<p class="sub">%s</p>' % esc(sub))
    a('<div class="headline">%s</div>' % esc(doc["headline"]))
    c = doc["confidence"]
    a('<p class="confrow"><span class="conf %s">Confidence: %s</span>%s</p>' % (c["level"], esc(c["level"]), esc(c["reason"])))
    if doc.get("answer"):
        a(rich(doc["answer"]))
    kp = doc.get("kpis") or []
    if kp:
        a('<div class="kpis">')
        for k in kp:
            d = k.get("direction", "flat")
            good_dir = k.get("good", "up")
            cls = "flat" if d == "flat" else ("good" if d == good_dir else "bad")
            a('<div class="kpi"><div class="l">%s</div><div class="v">%s</div>%s%s</div>' % (
                esc(k["label"]), esc(k["value"]),
                ('<div class="d %s">%s %s</div>' % (cls, ARROW.get(d, ARROW["flat"]), esc(k["delta"]))) if k.get("delta") else "",
                ('<div class="n">%s</div>' % esc(k["note"])) if k.get("note") else ""))
        a("</div>")
    for s in doc.get("sections") or []:
        if s.get("heading"):
            a("<h2>%s</h2>" % esc(s["heading"]))
        if s.get("text"):
            a(rich(s["text"]))
        if s.get("bullets"):
            a("<ul>%s</ul>" % "".join("<li>%s</li>" % esc(b) for b in s["bullets"]))
        if s.get("chart"):
            ch = s["chart"]
            svg = load_svg(ch, base)
            a("<figure>%s%s</figure>" % (svg, ("<figcaption>%s</figcaption>" % esc(ch["caption"])) if ch.get("caption") else ""))
        if s.get("table"):
            a(render_table(s["table"]))
    if doc.get("method"):
        a("<h2>Method</h2>")
        m = doc["method"]
        a(("<ul>%s</ul>" % "".join("<li>%s</li>" % esc(x) for x in m)) if isinstance(m, list) else rich(m))
    if doc.get("caveats") or doc.get("data_quality"):
        a("<h2>What could make this wrong</h2>")
        if doc.get("caveats"):
            a("<ul>%s</ul>" % "".join("<li>%s</li>" % esc(x) for x in doc["caveats"]))
        if doc.get("data_quality"):
            a('<p class="small"><strong>Data-quality notes</strong></p><ul>%s</ul>' % "".join("<li>%s</li>" % esc(x) for x in doc["data_quality"]))
    if doc.get("next_questions"):
        a("<h2>Next questions</h2><ul>%s</ul>" % "".join("<li>%s</li>" % esc(x) for x in doc["next_questions"]))
    foot = []
    if doc.get("sources"):
        foot.append("Sources: " + "; ".join(str(x) for x in doc["sources"]))
    foot.append("Built with the analyze skill. Every number comes from the scripts named in Method.")
    a("<footer>%s</footer></main></body></html>" % esc(" · ".join(foot)))
    return "\n".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json", nargs="?", help="report JSON (see assets/report.schema.json), or - for stdin")
    ap.add_argument("--out", help="HTML file to write (default: stdout)")
    ap.add_argument("--check", action="store_true", help="only validate the JSON and list problems")
    ap.add_argument("--example", action="store_true", help="print an example report JSON and exit")
    a = ap.parse_args(argv)
    if a.example:
        print(json.dumps(EXAMPLE, indent=2, ensure_ascii=False))
        return 0
    if not a.json:
        ap.error("give REPORT.json (or --example)")
    try:
        text = sys.stdin.read() if a.json == "-" else open(a.json, encoding="utf-8-sig").read()
        doc = json.loads(text)
    except OSError:
        print("error: cannot read %s" % a.json, file=sys.stderr)
        return 2
    except ValueError as ex:
        print("error: %s is not valid JSON (%s)" % (a.json, ex), file=sys.stderr)
        return 2
    problems = check(doc)
    if a.check:
        for p in problems:
            print("problem: " + p)
        print("ok" if not problems else "%d problem(s)" % len(problems))
        return 1 if problems else 0
    if problems:
        for p in problems:
            print("problem: " + p, file=sys.stderr)
        print("error: fix the problems above (run with --check to list them)", file=sys.stderr)
        return 2
    base = os.path.dirname(os.path.abspath(a.json)) if a.json != "-" else os.getcwd()
    try:
        page = build(doc, base)
    except ValueError as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 2
    if a.out and a.out != "-":
        try:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(page)
        except OSError as ex:
            print("error: cannot write %s (%s)" % (a.out, ex.strerror), file=sys.stderr)
            return 2
        print("wrote %s (%d bytes)" % (a.out, len(page.encode("utf-8"))), file=sys.stderr)
    else:
        sys.stdout.write(page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
