"""State machine, exception authority, the hash-chained trail and vendor idempotency, on a real database."""

from datetime import date, timedelta

import psycopg
import pytest
from conftest import key_with

from opsagents import db, trail
from opsagents import orders as om
from opsagents.agents.work import Ctx, step
from opsagents.llm.offline import OfflineLLM
from opsagents.rules import Decision
from opsagents.synthetic import book

DAY = date(2026, 4, 1)


def make_order(conn, rules, key="AUD:WC-2025-000001:2025-04-01:2026-04-01", vendor="HAP", order_type="physical",
               policy_number=None):
    if policy_number is None:
        policy_number = conn.execute("SELECT policy_number FROM pas.policies ORDER BY policy_number LIMIT 1"
                                     ).fetchone()["policy_number"]
    d = Decision(kind="audit", order_key=key, policy_number=policy_number, order_type=order_type,
                 vendor=vendor, trigger="expiration", period_start=date(2025, 4, 1), period_end=date(2026, 4, 1),
                 order_on=DAY, due_on=DAY + timedelta(days=90), rule_hits=["test"])
    order = om.create_from_decision(conn, d, DAY, rules["version"])
    conn.commit()
    return order


@pytest.fixture
def booked(conn):
    book.generate(conn, DAY, DAY + timedelta(days=30), expired=0, in_force=5, new_business=0)
    db.set_today(conn, DAY)
    conn.commit()
    return conn


def test_identification_is_idempotent(booked, rules):
    assert make_order(booked, rules) is not None
    assert make_order(booked, rules) is None
    assert booked.execute("SELECT count(*) AS n FROM ops.orders").fetchone()["n"] == 1


def test_disallowed_transition_is_refused(booked, rules):
    order = make_order(booked, rules)
    with pytest.raises(om.TransitionError):
        om.transition(booked, order, "closed", DAY, "agent:audit", "posted")


def test_only_a_person_can_take_an_order_out_of_review(booked, rules):
    order = make_order(booked, rules)
    order = om.open_exception(booked, order, rules, DAY, "variance_review", "test", {})
    with pytest.raises(om.TransitionError, match="only a person"):
        om.transition(booked, order, "approved", DAY, "agent:audit", "self_approval")


def test_resolution_needs_the_required_role(booked, rules):
    order = make_order(booked, rules)
    om.open_exception(booked, order, rules, DAY, "variance_review", "test", {})
    exc = booked.execute("SELECT id, required_role FROM ops.exceptions").fetchone()
    assert exc["required_role"] == "audit_reviewer"
    with pytest.raises(om.ResolutionError, match="needs role audit_reviewer"):
        om.resolve_exception(booked, exc["id"], "ops_lead", "approve", DAY)
    approved = om.resolve_exception(booked, exc["id"], "audit_reviewer", "approve", DAY, "ok", "Test Person")
    assert approved["state"] == "approved"
    with pytest.raises(om.ResolutionError, match="already resolved"):
        om.resolve_exception(booked, exc["id"], "audit_reviewer", "reject", DAY)


def test_reject_closes_for_manual_handling(booked, rules):
    order = make_order(booked, rules)
    om.open_exception(booked, order, rules, DAY, "unlisted_class", "x", {})
    exc = booked.execute("SELECT id FROM ops.exceptions").fetchone()
    closed = om.resolve_exception(booked, exc["id"], "underwriter", "reject", DAY)
    assert closed["state"] == "closed_manual" and closed["closed_on"] == DAY and closed["human_touched"]


def test_trail_verifies_and_detects_tampering(booked, rules):
    order = make_order(booked, rules)
    om.note(booked, order, DAY, "agent:audit", "reminder_sent", {"number": 1})
    om.note(booked, order, DAY, "agent:audit", "reminder_sent", {"number": 2})
    booked.commit()
    v = trail.verify(booked, order["id"])
    assert v["ok"] and v["events"] == 3

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        booked.execute("UPDATE ops.events SET payload = '{\"number\": 9}' WHERE seq = 2")
    booked.rollback()

    booked.execute("ALTER TABLE ops.events DISABLE TRIGGER events_append_only")
    booked.execute("UPDATE ops.events SET payload = '{\"number\": 9}' WHERE order_id = %s AND seq = 2", (order["id"],))
    booked.execute("ALTER TABLE ops.events ENABLE TRIGGER events_append_only")
    booked.commit()
    result = trail.verify(booked, order["id"])
    assert result["ok"] is False and result["broken_at_seq"] == 2


def test_lost_vendor_response_is_retried_without_a_second_order(booked, rules, outside):
    key = key_with(lambda h: h % 20 == 0 and h % 29 and h % 211)
    outside.http.post("/_sim/clock", json={"today": str(DAY)})
    placed = outside.submit_order("HAP", key, {"period_end": "2026-04-01", "audit_type": "phone", "classes": [
        {"class_code": "C-320", "description": "Carpentry, residential", "estimated_payroll": "100000"}],
        "policy_number": "WC-2025-000001", "insured_name": "x", "period_start": "2025-04-01"})
    assert placed["duplicate"] is True and placed["errors"] == ["HTTP 504"] and placed["attempts"] == 2
    n = booked.execute("SELECT count(*) AS n FROM sim.vendor_orders WHERE idempotency_key = %s", (key,)).fetchone()
    assert n["n"] == 1


def test_vendor_down_three_days_goes_to_ops_lead(booked, rules, outside):
    key = key_with(lambda h: h % 211 == 0)
    order = make_order(booked, rules, key=key)
    ctx = Ctx(conn=booked, rules=rules, llm=OfflineLLM(), outside=outside, today=DAY)
    for i in range(3):
        ctx.today = DAY + timedelta(days=i)
        outside.http.post("/_sim/clock", json={"today": str(ctx.today)})
        order = step(ctx, om.lock(booked, order["id"]))
        booked.commit()
    assert order["state"] == "in_review" and order["submit_failures"] == 3
    exc = booked.execute("SELECT * FROM ops.exceptions").fetchone()
    assert (exc["reason"], exc["required_role"], exc["resume_state"]) == ("vendor_unreachable", "ops_lead",
                                                                          "identified")
    om.resolve_exception(booked, exc["id"], "ops_lead", "approve", ctx.today)
    booked.commit()
    order = step(ctx, om.lock(booked, order["id"]))
    assert order["state"] == "ordered" and order["vendor_ref"].startswith("HAP-")
