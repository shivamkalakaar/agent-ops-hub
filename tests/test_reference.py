"""The independent judge and the frozen reference cases (evals/reference.py), offline with a fake LLM."""
import json
import re

import pytest

from core import drafting, llm
from core.db import connect
from evals import reference as R

POST = ("Vals AI ran its legal benchmark on the same open-weight model twice, once through Moonshot and once through Baseten.\n\n"
        "75.8% through Moonshot. 60.6% through Baseten.\n\nThe weights were identical. The serving wasn't, and that cost 15 points. "
        "Quantization, chat templates and tool-call parsing all sit between the checkpoint and your API call.\n\n"
        "So the cheapest provider can quietly cost you accuracy. The likely reason most teams miss it: pricing pages don't show it.\n\n"
        "Evaluate the model on the provider you'll actually ship on.\n\n#opensource")
SHIVAM, AGENT = "SHIVAM'S POST " + POST, "AGENT DRAFT " + POST
SOURCES = [{"url": "https://example.com/vals", "title": "Same Kimi K3, two providers, 15-point gap", "source": "Vals AI",
            "summary": "75.8% via Moonshot, 60.6% via Baseten.", "why": "Serving choices cost accuracy.", "origin": "brief"}]


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODE", "fake")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def ctx():
    with connect() as db:
        yield llm.RunContext(db=db, run_id=db.start_run("evals", "eval", "test"))


def _posts(m) -> tuple[str, str]:
    a, b = re.search(r"# Post A\n(.*?)\n\n# Post B\n(.*)", m[-1].content, re.S).groups()
    return a, b


def test_judge_scores_clamps_and_flags(ctx, monkeypatch):
    seen = []
    monkeypatch.setitem(llm.FAKE_RESPONSES, "reference_judge", lambda m: (seen.append(m), json.dumps(
        {"chain": ["same weights", "different scores", "so test the provider"], "unsupported": ["pricing pages don't show it"],
         "scores": {"plain": 5, "logic": "4", "one_idea": 9, "hook": 4.4, "grounded": 2, "human": "n/a"}, "fix": "Cut the last line."}))[1])
    r = R.judge(ctx, POST, SOURCES)
    assert r["scores"] == {"plain": 5, "logic": 4, "one_idea": 5, "hook": 4, "grounded": 2, "human": 1}
    assert r["overall"] == 3.5 and r["version"] == R.version()
    assert r["flags"] == ["not grounded: pricing pages don't show it"]
    system, user = seen[0][0].content, seen[0][-1].content
    assert system == (R.HERE / "judge.md").read_text().replace("{{KIND}}", "short LinkedIn post")  # fixed prompt, nothing from the editor
    assert "Serving choices cost accuracy." in user and POST in user
    assert any("link in the post body" in f for f in R.judge(ctx, POST + "\nhttps://example.com/vals", SOURCES)["flags"])


def test_rules_use_fixed_limits_that_shivams_own_posts_pass():
    assert R.rules(POST) == []
    assert any("hashtags" in v for v in R.rules(POST + " #a #b #c #d"))
    assert any("words" in v for v in R.rules("Too short to be a post."))
    for case in R.reference_cases():
        assert R.rules(case["reference"]) == [], case["id"]


def test_compare_runs_both_orders_and_cancels_position_bias(ctx, monkeypatch):
    calls = []
    monkeypatch.setitem(llm.FAKE_RESPONSES, "reference_compare", lambda m: (calls.append(_posts(m)), '{"better": "A", "why": "first"}')[1])
    r = R.compare(ctx, AGENT, SHIVAM, SOURCES)
    assert calls == [(AGENT, SHIVAM), (SHIVAM, AGENT)]
    assert (r["result"], r["score"], r["orders"]) == ("tie", 0.0, ["A", "A"])  # always "A" is position bias, not a preference

    prefers = lambda who: lambda m: json.dumps({"better": "A" if _posts(m)[0].startswith(who) else "B"})  # noqa: E731
    monkeypatch.setitem(llm.FAKE_RESPONSES, "reference_compare", prefers("SHIVAM"))
    assert R.compare(ctx, AGENT, SHIVAM, SOURCES)["result"] == "loss"
    monkeypatch.setitem(llm.FAKE_RESPONSES, "reference_compare", prefers("AGENT"))
    assert R.compare(ctx, AGENT, SHIVAM, SOURCES)["result"] == "win"
    monkeypatch.setitem(llm.FAKE_RESPONSES, "reference_compare", lambda m: '{"better": "neither"}')
    assert R.compare(ctx, AGENT, SHIVAM, SOURCES)["orders"] == ["tie", "tie"]


