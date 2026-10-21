"""How each simulated vendor and insured responds, decided once per order from a hash of its key.

Nothing here is random at run time: the same order key always gets the same outcome and
timeline, so a seeded run produces the same dashboard figures every time.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta

from opsagents.synthetic.classes import description

SURVEYORS = ["Ines Halloway", "Marten Coyle", "Priya Vandermeer", "Oskar Lindqvist-Bray", "Dalia Fenwright"]
AUDITORS = ["Teodor Lind", "Rosalind Achebe-Marsh", "Callum Ostrander", "Yvette Duquesnoy", "Bram Holloway"]

RECS = {
    "Critical": [
        "Stop roof work above 6 feet until fall protection is in place for every crew.",
        "Remove the two damaged extension ladders from service immediately.",
        "Guard the exposed table-saw blade in the shop before it is used again.",
        "Keep crews out of the unshored trench at the yard until it is sloped or boxed.",
    ],
    "Important": [
        "Provide guardrails or personal fall arrest for all work above 6 feet.",
        "Replace the frayed extension cords found in two crew trucks.",
        "Document a vehicle inspection before each shift for every crew truck.",
        "Store fuel cans in a ventilated cabinet away from the shop exit.",
        "Train crew leads on ladder setup and three-point contact within 60 days.",
    ],
    "Advisory": [
        "Post emergency contact numbers in each crew vehicle.",
        "Hold a short weekly toolbox talk and keep a sign-in sheet.",
        "Add a first-aid kit to the second service van.",
        "Keep the exit path in the shop clear of stacked material.",
    ],
}

# Payroll a vendor sometimes finds in a class that is not on the policy.
UNLISTED = {"C-210": "C-320", "C-215": "C-610", "C-320": "C-210", "C-330": "C-710", "C-410": "C-420",
            "C-420": "C-410", "C-510": "C-520", "C-520": "C-510", "C-610": "C-215", "C-710": "C-330"}


def rng_for(key: str) -> random.Random:
    return random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:16], 16))


def pick(rng: random.Random, weights: dict[str, float]) -> str:
    r = rng.random()
    acc = 0.0
    for name, w in weights.items():
        acc += w
        if r < acc:
            return name
    return "normal"


def _recs(rng: random.Random, outcome: str) -> tuple[str, list[dict]]:
    if outcome == "poor":
        rating = "Poor"
    else:
        rating = rng.choices(["Good", "Average", "Below average"], weights=[45, 45, 10])[0]
    recs = []
    if outcome == "critical":
        recs.append({"priority": "Critical", "text": rng.choice(RECS["Critical"])})
    n_imp = {"Good": 0, "Average": 1, "Below average": 2, "Poor": 2}[rating]
    recs += [{"priority": "Important", "text": t} for t in rng.sample(RECS["Important"], n_imp)]
    recs += [{"priority": "Advisory", "text": t} for t in rng.sample(RECS["Advisory"], rng.randint(0, 2))]
    return rating, recs


def inspection_spec(key: str, vendor: str, report_id: str, payload: dict, survey_date: date, outcome: str,
                    scanned: bool) -> dict:
    rng = rng_for(key + ":report")
    rating, recs = _recs(rng, outcome)
    main = payload["classes"][0]
    return {
        "kind": "inspection", "vendor": vendor, "report_id": report_id,
        "policy_number": payload["policy_number"], "insured_name": payload["insured_name"],
        "survey_date": survey_date.isoformat(),
        "survey_type": "Full on-site survey" if payload["survey_type"] == "full_survey" else "Desk survey by phone",
        "operations": f"{main['description']}. {rng.randint(3, 40)} employees.",
        "rating": rating, "recommendations": recs, "surveyor": rng.choice(SURVEYORS), "scanned": scanned,
    }


def audit_spec(key: str, vendor: str, report_id: str, payload: dict, outcome: str, scanned: bool,
               signed_by: str | None = None) -> dict:
    rng = rng_for(key + ":worksheet")
    r = rng.random()
    if outcome == "large_variance" or r < 0.10:
        factor = rng.uniform(1.30, 1.70)
    elif r < 0.18:
        factor = rng.uniform(0.55, 0.75)
    elif r < 0.25:
        factor = 1.0
    else:
        factor = rng.uniform(0.94, 1.07)
    rows = []
    for c in payload["classes"]:
        payroll = round(float(c["estimated_payroll"]) * factor / 100) * 100
        rows.append({"class_code": c["class_code"], "description": c["description"], "payroll": payroll})
    if outcome == "unlisted_class":
        code = UNLISTED.get(payload["classes"][0]["class_code"], "C-710")
        if code not in {c["class_code"] for c in payload["classes"]}:
            payroll = round(rows[0]["payroll"] * rng.uniform(0.12, 0.25) / 100) * 100
            rows.append({"class_code": code, "description": description(code), "payroll": payroll})
    total = sum(r["payroll"] for r in rows)
    stated = total + (rng.choice([9000, -9000, 900]) if outcome == "sum_mismatch" else 0)
    return {
        "kind": "audit", "vendor": vendor, "report_id": report_id,
        "policy_number": payload["policy_number"], "insured_name": payload["insured_name"],
        "period_start": payload["period_start"], "period_end": payload["period_end"],
        "audit_type": {"physical": "Physical", "phone": "Phone", "self": "Self-audit"}[payload["audit_type"]],
        "rows": rows, "stated_total": stated,
        "records": "payroll journal, quarterly wage reports, subcontractor certificates",
        "signed_by": signed_by or rng.choice(AUDITORS), "scanned": scanned,
    }


def d(day: date, n: int) -> date:
    return day + timedelta(days=n)


def plan_api_inspection(key: str, vendor: str, ref: str, payload: dict, today: date) -> list[tuple[date, str, dict]]:
    """Returns (due_on, channel, body) plans. Feed bodies are status changes."""
    rng = rng_for(key)
    outcome = pick(rng, {"critical": 0.10, "poor": 0.04, "unreachable_recovers": 0.04,
                         "unreachable_stuck": 0.03, "slow": 0.02})
    scanned = rng.random() < 0.06
    report_id = f"{ref}-R1"
    plans: list[tuple[date, str, dict]] = [(d(today, 1), "feed", {"status": "received"})]
    if outcome.startswith("unreachable"):
        plans.append((d(today, 10), "feed", {"status": "insured_unreachable"}))
        if outcome == "unreachable_stuck":
            return plans
        survey = d(today, 24)
        plans.append((d(today, 18), "feed", {"status": "scheduled", "scheduled_for": survey.isoformat()}))
        outcome = "normal"
    else:
        survey = d(today, rng.randint(8, 20) + (40 if outcome == "slow" else 0))
        plans.append((d(today, rng.randint(3, 7)), "feed", {"status": "scheduled", "scheduled_for": survey.isoformat()}))
    done = d(survey, rng.randint(1, 4))
    spec = inspection_spec(key, vendor, report_id, payload, survey, outcome, scanned)
    plans.append((done, "feed", {"status": "completed", "report_id": report_id, "report": spec}))
    return plans


def plan_api_audit(key: str, vendor: str, ref: str, payload: dict, today: date) -> list[tuple[date, str, dict]]:
    rng = rng_for(key)
    outcome = pick(rng, {"unlisted_class": 0.04, "sum_mismatch": 0.03, "awaiting_records": 0.08, "slow": 0.03})
    scanned = rng.random() < 0.05
    report_id = f"{ref}-W1"
    period_end = date.fromisoformat(payload["period_end"])
    base = max(today, period_end)
    visit = d(base, rng.randint(5, 30 if payload["audit_type"] == "physical" else 18) + (80 if outcome == "slow" else 0))
    plans: list[tuple[date, str, dict]] = [
        (d(today, 1), "feed", {"status": "received"}),
        (d(today, rng.randint(2, 6)), "feed", {"status": "scheduled", "scheduled_for": visit.isoformat()}),
    ]
    done = d(visit, rng.randint(1, 4))
    if outcome == "awaiting_records":
        plans.append((d(visit, 1), "feed", {"status": "awaiting_records"}))
        done = d(visit, rng.randint(12, 25))
    spec = audit_spec(key, vendor, report_id, payload, outcome, scanned)
    plans.append((done, "feed", {"status": "completed", "report_id": report_id, "report": spec}))
    return plans


def plan_desk_survey(key: str, ref: str, form: dict, reply_to: str, subject: str, today: date) -> list:
    rng = rng_for(key)
    outcome = pick(rng, {"critical": 0.08, "unreachable_recovers": 0.04, "unreachable_stuck": 0.03})
    scanned = rng.random() < 0.06
    report_id = f"{ref}-R1"
    insured = form["insured_name"]
    mail = lambda body, att=None: {"recipient": reply_to, "subject": f"Re: {subject}", "body": body,  # noqa: E731
                                   "attachment": att, "sender": "orders@quillmate-risk.example.com"}
    plans = [(d(today, 1), "mail", mail(
        f"Thank you. We received your order for a desk survey of {insured} and logged it under reference {ref}. "
        "We will call the insured within the week."))]
    if outcome.startswith("unreachable"):
        plans.append((d(today, 8), "mail", mail(
            f"We have been unable to reach {insured} at the number on file after three attempts. "
            "Could you ask them to call us back?")))
        if outcome == "unreachable_stuck":
            return plans
        call = d(today, 16)
        plans.append((d(today, 13), "mail", mail(f"The insured called back. The desk survey is scheduled for {call}.")))
        outcome = "normal"
    else:
        call = d(today, rng.randint(4, 12))
    spec = inspection_spec(key, "QRS", report_id, form, call, outcome, scanned)
    plans.append((d(call, rng.randint(1, 3)), "mail", mail(
        f"The desk survey for {insured} is complete and the report is attached. Please let us know if you have "
        "any questions.", {"report_id": report_id, "report": spec})))
    return plans


def plan_self_audit(key: str, form: dict, reply_to: str, insured_addr: str, subject: str, today: date) -> list:
    rng = rng_for(key)
    outcome = pick(rng, {"returns_late": 0.14, "never": 0.10, "business_closed": 0.04, "question": 0.03,
                         "sum_mismatch": 0.03})
    scanned = rng.random() < 0.05
    report_id = f"SA-{form['policy_number'][-6:]}-{form['period_end'][:4]}"
    owner = form.get("contact_name", "Owner")
    mail = lambda body, att=None: {"recipient": reply_to, "subject": f"Re: {subject}", "body": body,  # noqa: E731
                                   "attachment": att, "sender": insured_addr}
    if outcome == "never":
        return []
    if outcome == "business_closed":
        return [(d(today, rng.randint(3, 10)), "mail", mail(
            "We closed the business at the end of last month and have no employees now. "
            "Is there anything else you need from us?"))]
    plans = []
    if outcome == "question":
        plans.append((d(today, 5), "mail", mail(
            "Can we have until the end of next month to pull the payroll records together? Our bookkeeper is out.")))
    back = d(today, rng.randint(18, 36) if outcome in ("returns_late", "question") else rng.randint(4, 12))
    spec = audit_spec(key, "SELF", report_id, form, "sum_mismatch" if outcome == "sum_mismatch" else "normal",
                      scanned, signed_by=f"{owner}, owner")
    plans.append((back, "mail", mail("Please find our completed self-audit attached.",
                                     {"report_id": report_id, "report": spec})))
    return plans
