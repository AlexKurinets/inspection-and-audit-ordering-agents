from datetime import date, timedelta
from decimal import Decimal

from opsagents.rules import Exposure, Policy, audit_decision, inspection_decision


def policy(**kw) -> Policy:
    base = dict(policy_number="WC-2025-000001", line="WC", insured_name="Fernholt Carpentry LLC",
                insured_email="office@fernholt-carpentry.example.com", effective_date=date(2025, 6, 1),
                expiration_date=date(2026, 6, 1), status="in_force", cancellation_date=None, new_business=False,
                last_inspection_date=date(2024, 1, 10), deposit_premium=Decimal("12000"),
                exposures=(Exposure("C-320", Decimal("120000"), Decimal("9.8"), "standard"),))
    base.update(kw)
    return Policy(**base)


def test_new_business_above_threshold_gets_desk_survey_below_full_survey_premium(rules):
    p = policy(new_business=True, effective_date=date(2026, 3, 10), expiration_date=date(2027, 3, 10))
    d = inspection_decision(p, date(2026, 3, 10), rules)
    assert (d.order_type, d.vendor, d.trigger) == ("desk_survey", "QRS", "new_business")
    assert d.due_on == date(2026, 3, 10) + timedelta(days=45)


def test_new_business_full_survey_by_premium_and_by_hazard(rules):
    big = policy(new_business=True, effective_date=date(2026, 3, 10), deposit_premium=Decimal("30000"))
    assert inspection_decision(big, date(2026, 3, 10), rules).order_type == "full_survey"
    roofer = policy(new_business=True, effective_date=date(2026, 3, 10), deposit_premium=Decimal("4000"),
                    exposures=(Exposure("C-210", Decimal("20000"), Decimal("18.5"), "high"),))
    d = inspection_decision(roofer, date(2026, 3, 10), rules)
    assert (d.order_type, d.vendor) == ("full_survey", "LFS")
    assert "high-hazard" in " ".join(d.rule_hits)


def test_small_standard_new_business_is_not_surveyed(rules):
    p = policy(new_business=True, effective_date=date(2026, 3, 10), deposit_premium=Decimal("4000"))
    assert inspection_decision(p, date(2026, 3, 10), rules) is None


def test_renewal_survey_75_days_out_only_when_last_survey_is_stale(rules):
    stale = policy(last_inspection_date=date(2023, 1, 5))
    d = inspection_decision(stale, date(2026, 3, 18), rules)
    assert d.trigger == "renewal" and d.order_on == date(2026, 6, 1) - timedelta(days=75)
    assert inspection_decision(stale, date(2026, 3, 17), rules) is None
    fresh = policy(last_inspection_date=date(2024, 1, 10))
    assert inspection_decision(fresh, date(2026, 3, 18), rules) is None


def test_physical_audit_is_ordered_30_days_before_expiration(rules):
    p = policy(deposit_premium=Decimal("30000"))
    assert audit_decision(p, date(2026, 5, 1), rules) is None
    d = audit_decision(p, date(2026, 5, 2), rules)
    assert (d.order_type, d.vendor, d.order_on) == ("physical", "HAP", date(2026, 5, 2))
    assert d.due_on == date(2026, 6, 1) + timedelta(days=90)


def test_phone_and_self_audits_are_ordered_at_expiration(rules):
    phone = policy(deposit_premium=Decimal("8000"))
    assert audit_decision(phone, date(2026, 5, 31), rules) is None
    assert audit_decision(phone, date(2026, 6, 1), rules).order_type == "phone"
    small = policy(deposit_premium=Decimal("3000"))
    d = audit_decision(small, date(2026, 6, 1), rules)
    assert (d.order_type, d.vendor) == ("self", "SELF")


def test_small_general_liability_is_waived_but_workers_comp_never_is(rules):
    assert audit_decision(policy(line="GL", deposit_premium=Decimal("2000")), date(2026, 6, 1), rules) is None
    assert audit_decision(policy(line="WC", deposit_premium=Decimal("800")), date(2026, 6, 1), rules) is not None


def test_cancellation_is_audited_from_the_cancellation_date(rules):
    p = policy(status="cancelled", cancellation_date=date(2026, 2, 14), deposit_premium=Decimal("30000"))
    assert audit_decision(p, date(2026, 2, 13), rules) is None
    d = audit_decision(p, date(2026, 2, 14), rules)
    assert (d.trigger, d.period_end, d.order_on) == ("cancellation", date(2026, 2, 14), date(2026, 2, 14))
    assert inspection_decision(p, date(2026, 3, 20), rules) is None


def test_expired_policy_never_audited_is_found_as_backlog(rules):
    p = policy(expiration_date=date(2026, 1, 1), effective_date=date(2025, 1, 1), deposit_premium=Decimal("8000"))
    d = audit_decision(p, date(2026, 4, 1), rules)
    assert d.order_on == date(2026, 4, 1)
    assert d.due_on == date(2026, 4, 1) + timedelta(days=60)
    assert any("backlog" in h for h in d.rule_hits)
    done = policy(expiration_date=date(2026, 1, 1), effective_date=date(2025, 1, 1),
                  audited_period_ends=frozenset({date(2026, 1, 1)}))
    assert audit_decision(done, date(2026, 4, 1), rules) is None


def test_order_keys_are_stable_across_days(rules):
    p = policy(deposit_premium=Decimal("30000"))
    assert audit_decision(p, date(2026, 5, 2), rules).order_key == audit_decision(p, date(2026, 5, 20), rules).order_key
