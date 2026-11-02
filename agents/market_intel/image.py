"""One image per LinkedIn post: a pencil sketch of the post's idea with a handwritten caption under it.

A model plans it (prompts/image.md): a scene to draw and a caption. The image model (Nano Banana, ~$0.04) draws the
scene with no words in it, a vision model checks for stray text (one regeneration at most), and Pillow letters the
caption, so every number on the image is one the post has. If generation fails, a plain card with the caption
goes out instead, and a post is saved with or without an image.

Text plus an image out-performs text alone on LinkedIn (2026 benchmarks: ~4.5% engagement for text-only posts,
5-7% for image and document posts).
"""
import base64
import io
import re
from pathlib import Path

from core import drafting, images, llm, storage
from core.budget import BudgetExceeded
from agents.market_intel.roster import AUTHOR, PROMPTS
from agents.substack.render import _wrap

INK, MUTED, PAPER, ACCENT = (28, 25, 23), (120, 113, 108), (250, 249, 246), (194, 65, 12)
GRAPHITE = (58, 56, 54)
HAND = Path(__file__).parent / "fonts" / "PatrickHand-Regular.ttf"  # SIL Open Font License, see fonts/OFL.txt
W, H = 1080, 1350  # 4:5 portrait, the largest a single image shows in the mobile feed
TRIES = 2  # the first sketch plus at most one regeneration
STYLE = ("A hand-drawn graphite pencil sketch on slightly textured off-white sketchbook paper, like an engineer's notebook "
         "illustration: confident loose linework, light cross-hatching for shading, a few construction lines left visible, "
         "monochrome grey graphite with at most one subtle accent of burnt-orange coloured pencil, plenty of white space, "
         "square composition, subject centred with margin around it, the drawing on a plain sheet with no notebook edges, "
         "binding or page border. Absolutely no text, letters, numbers, labels, logos or "
         "brand marks anywhere.")


def _numbers(text: str) -> set[str]:
    """Every number in the text, normalised ("1,500" and "1500" match; "15.2" stays "15.2")."""
    return {n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,]*\.?\d*", text)}


def checked(spec: dict, post: str) -> dict | None:
    """The plan if its caption only uses numbers the post has; None otherwise."""
    caption = " ".join(str(spec.get("caption") or "").split())
    if not caption or not _numbers(caption) <= _numbers(post):
        return None
    words = caption.split()
    caption = " ".join(words[:18]) + ("…" if len(words) > 18 else "")
    return {"scene": " ".join(str(spec.get("scene") or "").split())[:800], "caption": caption,
            "alt": str(spec.get("alt") or caption)[:300]}


def fallback(post: str) -> dict:
    """The post's first line as the caption, with no scene, when the model's plan didn't check out."""
    first = next((l.strip() for l in post.splitlines() if l.strip() and not l.strip().startswith("#")), "")
    words = first.split()
    head = " ".join(words[:16]) + ("…" if len(words) > 16 else "")
    return {"scene": "", "caption": head, "alt": head}


def _font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(str(HAND), size)
    except OSError:
        return ImageFont.load_default(size=size)


def sketch_png(sketch: bytes, caption: str, footer: str = "AI Musing") -> bytes:
    """The sketch on top, the caption handwritten under it on the same paper, a small footer."""
    from PIL import Image, ImageDraw
    art = Image.open(io.BytesIO(sketch)).convert("RGB")
    side = min(art.size)
    inset = side // 18  # models like to draw the notebook's edge; trim it
    left, top = (art.width - side) // 2 + inset, (art.height - side) // 2 + inset
    art = art.crop((left, top, left + side - 2 * inset, top + side - 2 * inset)).resize((W, W))
    # the sketch's own paper colour (the average of a blank strip along the top), so the caption band matches
    paper = art.crop((W // 4, 4, 3 * W // 4, 24)).resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
    img = Image.new("RGB", (W, H), paper)
    img.paste(art, (0, 0))
    fade = 90  # blend the sketch's bottom edge into the caption band instead of a hard seam
    mask = Image.linear_gradient("L").resize((W, fade))
    img.paste(Image.new("RGB", (W, fade), paper), (0, W - fade), mask)
    d = ImageDraw.Draw(img)
    pad, band = 70, H - W
    size = 62
    while size > 40:
        lines = _wrap(d, caption, _font(size), W - 2 * pad, max_lines=3)
        if len(lines) <= 2:
            break
        size -= 4
    lines = _wrap(d, caption, _font(size), W - 2 * pad, max_lines=2)
    y = W + (band - 40 - len(lines) * int(size * 1.15)) // 2
    for ln in lines:
        d.text((W // 2, y), ln, fill=GRAPHITE, font=_font(size), anchor="ma")
        y += int(size * 1.15)
    d.text((W - pad // 2, H - 24), footer, fill=MUTED, font=_font(28), anchor="rs")
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


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


def _check(ctx, scene: str, raw: bytes) -> dict:
    url = "data:image/jpeg;base64," + base64.b64encode(storage.to_jpeg(raw, max_width=768)).decode()
    try:
        r = llm.call_json(ctx, "vision", "image_check", (PROMPTS / "image_check.md").read_text(), f"# Brief\n{scene}",
                          images=[url])
        return {"ok": bool(r.get("ok")), "score": int(r.get("score") or 0), "issues": [str(i) for i in r.get("issues") or []][:4]}
    except BudgetExceeded:
        raise
    except Exception:  # a flaky free vision model shouldn't cost us the sketch
        return {"ok": True, "score": 0, "issues": []}


def draw(ctx, scene: str) -> bytes | None:
    """The best sketch of the scene in at most TRIES attempts, or None if the image model failed."""
    best, issues = None, []
    for _ in range(TRIES):
        prompt = f"{STYLE}\n\nScene: {scene}" + (f"\n\nAvoid these problems from the last attempt: {'; '.join(issues)}" if issues else "")
        try:
            raw = images.generate(ctx, "image", prompt, aspect_ratio="1:1")["bytes"]
        except BudgetExceeded:
            raise
        except Exception as ex:  # noqa: BLE001
            print(f"image: sketch failed: {type(ex).__name__}: {str(ex)[:120]}")
            break
        verdict = _check(ctx, scene, raw)
        if best is None or verdict["score"] > best[1]:
            best = (raw, verdict["score"])
        if verdict["ok"]:
            break
        issues = verdict["issues"] or ["didn't match the brief"]
    return best[0] if best else None


def make(ctx, post: str, facts: str, week: str) -> dict | None:
    """Plan, check, draw and store the image. Never raises: a post is saved with or without one."""
    try:
        if not storage.usable():
            return None
        try:
            raw = llm.call_json(ctx, "verify", "image", drafting.fill((PROMPTS / "image.md").read_text(), AUTHOR=AUTHOR),
                                f"# Post\n{post}\n\n# Fact sheet\n{facts}")
        except ValueError:
            raw = {}
        spec = checked(raw if isinstance(raw, dict) else {}, post) or fallback(post)
        sketch = draw(ctx, spec["scene"]) if spec["scene"] else None
        kind, png = ("sketch", sketch_png(sketch, spec["caption"])) if sketch else ("card", card_png(spec["caption"]))
        url = storage.put(f"linkedin/{week}/image.jpg", storage.to_jpeg(png, max_width=W), "image/jpeg")
        return {"url": url, "alt": spec["alt"], "kind": kind}
    except Exception as ex:  # noqa: BLE001
        print(f"image: skipped: {type(ex).__name__}: {ex}")
        return None
