#!/usr/bin/env python3
"""After a brief is built, push its stories to the Agent Ops Hub database so the weekly LinkedIn draft can use them.

Stdlib + psql only (runs under launchd). Reads DATABASE_URL from hub.env next to this file; does nothing if it's missing.
The hub's GitHub Actions run scores the pushed stories (importance stays null until then).

Usage: python3 push_to_hub.py out/brief-YYYY-MM-DD.html
"""
import html
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LAYERS = ["compute", "models", "training", "inference", "applications", "business"]
ITEM = re.compile(r'<h2[^>]*>(?P<h2>.*?)</h2>|<a href="(?P<url>https?://[^"]+)"[^>]*font-weight:700[^>]*>(?P<title>.*?)</a>\s*'
                  r'<p[^>]*>(?P<summary>.*?)</p>\s*<p[^>]*><b>Why it matters:</b>(?P<why>.*?)</p>\s*<div[^>]*>(?P<source>.*?)</div>', re.S)

SQL = """
insert into items (url, title, source, layer, why, summary, published_at, collected_at, origin)
select url, title, source, layer, why, summary, now(), now(), 'brief'
from jsonb_to_recordset(:'payload'::jsonb) as x(url text, title text, source text, layer text, why text, summary text)
on conflict (url) do update set
  origin = 'brief',
  why = excluded.why,
  summary = case when length(excluded.summary) > length(coalesce(items.summary, '')) then excluded.summary else items.summary end;
"""


def text(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def parse(path):
    items, layer = [], None
    for m in ITEM.finditer(Path(path).read_text()):
        if m["h2"]:
            layer = next((l for l in LAYERS if l in m["h2"].lower()), None)
            continue
        if layer:
            items.append({"url": m["url"], "title": text(m["title"])[:300], "summary": text(m["summary"])[:800],
                          "why": text(m["why"])[:300], "source": text(m["source"])[:120], "layer": layer})
    return items


def database_url():
    env = ROOT / "hub.env"
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def main(brief):
    url = database_url()
    if not url:
        print("push_to_hub: no hub.env, skipping")
        return 0
    items = list({i["url"]: i for i in reversed(parse(brief))}.values())[::-1]  # same link twice in a brief: keep the first
    if not items:
        print("push_to_hub: no stories parsed from", brief)
        return 1
    r = subprocess.run(["psql", url, "-q", "-v", "ON_ERROR_STOP=1", "-v", "payload=" + json.dumps(items, ensure_ascii=False)],
                       input=SQL, text=True, capture_output=True, timeout=60)
    if r.returncode:
        print("push_to_hub: psql failed:", r.stderr.strip()[:300])
        return 1
    print(f"push_to_hub: sent {len(items)} stories to the hub")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
