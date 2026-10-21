"""The figures the dashboard shows, computed from ops and pas on every request."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg

from . import db


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def summary(conn: psycopg.Connection) -> dict[str, Any]:
    today = db.today(conn)
    per_kind = {}
    for kind in ("inspection", "audit", None):
        where, args = ("WHERE kind = %s", (kind,)) if kind else ("", ())
        r = conn.execute(f"""
            SELECT count(*) AS orders,
                   count(*) FILTER (WHERE state = 'closed') AS closed,
                   count(*) FILTER (WHERE state = 'closed_manual') AS closed_manual,
                   count(*) FILTER (WHERE state = 'closed' AND NOT human_touched) AS straight_through,
                   count(*) FILTER (WHERE state NOT IN ('closed', 'closed_manual')) AS open,
                   count(*) FILTER (WHERE state = 'in_review') AS in_review,
                   count(*) FILTER (WHERE ordered_on IS NOT NULL AND ordered_on <= order_on
                                    AND NOT rule_hits::text LIKE '%%backlog%%') AS on_schedule,
                   count(*) FILTER (WHERE ordered_on IS NOT NULL AND NOT rule_hits::text LIKE '%%backlog%%')
                       AS ordered_scheduled,
                   count(*) FILTER (WHERE rule_hits::text LIKE '%%backlog%%') AS backlog_found,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY completed_on - ordered_on)
                       FILTER (WHERE completed_on IS NOT NULL) AS median_turnaround_days
            FROM ops.orders {where}""", args).fetchone()
        finished = r["closed"] + r["closed_manual"]
        per_kind[kind or "all"] = {
            **{k: r[k] for k in ("orders", "closed", "closed_manual", "straight_through", "open", "in_review",
                                 "backlog_found")},
            "finished": finished,
            "stp_rate": _rate(r["straight_through"], finished),
            "on_schedule_rate": _rate(r["on_schedule"], r["ordered_scheduled"]),
            "median_turnaround_days": float(r["median_turnaround_days"]) if r["median_turnaround_days"] is not None
            else None,
        }

    within = conn.execute("""
        SELECT count(*) AS due, count(*) FILTER (WHERE completed_on <= period_end + 90) AS on_time
        FROM ops.orders WHERE kind = 'audit' AND period_end + 90 <= %s AND NOT rule_hits::text LIKE '%%backlog%%'
    """, (today,)).fetchone()
    exc = conn.execute("""
        SELECT count(*) AS opened, count(*) FILTER (WHERE status = 'open') AS open,
               count(*) FILTER (WHERE resolution = 'approve') AS approved,
               count(*) FILTER (WHERE resolution = 'reject') AS rejected
        FROM ops.exceptions""").fetchone()
    by_reason = conn.execute("""
        SELECT reason, string_agg(DISTINCT required_role, ', ') AS required_role, count(*) AS opened,
               count(*) FILTER (WHERE status = 'open') AS open
        FROM ops.exceptions GROUP BY reason ORDER BY opened DESC, reason""").fetchall()
    premium = conn.execute("""
        SELECT count(*) AS posted,
               count(*) FILTER (WHERE basis = 'audited') AS audited,
               count(*) FILTER (WHERE basis = 'estimated') AS estimated,
               COALESCE(sum(adjustment) FILTER (WHERE adjustment > 0), 0) AS additional,
               COALESCE(-sum(adjustment) FILTER (WHERE adjustment < 0), 0) AS returned,
               count(*) FILTER (WHERE posted_by = 'ops-agent') AS posted_by_agent_alone
        FROM pas.audit_endorsements""").fetchone()
    docs = conn.execute("SELECT count(*) AS documents, COALESCE(sum(ocr_pages), 0) AS ocr_pages "
                        "FROM ops.documents").fetchone()
    llm = conn.execute("SELECT count(*) AS calls, count(*) FILTER (WHERE NOT ok) AS failed, "
                       "string_agg(DISTINCT provider, ',') AS providers FROM ops.llm_calls").fetchone()
    checks = conn.execute("""
        SELECT count(*) AS extracted,
               count(*) FILTER (WHERE (extraction->>'ok')::boolean) AS verified
        FROM ops.orders WHERE extraction IS NOT NULL""").fetchone()
    return {
        "today": today,
        "orders": per_kind,
        "audits_within_90_days": {"due": within["due"], "on_time": within["on_time"],
                                  "rate": _rate(within["on_time"], within["due"])},
        "exceptions": {**exc, "by_reason": by_reason},
        "premium": {k: (float(v) if isinstance(v, Decimal) else v) for k, v in premium.items()},
        "documents": docs,
        "extraction": {**checks, "verified_rate": _rate(checks["verified"], checks["extracted"])},
        "llm": llm,
    }


def daily(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("""
        SELECT occurred_on AS day,
               count(*) FILTER (WHERE event_type = 'identified') AS identified,
               count(*) FILTER (WHERE to_state = 'ordered' AND from_state = 'identified') AS ordered,
               count(*) FILTER (WHERE event_type = 'report_received') AS reports,
               count(*) FILTER (WHERE event_type = 'exception_opened') AS exceptions,
               count(*) FILTER (WHERE to_state IN ('closed', 'closed_manual')) AS closed
        FROM ops.events GROUP BY occurred_on ORDER BY occurred_on""").fetchall()


def vendors(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT vendor, kind, count(*) AS orders,
               count(*) FILTER (WHERE completed_on IS NOT NULL) AS completed,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY completed_on - ordered_on)
                   FILTER (WHERE completed_on IS NOT NULL) AS median_days,
               count(*) FILTER (WHERE chased_on IS NOT NULL) AS chased,
               count(*) FILTER (WHERE state NOT IN ('closed', 'closed_manual')) AS open
        FROM ops.orders WHERE ordered_on IS NOT NULL GROUP BY vendor, kind ORDER BY kind, vendor""").fetchall()
    for r in rows:
        r["median_days"] = float(r["median_days"]) if r["median_days"] is not None else None
    return rows
