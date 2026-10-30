"""Offline end-to-end tests: fake LLM, temp SQLite, stubbed feeds. No network, no keys."""
import json
import re
from pathlib import Path
from datetime import datetime, timezone

import pytest

from core import drafting as _drafting, llm
from core.config import load_yaml
from agents.market_intel import graph
from agents.market_intel import __main__ as cli
from agents.market_intel import supervisor

REAL_CANDIDATES = _drafting.candidates  # the fixture pins one candidate; one test needs the real list

NOW = datetime.now(timezone.utc).isoformat(timespec="minutes")
ITEMS = [
    {"source": "Lab Blog", "tier": 1, "layer_hint": "models", "title": "Open model matches frontier at 1/10 cost",
     "url": "https://example.com/a", "published": NOW, "summary": "A new open-weight model scores 88 on X at $0.20/M tokens."},
    {"source": "Infra News", "tier": 2, "layer_hint": "compute", "title": "Neocloud signs $4B GPU deal",
     "url": "https://example.com/b", "published": NOW, "summary": "Four billion dollars of GPUs over five years."},
]
GOOD_POST = ("An open-weight model just scored 88 on X.\n\n"
             "It costs $0.20 per million tokens. I had to read that twice.\n\n"
             "Last year you paid frontier prices for that score, so teams rationed their agent calls. "
             "They'd run one pass, check it by hand, and move on.\n\n"
             "At this price you can run ten passes and let the model check itself. That changes what's worth building, "
             "especially for agent workflows with a lot of retries.\n\n"
             "I'd bet most agent workloads move to open models within a year, and the frontier labs compete on the hard 10%.\n\n"
             "#AI")


