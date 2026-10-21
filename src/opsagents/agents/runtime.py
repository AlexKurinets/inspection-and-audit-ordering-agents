"""One pass of both agents: sweep the rules, read vendor feeds and the mailbox, then step due orders.

Every unit of work commits on its own, and orders are claimed with FOR UPDATE SKIP LOCKED,
so several workers can run the same tick safely and a crash loses at most one step.
Running a tick twice on the same day does nothing the second time.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import psycopg

from .. import db
from .. import orders as om
from ..integrations import pas
from ..integrations.outside import Outside
from ..llm.base import LLM, LLMError
from ..rules import audit_decision, inspection_decision
from .work import WAITING, Ctx, actor, move, record_llm_call, report_arrived, step

log = logging.getLogger("opsagents")
API_VENDORS = ("LFS", "HAP")
TAG = re.compile(r"\[ORD-(\d+)\]")
STEPPABLE = (*WAITING, "identified", "report_received", "approved")


def cursor(conn: psycopg.Connection, name: str) -> int:
    row = conn.execute("SELECT position FROM ops.cursors WHERE name = %s", (name,)).fetchone()
    return row["position"] if row else 0


def set_cursor(conn: psycopg.Connection, name: str, position: int) -> None:
    conn.execute("INSERT INTO ops.cursors (name, position) VALUES (%s, %s) "
                 "ON CONFLICT (name) DO UPDATE SET position = EXCLUDED.position", (name, position))


def sweep(ctx: Ctx) -> int:
    """Apply the ordering rules to every policy in scope and create the orders that don't exist yet."""
    rules = ctx.rules
    existing = {r["order_key"] for r in ctx.conn.execute("SELECT order_key FROM ops.orders")}
    created = 0
    for policy in pas.policies_for_sweep(ctx.conn, ctx.today, rules["audits"]["backlog_lookback_days"]):
        for decide in (inspection_decision, audit_decision):
            d = decide(policy, ctx.today, rules)
            if d and d.order_key not in existing:
                if om.create_from_decision(ctx.conn, d, ctx.today, rules["version"]):
                    created += 1
                existing.add(d.order_key)
    ctx.conn.commit()
    return created


FEED_TO_STATE = {"received": "acknowledged", "scheduled": "scheduled", "insured_unreachable": "waiting_on_insured",
                 "awaiting_records": "waiting_on_insured"}


def apply_vendor_status(ctx: Ctx, order: dict, change: dict) -> None:
    status = change["status"]
    payload = {"vendor_status": status, "seq": change["seq"], "occurred_on": change["occurred_on"]}
    if order["state"] in om.FINAL:
        om.note(ctx.conn, order, ctx.today, actor(order), "vendor_update_after_close", payload)
        return
    if status == "completed":
        data = ctx.outside.report(order["vendor"], change["report_id"])
        report_arrived(ctx, order, data, f"{order['vendor']} report {change['report_id']}", payload)
        return
    if order["state"] not in WAITING:
        om.note(ctx.conn, order, ctx.today, actor(order), "vendor_status", payload)
        return
    target = FEED_TO_STATE.get(status, order["state"])
    fields: dict[str, Any] = {}
    if target == "waiting_on_insured":
        fields["next_action_on"] = ctx.today
    move(ctx, order, target, "vendor_status", payload, **fields)


def process_feeds(ctx: Ctx) -> int:
    n = 0
    for vendor in API_VENDORS:
        name = f"feed:{vendor}"
        for change in ctx.outside.status_changes(vendor, cursor(ctx.conn, name)):
            order = ctx.conn.execute("SELECT * FROM ops.orders WHERE vendor = %s AND vendor_ref = %s FOR UPDATE",
                                     (vendor, change["vendor_ref"])).fetchone()
            if order:
                apply_vendor_status(ctx, order, change)
            set_cursor(ctx.conn, name, change["seq"])
            ctx.conn.commit()
            n += 1
    return n


