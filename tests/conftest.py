from __future__ import annotations

import hashlib
import os
import shutil
from datetime import date

import pytest
from fastapi.testclient import TestClient

from opsagents import db
from opsagents.integrations.outside import Outside
from opsagents.llm.offline import OfflineLLM
from opsagents.rules import load_rules
from opsagents.synthetic import book, simulate
from vendorsim.app import create_app as create_sim

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://ops:ops@localhost:5474/ops_test")
HAS_TESSERACT = shutil.which("tesseract") is not None


@pytest.fixture
def conn():
    c = db.connect(TEST_DSN)
    db.reset(c)
    yield c
    c.close()


@pytest.fixture(scope="session")
def rules():
    return load_rules()


@pytest.fixture
def outside(conn):
    with TestClient(create_sim(TEST_DSN), base_url="http://sim") as client:
        yield Outside(http=client, backoff_seconds=0)


@pytest.fixture(scope="module")
def seeded():
    """A small book with three and a half months of simulated history, shared by a module's tests."""
    c = db.connect(TEST_DSN)
    db.reset(c)
    book.generate(c, date(2026, 3, 1), date(2026, 6, 15), expired=20, in_force=140, new_business=40)
    with TestClient(create_sim(TEST_DSN), base_url="http://sim") as client:
        out = Outside(http=client, backoff_seconds=0)
        # Reviewers stop ten days before the end, so the queue ends with open exceptions.
        errors = simulate.run(c, out, load_rules(), OfflineLLM(), date(2026, 3, 1), date(2026, 6, 15),
                              review_until=date(2026, 6, 5))
        yield {"conn": c, "outside": out, "errors": errors}
    c.close()


def key_with(predicate, prefix: str = "AUD:TEST") -> str:
    """An idempotency key whose hash puts the simulator into a particular failure mode."""
    for i in range(100_000):
        key = f"{prefix}:{i}"
        h = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
        if predicate(h):
            return key
    raise AssertionError("no key found")
