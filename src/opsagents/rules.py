"""Deterministic ordering rules.

The model never decides whether a policy needs an inspection or an audit, which type,
which vendor or on what date. Those decisions come from ``rules/ordering_rules.yaml``
through the pure functions below, so they can be replayed against a year of policies
and unit tested line by line.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

REPO_RULES = Path(__file__).resolve().parents[2] / "rules" / "ordering_rules.yaml"


def load_rules(path: str | os.PathLike | None = None) -> dict[str, Any]:
    path = Path(path or os.environ.get("RULES_PATH") or REPO_RULES)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass(frozen=True)
class Exposure:
    class_code: str
    estimated_payroll: Decimal
    rate: Decimal
    hazard: str


@dataclass(frozen=True)
class Policy:
    policy_number: str
    line: str
    insured_name: str
    insured_email: str
    effective_date: date
    expiration_date: date
    status: str
    cancellation_date: date | None
    new_business: bool
    last_inspection_date: date | None
    deposit_premium: Decimal
    exposures: tuple[Exposure, ...] = ()
    audited_period_ends: frozenset[date] = frozenset()
    contact_name: str = ""
    insured_phone: str = ""

    @property
    def high_hazard(self) -> bool:
        return any(e.hazard == "high" for e in self.exposures)

    @property
    def period_end(self) -> date:
        if self.status == "cancelled" and self.cancellation_date:
            return self.cancellation_date
        return self.expiration_date


@dataclass(frozen=True)
class Decision:
    kind: str
    order_key: str
    policy_number: str
    order_type: str
    vendor: str
    trigger: str
    period_start: date
    period_end: date
    order_on: date
    due_on: date
    rule_hits: list[str] = field(default_factory=list)


def _months_between(earlier: date, later: date) -> int:
    return (later.year - earlier.year) * 12 + (later.month - earlier.month) - (later.day < earlier.day)


def inspection_decision(policy: Policy, today: date, rules: dict) -> Decision | None:
    r = rules["inspections"]
    premium = policy.deposit_premium
    if policy.status == "cancelled" and policy.cancellation_date and policy.cancellation_date <= today:
        return None

    def survey_type(hits: list[str]) -> tuple[str, str]:
        if premium >= r["full_survey_min_premium"]:
            hits.append(f"premium {premium:,.0f} >= {r['full_survey_min_premium']:,} -> full survey")
            return "full_survey", r["vendors"]["full_survey"]
        if policy.high_hazard:
            hits.append("high-hazard class -> full survey")
            return "full_survey", r["vendors"]["full_survey"]
        hits.append("below full-survey threshold -> desk survey")
        return "desk_survey", r["vendors"]["desk_survey"]

    # New business: surveyed once it is bound, within its first 30 days.
    if policy.new_business and policy.effective_date <= today <= policy.effective_date + timedelta(days=30):
        hits: list[str] = []
        if premium >= r["new_business_min_premium"]:
            hits.append(f"new business, premium {premium:,.0f} >= {r['new_business_min_premium']:,}")
        elif r["high_hazard_always"] and policy.high_hazard:
            hits.append("new business, high-hazard class")
        else:
            return None
        order_type, vendor = survey_type(hits)
        return Decision(
            kind="inspection",
            order_key=f"INS:{policy.policy_number}:new_business:{policy.effective_date}",
            policy_number=policy.policy_number,
            order_type=order_type,
            vendor=vendor,
            trigger="new_business",
            period_start=policy.effective_date,
            period_end=policy.expiration_date,
            order_on=policy.effective_date,
            due_on=policy.effective_date + timedelta(days=r["due_days"]),
            rule_hits=hits,
        )

    # Renewal: re-surveyed ahead of expiration when the last survey is stale.
    lead_start = policy.expiration_date - timedelta(days=r["renewal_lead_days"])
    if lead_start <= today < policy.expiration_date and premium >= r["renewal_min_premium"]:
        last = policy.last_inspection_date
        if last is not None and _months_between(last, policy.expiration_date) < r["reinspect_after_months"]:
            return None
        age = "never surveyed" if last is None else f"last survey {last} is over {r['reinspect_after_months']} months old"
        hits = [f"renewal, premium {premium:,.0f} >= {r['renewal_min_premium']:,}", age]
        order_type, vendor = survey_type(hits)
        return Decision(
            kind="inspection",
            order_key=f"INS:{policy.policy_number}:renewal:{policy.expiration_date}",
            policy_number=policy.policy_number,
            order_type=order_type,
            vendor=vendor,
            trigger="renewal",
            period_start=policy.effective_date,
            period_end=policy.expiration_date,
            order_on=max(lead_start, policy.effective_date),
            due_on=max(lead_start, today) + timedelta(days=r["due_days"]),
            rule_hits=hits,
        )
    return None


def audit_type(policy: Policy, rules: dict) -> tuple[str, list[str]] | None:
    a = rules["audits"]
    premium = policy.deposit_premium
    if policy.line == "GL" and premium < a["waive_gl_below_premium"]:
        return None
    hits = ["workers' compensation, always audited" if policy.line == "WC"
            else f"general liability, premium {premium:,.0f} >= {a['waive_gl_below_premium']:,}"]
    if premium >= a["physical_min_premium"]:
        hits.append(f"premium {premium:,.0f} >= {a['physical_min_premium']:,} -> physical audit")
        return "physical", hits
    if premium >= a["phone_min_premium"]:
        hits.append(f"premium {premium:,.0f} >= {a['phone_min_premium']:,} -> phone audit")
        return "phone", hits
    hits.append(f"premium {premium:,.0f} < {a['phone_min_premium']:,} -> self-audit by mail")
    return "self", hits


def audit_decision(policy: Policy, today: date, rules: dict) -> Decision | None:
    a = rules["audits"]
    typed = audit_type(policy, rules)
    if typed is None:
        return None
    kind, hits = typed
    end = policy.period_end
    if end in policy.audited_period_ends:
        return None

    if policy.status == "cancelled" and policy.cancellation_date and policy.cancellation_date < policy.expiration_date:
        if today < policy.cancellation_date:
            return None
        trigger, order_on = "cancellation", policy.cancellation_date
        hits.append(f"cancelled {policy.cancellation_date}, ordered when processed")
    else:
        order_on = end - timedelta(days=a["lead_days"][kind])
        if today < order_on:
            return None
        trigger = "expiration"
        if a["lead_days"][kind]:
            hits.append(f"ordered {a['lead_days'][kind]} days before expiration so the vendor books after it")

    if today - end > timedelta(days=a["backlog_lookback_days"]):
        return None
    due_on = end + timedelta(days=a["complete_within_days"])
    if today > order_on + timedelta(days=7):
        hits.append(f"backlog: should have been ordered {order_on}")
        order_on = today
        due_on = max(due_on, today + timedelta(days=60))

    return Decision(
        kind="audit",
        order_key=f"AUD:{policy.policy_number}:{policy.effective_date}:{end}",
        policy_number=policy.policy_number,
        order_type=kind,
        vendor=a["vendors"][kind],
        trigger=trigger,
        period_start=policy.effective_date,
        period_end=end,
        order_on=order_on,
        due_on=due_on,
        rule_hits=hits,
    )


def required_role(rules: dict, kind: str, reason: str) -> str:
    return rules["authority"]["roles"][kind][reason]
