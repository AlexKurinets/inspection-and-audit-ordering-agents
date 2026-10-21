"""The outside world for the demo: two vendor REST APIs, an email-only vendor, insureds and a mailbox.

Vendors expose order submission with an Idempotency-Key, a change feed per vendor and
report downloads. The mailbox behaves like a shared inbox the agents read with a cursor.
``POST /_sim/clock`` moves simulated time forward and releases whatever was planned for it.
"""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import asynccontextmanager
from datetime import date

import psycopg
from fastapi import FastAPI, Header, HTTPException, Response
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from . import outcomes, reports

API_VENDORS = {"LFS": "inspection", "HAP": "audit"}
TAG = re.compile(r"\[(ORD-\d+)\]")


class ClockIn(BaseModel):
    today: date


class MailIn(BaseModel):
    sender: str
    recipient: str
    subject: str
    body: str
    form: dict | None = None


def create_app(dsn: str | None = None) -> FastAPI:
    pool = ConnectionPool(dsn or os.environ.get("DATABASE_URL", "postgresql://ops:ops@localhost:5474/ops"),
                          kwargs={"row_factory": dict_row}, min_size=1, max_size=4, open=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool.open()
        yield
        pool.close()

    app = FastAPI(title="Vendor and mailbox simulator", lifespan=lifespan)
    app.state.pool = pool

    def today(conn: psycopg.Connection) -> date:
        row = conn.execute("SELECT today FROM sim.clock WHERE id = 1").fetchone()
        if row is None:
            raise HTTPException(409, "simulator clock not set")
        return row["today"]

    def add_plans(conn: psycopg.Connection, plans: list, extra: dict) -> None:
        for due_on, channel, body in plans:
            conn.execute("INSERT INTO sim.plans (due_on, channel, body) VALUES (%s, %s, %s)",
                         (due_on, channel, Jsonb({**extra, **body})))

    @app.get("/health")
    def health() -> dict:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return {"ok": True}

    @app.get("/_sim/clock")
    def get_clock() -> dict:
        with pool.connection() as conn:
            return {"today": today(conn)}

    @app.post("/_sim/clock")
    def set_clock(body: ClockIn) -> dict:
        released = 0
        with pool.connection() as conn:
            conn.execute("INSERT INTO sim.clock (id, today) VALUES (1, %s) "
                         "ON CONFLICT (id) DO UPDATE SET today = EXCLUDED.today", (body.today,))
            due = conn.execute("SELECT * FROM sim.plans WHERE NOT released AND due_on <= %s ORDER BY due_on, id "
                               "FOR UPDATE", (body.today,)).fetchall()
            for p in due:
                b = p["body"]
                report = b.get("report") or (b.get("attachment") or {}).get("report")
                if report:
                    conn.execute("INSERT INTO sim.reports (report_id, spec) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                 (report["report_id"], Jsonb(report)))
                if p["channel"] == "feed":
                    conn.execute("INSERT INTO sim.feed (vendor, vendor_ref, status, occurred_on, report_id) "
                                 "VALUES (%s, %s, %s, %s, %s)",
                                 (b["vendor"], b["vendor_ref"], b["status"], p["due_on"], b.get("report_id")))
                else:
                    att = b.get("attachment")
                    conn.execute("INSERT INTO sim.mailbox (folder, sent_on, sender, recipient, subject, body, "
                                 "attachment) VALUES ('inbox', %s, %s, %s, %s, %s, %s)",
                                 (p["due_on"], b["sender"], b["recipient"], b["subject"], b["body"],
                                  f"/mail/attachments/{att['report_id']}" if att else None))
                conn.execute("UPDATE sim.plans SET released = true WHERE id = %s", (p["id"],))
                released += 1
        return {"today": body.today, "released": released}

    @app.post("/vendors/{vendor}/orders")
    def submit(vendor: str, payload: dict, response: Response, idempotency_key: str = Header(...)) -> dict:
        if vendor not in API_VENDORS:
            raise HTTPException(404, "unknown vendor")
        h = int(hashlib.sha256(idempotency_key.encode()).hexdigest()[:8], 16)
        with pool.connection() as conn:
            attempt = conn.execute(
                "INSERT INTO sim.submit_attempts (idempotency_key, attempts) VALUES (%s, 1) "
                "ON CONFLICT (idempotency_key) DO UPDATE SET attempts = sim.submit_attempts.attempts + 1 "
                "RETURNING attempts", (idempotency_key,)).fetchone()["attempts"]
            conn.commit()
            if h % 211 == 0 and attempt <= 9:
                raise HTTPException(503, "vendor maintenance window")
            if h % 29 == 0 and attempt <= 3:
                raise HTTPException(503, "upstream unavailable")
            existing = conn.execute("SELECT vendor_ref FROM sim.vendor_orders WHERE idempotency_key = %s",
                                    (idempotency_key,)).fetchone()
            if existing:
                response.status_code = 200
                return {"vendor_ref": existing["vendor_ref"], "duplicate": True}
            now = today(conn)
            n = conn.execute("SELECT count(*) AS n FROM sim.vendor_orders WHERE vendor = %s", (vendor,)).fetchone()["n"]
            ref = f"{vendor}-{100001 + n}"
            planner = outcomes.plan_api_inspection if API_VENDORS[vendor] == "inspection" else outcomes.plan_api_audit
            plans = planner(idempotency_key, vendor, ref, payload, now)
            conn.execute("INSERT INTO sim.vendor_orders (vendor_ref, vendor, idempotency_key, payload, outcome, "
                         "received_on) VALUES (%s, %s, %s, %s, %s, %s)",
                         (ref, vendor, idempotency_key, Jsonb(payload), Jsonb({"plans": len(plans)}), now))
            add_plans(conn, plans, {"vendor": vendor, "vendor_ref": ref})
            conn.commit()
            if h % 20 == 0 and attempt == 1:
                # The order is placed but the response is lost, as a gateway timeout.
                raise HTTPException(504, "gateway timeout")
        response.status_code = 201
        return {"vendor_ref": ref, "duplicate": False}

    @app.get("/vendors/{vendor}/changes")
    def changes(vendor: str, after: int = 0) -> dict:
        with pool.connection() as conn:
            rows = conn.execute("SELECT * FROM sim.feed WHERE vendor = %s AND seq > %s ORDER BY seq LIMIT 500",
                                (vendor, after)).fetchall()
        return {"changes": [{"seq": r["seq"], "vendor_ref": r["vendor_ref"], "status": r["status"],
                             "occurred_on": r["occurred_on"], "report_id": r["report_id"]} for r in rows]}

    def pdf_for(report_id: str) -> Response:
        with pool.connection() as conn:
            row = conn.execute("SELECT spec FROM sim.reports WHERE report_id = %s", (report_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such report")
        spec = row["spec"]
        pdf = reports.inspection_pdf(spec) if spec["kind"] == "inspection" else reports.audit_pdf(spec)
        return Response(pdf, media_type="application/pdf")

    @app.get("/vendors/{vendor}/reports/{report_id}")
    def vendor_report(vendor: str, report_id: str) -> Response:
        return pdf_for(report_id)

    @app.get("/mail/attachments/{report_id}")
    def attachment(report_id: str) -> Response:
        return pdf_for(report_id)

    @app.post("/mail/send")
    def send(mail: MailIn) -> dict:
        with pool.connection() as conn:
            now = today(conn)
            conn.execute("INSERT INTO sim.mailbox (folder, sent_on, sender, recipient, subject, body) "
                         "VALUES ('sent', %s, %s, %s, %s, %s)",
                         (now, mail.sender, mail.recipient, mail.subject, mail.body))
            tag = TAG.search(mail.subject)
            form = mail.form or {}
            if tag and form.get("type") == "desk_survey_order" and mail.recipient.endswith("quillmate-risk.example.com"):
                key = f"QRS:{tag.group(1)}"
                if not conn.execute("SELECT 1 FROM sim.vendor_orders WHERE idempotency_key = %s", (key,)).fetchone():
                    n = conn.execute("SELECT count(*) AS n FROM sim.vendor_orders WHERE vendor = 'QRS'").fetchone()["n"]
                    ref = f"Q-{48001 + n}"
                    conn.execute("INSERT INTO sim.vendor_orders (vendor_ref, vendor, idempotency_key, payload, "
                                 "outcome, received_on) VALUES (%s, 'QRS', %s, %s, '{}', %s)",
                                 (ref, key, Jsonb(form), now))
                    add_plans(conn, outcomes.plan_desk_survey(key, ref, form, mail.sender, mail.subject, now), {})
            elif tag and form.get("type") == "self_audit_request":
                key = f"SELF:{tag.group(1)}"
                if not conn.execute("SELECT 1 FROM sim.vendor_orders WHERE idempotency_key = %s", (key,)).fetchone():
                    conn.execute("INSERT INTO sim.vendor_orders (vendor_ref, vendor, idempotency_key, payload, "
                                 "outcome, received_on) VALUES (%s, 'SELF', %s, %s, '{}', %s)",
                                 (key, key, Jsonb(form), now))
                    add_plans(conn, outcomes.plan_self_audit(key, form, mail.sender, mail.recipient, mail.subject,
                                                             now), {})
        return {"queued": True}

    @app.get("/mail/inbox")
    def inbox(after: int = 0) -> dict:
        with pool.connection() as conn:
            rows = conn.execute("SELECT * FROM sim.mailbox WHERE folder = 'inbox' AND id > %s ORDER BY id LIMIT 500",
                                (after,)).fetchall()
        return {"messages": [{"id": r["id"], "sent_on": r["sent_on"], "sender": r["sender"],
                              "subject": r["subject"], "body": r["body"], "attachment": r["attachment"]}
                             for r in rows]}

    @app.get("/mail/sent")
    def sent(limit: int = 50) -> dict:
        with pool.connection() as conn:
            rows = conn.execute("SELECT * FROM sim.mailbox WHERE folder = 'sent' ORDER BY id DESC LIMIT %s",
                                (limit,)).fetchall()
        return {"messages": rows}

    return app


app = create_app()
