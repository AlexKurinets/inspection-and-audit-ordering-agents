"""Clients for the systems outside the agents: vendor REST APIs and the shared mailbox.

Production talked to two vendor APIs and to a shared Outlook mailbox through Microsoft
Graph. Here both point at the simulator service, with the same calls and failure modes:
timeouts, 5xx responses and an order accepted by a vendor whose response never arrived.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

AGENT_ADDRESS = "orders@ops-agents.example.com"
VENDOR_ADDRESSES = {
    "LFS": "accounts@larkspur-surveys.example.com",
    "QRS": "orders@quillmate-risk.example.com",
    "HAP": "service@harrowgate-audit.example.com",
}


class VendorUnavailable(Exception):
    pass


class Outside:
    def __init__(self, base_url: str | None = None, http: httpx.Client | None = None,
                 retries: int = 3, backoff_seconds: float | None = None):
        self.http = http or httpx.Client(base_url=base_url or os.environ.get("SIM_URL", "http://localhost:8141"),
                                         timeout=10.0)
        self.retries = retries
        self.backoff = float(os.environ.get("RETRY_BACKOFF_SECONDS", "0.2")) if backoff_seconds is None \
            else backoff_seconds

    def close(self) -> None:
        self.http.close()

    # vendors -------------------------------------------------------------

    def submit_order(self, vendor: str, idempotency_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Place an order. The same idempotency key always returns the same vendor reference, so a
        retry after a timeout cannot create a second order."""
        last = ""
        errors: list[str] = []
        for attempt in range(self.retries):
            try:
                r = self.http.post(f"/vendors/{vendor}/orders", json=payload,
                                   headers={"Idempotency-Key": idempotency_key})
                if r.status_code < 500:
                    r.raise_for_status()
                    body = r.json()
                    return {"vendor_ref": body["vendor_ref"], "duplicate": body["duplicate"],
                            "attempts": attempt + 1, "errors": errors}
                last = f"HTTP {r.status_code}"
            except httpx.TransportError as e:
                last = type(e).__name__
            errors.append(last)
            if attempt + 1 < self.retries and self.backoff:
                time.sleep(self.backoff * 2**attempt)
        raise VendorUnavailable(f"{vendor}: {last} after {self.retries} attempts")

    def status_changes(self, vendor: str, after: int) -> list[dict[str, Any]]:
        r = self.http.get(f"/vendors/{vendor}/changes", params={"after": after})
        r.raise_for_status()
        return r.json()["changes"]

    def report(self, vendor: str, report_id: str) -> bytes:
        r = self.http.get(f"/vendors/{vendor}/reports/{report_id}")
        r.raise_for_status()
        return r.content

    # mailbox -------------------------------------------------------------

    def send_mail(self, to: str, subject: str, body: str, form: dict[str, Any] | None = None) -> None:
        """Send from the shared mailbox. ``form`` is the structured order or self-audit form that
        production attached as a PDF."""
        r = self.http.post("/mail/send", json={"sender": AGENT_ADDRESS, "recipient": to, "subject": subject,
                                               "body": body, "form": form})
        r.raise_for_status()

    def inbox(self, after: int) -> list[dict[str, Any]]:
        r = self.http.get("/mail/inbox", params={"after": after})
        r.raise_for_status()
        return r.json()["messages"]

    def attachment(self, path: str) -> bytes:
        r = self.http.get(path)
        r.raise_for_status()
        return r.content
