"""One image per LinkedIn post, drawn by code: a bar chart when the post compares numbers, otherwise a card with its
key line. A model only picks what to show (prompts/image.md); every number it picks must appear in the post, and
Pillow draws every pixel, so the image can't say something the post doesn't. No image model, so it costs ~$0.

Text plus an image out-performs text alone on LinkedIn (2026 benchmarks: ~4.5% engagement for text-only posts,
5-7% for image and document posts).
"""
import io
import re

from core import drafting, llm, storage
from agents.market_intel.roster import AUTHOR, PROMPTS
from agents.substack.render import _wrap, chart_png

INK, MUTED, PAPER, ACCENT = (28, 25, 23), (120, 113, 108), (250, 249, 246), (194, 65, 12)


def _numbers(text: str) -> set[str]:
    """Every number in the text, normalised ("1,500" and "1500" match; "15.2" stays "15.2")."""
    return {n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,]*\.?\d*", text)}


def _fmt(value: float) -> str:
    return (f"{value:,.2f}".rstrip("0").rstrip(".")) if isinstance(value, float) else f"{value:,}"


def checked(spec: dict, post: str) -> dict | None:
    """The spec if it only shows what the post says; None otherwise."""
    have = _numbers(post)
    if spec.get("kind") == "chart":
        series = [s for s in spec.get("series") or [] if isinstance(s, dict) and isinstance(s.get("value"), (int, float))
                  and not isinstance(s.get("value"), bool) and str(s.get("label") or "").strip()][:6]
        if len(series) < 2 or any(_fmt(s["value"]).replace(",", "") not in have for s in series):
            return None
        return {"kind": "chart", "title": str(spec.get("title") or "")[:80], "unit": str(spec.get("unit") or "")[:4],
                "series": [{"label": str(s["label"])[:40], "value": s["value"]} for s in series],
                "source": str(spec.get("source") or "")[:80], "alt": str(spec.get("alt") or "")[:300]}
    if spec.get("kind") == "card" and (head := str(spec.get("headline") or "").strip()):
        sub = str(spec.get("sub") or "").strip()
        if not _numbers(f"{head} {sub}") <= have:
            return None
        return {"kind": "card", "headline": head[:120], "sub": sub[:140], "alt": str(spec.get("alt") or head)[:300]}
    return None


def fallback(post: str) -> dict:
    """A card with the post's first line, when the model's pick didn't check out."""
    first = next((l.strip() for l in post.splitlines() if l.strip() and not l.strip().startswith("#")), "")
    words = first.split()
    head = " ".join(words[:16]) + ("…" if len(words) > 16 else "")
    return {"kind": "card", "headline": head, "sub": "", "alt": head}


def card_png(headline: str, sub: str = "", footer: str = "AI Musing") -> bytes:
    """A 1200x1200 card (square fills more of the mobile feed than 16:9): big headline, a context line, a footer."""
    from PIL import Image, ImageDraw, ImageFont
    W = H = 1200
    pad = 110
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    font = lambda size: ImageFont.load_default(size=size)  # noqa: E731
    size = 86
    while size > 48:  # the largest size that fits in five lines
        lines = _wrap(d, headline, font(size), W - 2 * pad, max_lines=6)
        if len(lines) <= 5:
            break
        size -= 6
    lines = _wrap(d, headline, font(size), W - 2 * pad, max_lines=5)
    subs = _wrap(d, sub, font(42), W - 2 * pad, max_lines=3) if sub else []
    block = 90 + len(lines) * int(size * 1.22) + (40 + 56 * len(subs) if subs else 0)
    y = max(pad, (H - block) // 2 - 40)  # centred, a little above the middle
    d.rectangle((pad, y, pad + 90, y + 12), fill=ACCENT)
    y += 90
    for ln in lines:
        d.text((pad, y), ln, fill=INK, font=font(size))
        y += int(size * 1.22)
    if subs:
        y += 40
        for ln in subs:
            d.text((pad, y), ln, fill=MUTED, font=font(42))
            y += 56
    d.text((pad, H - pad), footer, fill=MUTED, font=font(34), anchor="ls")
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def render(spec: dict) -> bytes:
    if spec["kind"] == "chart":
        return chart_png(spec)
    return card_png(spec["headline"], spec.get("sub", ""))


def make(ctx, post: str, facts: str, week: str) -> dict | None:
    """Pick, check, draw and store the image. Never raises: a post is saved with or without one."""
    try:
        if not storage.usable():
            return None
        try:
            raw = llm.call_json(ctx, "verify", "image", drafting.fill((PROMPTS / "image.md").read_text(), AUTHOR=AUTHOR),
                                f"# Post\n{post}\n\n# Fact sheet\n{facts}")
        except ValueError:
            raw = {}
        spec = checked(raw if isinstance(raw, dict) else {}, post) or fallback(post)
        url = storage.put(f"linkedin/{week}/image.png", render(spec), "image/png")
        return {"url": url, "alt": spec.get("alt") or "", "kind": spec["kind"]}
    except Exception as ex:  # noqa: BLE001
        print(f"image: skipped: {type(ex).__name__}: {ex}")
        return None
