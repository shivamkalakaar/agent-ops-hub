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
    monkeypatch.setattr(graph, "fetch_article", lambda url: "The model scores 88 on X and costs $0.20 per million tokens.")
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
        assert "article_text" in graph._sources_block([{"source": "s", "title": "t", "summary": "x", "body": "b"}])


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
    quoted = 'One draft said "a cloud company wearing a model company\'s clothes." Another used "X isn\'t Y. It\'s Z." on purpose.'
    assert not graph.humanizer.scan(quoted)["structural"]
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


def test_keeps_more_human_version_when_rewrite_is_worse():
    ai = ("Open models aren't a side project anymore. They're the default.\n\n"
          "An open-weight model scores 88 on X at $0.20 per million tokens, and that changes the math. " * 2
          + "\n\nMy take: switch now.")
    llm.FAKE_RESPONSES["revise"] = lambda m: ai  # the rewrite reintroduces AI patterns
    assert cli.run("collect") == 0 and cli.run("draft") == 0
    from core.db import connect
    with connect() as db:
        d = db.one("select * from drafts")
        assert d["post_md"] == GOOD_POST            # first version kept: no violations, higher weighted score
        assert d["judge_scores"]["overall"] == graph.overall_score(
            {k: v for k, v in d["judge_scores"].items() if k in ("hook", "focus", "brevity", "concreteness", "voice", "grounding")},
            d["judge_scores"]["humanizer"])


def test_overall_weights_humanizer_40_percent():
    assert graph.overall_score({"hook": 5, "voice": 5}, 1) == 0.4 * 1 + 0.6 * 5
    assert graph.overall_score({"hook": 3, "voice": 3}, 5) == 0.4 * 5 + 0.6 * 3


def test_weekday_drafts_skip_used_stories_and_use_a_date_key():
    assert cli.run("collect") == 0 and cli.run("draft") == 0
    from core.db import connect
    with connect() as db:
        first = db.one("select * from drafts")
    assert first["week"] == datetime.now(timezone.utc).date().isoformat()
    assert cli.run("draft") == 0  # next run: story "a" is used, so it must build on "b"
    with connect() as db:
        second = db.one("select * from drafts where id != %s", (first["id"],))
    assert first["sources"][0]["url"] == "https://example.com/a" and second["sources"][0]["url"] == "https://example.com/b"


def test_scheduled_run_skips_when_two_drafts_are_waiting():
    assert cli.run("collect") == 0 and cli.run("draft") == 0 and cli.run("draft") == 0
    assert cli.run("draft", skip_if_pending=2) == 0
    from core.db import connect
    with connect() as db:
        runs = db.all("select status, stats from runs where kind='draft' order by started_at")
        assert db.pending_drafts() == 2
    assert runs[-1]["status"] == "skipped" and "2 drafts already waiting" in runs[-1]["stats"]["reason"]


def test_window_starts_at_last_draft_within_bounds():
    from datetime import timedelta
    now = datetime(2026, 10, 26, 15, tzinfo=timezone.utc)                      # a Monday
    friday = (now - timedelta(days=3)).isoformat()
    assert graph.window_start(friday, now) == now - timedelta(days=3)          # covers the weekend
    assert graph.window_start((now - timedelta(hours=2)).isoformat(), now) == now - timedelta(days=1)
    assert graph.window_start(None, now) == now - timedelta(days=4)


def test_flags_the_recap_template_and_repeated_endings():
    templated = ("Your Notion seat just became a usage bucket.\n\nNotion says it matched Opus at 90% lower cost. "
                 "That's their own benchmark, so read it that way.\n\nI'd stop pricing seats and start pricing usage.\n\n"
                 "Next time a vendor pitches you, ask what the last finished job cost.")
    v = " | ".join(graph.guardrail_violations(templated, set()))
    for needle in ["just became", "read it that way", "I'd stop X and start Y", "next time a vendor"]:
        assert needle in v
    recent = ["Some post.\n\nWhen a vendor demos an agent, how often does it stop?"]
    again = GOOD_POST.replace("#AI", "When a vendor demos it, ask for the bill.")
    assert any("ends the same way" in x for x in graph.guardrail_violations(again, set(), recent=recent))
    assert graph.guardrail_violations(GOOD_POST, set(), recent=recent) == []


def test_voice_examples_rotate_by_seed():
    a = graph.style_examples(n=4, short_only=False, max_words=graph.EXAMPLE_MAX_WORDS, seed="2026-10-22")
    b = graph.style_examples(n=4, short_only=False, max_words=graph.EXAMPLE_MAX_WORDS, seed="2026-10-23")
    assert a == graph.style_examples(n=4, short_only=False, max_words=graph.EXAMPLE_MAX_WORDS, seed="2026-10-22")
    assert all(len(p.split()) <= graph.EXAMPLE_MAX_WORDS for p in a + b)
    if len(graph.style_examples(n=99, short_only=False, max_words=graph.EXAMPLE_MAX_WORDS)) > 4:
        assert a != b


