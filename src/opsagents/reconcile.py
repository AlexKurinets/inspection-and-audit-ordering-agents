"""Earned premium from audited payroll, and whether the agent may post the adjustment itself."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

CENT = Decimal("0.01")


def money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def reconcile(payroll_by_class: dict[str, str], policy_rates: dict[str, Decimal], line_rates: dict[str, Decimal],
              deposit_premium: Decimal, rules: dict) -> dict[str, Any]:
    """Audited premium is payroll / 100 x rate per class, at the policy's rate where the class is
    on the policy and at the filed rate for the line where it isn't."""
    rows = []
    audited = Decimal(0)
    for code, payroll_text in sorted(payroll_by_class.items()):
        payroll = Decimal(payroll_text)
        rate = policy_rates.get(code, line_rates[code])
        premium = money(payroll / 100 * rate)
        audited += premium
        rows.append({"class_code": code, "payroll": str(payroll), "rate": str(rate), "premium": str(premium),
                     "on_policy": code in policy_rates})
    audited = money(audited)
    adjustment = money(audited - deposit_premium)
    a = rules["authority"]
    limit_abs = Decimal(str(a["audit_auto_post_max_abs"]))
    limit_pct = Decimal(str(a["audit_auto_post_max_pct"]))
    within_abs = abs(adjustment) <= limit_abs
    within_pct = abs(adjustment) <= money(deposit_premium * limit_pct)
    return {
        "rows": rows,
        "audited_premium": str(audited),
        "deposit_premium": str(money(deposit_premium)),
        "adjustment": str(adjustment),
        "auto_post": within_abs and within_pct,
        "limits": {"max_abs": str(limit_abs), "max_pct": str(limit_pct),
                   "within_abs": within_abs, "within_pct": within_pct},
    }
