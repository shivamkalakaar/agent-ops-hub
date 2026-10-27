"""Reviewer evals offline: fake LLM, temp SQLite. Scoring, the gate, and the fixture's deterministic expectations."""
import json

import pytest

from core import llm
from evals import run_reviewer_evals as rev

SOURCES = {"s": {"items": [{"url": "https://example.com/a", "title": "Open model matches frontier", "source": "Lab Blog",
                            "summary": "A new open-weight model scores 88 on X at $0.20 per million tokens, from Acme."}],
                 "facts": [{"claim": "scores 88 on X", "evidence": "scores 88 on X", "source_id": 0}], "context": []}}
POST = ("An open-weight model from Acme scored 88 on X.\n\nIt costs $0.20 per million tokens. I had to read that twice.\n\n"
        "Last year that score came at frontier prices, so teams rationed their agent calls and checked each one by hand.\n\n"
        "At this price you can run ten passes and let the model check itself, which changes what's worth building for "
        "agent workflows with a lot of retries.\n\n#AI")
ALL = dict.fromkeys(["guardrails", "editor", "fact_checker", "logic_checker", "final_check"], "pass")


def fixture(*cases):
    return {"sources": SOURCES, "cases": [{"sources": "s", "label": c["id"], **c} for c in cases]}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODE", "fake")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(rev, "HERE", tmp_path)
    post = lambda m: m[-1].content.split("# Post\n")[-1]  # noqa: E731
    fakes = {
        "editor": lambda m: json.dumps({"scores": dict.fromkeys(["hook", "focus", "brevity", "concreteness", "voice", "human", "grounding"],
                                                                2 if "game-changer" in post(m) else 5), "critique": "ok"}),
        "fact_checker": lambda m: json.dumps({"claims": [{"claim": "from Globex", "verdict": "unsupported"}] if "Globex" in post(m)
                                             else [{"claim": "from Acme", "verdict": "supported"}]}),
        "logic_checker": lambda m: json.dumps({"logic": 2, "plain": 4, "issues": [{"kind": "non_sequitur", "quote": "So prices", "problem": "p", "fix": "f"}]}
                                              if "So prices" in post(m) else {"logic": 5, "plain": 5, "issues": []}),
        "final_check": lambda m: json.dumps({"passed": "everyone will" not in post(m), "blockers": ["1: sweeping prediction"]}),
    }
    for k, v in fakes.items():
        monkeypatch.setitem(llm.FAKE_RESPONSES, k, v)


def test_catches_planted_defects_and_passes_own_posts(tmp_path):
    f = fixture({"id": "own", "kind": "own", "post": POST, "expect": ALL},
                {"id": "planted-lender", "kind": "planted", "post": POST.replace("Acme", "Globex"), "expect": {"fact_checker": ["Globex"]}},
                {"id": "planted-so", "kind": "planted", "post": POST.replace("I had to", "So prices will fall. I had to"),
                 "expect": {"logic_checker": ["non_sequitur"]}},
                {"id": "planted-tags", "kind": "planted", "post": POST + " #a #b", "expect": {"guardrails": ["more than 2 hashtags"]}})
    (tmp_path / "f.json").write_text(json.dumps(f))
    assert rev.run("market_intel", 4, tmp_path / "f.json") == 0
    out = json.loads((tmp_path / "results" / "reviewers.json").read_text())
    s = out["summary"]["reviewers"]
    assert s["fact_checker"]["caught"] == 1 and s["logic_checker"]["caught"] == 1 and s["guardrails"]["caught"] == 1
    assert all(v["false_alarms"] == 0 for v in s.values()) and s["editor"]["own"] == 1
    by_id = {r["id"]: r for r in out["results"]}
    assert set(by_id["planted-so"]["reviewers"]) == {"guardrails", "logic_checker"}  # LLM reviewers run only where expected
    md = (tmp_path / "results" / "reviewers.md").read_text()
    assert "Reviewer evals: LinkedIn agent passed" in md and "| fact_checker | 1/1 | 0/1 |" in md
    from core.db import connect
    with connect() as db:
        run = db.one("select * from runs where kind='reviewer_eval'")
    assert run["status"] == "succeeded" and run["stats"]["passed"] is True


def test_gate_fails_on_a_missed_planted_defect_or_too_many_false_alarms(tmp_path):
    f = fixture(*[{"id": f"own{n}", "kind": "own", "post": POST + (" everyone will" if n < 2 else ""), "expect": ALL} for n in range(4)],
                {"id": "planted-wrong-word", "kind": "planted", "post": POST, "expect": {"fact_checker": "flag"}},
                {"id": "bad-real", "kind": "bad", "post": POST, "expect": {"logic_checker": "flag"}})
    (tmp_path / "f.json").write_text(json.dumps(f))
    assert rev.run("market_intel", 4, tmp_path / "f.json") == 1
    out = json.loads((tmp_path / "results" / "reviewers.json").read_text())
    reasons = rev.check(out["results"], out["summary"])
    assert any(r.startswith("fact_checker missed planted-wrong-word") for r in reasons)
    assert any(r.startswith("final_check flagged 2 of 4") for r in reasons)  # 50% > 25%
    assert not any("bad-real" in r for r in reasons)  # real bad drafts are reported, not gated
    assert out["summary"]["reviewers"]["logic_checker"]["missed"] == 1
    assert "Reviewer evals: LinkedIn agent failed" in (tmp_path / "results" / "reviewers.md").read_text()


def test_verdict_scoring():
    assert rev.verdict("pass", ["copies a source's words (x)"], ["copies a source"]) == \
        {"flags": [], "accepted": ["copies a source's words (x)"], "expected": "pass", "ok": True}
    assert rev.verdict(["vague_ending", "came apart"], ["vague_ending: x", "jargon: came apart"], [])["ok"]
    v = rev.verdict(["JPMorgan"], ["unsupported: So Nebius will need money"], [])
    assert not v["ok"] and v["missing"] == ["JPMorgan"]  # flagging the wrong line isn't a catch
    assert not rev.verdict("flag", [], [])["ok"] and "ok" not in rev.verdict(None, ["x"], [])


def test_a_reviewer_that_crashes_is_an_error_not_a_false_alarm(tmp_path, monkeypatch):
    monkeypatch.setitem(llm.FAKE_RESPONSES, "editor", lambda m: (_ for _ in ()).throw(RuntimeError("boom")))
    (tmp_path / "f.json").write_text(json.dumps(fixture({"id": "own", "kind": "own", "post": POST, "expect": ALL})))
    assert rev.run("market_intel", 2, tmp_path / "f.json") == 1
    s = json.loads((tmp_path / "results" / "reviewers.json").read_text())["summary"]["reviewers"]["editor"]
    assert s["errors"] == 1 and s["false_alarms"] == 0


@pytest.mark.parametrize("agent", sorted(rev.AGENTS))
def test_fixture_guardrail_expectations_hold(agent):
    """The deterministic checks are free, so every case's guardrail expectation is checked here too."""
    spec = rev.AGENTS[agent]()
    f = json.loads((rev.Path(rev.__file__).parent / "reviewers" / f"{agent}.json").read_text())
    assert len({c["id"] for c in f["cases"]}) == len(f["cases"]) and all(c["label"] for c in f["cases"])
    for c in f["cases"]:
        if exp := c.get("expect", {}).get("guardrails"):
            v = rev.verdict(exp, spec["guardrails"](spec["state"](c, f["sources"][c["sources"]])), c.get("accept", []))
            assert v["ok"], (c["id"], v)
