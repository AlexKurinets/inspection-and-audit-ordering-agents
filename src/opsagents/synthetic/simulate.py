"""Drives the demo: one simulated day at a time, with simulated reviewers working the exception queue.

Each day the policy system processes its scheduled changes, the simulator's clock moves
(releasing vendor status changes and emails planned for that day), and the agents run one
tick. During the seeded history, simulated reviewers resolve exceptions two to four days
after they open, through the same code path as the dashboard's approve and reject buttons.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import psycopg

from .. import db
from .. import orders as om
from ..agents.runtime import tick
from ..integrations.outside import Outside
from ..llm.base import LLM
from .book import apply_pas_changes

REVIEWERS = {"audit_reviewer": "Nell Marchbanks", "underwriter": "Orin Halloran", "ops_lead": "Tamsin Elsby"}
NOTE = "simulated reviewer"


def set_sim_clock(outside: Outside, day: date) -> None:
    r = outside.http.post("/_sim/clock", json={"today": day.isoformat()})
    r.raise_for_status()


def decide(exc: dict) -> tuple[str, str]:
    reason = exc["reason"]
    if reason == "variance_review" and exc["id"] % 10 == 0:
        return "reject", "Sent back to the auditor for a re-audit; handled in the policy system."
    if reason == "extraction_unverified":
        if "failures" not in exc["evidence"]:
            return "reject", "The model could not read the scan; keyed by hand in the policy system."
        sums = any(f["check"] == "rows add up to total" for f in exc["evidence"]["failures"])
        if sums:
            return "reject", "Worksheet does not add up; corrected worksheet requested from the sender."
        return "approve", "Checked the figures against the PDF; extraction is right."
    if reason == "insured_unreachable":
        return "reject", "Referred for non-renewal review in the underwriting workbench."
    return "approve", {
        "variance_review": "Adjustment reviewed against the worksheet.",
        "unlisted_class": "Classification confirmed with the insured.",
        "underwriter_review": "Recommendations sent to the insured with a 30-day compliance date.",
        "insured_non_responsive": "Post an estimated audit; non-compliance letter sent.",
        "business_closed": "Business closure confirmed; post at the estimated payroll.",
        "vendor_unreachable": "Vendor confirmed the outage is over; retry.",
        "sla_breach": "Vendor gave a new completion date; extended 30 days.",
        "mail_needs_person": "Replied to the sender; the agent can continue.",
    }.get(reason, "Reviewed.")


def review_queue(conn: psycopg.Connection, today: date) -> int:
    rows = conn.execute("SELECT * FROM ops.exceptions WHERE status = 'open' ORDER BY id").fetchall()
    done = 0
    for exc in rows:
        lag = 2 + exc["id"] % 3
        if exc["opened_on"] + timedelta(days=lag) > today:
            continue
        action, text = decide(exc)
        role = exc["required_role"]
        om.resolve_exception(conn, exc["id"], role, action, today, f"{text} ({NOTE})", REVIEWERS[role])
        done += 1
    conn.commit()
    return done


def run_day(conn: psycopg.Connection, outside: Outside, rules: dict, llm: LLM, day: date,
            reviewers: bool) -> dict:
    db.set_today(conn, day)
    apply_pas_changes(conn, day)
    conn.commit()
    set_sim_clock(outside, day)
    stats = tick(conn, rules, llm, outside)
    if reviewers:
        stats["reviewed"] = review_queue(conn, day)
    return stats


def run(conn: psycopg.Connection, outside: Outside, rules: dict, llm: LLM, start: date, end: date,
        reviewers: bool = True, progress: Callable[[dict], None] | None = None,
        review_until: date | None = None) -> list[str]:
    errors: list[str] = []
    day = start
    while day <= end:
        stats = run_day(conn, outside, rules, llm, day, reviewers and (review_until is None or day <= review_until))
        errors += stats["errors"]
        if progress:
            progress(stats)
        day += timedelta(days=1)
    return errors
