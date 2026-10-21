"""Smoke test: every dashboard page renders against a live API over a seeded database."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from conftest import TEST_DSN

DASH = Path(__file__).resolve().parents[1] / "dashboard"
PAGES = ["views/overview.py", "views/exceptions.py", "views/orders.py", "views/trail.py"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def api_url(seeded):
    # A separate process, so the API's connection pool doesn't share state with the test's.
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "opsagents.api:app", "--port", str(port)],
                            env={**os.environ, "DATABASE_URL": TEST_DSN})
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/health").status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.2)
    yield url
    proc.terminate()
    proc.wait(10)


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(api_url, page, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("API_URL", api_url)
    monkeypatch.syspath_prepend(str(DASH))
    sys.modules.pop("views.common", None)
    at = AppTest.from_file(str(DASH / "app.py"), default_timeout=60)
    at.run()
    if page != PAGES[0]:
        at.switch_page(page)
        at.run()
    assert not at.exception, at.exception
    assert at.title and at.title[0].value
    if page == "views/trail.py":
        assert any("Hash chain verified" in s.value for s in at.success)
