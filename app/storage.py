from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.config import settings


class Storage:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or settings.sqlite_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS page_cache (
                    cache_key TEXT PRIMARY KEY,
                    response_json TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cf_clearance (
                    domain TEXT PRIMARY KEY,
                    cookie_header TEXT NOT NULL,
                    user_agent TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS crawl_jobs (
                    job_id TEXT PRIMARY KEY,
                    seed_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pages_visited INTEGER NOT NULL DEFAULT 0,
                    max_pages INTEGER NOT NULL,
                    max_depth INTEGER NOT NULL,
                    errors_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS crawl_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_crawl_results_job ON crawl_results(job_id, id);
                """
            )

    def get_cached_page(self, cache_key: str) -> dict[str, Any] | None:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT response_json FROM page_cache WHERE cache_key = ? AND expires_at > ?",
                (cache_key, now),
            ).fetchone()
            return json.loads(row["response_json"]) if row else None

    def set_cached_page(self, cache_key: str, data: dict[str, Any], ttl: int | None = None) -> None:
        expires = time.time() + (ttl if ttl is not None else settings.cache_ttl_seconds)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO page_cache (cache_key, response_json, expires_at)
                VALUES (?, ?, ?)
                """,
                (cache_key, json.dumps(data), expires),
            )

    def get_cf_clearance(self, domain: str) -> tuple[str, str] | None:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cookie_header, user_agent FROM cf_clearance WHERE domain = ? AND expires_at > ?",
                (domain.lower(), now),
            ).fetchone()
            return (row["cookie_header"], row["user_agent"]) if row else None

    def set_cf_clearance(self, domain: str, cookie_header: str, user_agent: str) -> None:
        expires = time.time() + settings.cf_clearance_ttl_seconds
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO cf_clearance (domain, cookie_header, user_agent, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (domain.lower(), cookie_header, user_agent, expires),
            )

    def create_crawl_job(self, job_id: str, seed_url: str, max_pages: int, max_depth: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO crawl_jobs (job_id, seed_url, status, pages_visited, max_pages, max_depth, errors_json, created_at)
                VALUES (?, ?, 'queued', 0, ?, ?, '[]', ?)
                """,
                (job_id, seed_url, max_pages, max_depth, time.time()),
            )

    def get_crawl_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM crawl_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return None
            return {
                "job_id": row["job_id"],
                "seed_url": row["seed_url"],
                "status": row["status"],
                "pages_visited": row["pages_visited"],
                "max_pages": row["max_pages"],
                "max_depth": row["max_depth"],
                "errors": json.loads(row["errors_json"]),
            }

    def update_crawl_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        pages_visited: int | None = None,
        errors: list[str] | None = None,
    ) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM crawl_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return
            # Preserve cooperative cancellation
            new_status = "cancelled" if row["status"] == "cancelled" else (status or row["status"])
            new_pages = pages_visited if pages_visited is not None else row["pages_visited"]
            new_errors = json.dumps(errors) if errors is not None else row["errors_json"]
            conn.execute(
                "UPDATE crawl_jobs SET status = ?, pages_visited = ?, errors_json = ? WHERE job_id = ?",
                (new_status, new_pages, new_errors, job_id),
            )

    def append_crawl_result(self, job_id: str, url: str, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO crawl_results (job_id, url, result_json) VALUES (?, ?, ?)",
                (job_id, url, json.dumps(result)),
            )

    def get_crawl_results(
        self, job_id: str, cursor: int = 0, limit: int = 25
    ) -> tuple[list[dict[str, Any]], int | None]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, result_json FROM crawl_results
                WHERE job_id = ? AND id > ?
                ORDER BY id ASC LIMIT ?
                """,
                (job_id, cursor, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            sliced = rows[:limit]
            items = [json.loads(r["result_json"]) for r in sliced]
            next_cursor = sliced[-1]["id"] if has_more and sliced else None
            return items, next_cursor


storage = Storage()
