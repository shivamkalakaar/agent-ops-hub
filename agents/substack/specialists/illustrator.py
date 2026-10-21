"""Illustrator: an art director plans the images, Nano Banana generates them, a vision model checks each one against
its brief (one regeneration at most), and a chart is drawn in code from numbers that appear in the sources.
A failed image is skipped with a note, never fatal: the post is still worth reviewing without it."""
import base64
import json
import re

from core import drafting, images, llm, storage
from core.budget import BudgetExceeded
from agents.substack import checks, render
from agents.substack.roster import AUTHOR, PROMPTS
from agents.substack.specialists import current, sources_text

prompt = drafting.prompt_loader(PROMPTS)
HOUSE_STYLE = ("modern editorial illustration for a thoughtful tech newsletter; flat-ish shapes with subtle grain and soft "
               "shadows; warm off-white background; limited palette of burnt orange, deep charcoal and muted sand; generous "
               "negative space; calm, intelligent mood; no text, letters, numbers or logos anywhere in the image")
MAX_INLINE = 2
TRIES = 2  # the first image plus at most one regeneration


def _check(ctx, brief: str, jpeg: bytes) -> dict:
    small = storage.to_jpeg(jpeg, max_width=768)
    url = "data:image/jpeg;base64," + base64.b64encode(small).decode()
    try:
        r = llm.call_json(ctx, "vision", "image_check", prompt("image_check"), f"# Brief\n{brief}", images=[url])
        return {"ok": bool(r.get("ok")), "score": int(r.get("score") or 0), "issues": [str(i) for i in r.get("issues") or []][:4]}
    except BudgetExceeded:
        raise
    except Exception as ex:  # a flaky free vision model shouldn't cost us the image
        return {"ok": True, "score": 0, "issues": [], "note": f"vision check unavailable: {type(ex).__name__}"}


def _make(ctx, cfg, job: dict, key: str, notes: list[str]) -> dict | None:
    best, issues = None, []
    for attempt in range(TRIES):
        text = f"{HOUSE_STYLE}.\n\n{job['prompt']}" + (f"\n\nAvoid these problems from the last attempt: {'; '.join(issues)}" if issues else "")
        try:
            gen = images.generate(ctx, "image", text, model=cfg.model)
        except BudgetExceeded:
            raise
        except Exception as ex:
            notes.append(f"{key}: generation failed ({type(ex).__name__}: {str(ex)[:120]})")
            break
        jpeg = storage.to_jpeg(gen["bytes"])
        verdict = _check(ctx, job["prompt"], jpeg)
        if not best or verdict["score"] > best["verdict"]["score"]:
            best = {"jpeg": jpeg, "verdict": verdict, "model": gen["model"], "attempts": attempt + 1}
        if verdict["ok"]:
            break
        issues = verdict["issues"] or ["didn't match the brief"]
        notes.append(f"{key}: regenerating ({'; '.join(issues)[:140]})")
    return best


def _chart_ok(spec: dict, text: str) -> bool:
    """Every plotted value must appear in the text, in some ordinary spelling."""
    series = spec.get("series") if isinstance(spec, dict) else None
    if not isinstance(series, list) or len(series) < 2:
        return False
    flat = text.replace(",", "")
    for s in series:
        v = s.get("value") if isinstance(s, dict) else None
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return False
        spellings = {f"{v:g}", str(int(v)) if float(v).is_integer() else f"{v:g}"}
        if not any(re.search(rf"(?<![\d.]){re.escape(x)}(?![\d])", flat) for x in spellings):
            return False
    return True


def run(state, ctx, cfg):
    if not storage.usable():  # don't pay for images that would be lost with the runner
        note = "skipped: image storage isn't configured (set the SUPABASE_URL and SUPABASE_SERVICE_KEY secrets)"
        return {"images": [], "image_notes": [note], "image_headings": []}, f"No images. {note}", {"notes": [note]}
    draft, brief = current(state), state["brief"]
    p = checks.parse(draft["md"])
    heads = checks.sections(p["body"])
    outline = "\n\n".join(f"## {h}\n{sec.strip()[:400]}" for h, sec in zip(heads, re.split(r"^## .+$", p["body"], flags=re.M)[1:]))
    # a chart has to be about the post: every value in the sources (true) and in the post itself (relevant)
    spec = brief.get("chart")
    chart = spec if _chart_ok(spec, sources_text(state)) and _chart_ok(spec, draft["md"]) else None
    user = (f"# Title\n{p['title']}\n\n# Subtitle\n{p['subtitle']}\n\n# Thesis\n{brief.get('thesis')}\n\n# Sections\n{outline}"
            + (f"\n\n# Chart data\n{json.dumps(chart, ensure_ascii=False)}" if chart else "")
            + (f"\n\n# Managing editor's note\n{state['instructions']}" if state.get("instructions") else ""))
    plan = llm.call_json(ctx, "writer", "illustrator", drafting.fill(cfg.prompt, AUTHOR=AUTHOR, HOUSE_STYLE=HOUSE_STYLE), user)
    jobs = []
    if isinstance(plan.get("cover"), dict) and plan["cover"].get("prompt"):
        jobs.append(("cover", plan["cover"]))
    for job in [j for j in plan.get("inline") or [] if isinstance(j, dict) and j.get("prompt")][:MAX_INLINE]:
        jobs.append(("inline", job))

    out, notes, folder = [], [], f"{state['slot']}/{ctx.run_id}"
    for n, (kind, job) in enumerate(jobs):
        made = _make(ctx, cfg, job, f"{kind} {n}", notes)
        if not made:
            continue
        url = storage.put(f"{folder}/{kind}-{n}.jpg", made["jpeg"], "image/jpeg")
        out.append({"kind": kind, "url": url, "alt": job.get("alt", ""), "caption": job.get("caption", ""),
                    "after_section": job.get("after_section"), "prompt": job["prompt"], "model": made["model"],
                    "check": made["verdict"], "attempts": made["attempts"]})
    if chart:
        place = plan.get("chart") if isinstance(plan.get("chart"), dict) else {}
        try:
            url = storage.put(f"{folder}/chart.png", render.chart_png(chart), "image/png")
            out.append({"kind": "chart", "url": url, "alt": place.get("alt") or chart.get("title", "Chart"),
                        "caption": place.get("caption") or f"Source: {chart.get('source', '')}",
                        "after_section": place.get("after_section") or (heads[1] if len(heads) > 1 else None), "spec": chart})
        except ValueError as ex:
            notes.append(f"chart skipped: {ex}")
    regens = sum(i.get("attempts", 1) - 1 for i in out)
    summary = (f"{sum(i['kind'] != 'chart' for i in out)}/{len(jobs)} images ({regens} regenerated)"
               f"{', plus a chart' if any(i['kind'] == 'chart' for i in out) else ''}" + (f". {notes[-1]}" if notes else ""))
    return ({"images": out, "image_notes": notes, "image_headings": heads}, summary,
            {"images": [{k: v for k, v in i.items() if k != "spec"} for i in out], "notes": notes})
