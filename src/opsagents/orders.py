"""The per-order state machine and the exception queue.

Every state change goes through ``transition``, which checks the transition table,
writes the event to the hash-chained trail and updates the row in one transaction.
Only a human actor may move an order out of ``in_review``.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import trail
from .rules import Decision, required_role

WORKING = ("identified", "ordered", "acknowledged", "scheduled", "waiting_on_insured", "report_received")
FINAL = ("closed", "closed_manual")

TRANSITIONS: dict[str, set[str]] = {
    "identified": {"ordered", "in_review"},
    "ordered": {"acknowledged", "scheduled", "waiting_on_insured", "report_received", "in_review"},
    "acknowledged": {"scheduled", "waiting_on_insured", "report_received", "in_review"},
    "scheduled": {"waiting_on_insured", "report_received", "in_review"},
    "waiting_on_insured": {"scheduled", "report_received", "in_review"},
    "report_received": {"closed", "in_review"},
    "in_review": {"approved", "closed_manual", *WORKING},
    "approved": {"closed"},
    "closed": set(),
    "closed_manual": set(),
}


class TransitionError(Exception):
    pass


def jsonb(value: Any) -> Jsonb:
    """Dates and decimals in evidence and extraction payloads are stored as strings."""
    return Jsonb(json.loads(json.dumps(value, default=str)))


def create_from_decision(conn: psycopg.Connection, d: Decision, today: date, rule_version: str) -> dict | None:
    """Insert an order for a rule decision. The unique order_key makes identification idempotent."""
    row = conn.execute(
        """INSERT INTO ops.orders (order_key, kind, policy_number, order_type, vendor, trigger, rule_version,
                                   rule_hits, period_start, period_end, state, identified_on, order_on, due_on,
                                   next_action_on)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'identified', %s, %s, %s, %s)
           ON CONFLICT (order_key) DO NOTHING RETURNING *""",
        (d.order_key, d.kind, d.policy_number, d.order_type, d.vendor, d.trigger, rule_version,
         jsonb(d.rule_hits), d.period_start, d.period_end, today, d.order_on, d.due_on, max(d.order_on, today)),
    ).fetchone()
    if row is None:
        return None
    trail.append(conn, row["id"], today, f"agent:{d.kind}", "identified",
                 {"rule_version": rule_version, "rule_hits": d.rule_hits, "order_type": d.order_type,
                  "vendor": d.vendor, "order_on": d.order_on, "due_on": d.due_on},
                 to_state="identified")
    return row


def lock(conn: psycopg.Connection, order_id: int) -> dict:
    return conn.execute("SELECT * FROM ops.orders WHERE id = %s FOR UPDATE", (order_id,)).fetchone()


def transition(conn: psycopg.Connection, order: dict, to_state: str, today: date, actor: str,
               event_type: str, payload: dict[str, Any] | None = None, **fields: Any) -> dict:
    current = order["state"]
    if to_state not in TRANSITIONS[current]:
        raise TransitionError(f"order {order['id']}: {current} -> {to_state} is not allowed")
    if current == "in_review" and not actor.startswith("human:"):
        raise TransitionError(f"order {order['id']}: only a person can move an order out of review")
    fields["state"] = to_state
    if to_state in FINAL:
        fields.setdefault("closed_on", today)
    order = update(conn, order, **fields)
    trail.append(conn, order["id"], today, actor, event_type, payload, from_state=current, to_state=to_state)
    return order


def update(conn: psycopg.Connection, order: dict, **fields: Any) -> dict:
    if not fields:
        return order
    cols = ", ".join(f"{k} = %s" for k in fields)
    values = [jsonb(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
    return conn.execute(f"UPDATE ops.orders SET {cols} WHERE id = %s RETURNING *", (*values, order["id"])).fetchone()


def note(conn: psycopg.Connection, order: dict, today: date, actor: str, event_type: str,
         payload: dict[str, Any] | None = None) -> None:
    """An event that does not change state, such as a reminder sent or a vendor chased."""
    trail.append(conn, order["id"], today, actor, event_type, payload, from_state=order["state"],
                 to_state=order["state"])


def open_exception(conn: psycopg.Connection, order: dict, rules: dict, today: date, reason: str,
                   detail: str, evidence: dict[str, Any] | None = None, resume_state: str | None = None) -> dict:
    role = required_role(rules, order["kind"], reason)
    resume = resume_state or order["state"]
    actor = f"agent:{order['kind']}"
    order = transition(conn, order, "in_review", today, actor, "exception_opened",
                       {"reason": reason, "required_role": role, "detail": detail},
                       human_touched=True, next_action_on=today)
    conn.execute(
        """INSERT INTO ops.exceptions (order_id, opened_on, reason, required_role, detail, evidence, resume_state)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (order["id"], today, reason, role, detail, jsonb(evidence or {}), resume),
    )
    return order


class ResolutionError(Exception):
    pass


class RoleError(ResolutionError):
    pass


# Where an approved exception sends the order. "approved" means the agent posts next.
APPROVE_TARGET = {
    "variance_review": "approved",
    "unlisted_class": "approved",
    "underwriter_review": "approved",
    "insured_non_responsive": "approved",
    "business_closed": "approved",
    "extraction_unverified": "report_received",
}


def resolve_exception(conn: psycopg.Connection, exception_id: int, role: str, action: str, today: date,
                      note_text: str = "", actor_name: str | None = None) -> dict:
    """Apply a person's decision. Runs in the caller's transaction."""
    exc = conn.execute("SELECT * FROM ops.exceptions WHERE id = %s FOR UPDATE", (exception_id,)).fetchone()
    if exc is None:
        raise ResolutionError(f"exception {exception_id} not found")
    if exc["status"] != "open":
        raise ResolutionError(f"exception {exception_id} is already resolved")
    if role != exc["required_role"]:
        raise RoleError(f"exception {exception_id} needs role {exc['required_role']}, not {role}")
    if action not in ("approve", "reject"):
        raise ResolutionError("action must be approve or reject")

    order = lock(conn, exc["order_id"])
    actor = f"human:{role}" + (f":{actor_name}" if actor_name else "")
    payload = {"exception_id": exception_id, "reason": exc["reason"], "action": action, "note": note_text}
    if action == "reject":
        order = transition(conn, order, "closed_manual", today, actor, "exception_rejected", payload)
    else:
        target = APPROVE_TARGET.get(exc["reason"], exc["resume_state"])
        fields: dict[str, Any] = {"next_action_on": today}
        if exc["reason"] == "extraction_unverified":
            fields["extraction_approved"] = True
        if exc["reason"] == "vendor_unreachable":
            fields["submit_failures"] = 0
        if exc["reason"] in ("sla_breach", "insured_unreachable"):
            fields["due_on"] = max(order["due_on"], today) + timedelta(days=30)
            fields["insured_contacted_on"] = None if exc["reason"] == "insured_unreachable" else order[
                "insured_contacted_on"]
        order = transition(conn, order, target, today, actor, "exception_approved", payload, **fields)
    conn.execute(
        """UPDATE ops.exceptions SET status = 'resolved', resolution = %s, resolved_by = %s, resolved_on = %s,
                  note = %s WHERE id = %s""",
        (action, actor, today, note_text, exception_id),
    )
    return order
