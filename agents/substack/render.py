"""Deterministic rendering. Models produce markdown and JSON; this code owns every byte of HTML and every chart pixel.

- Markdown → HTML with markdown-it in CommonMark mode and html=False, so any raw HTML a model writes is escaped
  as text. The dashboard re-renders edits with the JS port of the same parser and options.
- Images go into the markdown as ![alt](url) + an italic caption line, so they can be moved or deleted while editing.
- Charts are drawn here with Pillow from numbers the fact check accepted. PNG, because Substack's editor drops SVG.
"""
import io
import re
from html import escape

from markdown_it import MarkdownIt

MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})


def _alt(text: str) -> str:
    return re.sub(r"[\[\]\n]", " ", text or "").strip()[:200]


def image_md(img: dict) -> str:
    out = f"![{_alt(img.get('alt'))}]({img['url']})"
    if cap := (img.get("caption") or "").strip().replace("*", ""):
        out += f"\n\n*{cap}*"
    return out


def _norm(heading: str) -> str:
    return re.sub(r"[^\w\s]", "", heading or "").strip().lower()


def place_images(body: str, images: list[dict]) -> str:
    """Cover first; each inline image or chart after the first paragraph of the section it belongs to
    (or at the end, if that heading was rewritten away)."""
    cover = [i for i in images if i["kind"] == "cover"]
    rest = [i for i in images if i["kind"] != "cover"]
    blocks = re.split(r"\n(?=## )", body.strip())
    tail = []
    for img in rest:
        want = _norm(img.get("after_section"))
        for n, block in enumerate(blocks):
            heading = _norm(block.splitlines()[0].removeprefix("## ")) if block.startswith("## ") else ""
            if want and heading and (heading == want or heading.startswith(want) or want.startswith(heading)):
                parts = block.split("\n\n")
                at = 2 if len(parts) > 2 else len(parts)  # heading, first paragraph, then the image
                blocks[n] = "\n\n".join(parts[:at] + [image_md(img)] + parts[at:])
                break
        else:
            tail.append(image_md(img))
    out = "\n".join(blocks)
    if cover:
        out = image_md(cover[0]) + "\n\n" + out
    return out + ("\n\n" + "\n\n".join(tail) if tail else "")


def sources_md(sources: list[dict]) -> str:
    rows = [f"- [{_alt(s['title'])}]({s['url']}) · {_alt(s['source'])}" for s in sources
            if str(s.get("url", "")).startswith(("https://", "http://"))]
    return "## Sources\n\n" + "\n".join(rows) if rows else ""


def compose(body: str, images: list[dict], sources: list[dict]) -> str:
    return "\n\n".join(p for p in [place_images(body, images), sources_md(sources)] if p)


def to_html(md: str) -> str:
    return MD.render(md)


PAGE_CSS = """body{margin:0;background:#fff;color:#1a1a1a;font:19px/1.65 Georgia,"Times New Roman",serif}
main{max-width:680px;margin:0 auto;padding:48px 16px 80px}h1{font:700 36px/1.2 -apple-system,"Segoe UI",sans-serif;margin:0 0 8px}
.sub{color:#666;font-size:21px;margin:0 0 32px}h2{font:700 24px/1.3 -apple-system,"Segoe UI",sans-serif;margin:40px 0 12px}
img{max-width:100%;height:auto;border-radius:4px}p em:only-child{display:block;color:#777;font-size:15px;text-align:center}
a{color:#c2410c}blockquote{border-left:3px solid #ddd;margin:0;padding-left:16px;color:#444}
@media (prefers-color-scheme:dark){body{background:#111;color:#eee}.sub{color:#aaa}}"""


def page(title: str, subtitle: str, body_html: str) -> str:
    """A standalone preview / export page (the clipboard copy is body_html alone)."""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{escape(title)}</title><style>{PAGE_CSS}</style></head><body><main><h1>{escape(title)}</h1>"
            f'<p class="sub">{escape(subtitle)}</p>{body_html}</main></body></html>')


def _wrap(d, text: str, font, width: int, max_lines: int = 2) -> list[str]:
    """Word-wrap a label to the column; the last line gets an ellipsis if it still doesn't fit."""
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if d.textlength(trial, font=font) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,;:") + "…"
    while d.textlength(lines[-1], font=font) > width and len(lines[-1]) > 2:
        lines[-1] = lines[-1][:-2] + "…"
    return lines


def chart_png(spec: dict) -> bytes:
    """Horizontal bar chart, 1600x900. spec: {title, unit, series: [{label, value}], source}."""
    from PIL import Image, ImageDraw, ImageFont
    series = [s for s in spec.get("series", []) if isinstance(s.get("value"), (int, float))][:8]
    if len(series) < 2:
        raise ValueError("a chart needs at least two numeric values")
    W, H, pad = 1600, 900, 80
    img = Image.new("RGB", (W, H), (250, 249, 246))
    d = ImageDraw.Draw(img)
    font = lambda size: ImageFont.load_default(size=size)
    d.text((pad, pad - 20), str(spec.get("title", ""))[:80], fill=(28, 25, 23), font=font(48))
    top, bottom = pad + 90, H - pad - 40
    lines = {n: _wrap(d, str(s["label"]), font(30), 520) for n, s in enumerate(series)}
    label_w = max(d.textlength(ln, font=font(30)) for ls in lines.values() for ln in ls) + 30
    bar_x0, bar_x1 = pad + label_w, W - pad - 180
    peak = max(abs(s["value"]) for s in series) or 1
    row = (bottom - top) / len(series)
    unit = str(spec.get("unit", ""))[:12]
    prefix, unit = (unit, "") if unit in ("$", "€", "£") else ("", unit)  # currency goes before the number: $7.44
    for n, s in enumerate(series):
        y = top + n * row
        bh = min(row * 0.62, 70)
        cy = y + (row - bh) / 2
        d.multiline_text((pad, cy + bh / 2), "\n".join(lines[n]), fill=(68, 64, 60), font=font(30), anchor="lm", spacing=6)
        x1 = bar_x0 + (bar_x1 - bar_x0) * abs(s["value"]) / peak
        d.rounded_rectangle((bar_x0, cy, max(x1, bar_x0 + 4), cy + bh), radius=6, fill=(194, 65, 12) if n == 0 else (120, 113, 108))
        value = f"{s['value']:,}".rstrip("0").rstrip(".") if isinstance(s["value"], float) else f"{s['value']:,}"
        d.text((x1 + 16, cy + bh / 2), f"{prefix}{value}{unit}", fill=(28, 25, 23), font=font(32), anchor="lm")
    if src := str(spec.get("source", ""))[:120]:
        d.text((pad, H - pad), f"Source: {src}", fill=(120, 113, 108), font=font(26), anchor="lm")
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
