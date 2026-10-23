"""Per-specialist settings for multi-agent runs: code defaults (the registry) merged with the owner's dashboard
overrides (agent_settings) and the active prompt version (prompt_versions).

Overrides are validated here, not trusted: required specialists stay on, models must be on the allowlist in
config/models.yaml, and call caps are clamped to a hard ceiling.
"""
from dataclasses import dataclass
from pathlib import Path

from core.config import load_yaml
from core.llm import allowed

MAX_CALLS_CEILING = 5


@dataclass(frozen=True)
class Specialist:
    name: str
    role: str
    description: str
    tier: str                  # text tier from config/models.yaml, or "image" for image generation
    max_calls: int
    required: bool = False

    @property
    def kind(self) -> str:
        return "image" if self.tier == "image" else "text"

    def default_model(self) -> str:
        cfg = load_yaml("models.yaml")
        return cfg["image"]["primary"] if self.kind == "image" else cfg["tiers"][self.tier]["primary"]


@dataclass(frozen=True)
class Resolved:
    enabled: bool
    model: str | None          # None = the tier's default (with its fallbacks)
    max_calls: int
    prompt: str                # template with {{VARS}}
    prompt_version: str        # "file" or "v<N>"
    tier: str = ""             # the specialist's default tier, for specialists that read it from here


def from_files(specialists: list[Specialist], prompts: Path) -> dict[str, Resolved]:
    """Repo defaults only: what evals and offline tests run against."""
    return {s.name: Resolved(True, None, s.max_calls, _file(prompts, s.name), "file", s.tier) for s in specialists}


def _file(prompts: Path, name: str) -> str:
    path = prompts / f"{name}.md"
    return path.read_text() if path.exists() else ""


def load(db, agent: str, specialists: list[Specialist], prompts: Path) -> dict[str, Resolved]:
    """Sync the registry, seed prompt v1 from the repo files, then apply the owner's overrides."""
    allowed_models = load_yaml("models.yaml").get("allowed_overrides", {})
    db.sync_registry(agent, [{"specialist": s.name, "role": s.role, "description": s.description,
                              "default_model": s.default_model(), "allowed_models": allowed_models.get(s.kind, []),
                              "required": s.required, "default_max_calls": s.max_calls} for s in specialists])
    overrides = db.agent_settings(agent)
    out = {}
    for s in specialists:
        body = _file(prompts, s.name)
        if body:
            db.ensure_prompt(agent, s.name, body)
        o = overrides.get(s.name, {})
        version = "file"
        if o.get("prompt_version_id"):
            pv = db.prompt_version(o["prompt_version_id"])
            if pv and pv["agent"] == agent and pv["specialist"] == s.name and pv["body"].strip():
                body, version = pv["body"], f"v{pv['version']}"
        cap = o.get("max_calls")
        out[s.name] = Resolved(
            enabled=True if s.required else bool(o.get("enabled", True)),
            model=o.get("model") if allowed(o.get("model"), s.kind) else None,
            max_calls=max(1, min(int(cap), MAX_CALLS_CEILING)) if isinstance(cap, int) else s.max_calls,
            prompt=body, prompt_version=version, tier=s.tier)
    return out
