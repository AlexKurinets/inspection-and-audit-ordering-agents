"""What the two agents do with one order, depending on its state.

Both agents share this code. The inspection and audit differences are the ordering
channel, the follow-up schedule, what is extracted from the report and what gets posted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from .. import orders as om
from ..documents.ocr import read_pdf
from ..documents.verify import verify_audit, verify_inspection
from ..integrations import pas
from ..integrations.outside import VENDOR_ADDRESSES, Outside, VendorUnavailable
from ..llm.base import LLM, LLMError
from ..reconcile import reconcile
from . import templates

WAITING = ("ordered", "acknowledged", "scheduled", "waiting_on_insured")
EMAIL_VENDORS = ("QRS",)
MAX_SUBMIT_FAILURES = 3


@dataclass
class Ctx:
    conn: psycopg.Connection
    rules: dict
    llm: LLM
    outside: Outside
    today: date


def usd(amount: str | Decimal, signed: bool = False) -> str:
    x = Decimal(amount)
    sign = ("+" if x >= 0 else "-") if signed else ""
    return f"{sign}${abs(x):,.2f}"


def actor(order: dict) -> str:
    return f"agent:{order['kind']}"


def move(ctx: Ctx, order: dict, to_state: str, event_type: str, payload: dict | None = None, **fields: Any) -> dict:
    """Transition when the table allows it; otherwise keep the state and record the update."""
    if to_state != order["state"] and to_state in om.TRANSITIONS[order["state"]]:
        return om.transition(ctx.conn, order, to_state, ctx.today, actor(order), event_type, payload, **fields)
    order = om.update(ctx.conn, order, **fields)
    om.note(ctx.conn, order, ctx.today, actor(order), event_type, payload)
    return order


def record_llm_call(ctx: Ctx, order_id: int | None, task: str, ok: bool, started: float) -> None:
    ctx.conn.execute(
        "INSERT INTO ops.llm_calls (order_id, task, provider, ok, latency_ms, called_on) VALUES (%s, %s, %s, %s, %s, %s)",
        (order_id, task, ctx.llm.provider, ok, int((time.perf_counter() - started) * 1000), ctx.today),
    )


def store_document(ctx: Ctx, order: dict, data: bytes, source: str) -> dict:
    doc = read_pdf(data)
    ctx.conn.execute(
        """INSERT INTO ops.documents (order_id, source, sha256, pages, ocr_pages, text, received_on)
           VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (order_id, sha256) DO NOTHING""",
        (order["id"], source, doc.sha256, doc.pages, doc.ocr_pages, doc.text, ctx.today),
    )
    return {"source": source, "pages": doc.pages, "ocr_pages": doc.ocr_pages, "sha256": doc.sha256[:12]}


def latest_document(conn: psycopg.Connection, order_id: int) -> dict | None:
    return conn.execute("SELECT * FROM ops.documents WHERE order_id = %s ORDER BY id DESC LIMIT 1",
                        (order_id,)).fetchone()


def report_arrived(ctx: Ctx, order: dict, data: bytes, source: str, payload: dict) -> dict:
    meta = store_document(ctx, order, data, source)
    payload = {**payload, **meta}
    if order["state"] in WAITING:
        return om.transition(ctx.conn, order, "report_received", ctx.today, actor(order), "report_received",
                             payload, completed_on=ctx.today, next_action_on=ctx.today)
    order = om.update(ctx.conn, order, completed_on=ctx.today)
    om.note(ctx.conn, order, ctx.today, actor(order), "report_received_while_" + order["state"], payload)
    return order


# ---------------------------------------------------------------------------------------------------------------


def step(ctx: Ctx, order: dict) -> dict:
    state = order["state"]
    if state == "identified":
        return submit(ctx, order)
    if state == "report_received":
        return process_report(ctx, order)
    if state == "approved":
        return post(ctx, order)
    if state in WAITING:
        if latest_document(ctx.conn, order["id"]) is not None:
            order = om.transition(ctx.conn, order, "report_received", ctx.today, actor(order), "report_available",
                                  {"note": "report arrived while the order was with a person"},
                                  next_action_on=ctx.today)
            return process_report(ctx, order)
        return follow_up(ctx, order)
    return order


def submit(ctx: Ctx, order: dict) -> dict:
    if order["order_on"] > ctx.today:
        return om.update(ctx.conn, order, next_action_on=order["order_on"])
    policy = pas.get_policy(ctx.conn, order["policy_number"])
    vendor = order["vendor"]
    if vendor == "SELF":
        subject, body, form = templates.self_audit_request(order, policy)
        ctx.outside.send_mail(policy.insured_email, subject, body, form)
        order = om.transition(ctx.conn, order, "ordered", ctx.today, actor(order), "self_audit_requested",
                              {"to": policy.insured_email, "subject": subject}, ordered_on=ctx.today)
        what = f"Self-audit requested from {policy.insured_email}"
    elif vendor in EMAIL_VENDORS:
        subject, body, form = templates.desk_survey_order(order, policy)
        ctx.outside.send_mail(VENDOR_ADDRESSES[vendor], subject, body, form)
        order = om.transition(ctx.conn, order, "ordered", ctx.today, actor(order), "order_emailed",
                              {"to": VENDOR_ADDRESSES[vendor], "subject": subject}, ordered_on=ctx.today)
        what = f"Desk survey ordered from {templates.VENDORS[vendor][0]} by email"
    else:
        try:
            placed = ctx.outside.submit_order(vendor, order["order_key"], templates.vendor_payload(order, policy))
        except VendorUnavailable as e:
            failures = order["submit_failures"] + 1
            order = om.update(ctx.conn, order, submit_failures=failures, next_action_on=ctx.today + timedelta(days=1))
            om.note(ctx.conn, order, ctx.today, actor(order), "submit_failed", {"error": str(e), "failures": failures})
            if failures >= MAX_SUBMIT_FAILURES:
                return om.open_exception(ctx.conn, order, ctx.rules, ctx.today, "vendor_unreachable",
                                         f"{templates.VENDORS[vendor][0]} rejected the order on {failures} "
                                         f"separate days: {e}", {"last_error": str(e)}, resume_state="identified")
            return order
        order = om.transition(ctx.conn, order, "ordered", ctx.today, actor(order), "order_placed",
                              {"vendor_ref": placed["vendor_ref"], "idempotency_key": order["order_key"],
                               "attempts": placed["attempts"], "errors": placed["errors"],
                               "replayed": placed["duplicate"]},
                              vendor_ref=placed["vendor_ref"], ordered_on=ctx.today)
        what = f"{templates.TYPE_NAMES[order['order_type']]} ordered from {templates.VENDORS[vendor][0]}, " \
               f"ref {placed['vendor_ref']}"
    pas.log_activity(ctx.conn, order["policy_number"], ctx.today, f"{order['kind'].title()}: {what}.")
    return om.update(ctx.conn, order, next_action_on=next_checkpoint(ctx, order))


def next_checkpoint(ctx: Ctx, order: dict) -> date:
    i, a = ctx.rules["inspections"], ctx.rules["audits"]
    ordered = order["ordered_on"] or ctx.today
    candidates = [order["due_on"] + timedelta(days=1)]
    if order["kind"] == "inspection":
        if order["chased_on"] is None:
            candidates.append(ordered + timedelta(days=i["chase_vendor_after_days"]))
        if order["state"] == "waiting_on_insured":
            contacted = order["insured_contacted_on"]
            candidates.append(ctx.today if contacted is None
                              else contacted + timedelta(days=i["insured_unreachable_escalate_days"]))
    elif order["vendor"] == "SELF":
        n, days = order["reminders_sent"], a["self_audit_reminder_days"]
        candidates.append(ordered + timedelta(days=days[n] if n < len(days) else a["self_audit_escalate_day"]))
    else:
        if order["chased_on"] is None:
            candidates.append(order["due_on"] - timedelta(days=a["chase_vendor_days_before_due"]))
        if order["state"] == "waiting_on_insured" and order["insured_contacted_on"] is None:
            candidates.append(ctx.today)
    return max(min(candidates), ctx.today + timedelta(days=1))


def follow_up(ctx: Ctx, order: dict) -> dict:
    t, i, a = ctx.today, ctx.rules["inspections"], ctx.rules["audits"]
    if t > order["due_on"]:
        return om.open_exception(ctx.conn, order, ctx.rules, t, "sla_breach",
                                 f"Not complete by the due date {order['due_on']}; last state {order['state']}.",
                                 {"vendor_ref": order["vendor_ref"], "ordered_on": order["ordered_on"]})
    policy = pas.get_policy(ctx.conn, order["policy_number"])
    ordered = order["ordered_on"] or t

    if order["kind"] == "inspection" and order["state"] == "waiting_on_insured":
        contacted = order["insured_contacted_on"]
        if contacted is None:
            subject, body = templates.insured_contact_for_survey(order, policy)
            ctx.outside.send_mail(policy.insured_email, subject, body)
            order = om.update(ctx.conn, order, insured_contacted_on=t)
            om.note(ctx.conn, order, t, actor(order), "insured_contacted", {"to": policy.insured_email})
        elif t >= contacted + timedelta(days=i["insured_unreachable_escalate_days"]):
            return om.open_exception(
                ctx.conn, order, ctx.rules, t, "insured_unreachable",
                f"{templates.VENDORS[order['vendor']][0]} still cannot reach the insured "
                f"{(t - contacted).days} days after we asked them to call.",
                {"insured_contacted_on": contacted, "vendor_ref": order["vendor_ref"]})

    if order["kind"] == "audit" and order["vendor"] == "SELF":
        n, days = order["reminders_sent"], a["self_audit_reminder_days"]
        if n < len(days) and t >= ordered + timedelta(days=days[n]):
            subject, body = templates.self_audit_reminder(order, policy, n + 1)
            ctx.outside.send_mail(policy.insured_email, subject, body)
            order = om.update(ctx.conn, order, reminders_sent=n + 1)
            om.note(ctx.conn, order, t, actor(order), "reminder_sent", {"number": n + 1, "to": policy.insured_email})
        elif n >= len(days) and t >= ordered + timedelta(days=a["self_audit_escalate_day"]):
            return om.open_exception(
                ctx.conn, order, ctx.rules, t, "insured_non_responsive",
                f"No self-audit {(t - ordered).days} days after the request and {n} reminders.",
                {"requested_on": ordered, "reminders_sent": n})

    if order["kind"] == "audit" and order["vendor"] != "SELF" and order["state"] == "waiting_on_insured" \
            and order["insured_contacted_on"] is None:
        subject, body = templates.insured_records_request(order, policy)
        ctx.outside.send_mail(policy.insured_email, subject, body)
        order = om.update(ctx.conn, order, insured_contacted_on=t)
        om.note(ctx.conn, order, t, actor(order), "insured_asked_for_records", {"to": policy.insured_email})

    chase_on = (ordered + timedelta(days=i["chase_vendor_after_days"]) if order["kind"] == "inspection"
                else order["due_on"] - timedelta(days=a["chase_vendor_days_before_due"]))
    if order["vendor"] != "SELF" and order["chased_on"] is None and t >= chase_on:
        subject, body, _ = templates.vendor_chase(order, policy)
        ctx.outside.send_mail(VENDOR_ADDRESSES[order["vendor"]], subject, body)
        order = om.update(ctx.conn, order, chased_on=t)
        om.note(ctx.conn, order, t, actor(order), "vendor_chased", {"to": VENDOR_ADDRESSES[order["vendor"]]})

    return om.update(ctx.conn, order, next_action_on=next_checkpoint(ctx, order))


# ---------------------------------------------------------------------------------------------------------------


def _extract(ctx: Ctx, order: dict, text: str) -> dict:
    task = f"extract_{order['kind']}"
    started = time.perf_counter()
    try:
        ex = ctx.llm.extract_inspection(text) if order["kind"] == "inspection" else ctx.llm.extract_audit(text)
    except LLMError:
        record_llm_call(ctx, order["id"], task, False, started)
        raise
    record_llm_call(ctx, order["id"], task, True, started)
    if order["kind"] == "inspection":
        return verify_inspection(ex, text, order)
    policy_classes = {r["class_code"] for r in ctx.conn.execute(
        "SELECT class_code FROM pas.policy_exposures WHERE policy_number = %s", (order["policy_number"],))}
    return verify_audit(ex, text, order, pas.known_class_codes(ctx.conn), policy_classes)


def process_report(ctx: Ctx, order: dict) -> dict:
    doc = latest_document(ctx.conn, order["id"])
    if order["extraction"] and order["extraction_approved"]:
        result = order["extraction"]
    else:
        try:
            result = _extract(ctx, order, doc["text"])
        except LLMError as e:
            return om.open_exception(ctx.conn, order, ctx.rules, ctx.today, "extraction_unverified",
                                     f"The model could not read the report: {e}", {"document_id": doc["id"]})
        order = om.update(ctx.conn, order, extraction=result)
        om.note(ctx.conn, order, ctx.today, actor(order), "extracted",
                {"provider": ctx.llm.provider, "checks": len(result["checks"]), "failed": len(result["failures"]),
                 "ocr_pages": doc["ocr_pages"]})
        if not result["ok"]:
            failed = "; ".join(f"{f['check']} ({f['detail']})" for f in result["failures"][:4])
            return om.open_exception(ctx.conn, order, ctx.rules, ctx.today, "extraction_unverified",
                                     f"{len(result['failures'])} check(s) failed: {failed}",
                                     {"failures": result["failures"], "document_id": doc["id"]})

    if order["kind"] == "inspection":
        review = ctx.rules["inspections"]["underwriter_review"]
        hits = [r for r in result["recommendations"] if r["priority"] in review["recommendation_priorities"]]
        if result["risk_rating"] in review["risk_ratings"] or hits:
            why = [f"risk rating {result['risk_rating']}"] if result["risk_rating"] in review["risk_ratings"] else []
            why += [f"{r['ref']} {r['priority']}: {r['text']}" for r in hits]
            return om.open_exception(ctx.conn, order, ctx.rules, ctx.today, "underwriter_review", "; ".join(why),
                                     {"risk_rating": result["risk_rating"], "recommendations": hits})
        return post(ctx, order)

    policy = pas.get_policy(ctx.conn, order["policy_number"])
    rec = reconcile(result["payroll_by_class"], {e.class_code: e.rate for e in policy.exposures},
                    pas.line_rates(ctx.conn, policy.line), policy.deposit_premium, ctx.rules)
    order = om.update(ctx.conn, order, reconciliation=rec)
    om.note(ctx.conn, order, ctx.today, actor(order), "reconciled",
            {k: rec[k] for k in ("audited_premium", "deposit_premium", "adjustment", "auto_post")})
    if result["unlisted_classes"]:
        return om.open_exception(ctx.conn, order, ctx.rules, ctx.today, "unlisted_class",
                                 f"Payroll reported in {', '.join(result['unlisted_classes'])}, which is not on the "
                                 f"policy; adjustment {usd(rec['adjustment'], True)} at the filed rate.",
                                 {"unlisted": result["unlisted_classes"], "reconciliation": rec})
    if not rec["auto_post"]:
        lim = rec["limits"]
        return om.open_exception(ctx.conn, order, ctx.rules, ctx.today, "variance_review",
                                 f"Adjustment {usd(rec['adjustment'], True)} on a deposit of "
                                 f"{usd(rec['deposit_premium'])} is outside the agent's authority, which is "
                                 f"{usd(lim['max_abs'])} and {float(lim['max_pct']):.0%} of the deposit.",
                                 {"reconciliation": rec})
    return post(ctx, order)


def post(ctx: Ctx, order: dict) -> dict:
    approver = ctx.conn.execute(
        "SELECT resolved_by FROM ops.exceptions WHERE order_id = %s AND resolution = 'approve' ORDER BY id DESC "
        "LIMIT 1", (order["id"],)).fetchone()
    posted_by = "ops-agent" + (f", approved by {approver['resolved_by']}" if approver else "")
    if order["kind"] == "inspection":
        ex = order["extraction"]
        pas.post_inspection(ctx.conn, order, ex, ctx.today)
        crit = sum(r["priority"] == "Critical" for r in ex["recommendations"])
        note = (f"Inspection: survey {ex['survey_date']} posted, rating {ex['risk_rating']}, "
                f"{len(ex['recommendations'])} recommendation(s), {crit} critical.")
        payload: dict[str, Any] = {"risk_rating": ex["risk_rating"], "posted_by": posted_by}
    else:
        rec = order["reconciliation"]
        if rec is None:
            policy = pas.get_policy(ctx.conn, order["policy_number"])
            basis, audited, deposit, adj = "estimated", policy.deposit_premium, policy.deposit_premium, Decimal(0)
        else:
            basis = "audited"
            audited, deposit, adj = (Decimal(rec["audited_premium"]), Decimal(rec["deposit_premium"]),
                                     Decimal(rec["adjustment"]))
        pas.post_audit(ctx.conn, order, basis, audited, deposit, adj, ctx.today, posted_by)
        note = f"Audit: posted on an {basis} basis, premium {usd(audited)}, adjustment {usd(adj, True)}."
        payload = {"basis": basis, "audited_premium": audited, "adjustment": adj, "posted_by": posted_by}
    pas.log_activity(ctx.conn, order["policy_number"], ctx.today, note)
    return om.transition(ctx.conn, order, "closed", ctx.today, actor(order), "posted", payload)
