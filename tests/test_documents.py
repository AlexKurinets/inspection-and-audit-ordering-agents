"""Report rendering, PDF text, offline extraction and the verification checks, without a database."""

from datetime import date
from decimal import Decimal

import pytest
from conftest import HAS_TESSERACT

from opsagents.documents.ocr import read_pdf
from opsagents.documents.verify import verify_audit, verify_inspection
from opsagents.llm.base import Quoted
from opsagents.llm.offline import OfflineLLM
from opsagents.reconcile import reconcile
from vendorsim import outcomes, reports

PAYLOAD = {
    "policy_number": "WC-2025-000123", "insured_name": "Kestrelholt Roofing LLC", "audit_type": "physical",
    "survey_type": "full_survey", "period_start": "2025-04-01", "period_end": "2026-04-01",
    "classes": [{"class_code": "C-210", "description": "Roofing, residential", "estimated_payroll": "250000"},
                {"class_code": "C-901", "description": "Clerical office", "estimated_payroll": "40000"}],
}
ORDER = {"policy_number": "WC-2025-000123", "period_start": date(2025, 4, 1), "period_end": date(2026, 4, 1)}
KNOWN = {"C-210", "C-215", "C-320", "C-901", "C-905"}


def audit_text(outcome="normal", scanned=False) -> str:
    spec = outcomes.audit_spec("K1", "HAP", "HAP-100001-W1", PAYLOAD, outcome, scanned)
    return read_pdf(reports.audit_pdf(spec)).text


def test_text_pdf_is_read_without_ocr():
    spec = outcomes.audit_spec("K1", "HAP", "HAP-100001-W1", PAYLOAD, "normal", False)
    doc = read_pdf(reports.audit_pdf(spec))
    assert doc.pages == 1 and doc.ocr_pages == 0
    assert "Total payroll:" in doc.text and "C-210" in doc.text


def test_clean_worksheet_passes_every_check():
    text = audit_text()
    result = verify_audit(OfflineLLM().extract_audit(text), text, ORDER, KNOWN, {"C-210", "C-901"})
    assert result["ok"], result["failures"]
    assert set(result["payroll_by_class"]) == {"C-210", "C-901"}
    assert result["unlisted_classes"] == []


def test_rows_that_do_not_add_up_fail():
    text = audit_text("sum_mismatch")
    result = verify_audit(OfflineLLM().extract_audit(text), text, ORDER, KNOWN, {"C-210", "C-901"})
    assert not result["ok"]
    assert [f["check"] for f in result["failures"]] == ["rows add up to total"]


def test_payroll_in_a_class_not_on_the_policy_is_reported_separately():
    text = audit_text("unlisted_class")
    result = verify_audit(OfflineLLM().extract_audit(text), text, ORDER, KNOWN, {"C-210", "C-901"})
    assert result["ok"]
    assert result["unlisted_classes"] == ["C-320"]


def test_a_quote_the_document_does_not_contain_fails():
    text = audit_text()
    ex = OfflineLLM().extract_audit(text)
    ex.total_payroll = Quoted(value="$999,999", quote="Total payroll: $999,999")
    result = verify_audit(ex, text, ORDER, KNOWN, {"C-210", "C-901"})
    checks = {f["check"] for f in result["failures"]}
    assert "total payroll: quote in document" in checks


def test_a_value_missing_from_its_own_quote_fails():
    text = audit_text()
    ex = OfflineLLM().extract_audit(text)
    ex.lines[0].payroll = "$1,000"
    result = verify_audit(ex, text, ORDER, KNOWN, {"C-210", "C-901"})
    assert any(f["check"] == "class C-210: value in quote" for f in result["failures"])


def test_wrong_period_and_unknown_class_fail():
    text = audit_text()
    other = {**ORDER, "period_end": date(2026, 3, 1)}
    result = verify_audit(OfflineLLM().extract_audit(text), text, other, {"C-210"}, {"C-210"})
    failed = {f["check"] for f in result["failures"]}
    assert "period matches policy" in failed and "class C-901: known code" in failed


def test_inspection_report_extraction_and_critical_recommendation():
    spec = outcomes.inspection_spec("K2", "LFS", "LFS-100001-R1", PAYLOAD, date(2026, 3, 20), "critical", False)
    text = read_pdf(reports.inspection_pdf(spec)).text
    result = verify_inspection(OfflineLLM().extract_inspection(text), text, ORDER)
    assert result["ok"], result["failures"]
    assert result["recommendations"][0]["priority"] == "Critical"
    assert result["survey_date"] == "2026-03-20"


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract is not installed")
def test_scanned_worksheet_is_ocrd_and_still_verifies():
    spec = outcomes.audit_spec("K1", "HAP", "HAP-100001-W1", PAYLOAD, "normal", True)
    doc = read_pdf(reports.audit_pdf(spec))
    assert doc.ocr_pages == 1
    result = verify_audit(OfflineLLM().extract_audit(doc.text), doc.text, ORDER, KNOWN, {"C-210", "C-901"})
    assert result["ok"], result["failures"]
    clean = verify_audit(OfflineLLM().extract_audit(audit_text()), audit_text(), ORDER, KNOWN, {"C-210", "C-901"})
    assert result["payroll_by_class"] == clean["payroll_by_class"]


@pytest.mark.parametrize("subject,body,intent", [
    ("Re: Desk survey order [ORD-000004]",
     "Thank you. We received your order for a desk survey of X and logged it under reference Q-48001.", "acknowledged"),
    ("Re: x", "The desk survey for X is complete and the report is attached.", "report_delivered"),
    ("Re: x", "We have been unable to reach X at the number on file after three attempts.", "insured_unreachable"),
    ("Re: x", "Please find our completed self-audit attached.", "records_returned"),
    ("Re: x", "We closed the business at the end of last month and have no employees now.", "business_closed"),
    ("Re: x", "Can we have until the end of next month to pull the payroll records together?", "other"),
])
def test_offline_mail_reading(subject, body, intent):
    reading = OfflineLLM().read_mail(subject, body)
    assert reading.intent == intent
    assert reading.quote in " ".join(body.split())
    if intent == "acknowledged":
        assert reading.vendor_ref == "Q-48001"


def test_reconcile_premium_and_authority_limits(rules):
    rates = {"C-210": Decimal("18.50"), "C-901": Decimal("0.35")}
    line = {**rates, "C-320": Decimal("9.80")}
    small = reconcile({"C-210": "100000", "C-901": "40000"}, rates, line, Decimal("18000"), rules)
    assert small["audited_premium"] == "18640.00" and small["adjustment"] == "640.00" and small["auto_post"]
    over_abs = reconcile({"C-210": "120000"}, rates, line, Decimal("18000"), rules)
    assert over_abs["adjustment"] == "4200.00" and not over_abs["limits"]["within_abs"]
    over_pct = reconcile({"C-901": "300000"}, rates, line, Decimal("700"), rules)
    assert over_pct["limits"]["within_abs"] and not over_pct["limits"]["within_pct"] and not over_pct["auto_post"]
    unlisted = reconcile({"C-210": "100000", "C-320": "10000"}, rates, line, Decimal("19000"), rules)
    assert unlisted["audited_premium"] == "19480.00"
    assert [r["on_policy"] for r in unlisted["rows"]] == [True, False]
