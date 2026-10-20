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


def _sqlite_schema() -> str:
    sql = SCHEMA.read_text()
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

    def top_items(self, since_iso: str, limit: int = 25) -> list[dict]:
        return self.all("select * from items where published_at >= %s and importance is not null"
                        " order by importance desc, published_at desc limit %s", (since_iso, limit))

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


@contextmanager
def connect():
    db = DB()
    try:
        yield db
    finally:
        db.conn.close()
