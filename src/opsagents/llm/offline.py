"""Deterministic stand-in for the model, used by the tests and by the demo stack.

It reads the simulator's report layouts and email phrasing with regular expressions and
returns the same schemas, quotes included, so everything downstream of the model runs
unchanged. It only works because the synthetic documents follow known layouts; real
vendor reports are why the production build calls Claude (``llm/claude.py``).
"""

from __future__ import annotations

import re

from .base import AuditExtraction, InspectionExtraction, LLMError, MailReading, PayrollLine, Quoted, Recommendation

MONEY = r"\$[\d,]+(?:\.\d{2})?"
DATE = r"\d{4}-\d{2}-\d{2}"
POLICY = r"[A-Z]{2}-\d{4}-\d[\d ]{4,6}\d"  # OCR sometimes splits the digits


def _lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines() if ln.strip()]


def _field(lines: list[str], label: str, pattern: str, squash: bool = False) -> Quoted:
    rx = re.compile(rf"{label}\s*:?\s*({pattern})", re.IGNORECASE)
    for ln in lines:
        m = rx.search(ln)
        if m:
            value = m.group(1).strip()
            return Quoted(value=value.replace(" ", "") if squash else value, quote=ln)
    raise LLMError(f"could not find {label}")


def _sentence(body: str, needle: str) -> str:
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(body.split())):
        if needle.lower() in sentence.lower():
            return sentence
    return " ".join(body.split())[:200]


MAIL_RULES: list[tuple[str, str]] = [
    ("business_closed", "closed the business"),
    ("business_closed", "no longer operating"),
    ("records_returned", "completed self-audit"),
    ("insured_unreachable", "unable to reach"),
    ("report_delivered", "report is attached"),
    ("scheduled", "is scheduled for"),
    ("acknowledged", "received your order"),
]


class OfflineLLM:
    provider = "offline"

    def read_mail(self, subject: str, body: str) -> MailReading:
        text = f"{subject}\n{body}"
        for intent, needle in MAIL_RULES:
            if needle in text.lower():
                ref = re.search(r"reference\s+([A-Z]{1,4}-[A-Z0-9-]+)", text)
                when = re.search(rf"scheduled for ({DATE})", text)
                return MailReading(intent=intent, vendor_ref=ref.group(1) if ref else None,
                                   scheduled_date=when.group(1) if when else None, quote=_sentence(body, needle))
        return MailReading(intent="other", quote=_sentence(body, body.split(".")[0]))

    def extract_inspection(self, text: str) -> InspectionExtraction:
        lines = _lines(text)
        recs = []
        rx = re.compile(r"^(R\d+)\s*\((Critical|Important|Advisory)\)\s*(.+)$")
        for ln in lines:
            m = rx.match(ln)
            if m:
                recs.append(Recommendation(ref=m.group(1), priority=m.group(2), text=m.group(3), quote=ln))
        return InspectionExtraction(
            policy_number=_field(lines, "Policy number", POLICY, squash=True),
            survey_date=_field(lines, "Survey date", DATE),
            risk_rating=_field(lines, "Overall risk rating", r"Good|Average|Below average|Poor"),
            recommendations=recs,
        )

    def extract_audit(self, text: str) -> AuditExtraction:
        lines = _lines(text)
        period = None
        for ln in lines:
            m = re.search(rf"Audit period\s*:?\s*({DATE})\s+to\s+({DATE})", ln, re.IGNORECASE)
            if m:
                period = (Quoted(value=m.group(1), quote=ln), Quoted(value=m.group(2), quote=ln))
                break
        if period is None:
            raise LLMError("could not find the audit period")
        rows = []
        rx = re.compile(rf"^(C-\d{{3}})\s+(.+?)\s+({MONEY})$")
        for ln in lines:
            m = rx.match(ln)
            if m:
                rows.append(PayrollLine(class_code=m.group(1), description=m.group(2), payroll=m.group(3), quote=ln))
        return AuditExtraction(
            policy_number=_field(lines, "Policy number", POLICY, squash=True),
            period_start=period[0],
            period_end=period[1],
            total_payroll=_field(lines, "Total payroll", MONEY),
            lines=rows,
        )
