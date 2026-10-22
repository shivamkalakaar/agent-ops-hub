"""Storage. Postgres (Supabase) when DATABASE_URL is set, otherwise a local SQLite file.

Queries are written once with %s placeholders; SQLite gets them rewritten to ?.
"""
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from core.config import ROOT

SCHEMA = ROOT / "supabase" / "schema.sql"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _is_pg() -> bool:
    return os.environ.get("DATABASE_URL", "").startswith(("postgres://", "postgresql://"))


ADD_COLUMN = re.compile(r"^alter table (\w+) add column if not exists (.+?);?$", re.M)


def _sqlite_schema() -> str:
    sql = SCHEMA.read_text()
    sql = re.sub(r"^alter table .*$", "", sql, flags=re.M)  # migrations are replayed by _sqlite_migrate
    sql = sql.replace("integer generated always as identity primary key", "integer primary key autoincrement")
    sql = re.sub(r"\b(timestamptz|jsonb)\b", "text", sql)
    return sql


class DB:
    def __init__(self):
        if _is_pg():
            import psycopg
            from psycopg.rows import dict_row
            self.pg = True
            self.conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)
        else:
            self.pg = False
            path = os.environ.get("SQLITE_PATH", str(ROOT / "local.db"))
            self.conn = sqlite3.connect(path, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.executescript(_sqlite_schema())
            self._sqlite_migrate()

    def _sqlite_migrate(self):
        """Replay `alter table ... add column if not exists` (SQLite has no IF NOT EXISTS for columns)."""
        for table, column in ADD_COLUMN.findall(SCHEMA.read_text()):
            name = column.split()[0]
            if name not in {r[1] for r in self.conn.execute(f"pragma table_info({table})")}:
                column = re.sub(r"\b(timestamptz|jsonb)\b", "text", column)
                self.conn.execute(f"alter table {table} add column {column}")

    def json(self, obj):
        """Wrap a value bound for a jsonb column."""
        if self.pg:
            from psycopg.types.json import Jsonb
            return Jsonb(obj)
        return json.dumps(obj, ensure_ascii=False)

    def execute(self, sql: str, params=()):
        if not self.pg:
            sql = sql.replace("%s", "?")
        return self.conn.execute(sql, params)

    def all(self, sql: str, params=()) -> list[dict]:
        rows = [dict(r) for r in self.execute(sql, params).fetchall()]
        if not self.pg:  # SQLite hands jsonb columns back as text
            for r in rows:
                for k, v in r.items():
                    if isinstance(v, str) and v[:1] in "[{":
                        try:
                            r[k] = json.loads(v)
                        except ValueError:
                            pass
        return rows

    def one(self, sql: str, params=()) -> dict | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    # --- runs -------------------------------------------------------------
    def start_run(self, agent: str, kind: str, git_sha: str) -> str:
        run_id = new_id()
        self.execute("insert into runs (id, agent, kind, status, started_at, git_sha) values (%s,%s,%s,'running',%s,%s)",
                     (run_id, agent, kind, now(), git_sha))
        return run_id

    def finish_run(self, run_id: str, status: str, *, trace_url=None, error=None, stats=None):
        cost = self.one("select coalesce(sum(cost_usd),0) as c from llm_calls where run_id=%s", (run_id,))["c"]
        self.execute("update runs set status=%s, ended_at=%s, cost_usd=%s, trace_url=%s, error=%s, stats=%s where id=%s",
                     (status, now(), float(cost), trace_url, error, self.json(stats or {}), run_id))

    # --- llm calls / spend ---------------------------------------------------
    def log_llm_call(self, **c):
        self.execute(
            "insert into llm_calls (run_id, node, tier, model, prompt_tokens, completion_tokens, cost_usd, latency_ms, ok, created_at)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (c["run_id"], c["node"], c["tier"], c.get("model"), c.get("prompt_tokens"), c.get("completion_tokens"),
             c.get("cost_usd", 0.0), c.get("latency_ms"), c["ok"], now()))

    def spend_since(self, since_iso: str) -> float:
        row = self.one("select coalesce(sum(cost_usd),0) as c from llm_calls where created_at >= %s", (since_iso,))
        return float(row["c"])

    # --- items ---------------------------------------------------------------
    def known_urls(self, urls: list[str]) -> set[str]:
        if not urls:
            return set()
        marks = ",".join(["%s"] * len(urls))
        return {r["url"] for r in self.all(f"select url from items where url in ({marks})", urls)}

    def insert_items(self, items: list[dict], run_id: str):
        for i in items:
            self.execute(
                "insert into items (url, title, source, layer, importance, why, summary, published_at, collected_at, run_id)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (url) do nothing",
                (i["url"], i["title"], i["source"], i.get("layer"), i.get("importance"), i.get("why"),
                 i.get("summary"), i.get("published"), now(), run_id))

    def unscored_items(self, since_iso: str) -> list[dict]:
        """Items stored without a score, e.g. pushed from the daily brief; collect scores them."""
        rows = self.all("select * from items where importance is null and collected_at >= %s", (since_iso,))
        return [{**r, "published": r["published_at"]} for r in rows]

    def update_scores(self, items: list[dict]):
        for i in items:
            self.execute("update items set layer=coalesce(%s, layer), importance=%s, why=coalesce(why, %s) where url=%s",
                         (i.get("layer"), i.get("importance"), i.get("why"), i["url"]))

    def top_items(self, since_iso: str, per_origin: dict | None = None) -> list[dict]:
        """Best-scored recent items, with a fixed share per origin so the curated brief can't crowd out RSS."""
        rows = []
        for origin, n in (per_origin or {"brief": 15, "rss": 10}).items():
            rows += self.all("select * from items where published_at >= %s and importance is not null and origin = %s"
                             " order by importance desc, published_at desc limit %s", (since_iso, origin, n))
        return sorted(rows, key=lambda r: (-r["importance"], str(r["published_at"])), reverse=False)

    # --- drafts --------------------------------------------------------------
    def insert_draft(self, d: dict, run_id: str) -> str:
        draft_id = new_id()
        self.execute(
            "insert into drafts (id, week, angle, post_md, onepager_html, sources, judge_scores, revisions, status, created_at, run_id)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s)",
            (draft_id, d["week"], self.json(d["angle"]), d["post_md"], d["onepager_html"], self.json(d["sources"]),
             self.json(d["judge_scores"]), d["revisions"], now(), run_id))
        return draft_id

    def approved_posts(self, limit: int = 3) -> list[str]:
        rows = self.all("select coalesce(edited_post_md, post_md) as post from drafts where status='approved'"
                        " order by approved_at desc limit %s", (limit,))
        return [r["post"] for r in rows]

    def recent_angles(self, limit: int = 5) -> list[str]:
        """Angles of the latest drafts in any status, so a pending or rejected topic isn't picked again."""
        rows = self.all("select angle from drafts order by created_at desc limit %s", (limit,))
        return [r["angle"].get("angle", "") for r in rows if isinstance(r["angle"], dict)]

    def search_items(self, terms: list[str], since_iso: str, limit: int = 8) -> list[dict]:
        """Recent items whose title or summary mentions any of the terms (research follow-ups; no paid web search)."""
        terms = [t.strip().lower() for t in terms if t.strip()][:6]
        if not terms:
            return []
        cond = " or ".join(["lower(title) like %s or lower(coalesce(summary,'')) like %s"] * len(terms))
        params = [p for t in terms for p in (f"%{t}%", f"%{t}%")]
        return self.all(f"select * from items where published_at >= %s and ({cond})"
                        " order by importance desc nulls last, published_at desc limit %s", (since_iso, *params, limit))

    # --- agent steps (live timeline) -------------------------------------------
    def _insert_id(self, sql: str, params) -> int:
        if self.pg:
            return self.execute(sql + " returning id", params).fetchone()["id"]
        return self.execute(sql, params).lastrowid

    def last_call_id(self, run_id: str) -> int:
        return int(self.one("select coalesce(max(id),0) as m from llm_calls where run_id=%s", (run_id,))["m"])

    def run_cost_after(self, run_id: str, call_id: int) -> float:
        return float(self.one("select coalesce(sum(cost_usd),0) as c from llm_calls where run_id=%s and id>%s",
                              (run_id, call_id))["c"])

    def start_step(self, run_id: str, seq: int, agent: str, specialist: str, kind: str, reason: str | None = None) -> int:
        return self._insert_id("insert into agent_steps (run_id, seq, agent, specialist, kind, reason, status, started_at)"
                               " values (%s,%s,%s,%s,%s,%s,'running',%s)", (run_id, seq, agent, specialist, kind, reason, now()))

    def finish_step(self, step_id: int, status: str, *, summary=None, output=None, cost_usd=0.0, latency_ms=None, reason=None):
        self.execute("update agent_steps set status=%s, summary=%s, output=%s, cost_usd=%s, latency_ms=%s, ended_at=%s,"
                     " reason=coalesce(%s, reason) where id=%s",
                     (status, summary, self.json(output) if output is not None else None, cost_usd, latency_ms, now(), reason, step_id))

    # --- agent registry, owner settings, prompt versions ----------------------------
    def sync_registry(self, agent: str, rows: list[dict]):
        for pos, r in enumerate(rows):
            self.execute(
                "insert into agent_registry (agent, specialist, position, role, description, default_model, allowed_models,"
                " required, default_max_calls, updated_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (agent, specialist) do update set position=excluded.position, role=excluded.role,"
                " description=excluded.description, default_model=excluded.default_model, allowed_models=excluded.allowed_models,"
                " required=excluded.required, default_max_calls=excluded.default_max_calls, updated_at=excluded.updated_at",
                (agent, r["specialist"], pos, r["role"], r.get("description"), r.get("default_model"),
                 self.json(r.get("allowed_models") or []), r.get("required", False), r.get("default_max_calls"), now()))

    def agent_settings(self, agent: str) -> dict[str, dict]:
        return {r["specialist"]: r for r in self.all("select * from agent_settings where agent=%s", (agent,))}

    def ensure_prompt(self, agent: str, specialist: str, body: str):
        """Seed v1 from the repo file the first time, so the dashboard has something to edit."""
        if not self.one("select id from prompt_versions where agent=%s and specialist=%s limit 1", (agent, specialist)):
            self.execute("insert into prompt_versions (id, agent, specialist, version, body, note, created_at)"
                         " values (%s,%s,%s,1,%s,'seeded from the repo prompt file',%s)", (new_id(), agent, specialist, body, now()))

    def prompt_version(self, version_id: str) -> dict | None:
        return self.one("select * from prompt_versions where id=%s", (version_id,))

    # --- substack drafts ---------------------------------------------------------
    def insert_substack_draft(self, d: dict, run_id: str) -> str:
        draft_id = new_id()
        self.execute(
            "insert into substack_drafts (id, slot, seed_draft_id, brief, title, subtitle, body_md, body_html, images, sources,"
            " judge_scores, fact_report, final_report, revisions, status, created_at, run_id)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s)",
            (draft_id, d["slot"], d.get("seed_draft_id"), self.json(d["brief"]), d["title"], d.get("subtitle"), d["body_md"],
             d["body_html"], self.json(d["images"]), self.json(d["sources"]), self.json(d["judge_scores"]),
             self.json(d["fact_report"]), self.json(d["final_report"]), d["revisions"], now(), run_id))
        return draft_id

    def substack_draft(self, draft_id: str) -> dict | None:
        return self.one("select * from substack_drafts where id=%s", (draft_id,))

    def update_substack_draft(self, draft_id: str, d: dict):
        """A revision replaces the post in place; the owner's earlier edits are folded in, so they're cleared."""
        self.execute(
            "update substack_drafts set title=%s, subtitle=%s, body_md=%s, body_html=%s, judge_scores=%s, fact_report=%s,"
            " final_report=%s, revisions=revisions+%s, status='pending', edited_title=null, edited_subtitle=null,"
            " edited_body_md=null, approved_at=null where id=%s",
            (d["title"], d["subtitle"], d["body_md"], d["body_html"], self.json(d["judge_scores"]), self.json(d["fact_report"]),
             self.json(d["final_report"]), d["rewrites"], draft_id))

    def item_summary(self, url: str) -> str | None:
        row = self.one("select summary from items where url=%s", (url,))
        return row["summary"] if row else None

    def last_substack_at(self) -> str | None:
        row = self.one("select max(created_at) as t from substack_drafts")
        return str(row["t"]) if row and row["t"] else None

    def used_substack_urls(self, limit: int = 20) -> set[str]:
        """Source URLs of recent Substack drafts in any status, so a story isn't written up twice."""
        rows = self.all("select sources from substack_drafts order by created_at desc limit %s", (limit,))
        return {s["url"] for r in rows if isinstance(r["sources"], list) for s in r["sources"]}

    def recent_substack_angles(self, limit: int = 6) -> list[str]:
        rows = self.all("select title from substack_drafts order by created_at desc limit %s", (limit,))
        return [r["title"] for r in rows]

    def approved_substack_posts(self, limit: int = 2) -> list[str]:
        rows = self.all("select coalesce(edited_body_md, body_md) as body from substack_drafts where status in ('approved','published')"
                        " order by approved_at desc limit %s", (limit,))
        return [r["body"] for r in rows]

    def published_musings(self, since_iso: str, limit: int = 6) -> list[dict]:
        """Approved LinkedIn musings (with the owner's edits) that no Substack draft has expanded yet."""
        return self.all(
            "select d.id, d.week, d.angle, coalesce(d.edited_post_md, d.post_md) as post, d.sources, d.series_no from drafts d"
            " where d.status='approved' and coalesce(d.published_at, d.approved_at) >= %s"
            " and not exists (select 1 from substack_drafts s where s.seed_draft_id = d.id)"
            " order by coalesce(d.published_at, d.approved_at) desc limit %s", (since_iso, limit))

    def musing(self, draft_id: str) -> dict | None:
        return self.one("select id, week, angle, coalesce(edited_post_md, post_md) as post, sources, series_no from drafts"
                        " where id=%s", (draft_id,))


@contextmanager
def connect():
    db = DB()
    try:
        yield db
    finally:
        db.conn.close()
