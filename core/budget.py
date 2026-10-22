"""Spend guard: every LLM call is checked against the daily and monthly caps first."""
from datetime import datetime, timezone

from core.config import load_yaml


class BudgetExceeded(RuntimeError):
    pass


def check(db) -> dict:
    """Raise BudgetExceeded if either cap is spent; return current spend otherwise."""
    cfg = load_yaml("budget.yaml")
    now = datetime.now(timezone.utc)
    day = db.spend_since(now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat())
    month = db.spend_since(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat())
    if day >= cfg["daily_cap_usd"]:
        raise BudgetExceeded(f"daily cap ${cfg['daily_cap_usd']:.2f} reached (spent ${day:.4f})")
    cap = monthly_cap(cfg, now)
    if month >= cap:
        raise BudgetExceeded(f"monthly cap ${cap:.2f} reached (spent ${month:.4f})")
    return {"day_usd": day, "month_usd": month}


def monthly_cap(cfg: dict, now: datetime) -> float:
    """This month's cap: a one-off override for the month if there is one, else the standing cap."""
    return float((cfg.get("monthly_cap_overrides") or {}).get(now.strftime("%Y-%m"), cfg["monthly_cap_usd"]))


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens + completion_tokens) / 1000 * load_yaml("budget.yaml")["fallback_cost_per_1k_tokens_usd"]
