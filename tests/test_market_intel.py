"""Offline end-to-end tests: fake LLM, temp SQLite, stubbed feeds. No network, no keys."""
import json
from datetime import datetime, timezone

import pytest

from core import llm
from core.config import load_yaml
from agents.market_intel import graph
from agents.market_intel import __main__ as cli

NOW = datetime.now(timezone.utc).isoformat(timespec="minutes")
ITEMS = [
    {"source": "Lab Blog", "tier": 1, "layer_hint": "models", "title": "Open model matches frontier at 1/10 cost",
     "url": "https://example.com/a", "published": NOW, "summary": "A new open-weight model scores 88 on X at $0.20/M tokens."},
    {"source": "Infra News", "tier": 2, "layer_hint": "compute", "title": "Neocloud signs $4B GPU deal",
     "url": "https://example.com/b", "published": NOW, "summary": "Four billion dollars of GPUs over five years."},
]
GOOD_POST = ("An open-weight model just scored 88 on X.\n\n"
             "It costs $0.20 per million tokens. I had to read that twice.\n\n"
             "Last year you paid frontier prices for that score, so most teams I talk to rationed their agent calls. "
             "They'd run one pass, check it by hand, and move on.\n\n"
             "At this price you can run ten passes and let the model check itself. That changes what's worth building, "
             "especially for agent workflows with a lot of retries.\n\n"
             "I'd bet most agent workloads move to open models within a year, and the frontier labs compete on the hard 10%.\n\n"
             "#AI")


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODE", "fake")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setattr(graph, "fetch_all", lambda feeds: ([dict(i) for i in ITEMS], []))
    critic_calls = {"n": 0}

    def critic(_):
        critic_calls["n"] += 1  # first review is weak, forcing one revision
        s = 3 if critic_calls["n"] == 1 else 5
        return json.dumps({"scores": dict.fromkeys(["hook", "focus", "brevity", "concreteness", "voice", "grounding"], s),
                           "critique": "Sharpen the hook."})

    llm.FAKE_RESPONSES.update({
        "score": lambda m: json.dumps([{"id": 0, "layer": "models", "importance": 5, "why": "price drop"},
                                       {"id": 1, "layer": "compute", "importance": 4, "why": "capex"}]),
        "pick_angle": lambda m: '```json\n{"item_ids": [0], "angle": "open models win on cost", "hook": "10x cheaper", "why_now": "today"}\n```',
        "write_post": lambda m: GOOD_POST,
        "revise": lambda m: GOOD_POST,
        "critic": critic,
        "onepager": lambda m: json.dumps({"title": "Open models <script>x</script>", "dek": "d",
                                          "key_numbers": [{"value": "$0.20", "label": "per M tokens"}],
                                          "sections": [{"heading": "h", "body": "b"}] * 3, "takeaway": "t"}),
    })


def test_collect_then_draft_end_to_end():
    assert cli.run("collect") == 0
    assert cli.run("collect") == 0  # second run dedupes everything
    from core.db import connect
    with connect() as db:
        items = db.all("select * from items")
        assert len(items) == 2 and {i["importance"] for i in items} == {4, 5}
        runs = db.all("select * from runs order by started_at")
        assert runs[1]["stats"]["new"] == 0

    assert cli.run("draft") == 0
    with connect() as db:
        d = db.one("select * from drafts")
        assert d["status"] == "pending" and d["revisions"] == 1
        assert d["post_md"] == GOOD_POST and d["judge_scores"]["violations"] == []
        assert "<script>" not in d["onepager_html"] and "&lt;script&gt;" in d["onepager_html"]
        assert d["sources"][0]["url"] == "https://example.com/a"
        run = db.one("select * from runs where kind='draft'")
        assert run["status"] == "succeeded"
        calls = db.all("select node from llm_calls where run_id=%s order by id", (run["id"],))
        assert [c["node"] for c in calls] == ["pick_angle", "write_post", "critic", "revise", "critic", "onepager"]


def test_budget_guard_stops_run(monkeypatch):
    cfg = dict(load_yaml("budget.yaml"), daily_cap_usd=0.0)
    monkeypatch.setattr("core.budget.load_yaml", lambda name: cfg)
    assert cli.run("collect") == 0
    from core.db import connect
    with connect() as db:
        run = db.one("select * from runs")
        assert run["status"] == "budget_exceeded" and "daily cap" in run["error"]
        assert db.one("select count(*) as n from llm_calls")["n"] == 0


def test_guardrails():
    srcs = {"https://example.com/a"}
    assert graph.guardrail_violations(GOOD_POST, srcs) == []
    bad = "I'm excited to share this game-changer!\n" + "word " * 300 + "#a #b #c https://evil.com"
    v = " | ".join(graph.guardrail_violations(bad, srcs))
    for needle in ["characters", "words", "i'm excited", "game-changer", "hashtags", "not from the sources"]:
        assert needle in v


def test_humanizer_catches_structural_tells():
    ai = ("Anthropic might be a cloud company wearing a model company's clothes.\n\n"
          "Those leases aren't background news. They're a valuation input.\n\nMy take: track capacity, not benchmarks.")
    v = " | ".join(graph.guardrail_violations(ai, set(), "Anthropic signed $1.3T in leases."))
    assert "negation pivot" in v and "labelled closer" in v and "costume metaphor" in v
    v = graph.guardrail_violations("Anthropic signed $9T in leases this year. " * 3, set(), "Anthropic signed $1.3T in leases.")
    assert any("\"$9\"" in x and "not in the sources" in x for x in v)


def test_parse_json_tolerates_preamble():
    assert llm.parse_json('Sure! Here it is:\n{"a": [1, 2]} trailing') == {"a": [1, 2]}
    assert llm.parse_json("```json\n[1]\n```") == [1]


def test_evals_offline(monkeypatch, tmp_path):
    from evals import run_evals
    monkeypatch.setattr(run_evals, "BASELINE", tmp_path / "baseline.json")
    # the fake writer's canned post can't match real golden sources, so skip the fact guard here
    monkeypatch.setattr("core.humanizer.invented_facts", lambda sources, post: [])
    monkeypatch.setattr("sys.argv", ["run_evals", "--cases", "2"])
    assert run_evals.main() == 0
