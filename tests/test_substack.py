"""Offline tests for the Substack newsroom: fake LLM and images, temp SQLite, local image storage. No keys, no cost."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core import llm, settings, storage
from core.config import load_yaml
from core.db import connect
from agents.substack import __main__ as cli
from agents.substack import render, supervisor
from agents.substack.roster import AGENT, PROMPTS, SPECIALISTS
from agents.substack.specialists import research

NOW = datetime.now(timezone.utc).isoformat(timespec="minutes")
GOOD_MD = (Path(__file__).parent / "fixtures" / "substack_post.md").read_text()
ARTICLE = ("The model scores 88 on X and costs $0.20 per million tokens. It is weaker on long multi-step tool use. "
           "Teams sampled 5% of traffic for review; the frontier keeps the hardest 10% of tasks. "
           "The neocloud's $4B GPU deal runs over five years.")
ITEMS = [
    {"url": "https://example.com/a", "title": "Open model matches frontier at 1/10 cost", "source": "Lab Blog", "layer": "models",
     "importance": 5, "why": "price", "summary": "A new open-weight model scores 88 on X at $0.20/M tokens.", "published": NOW},
    {"url": "https://example.com/b", "title": "Neocloud signs $4B GPU deal", "source": "Infra News", "layer": "compute",
     "importance": 4, "why": "capex", "summary": "Four billion dollars of GPUs over five years.", "published": NOW},
]
H2 = "Why the price matters more than the score"


def _allowed(messages) -> list[str]:
    m = re.search(r"# Allowed now\n(.+)", messages[-1].content)
    return [o.strip() for o in m.group(1).split(",")] if m else []


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODE", "fake")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    for var in ("DATABASE_URL", "LANGFUSE_PUBLIC_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "GITHUB_ACTIONS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(storage, "put", lambda path, data, content_type: f"https://img.example.com/{path}")
    monkeypatch.setattr(research, "fetch_article", lambda url: ARTICLE)
    monkeypatch.setattr("core.drafting.candidates", lambda tier, override, n=None: [override])  # one writer call per draft
    llm.FAKE_RESPONSES.update({
        "supervisor": lambda m: json.dumps({"next": supervisor.canonical(_allowed(m)), "reason": "next in line",
                                            "instructions": "Focus on the price."}),
        "research": lambda m: json.dumps({"item_ids": [0, 1], "seed_draft_id": None, "thesis": "Cheap open models change agent design",
                                          "title": "Cheap open models just changed the agent math", "subtitle": "s", "why_now": "this week",
                                          "outline": [{"heading": "What actually happened", "point": "p"}, {"heading": H2, "point": "p"},
                                                      {"heading": "Where the money is going instead", "point": "p"}]}),
        "research_facts": lambda m: json.dumps({"facts": [{"claim": "scores 88 on X", "evidence": "scores 88 on X", "source_id": 0},
                                                          {"claim": "bad id", "evidence": "x", "source_id": 9}],
                                                "counterpoints": ["weaker on tool use"], "gaps": [],
                                                "chart": {"title": "Two numbers", "unit": "", "source": "Lab Blog",
                                                          "series": [{"label": "Score on X", "value": 88}, {"label": "GPU deal ($B)", "value": 4}]}}),
        "research_followup": lambda m: json.dumps({"search_terms": ["neocloud"]}),
        "writer": lambda m: GOOD_MD,
        "editor": lambda m: json.dumps({"scores": dict.fromkeys(["hook", "thesis", "structure", "depth", "concreteness", "voice", "human"], 5),
                                        "notes": [], "critique": "Tight."}),
        "fact_checker": lambda m: json.dumps({"claims": [{"claim": "scores 88 on X", "verdict": "supported", "source": "Lab Blog"}]}),
        "illustrator": lambda m: json.dumps({"cover": {"prompt": "a toll booth with its gate lifted", "alt": "An open toll gate", "caption": ""},
                                             "inline": [{"after_section": H2, "prompt": "many small boats", "alt": "Small boats", "caption": "Volume"}],
                                             "chart": {"after_section": "Where the money is going instead", "alt": "Bar chart", "caption": ""}}),
        "image_check": lambda m: json.dumps({"ok": True, "score": 5, "issues": []}),
        "final_check": lambda m: json.dumps({"ready": True, "blockers": [], "polish": ["fine"]}),
    })
    with connect() as db:
        db.insert_items([dict(i) for i in ITEMS], None)


def _steps(db, run_id=None):
    run_id = run_id or db.one("select id from runs where agent='substack' and kind in ('draft','revise')"
                              " order by started_at desc, rowid desc")["id"]
    return db.all("select * from agent_steps where run_id=%s order by seq", (run_id,))


def test_newsroom_end_to_end():
    assert cli.draft(None) == 0
    with connect() as db:
        d = db.one("select * from substack_drafts")
        run = db.one("select * from runs where agent='substack'")
        steps = _steps(db, run["id"])
    assert run["status"] == "succeeded" and run["stats"]["passed"] is True
    assert d["status"] == "pending" and d["judge_scores"]["passed_review"] is True and d["revisions"] == 0
    assert d["title"] == "Cheap open models just changed the agent math" and d["subtitle"].startswith("An open-weight model")
    work = [s["specialist"] for s in steps if s["kind"] == "work"]
    assert work == ["supervisor", "research", "writer", "editor", "fact_checker", "illustrator", "final_check", "supervisor"]
    assert all(s["status"] == "ok" and s["ended_at"] for s in steps)
    decisions = [s for s in steps if s["kind"] == "decision"]
    assert decisions[0]["reason"] == "only allowed move" and any(s["output"]["by"] == "llm" for s in decisions)
    assert [i["kind"] for i in d["images"]] == ["cover", "inline", "chart"]
    body = d["body_md"]
    assert body.startswith("![An open toll gate](") and "## Sources" in body
    assert body.index("![Small boats]") > body.index(f"## {H2}") and body.index("![Bar chart]") > body.index("## Where the money")
    assert "<img" in d["body_html"] and "<h2>Sources</h2>" in d["body_html"]
    assert len(d["brief"]["facts"]) == 1  # the fact citing a source that doesn't exist was dropped


def test_invalid_supervisor_choice_falls_back():
    llm.FAKE_RESPONSES["supervisor"] = lambda m: '{"next": "publish_now", "reason": "yolo"}'
    assert cli.draft(None) == 0
    with connect() as db:
        steps = _steps(db)
        d = db.one("select * from substack_drafts")
    assert any(s["output"] and s["output"].get("by") == "fallback" and "publish_now" in s["reason"] for s in steps)
    assert d["judge_scores"]["passed_review"] is True  # the canonical order still runs every check


def test_invented_number_blocks_until_rewritten():
    drafts = iter([GOOD_MD.replace("signed a [$4B GPU deal]", "signed a [$9B GPU deal]"), GOOD_MD])
    llm.FAKE_RESPONSES["writer"] = lambda m: next(drafts)
    assert cli.draft(None) == 0
    with connect() as db:
        d = db.one("select * from substack_drafts")
        steps = _steps(db)
    assert "$9B" not in d["body_md"] and d["revisions"] == 1 and d["judge_scores"]["passed_review"] is True
    edits = [s for s in steps if s["specialist"] == "editor"]
    assert any("not in the sources" in v for v in edits[0]["output"]["violations"])


def test_limits_finish_with_best_version_flagged():
    llm.FAKE_RESPONSES["final_check"] = lambda m: '{"ready": false, "blockers": ["the ending overclaims"]}'
    assert cli.draft(None) == 0
    with connect() as db:
        d = db.one("select * from substack_drafts")
        run = db.one("select * from runs where agent='substack'")
        steps = _steps(db)
    assert d["judge_scores"]["passed_review"] is False and run["status"] == "succeeded"
    calls = run["stats"]["calls"]
    assert calls["writer"] == 3 and calls["final_check"] == 3 and run["stats"]["steps"] <= 14
    assert d["final_report"]["blockers"] == ["the ending overclaims"]
    assert sum(s["specialist"] == "illustrator" for s in steps) == 1  # headings didn't change, so no re-illustration


def test_disabled_specialist_is_skipped_and_overrides_are_validated():
    with connect() as db:
        db.execute("insert into prompt_versions (id, agent, specialist, version, body, note, created_at)"
                   " values ('pv1','substack','editor',2,'CUSTOM EDITOR PROMPT {{AUTHOR}}','test',%s)", (NOW,))
        for sp, enabled, model, cap, pv in [("illustrator", False, None, None, None), ("writer", False, "evil/model", 99, None),
                                            ("editor", True, "qwen/qwen3.7-flash", 0, "pv1")]:
            db.execute("insert into agent_settings (agent, specialist, enabled, model, max_calls, prompt_version_id, updated_at)"
                       " values ('substack',%s,%s,%s,%s,%s,%s)", (sp, enabled, model, cap, pv, NOW))
        cfg = settings.load(db, AGENT, SPECIALISTS, PROMPTS)
        assert cfg["writer"].enabled and cfg["writer"].model is None and cfg["writer"].max_calls == 5  # required; bad model; clamped
        assert not cfg["illustrator"].enabled and cfg["editor"].model == "qwen/qwen3.7-flash" and cfg["editor"].max_calls == 1
        assert cfg["editor"].prompt.startswith("CUSTOM EDITOR") and cfg["editor"].prompt_version == "v2"
        assert {r["specialist"] for r in db.all("select specialist from agent_registry")} == {s.name for s in SPECIALISTS}
        assert db.one("select count(*) as n from prompt_versions where version=1")["n"] >= 6  # seeded from files
    seen = {}
    llm.FAKE_RESPONSES["editor"] = (lambda f: lambda m: (seen.setdefault("system", m[0].content), f(m))[1])(llm.FAKE_RESPONSES["editor"])
    assert cli.draft(None) == 0
    with connect() as db:
        steps = _steps(db)
        d = db.one("select * from substack_drafts")
    assert "illustrator" not in {s["specialist"] for s in steps} and d["images"] == []
    assert seen["system"] == "CUSTOM EDITOR PROMPT Shivam" and d["judge_scores"]["passed_review"] is True


def test_forced_seed_and_used_stories_are_skipped():
    with connect() as db:
        run_id = db.start_run("market_intel", "draft", "t")
        mid = db.insert_draft({"week": "2026-W42", "angle": {"angle": "a"}, "post_md": "Open models got cheap.", "onepager_html": "x",
                               "sources": [], "judge_scores": {}, "revisions": 0}, run_id)
        db.execute("update drafts set status='approved', approved_at=%s where id=%s", (NOW, mid))
        assert [m["id"] for m in db.published_musings("2000-01-01")] == [mid]
    assert cli.draft(mid) == 0
    with connect() as db:
        assert db.one("select seed_draft_id from substack_drafts")["seed_draft_id"] == mid
        assert db.published_musings("2000-01-01") == []  # expanded once, not offered again
        assert db.used_substack_urls() == {"https://example.com/a", "https://example.com/b"}
    with pytest.raises(RuntimeError, match="no new scored items"):
        cli.draft(None)  # both stories already written up


def test_budget_guard_stops_newsroom(monkeypatch):
    cfg = dict(load_yaml("budget.yaml"), daily_cap_usd=0.0, daily_cap_overrides={})
    monkeypatch.setattr("core.budget.load_yaml", lambda name: cfg)
    assert cli.draft(None) == 0
    with connect() as db:
        run = db.one("select * from runs where agent='substack'")
        assert run["status"] == "budget_exceeded"
        assert db.one("select count(*) as n from substack_drafts")["n"] == 0


def test_render_escapes_model_html_and_draws_charts():
    html = render.to_html('<script>alert(1)</script>\n\n[x](javascript:alert(1)) ![a"><img src=x onerror=1>](https://e.com/i.jpg)')
    assert "<script>" not in html and 'href="javascript' not in html and "<img src=x" not in html
    page = render.page('T</title><script>', "s", html)
    assert "<script>" not in page
    png = render.chart_png({"title": "t", "unit": "%", "series": [{"label": "a", "value": 1}, {"label": "b", "value": 2.5}]})
    assert png[:4] == b"\x89PNG"
    with pytest.raises(ValueError):
        render.chart_png({"series": [{"label": "a", "value": 1}]})


def test_substack_evals_offline(monkeypatch, tmp_path):
    from evals import run_evals
    monkeypatch.setattr(run_evals, "BASELINES", {"substack": tmp_path / "baseline.json"})
    monkeypatch.setattr(run_evals, "HERE", tmp_path)
    golden = tmp_path / "golden"
    golden.mkdir()
    for n in range(6):
        (golden / f"2026-10-0{n + 1}.json").write_text(json.dumps({"items": ITEMS}))
    monkeypatch.setattr("core.humanizer.invented_facts", lambda sources, post: [])  # canned post vs. fake golden sources
    monkeypatch.setattr("agents.substack.checks.drafting.humanizer.invented_facts", lambda sources, post: [])
    monkeypatch.setattr("sys.argv", ["run_evals", "--agent", "substack", "--cases", "2", "--update-baseline"])
    assert run_evals.main() == 0
    assert run_evals.substack_cases(5)[0][0] == "2026-10-04..2026-10-06"
    result = json.loads((tmp_path / "results" / "substack.json").read_text())
    assert result["summary"]["cases"] == 2 and result["summary"]["guardrail_pass_rate"] == 1
    assert all(r["calls"].get("illustrator") is None for r in result["results"])  # images off unless --images


def test_image_placement_tolerates_heading_punctuation_and_off_topic_charts_are_dropped():
    body = "Intro.\n\n## Price the finished outcome\n\nFirst para.\n\nSecond para.\n\n## Next\n\nMore."
    out = render.place_images(body, [{"kind": "inline", "url": "https://e.com/i.jpg", "alt": "a", "after_section": "Price the finished outcome:"}])
    assert out.index("![a]") < out.index("## Next") and out.index("![a]") > out.index("First para.")
    from agents.substack.specialists import illustrator
    spec = {"series": [{"label": "x", "value": 27}, {"label": "y", "value": 9}]}
    assert illustrator._chart_ok(spec, "went from 27% to 9%") and not illustrator._chart_ok(spec, "went from 27% to 12%")
    png = render.chart_png({"title": "t", "series": [{"label": "Junior share of technical hires (earlier period)", "value": 27},
                                                     {"label": "Junior share of technical hires (latest period)", "value": 9}]})
    assert png[:4] == b"\x89PNG"


@pytest.mark.parametrize("off", ["editor", "fact_checker", "illustrator"])
def test_any_optional_specialist_can_be_off_and_final_check_still_gates(off):
    with connect() as db:
        db.execute("insert into agent_settings (agent, specialist, enabled, updated_at) values ('substack',%s,%s,%s)", (off, False, NOW))
    assert cli.draft(None) == 0
    with connect() as db:
        work = [s["specialist"] for s in _steps(db) if s["kind"] == "work"]
        d = db.one("select * from substack_drafts")
    assert off not in work and "final_check" in work and work.index("final_check") == len(work) - 2
    assert d["judge_scores"]["passed_review"] is True


def test_unparseable_json_retries_on_the_fallback_model():
    seen = []

    def fact(m):
        seen.append(1)
        return "" if len(seen) == 1 else json.dumps({"supported_count": 7, "claims": []})
    llm.FAKE_RESPONSES["fact_checker"] = fact
    assert cli.draft(None) == 0
    with connect() as db:
        d = db.one("select * from substack_drafts")
    assert len(seen) == 2 and d["fact_report"]["supported"] == 7 and d["judge_scores"]["passed_review"] is True


def test_saved_fallback_version_is_the_one_with_fewest_fact_problems():
    """Writer runs out of calls without passing: the saved version must weigh fact-check problems, not just guardrails."""
    versions = iter([GOOD_MD.replace("Most coverage", "Most early coverage"), GOOD_MD, GOOD_MD.replace("Most coverage", "Nearly all coverage")])
    llm.FAKE_RESPONSES["writer"] = lambda m: next(versions)
    fact_calls = iter([3, 0, 1])  # unsupported claims found in v1, v2, v3

    def fact(m):
        n = next(fact_calls)
        return json.dumps({"supported_count": 10, "claims": [{"claim": f"claim {i}", "verdict": "unsupported", "fix": "remove"} for i in range(n)]})
    llm.FAKE_RESPONSES["fact_checker"] = fact
    llm.FAKE_RESPONSES["final_check"] = lambda m: '{"ready": false, "blockers": ["no"]}'
    assert cli.draft(None) == 0
    with connect() as db:
        d = db.one("select * from substack_drafts")
    assert d["judge_scores"]["passed_review"] is False and d["judge_scores"]["version"] == 2
    assert "Most coverage" in d["body_md"]


def test_images_are_skipped_on_ci_without_storage(monkeypatch):
    monkeypatch.setattr(storage, "usable", lambda: False)
    assert cli.draft(None) == 0
    with connect() as db:
        d = db.one("select * from substack_drafts")
        step = next(s for s in _steps(db) if s["specialist"] == "illustrator")
    assert d["images"] == [] and "storage isn't configured" in step["summary"]
    assert d["judge_scores"]["passed_review"] is True  # the post still goes out for review


def test_storage_usable_only_when_configured_on_ci(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert not storage.usable()
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sb_secret_x")
    assert storage.usable()
    monkeypatch.delenv("GITHUB_ACTIONS")
    monkeypatch.delenv("SUPABASE_SERVICE_KEY")
    assert storage.usable()  # local runs fall back to ./out/img


def test_hedges_are_warnings_not_blockers():
    llm.FAKE_RESPONSES["fact_checker"] = lambda m: json.dumps({"supported_count": 9, "claims": [
        {"claim": "costs $0.20 per million tokens", "verdict": "hedge", "fix": "say it's the lab's price sheet"}]})
    assert cli.draft(None) == 0
    with connect() as db:
        d = db.one("select * from substack_drafts")
    assert d["judge_scores"]["passed_review"] is True and d["final_report"]["blockers"] == []
    assert any(p.startswith("needs hedging") for p in d["final_report"]["polish"])


def test_checked_version_beats_unchecked_one_with_fewer_known_problems():
    from agents.substack import ranking
    from core.settings import Resolved
    base = {"md": "x", "overall": 3.0, "accent": {"score": 3}, "scores": {}}
    state = {"settings": {"fact_checker": Resolved(True, None, 3, "", "file")}, "candidates": {
        "1": {**base, "version": 1, "violations": ["a", "b"]},                                              # never checked
        "3": {**base, "version": 3, "violations": ["a", "b"], "fact_issues": ["c", "d", "e"], "fact_checked": True}}}
    assert ranking.best(state)["version"] == 3


def test_long_subtitle_is_a_warning_not_a_violation():
    from agents.substack import checks
    md = GOOD_MD.replace("*An open-weight model scored", "*" + "x" * 120 + " An open-weight model scored")
    assert not any("subtitle" in v for v in checks.violations(md, {"https://example.com/a", "https://example.com/b"}))
    assert checks.warnings(md) and "subtitle" in checks.warnings(md)[0]


def test_writer_sees_source_urls_and_last_rewrite_is_kept_for_the_final_check():
    seen = {}

    def writer_fn(m):
        seen.setdefault("user", m[-1].content)
        return GOOD_MD
    llm.FAKE_RESPONSES["writer"] = writer_fn
    llm.FAKE_RESPONSES["editor"] = lambda m: json.dumps({"scores": dict.fromkeys(  # always weak: wants a rewrite
        ["hook", "thesis", "structure", "depth", "concreteness", "voice", "human"], 2), "notes": [], "critique": "weak"})
    finals = iter(['{"ready": false, "blockers": ["fix the ending"]}'] + ['{"ready": true, "blockers": []}'] * 5)
    llm.FAKE_RESPONSES["final_check"] = lambda m: next(finals)
    assert cli.draft(None) == 0
    assert "URL: https://example.com/a" in seen["user"] and "The only URLs you may link" in seen["user"]
    with connect() as db:
        work = [s["specialist"] for s in _steps(db) if s["kind"] == "work"]
    writes = [i for i, w in enumerate(work) if w == "writer"]
    assert len(writes) == 3 and work.index("final_check") < writes[-1]  # third write came after the final check


def test_revise_fixes_blockers_in_place_and_keeps_images():
    llm.FAKE_RESPONSES["final_check"] = lambda m: '{"ready": false, "blockers": ["the ending overclaims"]}'
    assert cli.draft(None) == 0
    with connect() as db:
        d = db.one("select * from substack_drafts")
        db.execute("update substack_drafts set edited_body_md=%s where id=%s", (d["body_md"].replace("Most coverage", "Most early coverage"), d["id"]))
    assert d["judge_scores"]["passed_review"] is False
    seen = {}
    llm.FAKE_RESPONSES["writer"] = lambda m: (seen.setdefault("user", m[-1].content), GOOD_MD)[1]
    llm.FAKE_RESPONSES["final_check"] = lambda m: '{"ready": true, "blockers": []}'
    assert cli.revise_draft(d["id"]) == 0
    with connect() as db:
        after = db.one("select * from substack_drafts")
        n = db.one("select count(*) as n from substack_drafts")["n"]
    assert n == 1 and after["judge_scores"]["passed_review"] is True and after["edited_body_md"] is None
    assert "the ending overclaims" in seen["user"] and "Most early coverage" in seen["user"]  # blockers + owner's edits sent
    assert "![" not in seen["user"].split("# Current draft")[1].split("# Editor")[0]           # images stripped for the writer
    assert after["body_md"].count("![") == 3 and after["revisions"] == d["revisions"] + 1     # images put back


def test_revise_keeps_the_saved_version_when_rewrites_dont_help():
    llm.FAKE_RESPONSES["final_check"] = lambda m: '{"ready": false, "blockers": ["a"]}'
    assert cli.draft(None) == 0
    with connect() as db:
        before = db.one("select * from substack_drafts")
    llm.FAKE_RESPONSES["final_check"] = lambda m: '{"ready": false, "blockers": ["a", "b", "c"]}'
    assert cli.revise_draft(before["id"]) == 0
    with connect() as db:
        after = db.one("select * from substack_drafts")
        steps = _steps(db)
    assert after["body_md"] == before["body_md"] and "Kept the saved version" in steps[-1]["summary"]
    assert sum(s["specialist"] == "writer" for s in steps) == 2  # MAX_ATTEMPTS


def test_monthly_cap_override_applies_only_to_its_month():
    from core import budget
    cfg = {"monthly_cap_usd": 6.0, "monthly_cap_overrides": {"2026-10": 10.0}}
    assert budget.monthly_cap(cfg, datetime(2026, 10, 21, tzinfo=timezone.utc)) == 10.0
    assert budget.monthly_cap(cfg, datetime(2026, 11, 1, tzinfo=timezone.utc)) == 6.0
    assert budget.monthly_cap({"monthly_cap_usd": 6.0}, datetime(2026, 10, 21, tzinfo=timezone.utc)) == 6.0


def test_substack_gate_allows_one_flagged_post_in_three(tmp_path):
    from evals import run_evals
    base = {"mean": 4.2, "humanizer_mean": 4.3}
    ok = run_evals.gate({**base, "guardrail_pass_rate": 2 / 3}, tmp_path / "b.json", False, run_evals.MIN_GUARDRAIL_PASS["substack"])
    bad = run_evals.gate({**base, "guardrail_pass_rate": 1 / 3}, tmp_path / "b.json", False, run_evals.MIN_GUARDRAIL_PASS["substack"])
    strict = run_evals.gate({**base, "guardrail_pass_rate": 2 / 3}, tmp_path / "b.json", False)
    assert (ok, bad, strict) == (0, 1, 1)


def test_daily_cap_override_applies_only_to_its_date():
    from core import budget
    cfg = {"daily_cap_usd": 3.0, "daily_cap_overrides": {"2026-10-23": 7.0}}
    assert budget.daily_cap(cfg, datetime(2026, 10, 23, 23, 59, tzinfo=timezone.utc)) == 7.0
    assert budget.daily_cap(cfg, datetime(2026, 10, 24, tzinfo=timezone.utc)) == 3.0
    assert budget.daily_cap({"daily_cap_usd": 3.0}, datetime(2026, 10, 23, tzinfo=timezone.utc)) == 3.0
