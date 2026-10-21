"""A small synthetic book run through three and a half months of simulated days, then the API on top."""

from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import TEST_DSN
from fastapi.testclient import TestClient

from opsagents import db, metrics, trail
from opsagents.agents.runtime import tick
from opsagents.api import create_app
from opsagents.llm.offline import OfflineLLM
from opsagents.rules import load_rules


def test_simulation_runs_without_errors(seeded):
    assert seeded["errors"] == []


def test_both_agents_finish_orders_and_most_go_straight_through(seeded):
    s = metrics.summary(seeded["conn"])
    for kind in ("inspection", "audit"):
        m = s["orders"][kind]
        assert m["finished"] > 0, kind
        assert 0.5 <= m["stp_rate"] <= 0.95, (kind, m["stp_rate"])
    assert s["exceptions"]["opened"] > 0
    assert s["premium"]["posted"] > 0 and s["premium"]["posted_by_agent_alone"] > 0


def test_every_trail_verifies(seeded):
    conn = seeded["conn"]
    ids = [r["id"] for r in conn.execute("SELECT id FROM ops.orders")]
    assert ids and all(trail.verify(conn, i)["ok"] for i in ids)


def test_every_finished_order_was_posted_or_closed_by_a_person(seeded):
    conn = seeded["conn"]
    unposted = conn.execute("""
        SELECT o.id FROM ops.orders o
        LEFT JOIN pas.audit_endorsements a ON a.order_id = o.id
        LEFT JOIN pas.inspections i ON i.order_id = o.id
        WHERE o.state = 'closed' AND a.order_id IS NULL AND i.order_id IS NULL""").fetchall()
    assert unposted == []
    manual = conn.execute("""SELECT count(*) AS n FROM ops.orders o WHERE state = 'closed_manual' AND NOT EXISTS
        (SELECT 1 FROM ops.exceptions e WHERE e.order_id = o.id AND e.resolution = 'reject')""").fetchone()["n"]
    assert manual == 0


def test_agent_never_posts_outside_its_authority(seeded, rules):
    rows = seeded["conn"].execute("""SELECT adjustment, deposit_premium FROM pas.audit_endorsements
                                     WHERE posted_by = 'ops-agent' AND basis = 'audited'""").fetchall()
    limit = rules["authority"]
    assert rows
    pct = Decimal(str(limit["audit_auto_post_max_pct"]))
    for r in rows:
        assert abs(r["adjustment"]) <= Decimal(str(limit["audit_auto_post_max_abs"]))
        assert abs(r["adjustment"]) <= (r["deposit_premium"] * pct).quantize(Decimal("0.01"))


def test_no_vendor_order_placed_twice(seeded):
    dupes = seeded["conn"].execute("""SELECT payload->>'policy_number' AS p, payload->>'period_end' AS e, count(*)
        FROM sim.vendor_orders WHERE vendor = 'HAP' GROUP BY 1, 2 HAVING count(*) > 1""").fetchall()
    assert dupes == []


def test_a_second_tick_on_the_same_day_changes_nothing(seeded):
    conn, outside = seeded["conn"], seeded["outside"]
    before = conn.execute("SELECT count(*) AS n FROM ops.events").fetchone()["n"]
    stats = tick(conn, load_rules(), OfflineLLM(), outside)
    after = conn.execute("SELECT count(*) AS n FROM ops.events").fetchone()["n"]
    assert stats["errors"] == [] and stats["identified"] == 0 and stats["steps"] == 0
    assert after == before


@pytest.fixture(scope="module")
def api(seeded):
    with TestClient(create_app(TEST_DSN)) as client:
        yield client


def test_api_reads(api):
    assert api.get("/health").json()["ok"] is True
    s = api.get("/metrics/summary").json()
    assert set(s["orders"]) == {"inspection", "audit", "all"}
    assert api.get("/metrics/daily").json()
    assert api.get("/metrics/vendors").json()
    first = api.get("/orders", params={"limit": 1}).json()["orders"][0]
    detail = api.get(f"/orders/{first['id']}").json()
    assert detail["trail"]["ok"] and detail["events"][0]["event_type"] == "identified"


def test_resolving_needs_a_token_for_the_right_role(api, monkeypatch):
    monkeypatch.setenv("ROLE_TOKENS", "audit_reviewer:t-audit,underwriter:t-uw,ops_lead:t-ops")
    open_ = api.get("/exceptions").json()
    assert open_, "the seeded queue should end with open exceptions"
    exc = open_[0]
    assert api.post(f"/exceptions/{exc['id']}/resolve", json={"action": "approve"}).status_code == 401
    wrong = {"audit_reviewer": "t-uw", "underwriter": "t-ops", "ops_lead": "t-audit"}[exc["required_role"]]
    assert api.post(f"/exceptions/{exc['id']}/resolve", json={"action": "approve"},
                    headers={"X-Role-Token": wrong}).status_code == 403


def test_approval_is_carried_out_by_the_worker(api, seeded, monkeypatch):
    monkeypatch.setenv("ROLE_TOKENS", "audit_reviewer:t-audit,underwriter:t-uw,ops_lead:t-ops")
    conn = seeded["conn"]
    exc = conn.execute("""SELECT e.* FROM ops.exceptions e WHERE status = 'open' AND reason = 'variance_review'
                          ORDER BY id LIMIT 1""").fetchone()
    if exc is None:
        pytest.skip("no open variance review in this seed")
    r = api.post(f"/exceptions/{exc['id']}/resolve", json={"action": "approve", "note": "checked"},
                 headers={"X-Role-Token": "t-audit"})
    assert r.status_code == 200 and r.json()["state"] == "approved"
    stats = tick(conn, load_rules(), OfflineLLM(), seeded["outside"])
    assert stats["errors"] == []
    posted = conn.execute("SELECT * FROM pas.audit_endorsements WHERE order_id = %s", (exc["order_id"],)).fetchone()
    assert posted["posted_by"].startswith("ops-agent, approved by human:audit_reviewer")
    assert trail.verify(conn, exc["order_id"])["ok"]


def test_advancing_a_day_moves_the_business_date(seeded):
    from opsagents.synthetic import simulate

    conn = seeded["conn"]
    day = db.today(conn) + timedelta(days=1)
    stats = simulate.run_day(conn, seeded["outside"], load_rules(), OfflineLLM(), day, reviewers=False)
    assert stats["errors"] == [] and db.today(conn) == day
