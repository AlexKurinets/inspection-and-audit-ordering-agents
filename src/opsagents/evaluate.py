"""Field-level extraction accuracy against known answers.

In production the known answers were values the audit team had keyed by hand. Here they
are the specs the simulator renders its PDFs from, a share of them as scans, so the same
harness runs against the offline stand-in or, with LLM_PROVIDER=claude, against the API.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from vendorsim import outcomes, reports

from .documents.ocr import read_pdf
from .documents.verify import parse_money
from .llm.base import LLM, LLMError
from .synthetic.classes import CLASS_MANUAL, TRADES


def _payload(rng: random.Random, i: int) -> dict:
    trade = rng.choice(TRADES)
    codes = [trade] + (["C-901"] if rng.random() < 0.6 else [])
    end = date(2026, 1, 1) + timedelta(days=rng.randint(0, 180))
    return {
        "policy_number": f"WC-2025-{900000 + i:06d}", "insured_name": f"Eval Insured {i} LLC",
        "survey_type": rng.choice(["full_survey", "desk_survey"]), "audit_type": rng.choice(["physical", "phone"]),
        "period_start": str(end - timedelta(days=365)), "period_end": str(end),
        "classes": [{"class_code": c, "description": CLASS_MANUAL[c][0],
                     "estimated_payroll": str(rng.randint(200, 3000) * 100)} for c in codes],
    }


def run_eval(llm: LLM, n: int = 100, seed: int = 7) -> dict:
    rng = random.Random(seed)
    right = total = failed_calls = scanned = 0
    misses: list[str] = []
    for i in range(n):
        p = _payload(rng, i)
        key = f"EVAL:{seed}:{i}"
        is_scan = rng.random() < 0.2
        scanned += is_scan
        if i % 2 == 0:
            spec = outcomes.inspection_spec(key, "LFS", f"EV-{i}", p, date(2026, 2, 1), "normal", is_scan)
            expected = {"policy_number": p["policy_number"], "survey_date": spec["survey_date"],
                        "risk_rating": spec["rating"], "recommendations": len(spec["recommendations"])}
            text = read_pdf(reports.inspection_pdf(spec)).text
            try:
                ex = llm.extract_inspection(text)
                got = {"policy_number": ex.policy_number.value, "survey_date": ex.survey_date.value,
                       "risk_rating": ex.risk_rating.value, "recommendations": len(ex.recommendations)}
            except LLMError:
                failed_calls += 1
                got = {}
        else:
            spec = outcomes.audit_spec(key, "HAP", f"EV-{i}", p, "normal", is_scan)
            expected = {"policy_number": p["policy_number"], "period_start": p["period_start"],
                        "period_end": p["period_end"], "total_payroll": float(spec["stated_total"]),
                        **{f"payroll {r['class_code']}": float(r["payroll"]) for r in spec["rows"]}}
            text = read_pdf(reports.audit_pdf(spec)).text
            try:
                ex = llm.extract_audit(text)
                got = {"policy_number": ex.policy_number.value, "period_start": ex.period_start.value,
                       "period_end": ex.period_end.value, "total_payroll": float(parse_money(ex.total_payroll.value)),
                       **{f"payroll {ln.class_code}": float(parse_money(ln.payroll)) for ln in ex.lines}}
            except (LLMError, ValueError):
                failed_calls += 1
                got = {}
        for field, value in expected.items():
            total += 1
            if got.get(field) == value:
                right += 1
            elif len(misses) < 10:
                misses.append(f"{key} {field}: expected {value!r}, got {got.get(field)!r}")
    return {"provider": llm.provider, "documents": n, "scanned": scanned, "fields": total, "correct": right,
            "field_accuracy": round(right / total, 4) if total else None, "failed_calls": failed_calls,
            "sample_misses": misses}
