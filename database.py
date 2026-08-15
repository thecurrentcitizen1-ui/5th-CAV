from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row

from config import CONFIG

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "sql" / "schema.sql"


@contextmanager
def connection():
    if not CONFIG.database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    conn = psycopg.connect(CONFIG.database_url, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema() -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def fetch_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.fetchone()


def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return list(cur.fetchall())


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