def test_reference_cases_are_complete_enough_to_write_from():
    cases = R.reference_cases()
    assert len(cases) >= 3 and len({c["topic"] for c in cases}) == len(cases)
    for c in cases:
        assert c["reference"].strip() and (c["reference"] != c["agent_post"] or c["performance"] == "well"), c["id"]
        for i in c["items"]:
            assert {"url", "title", "summary", "why", "source", "layer", "importance"} <= i.keys(), c["id"]
            assert i["summary"] and i["why"]
        assert any(i.get("body") for i in c["items"]), c["id"]
        assert "Summary:" in drafting.sources_block(c["items"])
        fetch = R.frozen_fetch(c["items"])
        assert fetch(c["items"][0]["url"]) == (c["items"][0].get("body") or "") and fetch("https://elsewhere") == ""


def test_draft_items_skip_a_reused_url_and_duplicate_stories():
    class DB:
        rows = {"https://a": {"title": "Story A", "summary": "sa", "why": "wa", "source": "S", "layer": "models", "importance": "5", "origin": "brief"},
                "https://c": {"title": "A different story at a reused URL", "summary": "x", "why": "x"}}

        def item(self, url):
            return self.rows.get(url)
    d = {"sources": json.dumps([{"url": "https://a", "title": "Story A", "source": "S"}, {"url": "https://c", "title": "Story A"},
                                {"url": "https://b", "title": "Story B", "source": "T"}])}
    items = R.draft_items(DB(), d)
    assert [(i["url"], i["summary"], i["importance"]) for i in items] == [("https://a", "sa", 5), ("https://b", "", 4)]


def test_summary_and_gate_tolerate_one_noisy_case():
    def row(case, overall, flags=(), score=0.0):
        return {"case": case, "judge": {"overall": overall, "scores": dict.fromkeys(R.DIMENSIONS, round(overall)), "flags": list(flags)},
                "compare": {"score": score, "result": {1.0: "win", -1.0: "loss"}.get(score, "tie")}}
    base = R.summarize([row("a", 3.5), row("b", 3.5), row("c", 3.5, score=-1.0), row("d", 3.5)])
    assert (base["judge_mean"], base["vs_shivam"], base["results"]) == (3.5, 0.38, {"win": 0, "tie": 3, "loss": 1})
    one_bad = R.summarize([row("a", 3.5), row("b", 3.5), row("c", 2.5, ["link in the post body"]), row("d", 3.5)])
    assert R.gate(one_bad, base) == []
    worse = R.summarize([row("a", 3.0, ["x"]), row("b", 3.0, ["y"]), row("c", 3.0), row("d", 3.0)])
    assert len(R.gate(worse, base)) == 2  # two flagged cases, and the mean dropped more than the tolerance
    assert R.gate(R.summarize([row(c, 2.0) for c in "abcd"]), {**base, "version": "older-judge"}) == []  # different yardstick: no comparison
    losses = R.summarize([row(c, 3.5, score=-1.0) for c in "abcd"])
    assert R.gate(losses, losses) == ["preferred over Shivam's post 0.00 of the time; the floor is 0.25"]  # even when the baseline is as bad


def test_an_empty_compare_reply_is_a_tie_not_a_crash(monkeypatch, ctx):
    monkeypatch.setitem(llm.FAKE_RESPONSES, "reference_compare", lambda m: "")
    r = R.compare(ctx, AGENT, SHIVAM, SOURCES)
    assert r["orders"] == ["tie", "tie"] and r["result"] == "tie" and r["score"] == 0
