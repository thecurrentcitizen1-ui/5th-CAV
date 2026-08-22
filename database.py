from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from flask import g, has_request_context

from config import CONFIG

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "sql" / "schema.sql"


def _new_connection(*, autocommit: bool = False):
    if not CONFIG.database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(
        CONFIG.database_url,
        row_factory=dict_row,
        autocommit=autocommit,
        connect_timeout=10,
    )


def _request_connection():
    """Reuse one PostgreSQL connection for the lifetime of an HTTP request."""
    conn = getattr(g, "_battalion_db_conn", None)
    if conn is None or conn.closed:
        conn = _new_connection(autocommit=True)
        g._battalion_db_conn = conn
    return conn


def close_request_connection(_error=None):
    conn = g.pop("_battalion_db_conn", None) if has_request_context() else None
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


@contextmanager
def connection():
    if has_request_context():
        # Autocommit request connection: each statement is independent, so a
        # caught SQL error cannot leave all later page queries in an aborted tx.
        conn = _request_connection()
        yield conn
        return

    # Bootstrap/background code keeps normal transaction semantics.
    conn = _new_connection(autocommit=False)
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
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (15051966,))
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