def apply_mail(ctx: Ctx, order: dict, msg: dict, reading) -> None:
    intent = reading.intent if reading else None
    payload = {"message_id": msg["id"], "from": msg["sender"], "intent": intent,
               "quote": reading.quote if reading else None}
    if order["state"] in om.FINAL:
        om.note(ctx.conn, order, ctx.today, actor(order), "mail_after_close", payload)
        return
    if intent in ("report_delivered", "records_returned") and msg["attachment"]:
        data = ctx.outside.attachment(msg["attachment"])
        report_arrived(ctx, order, data, f"email attachment from {msg['sender']}", payload)
        return
    if order["state"] not in WAITING:
        om.note(ctx.conn, order, ctx.today, actor(order), "mail_received", payload)
        return
    if intent == "acknowledged":
        fields = {"vendor_ref": reading.vendor_ref} if reading.vendor_ref else {}
        move(ctx, order, "acknowledged", "vendor_acknowledged", payload, **fields)
    elif intent == "scheduled":
        move(ctx, order, "scheduled", "appointment_scheduled", {**payload, "date": reading.scheduled_date})
    elif intent == "insured_unreachable" and order["kind"] == "inspection":
        move(ctx, order, "waiting_on_insured", "vendor_cannot_reach_insured", payload, next_action_on=ctx.today)
    elif intent == "business_closed" and order["kind"] == "audit":
        om.open_exception(ctx.conn, order, ctx.rules, ctx.today, "business_closed",
                          f"Insured says: \"{reading.quote}\"", {"message_id": msg["id"], "subject": msg["subject"]})
    else:
        quote = reading.quote if reading else msg["body"][:200]
        om.open_exception(ctx.conn, order, ctx.rules, ctx.today, "mail_needs_person",
                          f"{msg['sender']} wrote: \"{quote}\"",
                          {"message_id": msg["id"], "subject": msg["subject"], "body": msg["body"]})


def process_mail(ctx: Ctx) -> int:
    n = 0
    for msg in ctx.outside.inbox(cursor(ctx.conn, "mail:inbox")):
        tag = TAG.search(msg["subject"])
        order = ctx.conn.execute("SELECT * FROM ops.orders WHERE id = %s FOR UPDATE",
                                 (int(tag.group(1)),)).fetchone() if tag else None
        started = time.perf_counter()
        try:
            reading = ctx.llm.read_mail(msg["subject"], msg["body"])
            record_llm_call(ctx, order["id"] if order else None, "read_mail", True, started)
        except LLMError:
            reading = None
            record_llm_call(ctx, order["id"] if order else None, "read_mail", False, started)
        ctx.conn.execute(
            """INSERT INTO ops.inbound_mail (message_id, order_id, received_on, sender, subject, intent, quote)
               VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (msg["id"], order["id"] if order else None, ctx.today, msg["sender"], msg["subject"],
             reading.intent if reading else "unreadable", reading.quote if reading else ""))
        if order:
            apply_mail(ctx, order, msg, reading)
        set_cursor(ctx.conn, "mail:inbox", msg["id"])
        ctx.conn.commit()
        n += 1
    return n


def step_due(ctx: Ctx, errors: list[str]) -> int:
    ids = [r["id"] for r in ctx.conn.execute(
        "SELECT id FROM ops.orders WHERE state = ANY(%s) AND next_action_on <= %s ORDER BY id",
        (list(STEPPABLE), ctx.today))]
    ctx.conn.commit()
    n = 0
    for order_id in ids:
        order = ctx.conn.execute(
            "SELECT * FROM ops.orders WHERE id = %s AND state = ANY(%s) AND next_action_on <= %s "
            "FOR UPDATE SKIP LOCKED", (order_id, list(STEPPABLE), ctx.today)).fetchone()
        if order is None:
            ctx.conn.rollback()
            continue
        try:
            step(ctx, order)
            ctx.conn.commit()
            n += 1
        except Exception as e:  # one bad order must not stop the others
            ctx.conn.rollback()
            log.exception("order %s failed in state %s", order_id, order["state"])
            errors.append(f"order {order_id}: {type(e).__name__}: {e}")
    return n


def tick(conn: psycopg.Connection, rules: dict, llm: LLM, outside: Outside) -> dict[str, Any]:
    ctx = Ctx(conn=conn, rules=rules, llm=llm, outside=outside, today=db.today(conn))
    conn.commit()
    errors: list[str] = []
    stats = {"today": str(ctx.today), "identified": sweep(ctx), "feed_changes": process_feeds(ctx),
             "mail": process_mail(ctx)}
    stats["steps"] = step_due(ctx, errors)
    stats["errors"] = errors
    return stats
