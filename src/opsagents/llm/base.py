"""What the model is allowed to do: read an email or a report and return a fixed schema.

It has no tools, no credentials and no say over what happens next. Every value comes
back with the verbatim text it was read from, which ``documents/verify.py`` checks.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field


class Quoted(BaseModel):
    value: str = Field(description="The value exactly as written in the document")
    quote: str = Field(description="A verbatim span of the document that contains the value")


class PayrollLine(BaseModel):
    class_code: str
    description: str
    payroll: str = Field(description="Payroll exactly as written, e.g. $412,300")
    quote: str = Field(description="The verbatim line this row was read from")


class AuditExtraction(BaseModel):
    policy_number: Quoted
    period_start: Quoted
    period_end: Quoted
    total_payroll: Quoted
    lines: list[PayrollLine]


class Recommendation(BaseModel):
    ref: str = Field(description="The recommendation's label in the report, e.g. R2")
    priority: Literal["Critical", "Important", "Advisory"]
    text: str
    quote: str = Field(description="The verbatim line this recommendation was read from")


class InspectionExtraction(BaseModel):
    policy_number: Quoted
    survey_date: Quoted
    risk_rating: Quoted = Field(description="One of Good, Average, Below average, Poor")
    recommendations: list[Recommendation]


MailIntent = Literal[
    "acknowledged",        # vendor confirms receipt, usually with its own reference
    "scheduled",           # vendor or insured confirms an appointment date
    "report_delivered",    # vendor sends the completed report
    "insured_unreachable", # vendor cannot reach the insured
    "records_returned",    # insured sends the completed self-audit
    "business_closed",     # insured says the business has closed or stopped operating
    "other",
]


class MailReading(BaseModel):
    intent: MailIntent
    vendor_ref: str | None = Field(default=None, description="The sender's own reference, if one is given")
    scheduled_date: str | None = Field(default=None, description="Appointment date as written, if one is given")
    quote: str = Field(description="The verbatim sentence the intent was read from")


class LLMError(Exception):
    """The model refused, timed out or returned something that did not fit the schema."""


class LLM(Protocol):
    provider: str

    def read_mail(self, subject: str, body: str) -> MailReading: ...

    def extract_inspection(self, text: str) -> InspectionExtraction: ...

    def extract_audit(self, text: str) -> AuditExtraction: ...
