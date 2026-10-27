"""Image generation through OpenRouter (Nano Banana by default, see the `image` block in config/models.yaml).

Same contract as core.llm.call: budget check first, cost and latency logged to llm_calls (tier "image"),
OpenRouter falls back through the `models` array. LLM_MODE=fake returns a tiny PNG for offline tests.
"""
import base64
import os
import time

import httpx

from core import budget
from core.config import load_yaml
from core.llm import OPENROUTER_URL, RunContext, allowed

# 1x1 PNG, used in fake mode
FAKE_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _decode(data_url: str) -> tuple[bytes, str]:
    head, _, b64 = data_url.partition(",")
    mime = head.removeprefix("data:").split(";")[0] or "image/png"
    return base64.b64decode(b64), mime


RETRY_AFTER_S = 8


def _post(body: dict, primary: str, fallbacks: list[str]) -> dict:
    """Nano Banana is often rate-limited upstream (429 from Google's shared pool) for a few seconds, and the
    fallback costs ~2x. So: the primary alone, once more after a pause on a 429, then the full fallback chain."""
    headers = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
               "HTTP-Referer": "https://github.com/shivamkalakaar/agent-ops-hub", "X-Title": "Agent Ops Hub"}
    for models in ([primary], [primary], list(dict.fromkeys([primary, *fallbacks]))):
        r = httpx.post(f"{OPENROUTER_URL}/chat/completions", json={**body, "models": models}, timeout=180, headers=headers)
        if r.status_code == 429 and len(models) == 1:
            time.sleep(RETRY_AFTER_S)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def generate(ctx: RunContext, node: str, prompt: str, *, model: str | None = None,
             aspect_ratio: str | None = None) -> dict:
    """Returns {"bytes", "mime", "model", "cost_usd"}. Raises on failure (the illustrator decides what to skip)."""
    budget.check(ctx.db)
    cfg = load_yaml("models.yaml")["image"]
    primary = model if allowed(model, "image") else cfg["primary"]
    log = {"run_id": ctx.run_id, "node": node, "tier": "image"}
    start = time.monotonic()
    if os.environ.get("LLM_MODE") == "fake":
        ctx.log_call(**log, model="fake", prompt_tokens=0, completion_tokens=0, cost_usd=0.0, latency_ms=0, ok=True)
        return {"bytes": FAKE_PNG, "mime": "image/png", "model": "fake", "cost_usd": 0.0}
    body = {"messages": [{"role": "user", "content": prompt}],
            "modalities": ["image", "text"], "usage": {"include": True},
            "image_config": {"aspect_ratio": aspect_ratio or cfg.get("aspect_ratio", "16:9")}}
    try:
        data = _post(body, primary, cfg.get("fallbacks", []))
        images = data["choices"][0]["message"].get("images") or []
        if not images:
            raise RuntimeError(f"no image in reply: {str(data['choices'][0]['message'].get('content'))[:200]}")
        raw, mime = _decode(images[0]["image_url"]["url"])
    except Exception:
        ctx.log_call(**log, model=primary, ok=False, latency_ms=int((time.monotonic() - start) * 1000))
        raise
    usage = data.get("usage") or {}
    pt, ct = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    cost = usage.get("cost")
    cost = float(cost) if cost is not None else budget.estimate_cost(pt, ct)
    ctx.log_call(**log, model=data.get("model", primary), prompt_tokens=pt, completion_tokens=ct, cost_usd=cost,
                        latency_ms=int((time.monotonic() - start) * 1000), ok=True)
    return {"bytes": raw, "mime": mime, "model": data.get("model", primary), "cost_usd": cost}
