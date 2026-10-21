"""Outbound email. Fixed templates, never model-written text, because these go to insureds and vendors."""

from __future__ import annotations

from ..rules import Policy
from ..synthetic.classes import description

VENDORS = {
    "LFS": ("Larkspur Field Surveys", "555-0142"),
    "QRS": ("Quillmate Risk Services", "555-0167"),
    "HAP": ("Harrowgate Audit Partners", "555-0188"),
}
OPS_PHONE = "555-0110"
TYPE_NAMES = {"physical": "physical audit", "phone": "phone audit", "full_survey": "full survey",
              "desk_survey": "desk survey", "self": "self-audit"}
SIGNATURE = f"Premium Audit and Loss Control\n{OPS_PHONE}"


def tag(order: dict) -> str:
    return f"[ORD-{order['id']:06d}]"


def classes(policy: Policy, with_payroll: bool) -> list[dict]:
    out = []
    for e in policy.exposures:
        row = {"class_code": e.class_code, "description": description(e.class_code), "hazard": e.hazard}
        if with_payroll:
            row["estimated_payroll"] = str(e.estimated_payroll)
        out.append(row)
    return out


def vendor_payload(order: dict, policy: Policy) -> dict:
    base = {"policy_number": policy.policy_number, "insured_name": policy.insured_name,
            "insured_phone": policy.insured_phone, "contact_name": policy.contact_name}
    if order["kind"] == "inspection":
        return {**base, "survey_type": order["order_type"], "classes": classes(policy, False),
                "due_on": str(order["due_on"])}
    return {**base, "audit_type": order["order_type"], "period_start": str(order["period_start"]),
            "period_end": str(order["period_end"]), "classes": classes(policy, True)}


def desk_survey_order(order: dict, policy: Policy) -> tuple[str, str, dict]:
    subject = f"Desk survey order {policy.policy_number} {tag(order)}"
    body = (f"Please complete a desk survey for {policy.insured_name}, policy {policy.policy_number}, "
            f"by {order['due_on']}. The insured contact is {policy.contact_name} at {policy.insured_phone}. "
            f"The order form is attached.\n\n{SIGNATURE}")
    return subject, body, {"type": "desk_survey_order", **vendor_payload(order, policy)}


def self_audit_request(order: dict, policy: Policy) -> tuple[str, str, dict]:
    subject = f"Self-audit request for policy {policy.policy_number} {tag(order)}"
    body = (f"Hello {policy.contact_name},\n\nYour policy {policy.policy_number} covered {order['period_start']} "
            f"to {order['period_end']}. Please fill in the attached self-audit form with your actual payroll "
            f"for each classification in that period and reply to this email with it. If anything has changed "
            f"in your business, tell us in your reply or call us at {OPS_PHONE}.\n\n{SIGNATURE}")
    form = {"type": "self_audit_request", **vendor_payload(order, policy)}
    return subject, body, form


def self_audit_reminder(order: dict, policy: Policy, n: int) -> tuple[str, str]:
    subject = f"Reminder {n}: self-audit for policy {policy.policy_number} {tag(order)}"
    body = (f"Hello {policy.contact_name},\n\nWe haven't received the self-audit form for policy "
            f"{policy.policy_number} yet. Please reply with the completed form, or call us at {OPS_PHONE} "
            f"if you need help with it.\n\n{SIGNATURE}")
    return subject, body


def insured_contact_for_survey(order: dict, policy: Policy) -> tuple[str, str]:
    name, phone = VENDORS[order["vendor"]]
    subject = f"Please call {name} about your safety survey {tag(order)}"
    body = (f"Hello {policy.contact_name},\n\n{name} has been trying to reach you to arrange the safety survey "
            f"for policy {policy.policy_number}. Please call them at {phone} this week.\n\n{SIGNATURE}")
    return subject, body


def insured_records_request(order: dict, policy: Policy) -> tuple[str, str]:
    name, phone = VENDORS[order["vendor"]]
    subject = f"Payroll records for your premium audit {tag(order)}"
    body = (f"Hello {policy.contact_name},\n\nThe auditor from {name} is waiting on payroll records for policy "
            f"{policy.policy_number}. Please send them or call {phone} so the audit can be completed.\n\n"
            f"{SIGNATURE}")
    return subject, body


def vendor_chase(order: dict, policy: Policy) -> tuple[str, str, str]:
    name, _ = VENDORS[order["vendor"]]
    what = "survey" if order["kind"] == "inspection" else "audit"
    subject = f"Status of {what} {order['vendor_ref'] or policy.policy_number} {tag(order)}"
    body = (f"Hello {name},\n\nThe {what} for {policy.insured_name}, policy {policy.policy_number}, is due "
            f"{order['due_on']}. Could you confirm the date it will be completed?\n\n{SIGNATURE}")
    return subject, body, name
