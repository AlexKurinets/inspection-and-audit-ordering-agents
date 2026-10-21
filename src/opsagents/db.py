from __future__ import annotations

import os
from datetime import date
from importlib import resources

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "postgresql://ops:ops@localhost:5474/ops"


def dsn() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DSN)


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or dsn(), row_factory=dict_row)


def migrate(conn: psycopg.Connection) -> None:
    sql = resources.files("opsagents").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.execute(sql)
    conn.commit()


def reset(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA IF EXISTS ops CASCADE; DROP SCHEMA IF EXISTS sim CASCADE; DROP SCHEMA IF EXISTS pas CASCADE")
    conn.commit()
    migrate(conn)


def today(conn: psycopg.Connection) -> date:
    """The agents' business date. In sim mode it is the simulated day; otherwise the real one."""
    if os.environ.get("CLOCK_MODE", "sim") == "real":
        return date.today()
    row = conn.execute("SELECT today FROM ops.clock WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError("ops.clock is not set; run `opsagents seed` or `opsagents advance` first")
    return row["today"]


def set_today(conn: psycopg.Connection, day: date) -> None:
    conn.execute(
        "INSERT INTO ops.clock (id, today) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET today = EXCLUDED.today",
        (day,),
    )