def _allowed(messages) -> list[str]:
    m = re.search(r"# Allowed now\n(.+)", messages[-1].content)
    return [o.strip() for o in m.group(1).split(",")] if m else []


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODE", "fake")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setattr(graph, "fetch_all", lambda feeds: ([dict(i) for i in ITEMS], []))
    monkeypatch.setattr("core.drafting.candidates", lambda tier, override, n=None: [override])  # one writer call per draft
    monkeypatch.setattr(graph, "fetch_article", lambda url: "The model scores 88 on X and costs $0.20 per million tokens.")
    critic_calls = {"n": 0}

    def critic(_):
        critic_calls["n"] += 1  # first review is weak, forcing one revision
        s = 3 if critic_calls["n"] == 1 else 5
        return json.dumps({"scores": dict.fromkeys(["hook", "focus", "brevity", "concreteness", "voice", "grounding"], s),
                           "critique": "Sharpen the hook."})

    llm.FAKE_RESPONSES.update({
        "score": lambda m: json.dumps([{"id": 0, "layer": "models", "importance": 5, "appeal": 4, "why": "price drop"},
                                       {"id": 1, "layer": "compute", "importance": 4, "appeal": 3, "why": "capex"}]),
        "scout": lambda m: '```json\n{"item_ids": [0], "angle": "open models win on cost", "hook": "10x cheaper", "why_now": "today"}\n```',
        "researcher": lambda m: json.dumps({"facts": [{"claim": "scores 88 on X", "evidence": "The model scores 88 on X", "source_id": 0},
                                                      {"claim": "made up", "evidence": "a quote that is nowhere", "source_id": 0}],
                                            "context": ["price only"]}),
        "writer": lambda m: GOOD_POST,
        "editor": critic,
        "fact_checker": lambda m: json.dumps({"claims": [{"claim": "scored 88 on X", "verdict": "supported"}]}),
        "reference_judge": lambda m: json.dumps({"scores": dict.fromkeys(["plain", "logic", "one_idea", "hook", "grounded", "human"], 4),
                                                 "chain": ["a", "so b"], "unsupported": [], "fix": ""}),
        "reference_compare": lambda m: json.dumps({"better": "B", "why": "clearer"}),
        "final_check": lambda m: json.dumps({"passed": True, "blockers": [], "polish": []}),
        "supervisor": lambda m: json.dumps({"next": supervisor.canonical(_allowed(m)), "reason": "next in line"}),
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
        # the coordinator is only asked when more than one move is allowed
        # the coordinator is only asked when more than one move is allowed; one rewrite, after every reviewer reported
        assert [c["node"] for c in calls] == ["scout", "researcher", "writer", "supervisor", "editor", "fact_checker", "final_check",
                                              "writer", "supervisor", "editor", "fact_checker", "final_check", "onepager"]
        steps = db.all("select specialist, kind, status from agent_steps where run_id=%s order by seq", (run["id"],))
        assert steps[0]["specialist"] == "supervisor" and steps[-1]["specialist"] == "supervisor"
        assert {s["specialist"] for s in steps if s["kind"] == "work"} == {"supervisor", "scout", "researcher", "writer", "editor",
                                                                           "fact_checker", "final_check", "onepager"}
        assert d["judge_scores"]["passed_review"] is True
        reg = db.all("select specialist from agent_registry where agent='market_intel'")
        assert len(reg) == 8  # the dashboard sees the whole team
        assert "article_text" in graph._sources_block([{"source": "s", "title": "t", "summary": "x", "body": "b"}])


def test_budget_guard_stops_run(monkeypatch):
    cfg = dict(load_yaml("budget.yaml"), daily_cap_usd=0.0, daily_cap_overrides={})
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
    monkeypatch.setattr(run_evals, "BASELINES", {"market_intel": tmp_path / "baseline.json"})
    monkeypatch.setattr(run_evals, "HERE", tmp_path)
    golden = tmp_path / "golden"
    golden.mkdir()
    for p in sorted((Path(run_evals.__file__).parent / "golden").glob("*.json"))[-2:]:
        (golden / p.name).write_text(p.read_text())
    # the fake writer's canned post can't match real golden sources, so skip the fact guard here
    monkeypatch.setattr("core.humanizer.invented_facts", lambda sources, post: [])
    monkeypatch.setenv("EVAL_TRIGGER", "pr")
    monkeypatch.setenv("PR_NUMBER", "7")
    learned = []
    monkeypatch.setattr("core.lessons.learn", lambda *a, **k: learned.append(a) or {"read": 0})
    prompts = []
    write = llm.FAKE_RESPONSES["writer"]
    monkeypatch.setitem(llm.FAKE_RESPONSES, "writer", lambda m: (prompts.append(m[-1].content), write(m))[1])
    monkeypatch.setattr("sys.argv", ["run_evals", "--cases", "2"])
    assert run_evals.main() == 0
    assert learned == []  # evals never consume the owner's queue decisions
    assert all(run_evals.LESSONS in p for p in prompts)  # and always read the same frozen lessons
    from core.db import connect
    with connect() as db:
        run = db.one("select * from runs where kind='eval'")
        steps = db.all("select * from agent_steps where run_id=%s order by seq", (run["id"],))
    s = run["stats"]
    assert run["status"] == "succeeded" and s["trigger"] == "pr" and s["pr"] == "7" and s["passed"] is True
    assert len(s["results"]) == 3 and s["results"][0]["post"] and s["gate"] == []  # the PR set: three of Shivam's posts
    assert s["judge"]["cases"] == 3 and s["judge"]["vs_shivam"] == 0.5 and s["judge"]["judge_mean"] == 4  # one fake judge, both orders
    cases = [x for x in steps if x["specialist"] == "eval_case"]
    assert len(cases) == 3 and cases[0]["summary"].startswith("ref ") and cases[0]["seq"] == 900
    md = (tmp_path / "results" / "market_intel.md").read_text()
    assert "Evals: LinkedIn agent passed" in md and "| Case | Judge | vs Shivam |" in md


def test_keeps_more_human_version_when_rewrite_is_worse():
    ai = ("Open models aren't a side project anymore. They're the default.\n\n"
          "An open-weight model scores 88 on X at $0.20 per million tokens, and that changes the math. " * 2
          + "\n\nMy take: switch now.")
    drafts = iter([GOOD_POST, ai, ai])
    llm.FAKE_RESPONSES["writer"] = lambda m: next(drafts)  # the rewrite reintroduces AI patterns
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


def test_draft_uses_only_the_last_24_hours():
    from datetime import timedelta
    from core.db import connect
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    with connect() as db:
        rid = db.start_run("market_intel", "collect", "t")
        db.insert_items([dict(ITEMS[0], importance=5, appeal=5), dict(ITEMS[1], published=old, importance=5, appeal=5)], rid)
        state = graph.load_items({}, {"configurable": {"ctx": llm.RunContext(db=db, run_id=rid)}})
    assert [i["url"] for i in state["items"]] == ["https://example.com/a"]


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


def test_writer_sees_the_owners_rewrites_as_pairs_rotated_by_seed():
    from agents.market_intel import specialists
    ex = [{"id": f"d{i}", "draft": f"agent draft {i}", "post": f"posted {i}"} for i in range(5)]
    a, b = specialists.pairs(ex, "2026-10-22-0"), specialists.pairs(ex, "2026-10-22-1")
    assert a == specialists.pairs(ex, "2026-10-22-0") and a.count("## Pair") == specialists.PAIRS
    assert "### The agent's draft\nagent draft" in a and "### What Shivam posted\nposted" in a
    assert a != b  # each writer candidate sees a different set, so no one example gets copied
    assert "none yet" in specialists.pairs([], "x")

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
    write = llm.FAKE_RESPONSES["writer"]
    llm.FAKE_RESPONSES["writer"] = lambda m: (prompts.append(m[-1].content), write(m))[1]
    assert cli.run("draft") == 0
    assert "I checked the pricing page myself" in seen["user"] and "Recap, no real take" in seen["user"]
    assert "where am I in this?" in seen["user"]
    # one decision isn't enough: the LinkedIn writer only sees a lesson two decisions back
    assert "feedback has taught you" not in prompts[0]
    with connect() as db:
        assert db.one("select count(*) as n from drafts where learned_at is null and status != 'pending'")["n"] == 0
        assert len(lessons.active(db, "market_intel")) == 2
        assert [r["stats"]["learned"].get("added") for r in db.all("select stats from runs where kind='draft'")
                if r["stats"]["learned"].get("read")] == [2]
        # the next run has nothing new to read, so it doesn't call the model again
        assert lessons.events(db, "market_intel") == [] and lessons.learn(db, None, "market_intel", "Shivam") == {"read": 0}
        recap = next(r["id"] for r in lessons.active(db, "market_intel") if "recaps" in r["lesson"])
        lessons.apply(db, "market_intel", {"confirm": [recap]}, set())
        shown = lessons.block(db, "market_intel", "Shivam")
        assert "own position" in shown and "first-hand check" not in shown


def test_lessons_merge_and_stay_capped():
    from core import lessons
    from core.db import connect
    with connect() as db:
        lessons.apply(db, "market_intel", {"add": [{"lesson": f"Lesson number {i} is specific.", "from": []} for i in range(5)]}, set())
        for _ in range(3):
            lessons.apply(db, "market_intel", {"add": [{"lesson": f"Another specific lesson {i}.", "from": []} for i in range(5)]}, set())
        rows = lessons.active(db, "market_intel")
        assert len(rows) == lessons.LIMITS["market_intel"]["max_active"] == 8
        lessons.apply(db, "substack", {"add": [{"lesson": f"Substack lesson {i} is specific.", "from": []} for i in range(5)]}, set())
        for _ in range(3):
            lessons.apply(db, "substack", {"add": [{"lesson": f"More substack lesson {i}.", "from": []} for i in range(5)]}, set())
        assert len(lessons.active(db, "substack")) == lessons.MAX_ACTIVE
        top = rows[-1]["id"]
        lessons.apply(db, "market_intel", {"confirm": [top, "nope"], "revise": [{"id": top, "lesson": "A sharper version of it."}]}, set())
        rows = lessons.active(db, "market_intel")
        assert rows[0]["id"] == top and rows[0]["evidence"] == 2 and rows[0]["lesson"] == "A sharper version of it."
        lessons.apply(db, "market_intel", {"retire": [top]}, set())
        assert top not in {r["id"] for r in lessons.active(db, "market_intel")}
        assert "Substack lesson" in lessons.block(db, "substack", "Shivam")  # no evidence floor for the Substack agent
    assert lessons.edit_share("a b c d", None) == 0.0 and 0 < lessons.edit_share("a b c d", "a b x d") < 1


def test_redraft_rewrites_in_place_around_the_take():
    from core.db import connect
    assert cli.run("collect") == 0 and cli.run("draft") == 0
    with connect() as db:
        d = db.one("select * from drafts")
        db.execute("update drafts set my_take=%s where id=%s", ("Cheap evals matter more than cheap tokens.", d["id"]))
    prompts = []
    write = llm.FAKE_RESPONSES["writer"]
    llm.FAKE_RESPONSES["writer"] = lambda m: (prompts.append(m[-1].content), write(m))[1]
    assert cli.run("draft", redraft=d["id"]) == 0
    assert "Cheap evals matter more than cheap tokens." in prompts[0]
    with connect() as db:
        rows = db.all("select * from drafts")
        assert len(rows) == 1 and rows[0]["status"] == "pending" and rows[0]["run_id"] != d["run_id"]


def test_a_second_draft_on_the_same_day_gets_its_own_one_pager_key():
    from core.db import connect
    assert cli.run("collect") == 0 and cli.run("draft") == 0
    with connect() as db:
        first = db.one("select week from drafts")["week"]
        db.execute("update drafts set sources=%s", (db.json([]),))  # free the stories for a second draft
    assert cli.run("draft") == 0
    with connect() as db:
        assert sorted(r["week"] for r in db.all("select week from drafts")) == [first, f"{first}-2"]


def test_unsupported_claims_go_back_to_the_writer_and_final_check_gates_the_save():
    from core.db import connect
    seen, checks = [], iter([{"claims": [{"claim": "costs $0.20", "verdict": "unsupported", "fix": "drop it"}]},
                             {"claims": [{"claim": "costs $0.20", "verdict": "supported"}]}])
    llm.FAKE_RESPONSES["editor"] = lambda m: json.dumps({"scores": dict.fromkeys(["hook", "focus", "brevity", "concreteness", "voice", "grounding"], 5),
                                                          "critique": "Good."})
    llm.FAKE_RESPONSES["fact_checker"] = lambda m: json.dumps(next(checks, {"claims": [{"claim": "costs $0.20", "verdict": "supported"}]}))
    llm.FAKE_RESPONSES["writer"] = lambda m: (seen.append(m[-1].content), GOOD_POST)[1]
    finals = iter([{"passed": False, "blockers": ["2: reads as a recap; say what Shivam thinks"]}, {"passed": True, "blockers": []}])
    llm.FAKE_RESPONSES["final_check"] = lambda m: json.dumps(next(finals, {"passed": True, "blockers": []}))
    assert cli.run("collect") == 0 and cli.run("draft") == 0
    # one rewrite gets every reviewer's notes at once
    assert len(seen) == 2 and '"costs $0.20" → drop it' in seen[1] and "Fact check: unsupported (must fix)" in seen[1]
    assert "Final check blockers (must fix)" in seen[1] and "reads as a recap" in seen[1]
    with connect() as db:
        d = db.one("select * from drafts")
        assert d["judge_scores"]["passed_review"] is True and d["revisions"] == 1  # v2 passed the final check


def test_a_gap_in_the_argument_goes_back_to_the_writer_with_the_editors_fixes():
    from core.db import connect
    seen = []
    gap = {"scores": dict.fromkeys(["take", "focus", "plain", "voice", "ending", "grounding"], 5) | {"logic": 3},
           "fixes": [{"kind": "logic", "quote": "if the mid-tier keeps closing the gap", "problem": "whose gap?",
                      "fix": "say top models get cheaper and cheap models get better"}]}
    reviews = iter([gap])
    llm.FAKE_RESPONSES["editor"] = lambda m: json.dumps(next(reviews, {
        "scores": dict.fromkeys(["take", "focus", "logic", "plain", "voice", "ending", "grounding"], 5), "fixes": []}))
    llm.FAKE_RESPONSES["writer"] = lambda m: (seen.append(m[-1].content), GOOD_POST)[1]
    assert cli.run("collect") == 0 and cli.run("draft") == 0
    assert len(seen) == 2 and '- logic: "if the mid-tier keeps closing the gap": whose gap?' in seen[1]
    with connect() as db:
        d = db.one("select * from drafts")
        assert d["judge_scores"]["passed_review"] is True and d["revisions"] == 1
        assert "whose gap?" not in d["judge_scores"]["critique"]  # v2's review is the one saved

def test_one_rewrite_after_every_review_then_finish():
    from agents.market_intel import supervisor
    from core import settings
    from agents.market_intel.roster import PROMPTS, SPECIALISTS
    cfg = settings.from_files(SPECIALISTS, PROMPTS)
    weak = {"version": 1, "scores": {"take": 2, "logic": 5}, "accent": {"score": 5}, "violations": []}
    base = {"drafts": [{"version": 1, "post": GOOD_POST}], "angle": {"angle": "a"}, "researched": True, "calls": {"writer": 1},
            "edit_report": weak, "fact_report": {"version": 1, "blocking": 0}}
    assert supervisor.allowed(base, cfg) == ["final_check"]  # no rewrite until the final check has read it too
    passed = {**base, "final_report": {"version": 1, "passed": True, "blockers": []}}
    assert supervisor.allowed(passed, cfg) == ["writer"]  # a weak take is worth the one rewrite even when nothing blocks
    v2 = {**passed, "drafts": passed["drafts"] + [{"version": 2, "post": GOOD_POST}], "calls": {"writer": 2},
          "edit_report": {**weak, "version": 2}, "fact_report": {"version": 2, "blocking": 0},
          "final_report": {"version": 2, "passed": False, "blockers": ["1: by 2028"]}}
    assert supervisor.allowed(v2, cfg) == ["onepager"]  # out of rewrites: save the best version
    assert supervisor.allowed({**base, "skip": "no take passed"}, cfg) == ["FINISH"]

def test_a_failing_final_check_is_flagged_not_hidden():
    from core.db import connect
    llm.FAKE_RESPONSES["final_check"] = lambda m: json.dumps({"passed": False, "blockers": ["1: breaks the lesson on recaps"]})
    assert cli.run("collect") == 0 and cli.run("draft") == 0
    with connect() as db:
        d = db.one("select * from drafts")
        assert d["judge_scores"]["passed_review"] is False and "breaks the lesson" in d["judge_scores"]["final_blockers"][0]


def test_scout_skips_an_angle_too_close_to_a_recent_post_and_researcher_drops_fake_quotes():
    from agents.market_intel import specialists
    from core import settings
    from agents.market_intel.roster import PROMPTS, SPECIALISTS
    cfg = settings.from_files(SPECIALISTS, PROMPTS)
    llm.FAKE_RESPONSES["scout"] = lambda m: json.dumps({"candidates": [
        {"item_ids": [0], "angle": "neocloud debt costs set the GPU price", "hook": "h"},
        {"item_ids": [1], "angle": "open models win on cost", "hook": "h"}], "pick": 0, "why": "biggest"})
    items = [dict(i, importance=5) for i in ITEMS]
    state = {"items": items, "recent_angles": ["neocloud debt costs set the GPU price for buyers"]}
    from core.db import connect
    with connect() as db:
        ctx = llm.RunContext(db=db, run_id=db.start_run("market_intel", "draft", "t"))
        update, summary, out = specialists.scout(state, ctx, cfg["scout"])
        assert update["angle"]["angle"] == "open models win on cost" and out["why"].startswith("switched")
        update, summary, out = specialists.researcher({**state, **update}, ctx, cfg["researcher"])
    assert [f["claim"] for f in update["facts"]] == ["scores 88 on X"] and "dropped 1" in summary


def test_brief_stories_keep_their_importance_and_rank_by_appeal():
    from core.db import connect
    with connect() as db:
        rid = db.start_run("market_intel", "collect", "t")
        db.insert_items([dict(ITEMS[1], url="https://example.com/rss", importance=5, appeal=2)], rid)
        for url, imp, tldr in [("https://example.com/tldr", 5, True), ("https://example.com/plain", 4, False)]:
            db.execute("insert into items (url, title, source, layer, importance, tldr, published_at, collected_at, origin)"
                       " values (%s,'t','Brief','models',%s,%s,%s,%s,'brief')", (url, imp, tldr, NOW, NOW))
        pending = {i["url"] for i in db.unscored_items("2000-01-01")}
        assert pending == {"https://example.com/tldr", "https://example.com/plain"}  # appeal still missing
        db.update_scores([{"url": "https://example.com/tldr", "importance": 2, "appeal": 5, "layer": "business"},
                          {"url": "https://example.com/plain", "importance": 1, "appeal": 5}])
        top = db.top_items("2000-01-01")
    assert [(i["url"].rsplit("/", 1)[1], i["importance"], i["appeal"]) for i in top] == [
        ("tldr", 5, 5), ("rss", 5, 2), ("plain", 4, 5)]  # the brief's importance wins over the cheap scorer
    assert top[0]["layer"] == "models" and top[0]["tldr"] in (True, 1)


def test_scout_adds_the_briefs_corroborating_links():
    from agents.market_intel.specialists import _with_corroboration
    s = {"url": "https://paywall.example/x", "title": "Deal", "source": "The Information", "origin": "brief",
         "also": [{"url": "https://free.example/x", "source": "RuntimeWire"}, {"url": "https://other.example/x", "source": "TC"}]}
    out = _with_corroboration([s, dict(ITEMS[0])])
    assert [o["url"] for o in out] == ["https://paywall.example/x", "https://example.com/a", "https://free.example/x"]
    assert out[2]["source"] == "RuntimeWire" and out[2]["corroborates"] == s["url"]


def test_writer_sees_the_briefs_read_and_scout_drops_thesis_angles():
    from core import drafting, settings
    from agents.market_intel import specialists
    from agents.market_intel.roster import PROMPTS, SPECIALISTS
    brief = dict(ITEMS[0], origin="brief", why="Cheap open models move agent work off frontier APIs and onto whoever serves them.")
    assert "Why it matters (the AI Stack Brief's read): Cheap open models" in drafting.sources_block([brief])
    assert "Why it matters" not in drafting.sources_block([dict(ITEMS[1], why="rss note")])
    assert drafting.copied_spans("Funny thing: cheap open models move agent work off frontier APIs and onto whoever serves them.",
                                 drafting.sources_block([brief]))  # the brief's line still can't be pasted in
    cfg = settings.from_files(SPECIALISTS, PROMPTS)
    llm.FAKE_RESPONSES["scout"] = lambda m: json.dumps({"candidates": [
        {"item_ids": [0], "angle": "The moat is no longer the model, it's the price", "hook": "h"},
        {"item_ids": [1], "angle": "A $4B GPU deal over five years", "hook": "h"}], "pick": 0})
    from core.db import connect
    with connect() as db:
        ctx = llm.RunContext(db=db, run_id=db.start_run("market_intel", "draft", "t"))
        update, _, out = specialists.scout({"items": [brief, dict(ITEMS[1])], "recent_angles": []}, ctx, cfg["scout"])
    assert update["angle"]["angle"].startswith("A $4B") and "thesis" in out["why"]


def test_novelty_flags_same_ground_hook_and_ending():
    from core import novelty
    recent = ["Nscale's backlog is 85% two customers.\n\nLenders can see it and the debt costs more.\n\nAsk who else they serve."]
    assert novelty.check(recent[0], recent) and novelty.check("A fresh post about robots.\n\nAsk who else they serve.", recent) == [
        "ends the same way as a recent post (\"ask who else …\"); find a different ending"]
    assert novelty.check(GOOD_POST, recent) == []


def test_rejected_drafts_become_eval_cases(monkeypatch, tmp_path):
    from evals import run_evals
    from core.db import connect
    assert cli.run("collect") == 0 and cli.run("draft") == 0
    with connect() as db:
        db.execute("update drafts set status='rejected', feedback=%s", (db.json({"reasons": ["Sounds like AI"], "note": "stock"}),))
        cases = run_evals.rejections(db)
    assert len(cases) == 1 and cases[0]["reasons"] == ["Sounds like AI"] and cases[0]["items"][0]["url"] == "https://example.com/a"
    llm.FAKE_RESPONSES["rejection_check"] = lambda m: json.dumps({"repeats": True, "why": "same closer"})
    with connect() as db:
        ctx = llm.RunContext(db=db, run_id=db.start_run("market_intel", "eval", "t"))
        r = run_evals.run_rejection_case(ctx, cases[0])
    assert r["rejected"]["repeats"] and any("repeats what was rejected (Sounds like AI)" in v for v in r["violations"])



def _fake_case(n: int, gate=None, fail=None):
    from core.steps import step

    def fn(ctx):
        if gate:
            gate.wait()  # every case has to be running at once to get past this
        if fail:
            raise fail
        for i in (1, 2):
            with step(ctx, "market_intel", "writer", "work", i):
                ctx.log_call(run_id=ctx.run_id, node="writer", tier="voice", cost_usd=0.01 * (n + 1), ok=True)
        return {"case": f"c{n}", "mean": 4.5, "scores": {"hook": 5}, "violations": [], "humanizer": 5}
    return fn


def test_eval_cases_run_concurrently_with_their_own_steps_and_costs(monkeypatch, tmp_path):
    import threading
    from evals import run_evals
    from core.db import connect
    monkeypatch.setattr(run_evals, "HERE", tmp_path)
    monkeypatch.setattr(run_evals, "BASELINES", {"market_intel": tmp_path / "baseline.json"})
    gate = threading.Barrier(3, timeout=10)
    cases = [(f"c{n}", _fake_case(n, gate)) for n in range(3)]
    assert run_evals.run_suite("market_intel", cases, lambda r: r["case"], False, workers=3) == 0
    with connect() as db:
        run = db.one("select * from runs where kind='eval'")
        steps = db.all("select * from agent_steps where run_id=%s order by seq", (run["id"],))
    assert [r["case"] for r in run["stats"]["results"]] == ["c0", "c1", "c2"]  # in case order, however they finished
    work = {x["seq"]: float(x["cost_usd"]) for x in steps if x["specialist"] == "writer"}
    assert work == {1: 0.01, 2: 0.01, 101: 0.02, 102: 0.02, 201: 0.03, 202: 0.03}  # own seq block, own cost
    evals = [x for x in steps if x["specialist"] == "eval_case"]
    assert [(x["seq"], round(float(x["cost_usd"]), 4), x["summary"]) for x in evals] == [
        (900, 0.02, "c0"), (901, 0.04, "c1"), (902, 0.06, "c2")]
    assert float(run["cost_usd"]) == pytest.approx(0.12)


def test_a_budget_stop_in_one_case_keeps_the_finished_ones(monkeypatch, tmp_path):
    from evals import run_evals
    from core.budget import BudgetExceeded
    from core.db import connect
    monkeypatch.setattr(run_evals, "HERE", tmp_path)
    cases = [("c0", _fake_case(0)), ("c1", _fake_case(1, fail=BudgetExceeded("daily cap"))), ("c2", _fake_case(2))]
    assert run_evals.run_suite("market_intel", cases, lambda r: r["case"], False, workers=1) == 1
    with connect() as db:
        run = db.one("select * from runs where kind='eval'")
    done = [r["case"] for r in run["stats"]["results"]]
    assert run["status"] == "budget_exceeded" and run["error"] == "daily cap" and done[0] == "c0" and "c1" not in done


def test_evals_skip_the_one_pager_and_the_editor_reviews_on_its_own_tier():
    from evals import run_evals
    from core.db import connect

    def no_onepager(m):
        raise AssertionError("evals never score the one-pager")
    llm.FAKE_RESPONSES["onepager"] = no_onepager
    prompts = []
    write = llm.FAKE_RESPONSES["writer"]
    llm.FAKE_RESPONSES["writer"] = lambda m: (prompts.append(m[0].content), write(m))[1]
    with connect() as db:
        ctx = llm.RunContext(db=db, run_id=db.start_run("market_intel", "eval", "t"))
        r = run_evals.run_case(ctx, "2026-10-27", [dict(i, importance=5) for i in ITEMS], exclude="942e36b0112d")
        calls = db.all("select node, tier from llm_calls where run_id=%s", (ctx.run_id,))
    assert r["passed_final"] and "onepager" not in r["calls"]
    assert {c["tier"] for c in calls if c["node"] == "editor"} == {"review"}
    assert load_yaml("models.yaml")["tiers"]["review"]["reasoning"] == {"enabled": False}
    # the frozen rewrites reach the writer, but never the one built from the case's own reference post
    assert "### What Shivam posted" in prompts[0] and "Open models now handle 72% of the tokens" not in prompts[0]

def test_writer_keeps_the_best_of_several_candidates(monkeypatch):
    from agents.market_intel import specialists
    from agents.market_intel.roster import PROMPTS, SPECIALISTS
    from core import settings
    from core.db import connect
    monkeypatch.setattr("core.drafting.candidates", lambda tier, override, n=None: ["z-ai/glm-5.3", "z-ai/glm-5.3", "deepseek/deepseek-v4.1-flash"][:n])
    invented = GOOD_POST.replace("$0.20", "$0.07")  # a number that isn't in the sources
    templated = GOOD_POST.replace("I had to read that twice.", "My read: this just became a bucket.")
    replies = iter([invented, templated, GOOD_POST])
    llm.FAKE_RESPONSES["writer"] = lambda m: next(replies)
    state = {"angle": {"angle": "a"}, "sources": [{"url": "https://example.com/a", "title": "t", "source": "s",
             "summary": "An open-weight model scores 88 on X at $0.20 per million tokens."}], "facts": [], "week": "2026-10-23"}
    with connect() as db:
        ctx = llm.RunContext(db=db, run_id=db.start_run("market_intel", "draft", "t"))
        update, summary, out = specialists.writer(state, ctx, settings.from_files(SPECIALISTS, PROMPTS)["writer"])
    assert update["drafts"][-1]["post"] == GOOD_POST and out["model"] == "deepseek/deepseek-v4.1-flash"
    flags = [c["flags"] for c in out["candidates"]]
    assert flags[2] < flags[0] and flags[2] < flags[1] and "best of 3" in summary


def test_rejection_cases_are_reported_not_gated(tmp_path):
    from evals import run_evals
    results = [{"case": "2026-10-19", "mean": 4.5, "humanizer": 5, "violations": [], "scores": {"hook": 5}},
               {"case": "rejected x", "mean": 4.1, "humanizer": 4, "violations": ["repeats what was rejected"], "scores": {"hook": 4},
                "rejected": {"id": "x", "reasons": ["Sounds like AI"], "repeats": True}}]
    s = run_evals.summarize("market_intel", results)
    assert s["guardrail_pass_rate"] == 1.0 and s["rejection_repeats"] == 1 and s["golden_cases"] == 1
    assert run_evals.check(s, tmp_path / "none.json") == ([], None)
    assert "repeats a rejected problem: 1 of 1 (reported, not gated)" in run_evals.comment("market_intel", "r", s, results, [], None)


def test_rewrites_keep_model_variety_and_a_runaway_or_empty_draft_is_rescued(monkeypatch):
    from core import drafting
    monkeypatch.setattr(drafting, "candidates", REAL_CANDIDATES)
    monkeypatch.setattr("core.config.load_yaml", lambda name: {"tiers": {"voice": {
        "candidates": ["glm", "glm", "ds"], "fallbacks": ["ds"]}}})
    assert drafting.candidates("voice", None, n=2) == ["glm", "ds"] and drafting.candidates("voice", None) == ["glm", "glm", "ds"]
    assert drafting.candidates("voice", "sonnet", n=2) == ["sonnet"]
    replies = iter(["", "loop " * 2000, "A real post."])
    llm.FAKE_RESPONSES["writer"] = lambda m: next(replies)
    from core.db import connect
    with connect() as db:
        ctx = llm.RunContext(db=db, run_id=db.start_run("market_intel", "draft", "t"))
        outs = drafting.write_candidates(ctx, "voice", "writer", ["glm", "glm"], lambda i: "s", "u", str.strip, max_words=600)
    assert outs == [{"model": "ds", "text": "A real post."}]  # both candidates unusable: the rescue model's draft


def test_posts_that_did_well_teach_the_most_and_guide_the_writer():
    from core import lessons
    from core.db import connect
    assert cli.run("collect") == 0 and cli.run("draft") == 0
    with connect() as db:
        d = db.one("select * from drafts")
        db.execute("update drafts set status='approved', approved_at=%s, feedback=%s, learned_at=null where id=%s",
                   ("2026-10-23T00:00:00+00:00", db.json({"performance": "well"}), d["id"]))
        assert db.top_posts() == [d["post_md"]]
        evs = lessons.events(db, "market_intel")
        assert evs[0]["performance"] == "did well with readers"
        lessons.apply(db, "market_intel", {"add": [{"lesson": "Stack numbers from three companies, then one dry line.",
                                                    "from": [d["id"]]}]}, {d["id"]}, strong={d["id"]})
        assert lessons.active(db, "market_intel")[0]["evidence"] == 3
        db.execute("update drafts set sources=%s", (db.json([]),))  # free the stories for the next draft
    prompts = []
    write = llm.FAKE_RESPONSES["writer"]
    llm.FAKE_RESPONSES["writer"] = lambda m: (prompts.append(m[-1].content), write(m))[1]
    llm.FAKE_RESPONSES["learn"] = lambda m: json.dumps({"add": [], "confirm": [], "revise": [], "retire": []})
    assert cli.run("draft") == 0
    assert "posts that did well with readers" in prompts[0] and d["post_md"][:40] in prompts[0]


def test_flags_a_brief_line_passed_off_as_the_authors_own():
    why = "Why it matters: With open weights, it's tempting to pick whichever provider has the lowest token price."
    post = GOOD_POST.replace("#AI", "With open weights, it's tempting to pick whichever provider has the lowest token price.")
    assert any("copies a source's words" in v for v in graph.guardrail_violations(post, set(), why))
    quoted = GOOD_POST.replace("#AI", 'Vals says "it\'s tempting to pick whichever provider has the lowest token price" too.')
    assert not any("copies a source's words" in v for v in graph.guardrail_violations(quoted, set(), why))


def test_take_test_prefers_a_real_claim_and_a_scheduled_run_skips_when_none_passes():
    from agents.market_intel import specialists
    from core.db import connect
    claim = {"kind": "claim", "take": "Pick the provider by measured accuracy, not token price",
             "disagree": "The cheapest provider is usually good enough", "decision": "Run your own eval on each provider"}
    assert specialists.passes_take_test(claim)
    assert not specialists.passes_take_test({**claim, "kind": "recap"})
    assert not specialists.passes_take_test({**claim, "take": "Every lab will charge per task by 2028"})  # a prediction
    assert not specialists.passes_take_test({**claim, "decision": ""})
    recap = {"item_ids": [0], "angle": "a launch", "kind": "recap", "take": "Google launched a model this week"}
    llm.FAKE_RESPONSES["scout"] = lambda m: json.dumps({"candidates": [recap, {**claim, "item_ids": [1], "angle": "b"}], "pick": 0})
    from core import settings
    from agents.market_intel.roster import PROMPTS, SPECIALISTS
    with connect() as db:
        ctx = llm.RunContext(db=db, run_id=db.start_run("market_intel", "draft", "t"))
        out, summary, _ = specialists.scout({"items": [dict(i, importance=5) for i in ITEMS]}, ctx,
                                            settings.from_files(SPECIALISTS, PROMPTS)["scout"])
    assert out["angle"]["take"] == claim["take"] and "skip" not in out  # switched to the candidate whose take passes

    llm.FAKE_RESPONSES["scout"] = lambda m: json.dumps({"candidates": [recap], "pick": 0})
    assert cli.run("collect") == 0 and cli.run("draft", skip_if_pending=2) == 0
    with connect() as db:
        assert db.one("select count(*) as n from drafts")["n"] == 0  # nothing worth posting: no draft today
        run = db.one("select * from runs where kind='draft' and status != 'running'")
        assert run["status"] == "succeeded" and "take test" in run["stats"]["skipped"]
    assert cli.run("draft") == 0  # "Generate a new draft" always writes one
    with connect() as db:
        assert db.one("select count(*) as n from drafts")["n"] == 1
