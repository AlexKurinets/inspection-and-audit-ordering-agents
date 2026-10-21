"""Policy administration system adapter.

In production this wrapped the carrier's policy admin REST API. Here it reads and writes
the ``pas`` schema, and nothing else in the agents touches that schema directly.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import psycopg

from ..rules import Exposure, Policy

AUTHOR = "ops-agent"


def _policy(row: dict, exposures: list[dict], audited: list[date]) -> Policy:
    return Policy(
        policy_number=row["policy_number"], line=row["line"], insured_name=row["insured_name"],
        insured_email=row["insured_email"], effective_date=row["effective_date"],
        expiration_date=row["expiration_date"], status=row["status"], cancellation_date=row["cancellation_date"],
        new_business=row["new_business"], last_inspection_date=row["last_inspection_date"],
        deposit_premium=row["deposit_premium"],
        exposures=tuple(Exposure(e["class_code"], e["estimated_payroll"], e["rate"], e["hazard"]) for e in exposures),
        audited_period_ends=frozenset(audited), contact_name=row["contact_name"],
        insured_phone=row["insured_phone"],
    )


def policies_for_sweep(conn: psycopg.Connection, today: date, lookback_days: int) -> list[Policy]:
    rows = conn.execute(
        """SELECT * FROM pas.policies
           WHERE effective_date <= %s AND expiration_date >= %s ORDER BY policy_number""",
        (today, today - timedelta(days=lookback_days)),
    ).fetchall()
    numbers = [r["policy_number"] for r in rows]
    exposures: dict[str, list[dict]] = {}
    for e in conn.execute(
        """SELECT x.*, c.hazard FROM pas.policy_exposures x JOIN pas.class_codes c ON c.code = x.class_code
           WHERE x.policy_number = ANY(%s) ORDER BY x.class_code""", (numbers,)
    ):
        exposures.setdefault(e["policy_number"], []).append(e)
    audited: dict[str, list[date]] = {}
    for a in conn.execute("SELECT * FROM pas.audit_history WHERE policy_number = ANY(%s)", (numbers,)):
        audited.setdefault(a["policy_number"], []).append(a["period_end"])
    return [_policy(r, exposures.get(r["policy_number"], []), audited.get(r["policy_number"], [])) for r in rows]


def get_policy(conn: psycopg.Connection, policy_number: str) -> Policy:
    row = conn.execute("SELECT * FROM pas.policies WHERE policy_number = %s", (policy_number,)).fetchone()
    exposures = conn.execute(
        """SELECT x.*, c.hazard FROM pas.policy_exposures x JOIN pas.class_codes c ON c.code = x.class_code
           WHERE x.policy_number = %s ORDER BY x.class_code""", (policy_number,)
    ).fetchall()
    audited = [a["period_end"] for a in conn.execute(
        "SELECT period_end FROM pas.audit_history WHERE policy_number = %s", (policy_number,))]
    return _policy(row, exposures, audited)


def known_class_codes(conn: psycopg.Connection) -> set[str]:
    return {r["code"] for r in conn.execute("SELECT code FROM pas.class_codes")}


def line_rates(conn: psycopg.Connection, line: str) -> dict[str, Decimal]:
    return {r["class_code"]: r["rate"] for r in conn.execute(
        "SELECT class_code, rate FROM pas.class_rates WHERE line = %s", (line,))}


def log_activity(conn: psycopg.Connection, policy_number: str, today: date, note: str) -> None:
    conn.execute("INSERT INTO pas.activity (policy_number, logged_on, author, note) VALUES (%s, %s, %s, %s)",
                 (policy_number, today, AUTHOR, note))


def post_inspection(conn: psycopg.Connection, order: dict, extraction: dict, today: date) -> None:
    recs = extraction["recommendations"]
    survey_date = date.fromisoformat(extraction["survey_date"])
    conn.execute(
        """INSERT INTO pas.inspections (order_id, policy_number, survey_date, risk_rating, recommendation_count,
                                        critical_count, posted_on)
           VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (order_id) DO NOTHING""",
        (order["id"], order["policy_number"], survey_date, extraction["risk_rating"], len(recs),
         sum(r["priority"] == "Critical" for r in recs), today),
    )
    conn.execute(
        """UPDATE pas.policies SET last_inspection_date = GREATEST(COALESCE(last_inspection_date, %s), %s)
           WHERE policy_number = %s""", (survey_date, survey_date, order["policy_number"]))


def post_audit(conn: psycopg.Connection, order: dict, basis: str, audited_premium: Decimal,
               deposit_premium: Decimal, adjustment: Decimal, today: date, posted_by: str) -> None:
    conn.execute(
        """INSERT INTO pas.audit_endorsements (order_id, policy_number, period_start, period_end, basis,
                                               audited_premium, deposit_premium, adjustment, posted_on, posted_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (order_id) DO NOTHING""",
        (order["id"], order["policy_number"], order["period_start"], order["period_end"], basis, audited_premium,
         deposit_premium, adjustment, today, posted_by),
    )
    conn.execute(
        """INSERT INTO pas.audit_history (policy_number, period_end, completed_on) VALUES (%s, %s, %s)
           ON CONFLICT DO NOTHING""", (order["policy_number"], order["period_end"], today))
