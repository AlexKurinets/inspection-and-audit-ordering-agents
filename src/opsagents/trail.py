"""Per-order event log, hash-chained so any edit after the fact is detectable.

Each event's hash covers its own fields plus the previous event's hash. The table also
has a trigger that rejects UPDATE and DELETE, so tampering needs a superuser to switch
the trigger off first, and ``verify`` still finds the first altered event.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

GENESIS = "0" * 64


def _canonical(order_id: int, seq: int, occurred_on: date, actor: str, event_type: str,
               from_state: str | None, to_state: str | None, payload: dict) -> str:
    return json.dumps(
        {
            "order_id": order_id,
            "seq": seq,
            "occurred_on": occurred_on.isoformat(),
            "actor": actor,
            "event_type": event_type,
            "from_state": from_state,
            "to_state": to_state,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def chain_hash(prev_hash: str, canonical: str) -> str:
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


def append(conn: psycopg.Connection, order_id: int, occurred_on: date, actor: str, event_type: str,
           payload: dict[str, Any] | None = None, from_state: str | None = None,
           to_state: str | None = None) -> dict:
    """Append one event. Callers hold the order row lock, which serializes ``seq``."""
    payload = json.loads(json.dumps(payload or {}, default=str))
    last = conn.execute(
        "SELECT seq, hash FROM ops.events WHERE order_id = %s ORDER BY seq DESC LIMIT 1", (order_id,)
    ).fetchone()
    seq = (last["seq"] + 1) if last else 1
    prev = last["hash"] if last else GENESIS
    digest = chain_hash(prev, _canonical(order_id, seq, occurred_on, actor, event_type, from_state, to_state, payload))
    return conn.execute(
        """INSERT INTO ops.events (order_id, seq, occurred_on, actor, event_type, from_state, to_state,
                                   payload, prev_hash, hash)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (order_id, seq, occurred_on, actor, event_type, from_state, to_state, Jsonb(payload), prev, digest),
    ).fetchone()


def verify(conn: psycopg.Connection, order_id: int) -> dict:
    rows = conn.execute("SELECT * FROM ops.events WHERE order_id = %s ORDER BY seq", (order_id,)).fetchall()
    prev = GENESIS
    for i, r in enumerate(rows, start=1):
        expected = chain_hash(prev, _canonical(r["order_id"], r["seq"], r["occurred_on"], r["actor"],
                                               r["event_type"], r["from_state"], r["to_state"], r["payload"]))
        if r["seq"] != i or r["prev_hash"] != prev or r["hash"] != expected:
            return {"ok": False, "events": len(rows), "broken_at_seq": r["seq"]}
        prev = r["hash"]
    return {"ok": True, "events": len(rows), "broken_at_seq": None, "head": prev}
