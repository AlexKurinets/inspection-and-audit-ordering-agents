"""Read API for the dashboard, plus the one write a person makes: resolving an exception.

Resolving requires the role token for the exception's required role. The API never posts
to the policy system; it records the decision and the worker carries it out.
"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from . import db, metrics, trail
from .orders import ResolutionError, RoleError, resolve_exception


def role_tokens() -> dict[str, str]:
    raw = os.environ.get("ROLE_TOKENS", "")
    return dict(pair.split(":", 1) for pair in raw.split(",") if ":" in pair)


def role_for(token: str | None) -> str:
    for role, expected in role_tokens().items():
        if token and hmac.compare_digest(token, expected):
            return role
    raise HTTPException(401, "unknown or missing X-Role-Token")


class Resolution(BaseModel):
    action: Literal["approve", "reject"]
    note: str = ""
    name: str | None = None


def create_app(dsn: str | None = None) -> FastAPI:
    pool = ConnectionPool(dsn or db.dsn(), kwargs={"row_factory": dict_row}, min_size=1, max_size=6, open=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool.open()
        yield
        pool.close()

    app = FastAPI(title="Inspection and audit ordering agents", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict:
        with pool.connection() as conn:
            return {"ok": True, "today": db.today(conn)}

    @app.get("/metrics/summary")
    def summary() -> dict:
        with pool.connection() as conn:
            return metrics.summary(conn)

    @app.get("/metrics/daily")
    def daily() -> list[dict]:
        with pool.connection() as conn:
            return metrics.daily(conn)

    @app.get("/metrics/vendors")
    def vendors() -> list[dict]:
        with pool.connection() as conn:
            return metrics.vendors(conn)

    @app.get("/orders")
    def list_orders(kind: str | None = None, state: str | None = None, policy: str | None = None,
                    limit: int = Query(200, le=2000), offset: int = 0) -> dict:
        clauses, args = [], []
        for col, val in (("kind", kind), ("state", state), ("policy_number", policy)):
            if val:
                clauses.append(f"{col} = %s")
                args.append(val)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with pool.connection() as conn:
            total = conn.execute(f"SELECT count(*) AS n FROM ops.orders {where}", args).fetchone()["n"]
            rows = conn.execute(
                f"""SELECT id, order_key, kind, policy_number, order_type, vendor, trigger, state, identified_on,
                           order_on, ordered_on, due_on, completed_on, closed_on, vendor_ref, human_touched
                    FROM ops.orders {where} ORDER BY id LIMIT %s OFFSET %s""", [*args, limit, offset]).fetchall()
        return {"total": total, "orders": rows}

    @app.get("/orders/{order_id}")
    def order_detail(order_id: int) -> dict:
        with pool.connection() as conn:
            order = conn.execute("SELECT * FROM ops.orders WHERE id = %s", (order_id,)).fetchone()
            if order is None:
                raise HTTPException(404, "no such order")
            policy = conn.execute("SELECT * FROM pas.policies WHERE policy_number = %s",
                                  (order["policy_number"],)).fetchone()
            return {
                "order": order,
                "policy": policy,
                "exposures": conn.execute("SELECT * FROM pas.policy_exposures WHERE policy_number = %s",
                                          (order["policy_number"],)).fetchall(),
                "events": conn.execute("SELECT seq, occurred_on, actor, event_type, from_state, to_state, payload, "
                                       "prev_hash, hash FROM ops.events WHERE order_id = %s ORDER BY seq",
                                       (order_id,)).fetchall(),
                "trail": trail.verify(conn, order_id),
                "exceptions": conn.execute("SELECT * FROM ops.exceptions WHERE order_id = %s ORDER BY id",
                                           (order_id,)).fetchall(),
                "documents": conn.execute("SELECT id, source, sha256, pages, ocr_pages, received_on, text "
                                          "FROM ops.documents WHERE order_id = %s ORDER BY id", (order_id,)).fetchall(),
                "pas_activity": conn.execute("SELECT logged_on, author, note FROM pas.activity WHERE policy_number = %s "
                                             "ORDER BY id", (order["policy_number"],)).fetchall(),
                "endorsement": conn.execute("SELECT * FROM pas.audit_endorsements WHERE order_id = %s",
                                            (order_id,)).fetchone(),
            }

    @app.get("/exceptions")
    def list_exceptions(status: str = "open", limit: int = Query(200, le=2000)) -> list[dict]:
        with pool.connection() as conn:
            return conn.execute(
                """SELECT e.*, o.kind, o.policy_number, o.order_type, o.vendor, o.state AS order_state
                   FROM ops.exceptions e JOIN ops.orders o ON o.id = e.order_id
                   WHERE e.status = %s ORDER BY e.opened_on, e.id LIMIT %s""", (status, limit)).fetchall()

    @app.post("/exceptions/{exception_id}/resolve")
    def resolve(exception_id: int, body: Resolution, x_role_token: str | None = Header(default=None)) -> dict:
        role = role_for(x_role_token)
        with pool.connection() as conn:
            try:
                order = resolve_exception(conn, exception_id, role, body.action, db.today(conn), body.note, body.name)
            except RoleError as e:
                conn.rollback()
                raise HTTPException(403, str(e)) from e
            except ResolutionError as e:
                conn.rollback()
                raise HTTPException(409, str(e)) from e
        return {"order_id": order["id"], "state": order["state"], "resolved_by": role}

    return app


app = create_app()
