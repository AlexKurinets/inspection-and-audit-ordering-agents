"""Checks that run on every extraction before anything is computed or posted.

A value is accepted only when its quote is found in the document text and the value is
found inside its quote. Audit worksheets must also add up, cover the policy period and
use class codes that exist. Anything that fails goes to a person with the failed checks.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from ..llm.base import AuditExtraction, InspectionExtraction

RATINGS = ("Good", "Average", "Below average", "Poor")
TOLERANCE = Decimal("1.00")


def normalize(s: str) -> str:
    s = s.replace("–", "-").replace("—", "-").replace("’", "'")
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_found(quote: str, document: str) -> bool:
    q = normalize(quote)
    return len(q) >= 4 and q in normalize(document)


def value_in_quote(value: str, quote: str) -> bool:
    """Compared with all whitespace removed, because OCR often splits a number ("0001 26")."""
    v = re.sub(r"\s", "", normalize(value))
    return bool(v) and v in re.sub(r"\s", "", normalize(quote))


def parse_money(s: str) -> Decimal:
    try:
        return Decimal(re.sub(r"[^\d.]", "", s))
    except InvalidOperation as e:
        raise ValueError(f"not an amount: {s!r}") from e


def parse_date(s: str) -> date:
    return date.fromisoformat(s.strip())


class Checks:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.items.append({"check": name, "ok": bool(ok), "detail": detail})
        return ok

    def quoted(self, name: str, value: str, quote: str, document: str) -> bool:
        if not self.add(f"{name}: quote in document", quote_found(quote, document), quote):
            return False
        return self.add(f"{name}: value in quote", value_in_quote(value, quote), f"{value!r}")

    @property
    def ok(self) -> bool:
        return all(i["ok"] for i in self.items)

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [i for i in self.items if not i["ok"]]


def verify_audit(ex: AuditExtraction, document: str, order: dict, known_classes: set[str],
                 policy_classes: set[str]) -> dict[str, Any]:
    c = Checks()
    c.quoted("policy number", ex.policy_number.value, ex.policy_number.quote, document)
    c.add("policy number matches order", ex.policy_number.value == order["policy_number"], ex.policy_number.value)
    c.quoted("period start", ex.period_start.value, ex.period_start.quote, document)
    c.quoted("period end", ex.period_end.value, ex.period_end.quote, document)
    try:
        start, end = parse_date(ex.period_start.value), parse_date(ex.period_end.value)
        c.add("period matches policy", (start, end) == (order["period_start"], order["period_end"]),
              f"{start} to {end}, expected {order['period_start']} to {order['period_end']}")
    except ValueError as e:
        c.add("period is a date", False, str(e))

    c.quoted("total payroll", ex.total_payroll.value, ex.total_payroll.quote, document)
    lines: dict[str, str] = {}
    total = Decimal(0)
    for ln in ex.lines:
        if c.quoted(f"class {ln.class_code}", ln.payroll, ln.quote, document):
            c.add(f"class {ln.class_code}: code in quote", value_in_quote(ln.class_code, ln.quote), ln.class_code)
        c.add(f"class {ln.class_code}: known code", ln.class_code in known_classes, ln.class_code)
        try:
            amount = parse_money(ln.payroll)
            lines[ln.class_code] = str(amount)
            total += amount
        except ValueError as e:
            c.add(f"class {ln.class_code}: amount", False, str(e))
    c.add("at least one class", bool(ex.lines), f"{len(ex.lines)} rows")
    try:
        stated = parse_money(ex.total_payroll.value)
        c.add("rows add up to total", abs(stated - total) <= TOLERANCE, f"rows {total:,.2f}, stated {stated:,.2f}")
    except ValueError as e:
        stated = None
        c.add("total is an amount", False, str(e))

    return {
        "ok": c.ok,
        "checks": c.items,
        "failures": c.failures,
        "payroll_by_class": lines,
        "total_payroll": str(stated) if stated is not None else None,
        "unlisted_classes": sorted(set(lines) - policy_classes),
    }


def verify_inspection(ex: InspectionExtraction, document: str, order: dict) -> dict[str, Any]:
    c = Checks()
    c.quoted("policy number", ex.policy_number.value, ex.policy_number.quote, document)
    c.add("policy number matches order", ex.policy_number.value == order["policy_number"], ex.policy_number.value)
    c.quoted("survey date", ex.survey_date.value, ex.survey_date.quote, document)
    try:
        parse_date(ex.survey_date.value)
    except ValueError as e:
        c.add("survey date is a date", False, str(e))
    c.quoted("risk rating", ex.risk_rating.value, ex.risk_rating.quote, document)
    c.add("risk rating is a known value", ex.risk_rating.value in RATINGS, ex.risk_rating.value)
    for r in ex.recommendations:
        if c.quoted(f"recommendation {r.ref}", r.ref, r.quote, document):
            c.add(f"recommendation {r.ref}: priority in quote", value_in_quote(r.priority, r.quote), r.priority)
    return {
        "ok": c.ok,
        "checks": c.items,
        "failures": c.failures,
        "risk_rating": ex.risk_rating.value,
        "survey_date": ex.survey_date.value,
        "recommendations": [r.model_dump() for r in ex.recommendations],
    }
