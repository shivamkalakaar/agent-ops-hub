"""Deterministic one-pager rendering. The model returns JSON; we own the HTML, so nothing the
model writes is ever injected as markup into the dashboard."""
from html import escape

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1c1917;--muted:#6b6560;--line:#e7e5e4;--accent:#c2410c}
@media (prefers-color-scheme:dark){:root{--bg:#141210;--card:#1e1b18;--ink:#f2efe9;--muted:#a39d96;--line:#34302b;--accent:#fb923c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:680px;margin:0 auto;padding:40px 16px 64px}
.kicker{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-size:30px;line-height:1.2;margin:8px 0 10px}.dek{color:var(--muted);font-size:18px;margin:0 0 28px}
.nums{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:0 0 28px}
.num{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.num b{display:block;font-size:26px;line-height:1.1}.num span{color:var(--muted);font-size:13px}
h2{font-size:15px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line);padding-bottom:6px;margin:28px 0 10px}
.take{margin-top:32px;background:var(--card);border-left:3px solid var(--accent);border-radius:6px;padding:14px 18px;font-weight:600}
ol{padding-left:20px;color:var(--muted);font-size:14px}a{color:var(--accent)}
footer{margin-top:40px;color:var(--muted);font-size:13px}
"""


def render_onepager(doc: dict, sources: list[dict], week: str, author: str) -> str:
    nums = "".join(f'<div class="num"><b>{escape(n["value"])}</b><span>{escape(n["label"])}</span></div>'
                   for n in doc.get("key_numbers", [])[:3])
    secs = "".join(f'<h2>{escape(s["heading"])}</h2><p>{escape(s["body"])}</p>' for s in doc.get("sections", [])[:3])
    srcs = "".join(f'<li><a href="{escape(s["url"], quote=True)}" rel="noopener">{escape(s["title"])}</a>'
                   f' · {escape(s["source"])}</li>' for s in sources if s["url"].startswith(("https://", "http://")))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(doc["title"])}</title>
<style>{CSS}</style></head><body><main>
<div class="kicker">AI market brief · {escape(week)}</div>
<h1>{escape(doc["title"])}</h1><p class="dek">{escape(doc.get("dek", ""))}</p>
{f'<div class="nums">{nums}</div>' if nums else ''}{secs}
<div class="take">{escape(doc.get("takeaway", ""))}</div>
<h2>Sources</h2><ol>{srcs}</ol>
<footer>{escape(author)} · drafted by an agent, reviewed and approved by a human.</footer>
</main></body></html>"""