def test_learns_from_edits_and_rejections_then_uses_the_lessons():
    from core import lessons
    from core.db import connect
    assert cli.run("collect") == 0 and cli.run("draft") == 0
    with connect() as db:
        d = db.one("select * from drafts")
        edited = GOOD_POST.replace("I had to read that twice.", "I checked the pricing page myself.")
        db.execute("update drafts set status='approved', edited_post_md=%s, approved_at=%s where id=%s",
                   (edited, "2026-10-22T00:00:00+00:00", d["id"]))
        db.execute("insert into drafts (id, week, angle, post_md, onepager_html, sources, revisions, status, feedback, created_at)"
                   " values ('r1', 'x', %s, 'A recap post.', '', %s, 0, 'rejected', %s, '2026-10-22T00:00:00+00:00')",
                   (db.json({}), db.json([]), db.json({"reasons": ["Recap, no real take"], "note": "where am I in this?"})))
    seen = {}

    def learn(messages):
        seen["user"] = messages[-1].content
        return json.dumps({"add": [{"lesson": "Adds a first-hand check of the claim instead of a stock reaction line.",
                                    "kind": "voice", "from": [d["id"]]},
                                   {"lesson": "Rejects pure recaps: every post needs Shivam's own position.", "kind": "structure",
                                    "from": ["r1"]}], "confirm": [], "revise": [], "retire": []})
    llm.FAKE_RESPONSES["learn"] = learn
    prompts = []
    write = llm.FAKE_RESPONSES["write_post"]
    llm.FAKE_RESPONSES["write_post"] = lambda m: (prompts.append(m[-1].content), write(m))[1]
    assert cli.run("draft") == 0
    assert "I checked the pricing page myself" in seen["user"] and "Recap, no real take" in seen["user"]
    assert "where am I in this?" in seen["user"]
    assert "own position" in prompts[0] and "feedback has taught you" in prompts[0]
    with connect() as db:
        assert db.one("select count(*) as n from drafts where learned_at is null and status != 'pending'")["n"] == 0
        assert len(lessons.active(db, "market_intel")) == 2
        assert [r["stats"]["learned"].get("added") for r in db.all("select stats from runs where kind='draft'")
                if r["stats"]["learned"].get("read")] == [2]
        # the next run has nothing new to read, so it doesn't call the model again
        assert lessons.events(db, "market_intel") == [] and lessons.learn(db, None, "market_intel", "Shivam") == {"read": 0}


def test_lessons_merge_and_stay_capped():
    from core import lessons
    from core.db import connect
    with connect() as db:
        lessons.apply(db, "market_intel", {"add": [{"lesson": f"Lesson number {i} is specific.", "from": []} for i in range(5)]}, set())
        for _ in range(3):
            lessons.apply(db, "market_intel", {"add": [{"lesson": f"Another specific lesson {i}.", "from": []} for i in range(5)]}, set())
        rows = lessons.active(db, "market_intel")
        assert len(rows) == lessons.MAX_ACTIVE
        top = rows[-1]["id"]
        lessons.apply(db, "market_intel", {"confirm": [top, "nope"], "revise": [{"id": top, "lesson": "A sharper version of it."}]}, set())
        rows = lessons.active(db, "market_intel")
        assert rows[0]["id"] == top and rows[0]["evidence"] == 2 and rows[0]["lesson"] == "A sharper version of it."
        lessons.apply(db, "market_intel", {"retire": [top]}, set())
        assert top not in {r["id"] for r in lessons.active(db, "market_intel")}
        assert "(seen" not in lessons.block(db, "substack", "Shivam") and lessons.block(db, "substack", "Shivam") == ""
    assert lessons.edit_share("a b c d", None) == 0.0 and 0 < lessons.edit_share("a b c d", "a b x d") < 1


def test_redraft_rewrites_in_place_around_the_take():
    from core.db import connect
    assert cli.run("collect") == 0 and cli.run("draft") == 0
    with connect() as db:
        d = db.one("select * from drafts")
        db.execute("update drafts set my_take=%s where id=%s", ("Cheap evals matter more than cheap tokens.", d["id"]))
    prompts = []
    write = llm.FAKE_RESPONSES["write_post"]
    llm.FAKE_RESPONSES["write_post"] = lambda m: (prompts.append(m[-1].content), write(m))[1]
    assert cli.run("draft", redraft=d["id"]) == 0
    assert "Cheap evals matter more than cheap tokens." in prompts[0]
    with connect() as db:
        rows = db.all("select * from drafts")
        assert len(rows) == 1 and rows[0]["status"] == "pending" and rows[0]["run_id"] != d["run_id"]
