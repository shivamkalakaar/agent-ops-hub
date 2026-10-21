"""Tiered OpenRouter client. One entry point, `call()`, which:
budget-checks, routes to the tier's model (OpenRouter falls back on failure),
records tokens/cost/latency in llm_calls, and passes Langfuse callbacks through.

LLM_MODE=fake swaps in canned responses (registered per node) for offline tests.
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from core import budget
from core.config import load_yaml

OPENROUTER_URL = "https://openrouter.ai/api/v1"
FAKE_RESPONSES: dict[str, Callable[[list[BaseMessage]], str]] = {}


@dataclass
class RunContext:
    """Per-run handles every node needs. Lives outside graph state so it isn't serialized."""
    db: object
    run_id: str
    callbacks: list = field(default_factory=list)


def allowed(model: str | None, kind: str = "text") -> bool:
    """Dashboard model overrides must be on the allowlist in config/models.yaml."""
    return bool(model) and model in load_yaml("models.yaml").get("allowed_overrides", {}).get(kind, [])


def _model(tier: str, model: str | None = None):
    """The tier's model, or an allowlisted override as primary with the tier's fallbacks behind it."""
    from langchain_openai import ChatOpenAI
    t = load_yaml("models.yaml")["tiers"][tier]
    primary = model if allowed(model) else t["primary"]
    return ChatOpenAI(
        base_url=OPENROUTER_URL,
        api_key=os.environ["OPENROUTER_API_KEY"],
        model=primary,
        temperature=t.get("temperature", 0.3),
        max_tokens=t.get("max_tokens"),  # a hard stop on runaway generations (reasoning tokens count too)
        timeout=120,
        max_retries=2,
        default_headers={"HTTP-Referer": "https://github.com/shivamkalakaar/agent-ops-hub", "X-Title": "Agent Ops Hub"},
        extra_body={"models": list(dict.fromkeys([primary, *t.get("fallbacks", [])])), "usage": {"include": True},
                    **({"reasoning": t["reasoning"]} if "reasoning" in t else {})},
    )


def _usage(msg) -> tuple[str | None, int, int, float | None]:
    meta = getattr(msg, "response_metadata", {}) or {}
    raw = meta.get("token_usage") or {}
    um = getattr(msg, "usage_metadata", None) or {}
    pt = um.get("input_tokens") or raw.get("prompt_tokens") or 0
    ct = um.get("output_tokens") or raw.get("completion_tokens") or 0
    return meta.get("model_name"), pt, ct, raw.get("cost")


def call(ctx: RunContext, tier: str, node: str, system: str, user: str, *,
         model: str | None = None, images: list[str] | None = None) -> str:
    """`model`: an allowlisted override for this call. `images`: data or https URLs sent with the user message."""
    budget.check(ctx.db)
    content = [{"type": "text", "text": user}, *({"type": "image_url", "image_url": {"url": u}} for u in images)] if images else user
    messages = [SystemMessage(system), HumanMessage(content)]
    start = time.monotonic()
    log = {"run_id": ctx.run_id, "node": node, "tier": tier}
    if os.environ.get("LLM_MODE") == "fake":
        text = FAKE_RESPONSES[node](messages)
        ctx.db.log_llm_call(**log, model="fake", prompt_tokens=0, completion_tokens=0, cost_usd=0.0,
                            latency_ms=int((time.monotonic() - start) * 1000), ok=True)
        return text
    try:
        msg = _model(tier, model).invoke(messages, config={"callbacks": ctx.callbacks, "run_name": node,
                                                    "metadata": {"tier": tier}})
    except Exception:
        ctx.db.log_llm_call(**log, ok=False, latency_ms=int((time.monotonic() - start) * 1000))
        raise
    model, pt, ct, cost = _usage(msg)
    ctx.db.log_llm_call(**log, model=model, prompt_tokens=pt, completion_tokens=ct,
                        cost_usd=cost if cost is not None else budget.estimate_cost(pt, ct),
                        latency_ms=int((time.monotonic() - start) * 1000), ok=True)
    return msg.content if isinstance(msg.content, str) else str(msg.content)


def parse_json(text: str):
    """Pull the first JSON object/array out of a model reply (tolerates ```json fences and preamble)."""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    body = m.group(1) if m else text
    start = min((i for i in (body.find("{"), body.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError(f"no JSON in reply: {text[:200]!r}")
    return json.JSONDecoder().raw_decode(body[start:])[0]


def call_json(ctx: RunContext, tier: str, node: str, system: str, user: str, **kw):
    """call() + parse_json(), with one repair retry. Plain prompting instead of tool calling
    so it works on free models that lack structured-output support. The retry goes to the tier's first fallback
    model: an empty or looping reply (seen from DeepSeek, cut off by max_tokens) tends to repeat on the same model."""
    text = call(ctx, tier, node, system, user, **kw)
    try:
        return parse_json(text)
    except ValueError:
        fallbacks = load_yaml("models.yaml")["tiers"][tier].get("fallbacks", [])
        retry = {**kw, "model": kw.get("model") or (fallbacks[0] if fallbacks and allowed(fallbacks[0]) else None)}
        text = call(ctx, tier, node, system, user + "\n\nYour last reply was not valid JSON. Reply with ONLY the JSON.", **retry)
        return parse_json(text)
