"""One-off: turn past AI Stack Briefs (~/AI_update_agent/out) into eval cases.
Each brief's stories become the candidate items for one simulated weekly draft."""
import html
import json
import re
import sys
from pathlib import Path

LAYERS = {"compute": "compute", "models": "models", "training": "training", "inference": "inference",
          "applications": "applications", "business": "business"}
ITEM = re.compile(r'<h2[^>]*>(?P<h2>.*?)</h2>|<a href="(?P<url>https?://[^"]+)"[^>]*font-weight:700[^>]*>(?P<title>.*?)</a>\s*'
                  r'<p[^>]*>(?P<summary>.*?)</p>\s*<p[^>]*><b>Why it matters:</b>(?P<why>.*?)</p>\s*<div[^>]*>(?P<source>.*?)</div>', re.S)


def text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def parse(path: Path) -> list[dict]:
    items, layer = [], None
    for m in ITEM.finditer(path.read_text()):
        if m["h2"]:
            layer = next((v for k, v in LAYERS.items() if k in m["h2"].lower()), None)
            continue
        if layer:
            items.append({"url": m["url"], "title": text(m["title"]), "summary": text(m["summary"])[:600],
                          "why": text(m["why"])[:200], "source": text(m["source"]), "layer": layer, "importance": 4})
    return items


def main(src: str):
    out = Path(__file__).parent / "golden"
    for path in sorted(Path(src).expanduser().glob("brief-*.html")):
        items = parse(path)
        if len(items) >= 5:
            (out / f"{path.stem.removeprefix('brief-')}.json").write_text(json.dumps({"items": items}, indent=1, ensure_ascii=False))
            print(f"{path.name}: {len(items)} items")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "~/AI_update_agent/out")
