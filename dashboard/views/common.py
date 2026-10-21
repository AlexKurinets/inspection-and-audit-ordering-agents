"""Shared helpers. The dashboard only reads the API; it has no database access of its own."""

from __future__ import annotations

import os

import httpx
import streamlit as st

API_URL = os.environ.get("API_URL", "http://localhost:8140")

# Categorical slots 1 and 2 of the reference palette, light mode.
KIND_COLORS = {"audit": "#2a78d6", "inspection": "#eb6834"}
SINGLE = "#2a78d6"

REASONS = {
    "variance_review": "Adjustment outside authority",
    "unlisted_class": "Payroll in a class not on the policy",
    "underwriter_review": "Critical recommendation or poor rating",
    "extraction_unverified": "Extraction failed a check",
    "insured_non_responsive": "No self-audit after reminders",
    "business_closed": "Insured says business closed",
    "insured_unreachable": "Vendor cannot reach insured",
    "vendor_unreachable": "Vendor API rejected the order",
    "sla_breach": "Past due date",
    "mail_needs_person": "Email needs a person",
}


ORDER_TYPES = {
    "full_survey": "full survey by Larkspur Field Surveys",
    "desk_survey": "desk survey by Quillmate Risk Services",
    "physical": "physical audit by Harrowgate Audit Partners",
    "phone": "phone audit by Harrowgate Audit Partners",
    "self": "self-audit by mail",
}


def get(path: str, **params):
    r = httpx.get(f"{API_URL}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def post(path: str, json: dict, token: str):
    return httpx.post(f"{API_URL}{path}", json=json, headers={"X-Role-Token": token}, timeout=30)


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def money(x: float) -> str:
    return f"${x:,.0f}"


def signed(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def reconciliation_table(rec: dict):
    import pandas as pd

    return pd.DataFrame([{
        "class": r["class_code"], "payroll": f"${float(r['payroll']):,.0f}", "rate per $100": r["rate"],
        "premium": f"${float(r['premium']):,.2f}", "on policy": "yes" if r["on_policy"] else "no, filed rate",
    } for r in rec["rows"]])


def thousands(x: float) -> str:
    return f"${x / 1000:,.1f}k"


def md(text: str) -> str:
    """Escape dollar signs so Streamlit's markdown doesn't read a pair of them as LaTeX."""
    return text.replace("$", "\\$")


def header(title: str) -> dict:
    s = get("/metrics/summary")
    st.title(title)
    st.caption(f"Business date {s['today']} · synthetic book · data from {API_URL}")
    return s
