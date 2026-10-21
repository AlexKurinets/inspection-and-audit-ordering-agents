"""A synthetic book of small-contractor workers' comp and general liability policies.

Every name, place, phone number and amount here is invented. The book has three groups:
policies that expired in the year before the simulation (most already audited by hand,
a few never ordered), policies in force when it starts, and new business bound during it.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta
from decimal import Decimal

import psycopg

from .classes import CLASS_MANUAL, TRADE_WEIGHTS, TRADES

PREFIXES = ["Bramble", "Hollow", "Cinder", "Fern", "Gable", "Heron", "Juniper", "Kestrel", "Lantern", "Marrow",
            "Nettle", "Pebble", "Quarry", "Rook", "Sable", "Thistle", "Umber", "Yarrow", "Alder", "Cobble",
            "Drift", "Flint", "Garnet", "Wren", "Bracken", "Corvin", "Dunmar", "Fallow", "Gorse", "Larch"]
SUFFIXES = ["crest", "brook", "stone", "wick", "mere", "ridge", "gate", "holt", "haven", "ford", "combe", "ley"]
TRADE_WORDS = {"C-210": "Roofing", "C-215": "Roofing", "C-320": "Carpentry", "C-330": "Drywall",
               "C-410": "Electric", "C-420": "Plumbing", "C-510": "Landscapes", "C-520": "Tree Care",
               "C-610": "Demolition", "C-710": "Painting"}
ENTITIES = ["LLC", "Inc.", "Co.", "& Sons", "Group"]
FIRST = ["Ada", "Bram", "Celia", "Dorian", "Edda", "Fenn", "Greta", "Hollis", "Ione", "Jory", "Kit", "Lusa",
         "Moss", "Nell", "Orin", "Perpetua", "Quill", "Rafe", "Sunniva", "Tamsin", "Ulric", "Vesna", "Wilder"]
LAST = ["Achterberg", "Bellweather", "Castellane", "Dunmore", "Elsby", "Farrowgate", "Grimsdyke", "Halloran",
        "Ingleby", "Jessop-Vale", "Kettleby", "Lorrimer", "Marchbanks", "Nettlefold", "Oxenham", "Pellegrue"]
CITIES = ["Quillon Bay", "Thorncastle", "Vellmore", "Wrenhaven", "Brackenford", "Dunmarrow", "Fallowmere",
          "Gorsebury", "Corvin Falls", "Ashmere Junction"]


def _payroll(rng: random.Random, median: float, sigma: float) -> Decimal:
    return Decimal(round(math.exp(rng.gauss(math.log(median), sigma)) / 100) * 100)


def _insured(rng: random.Random, trade: str, used: set[str]) -> str:
    while True:
        name = f"{rng.choice(PREFIXES)}{rng.choice(SUFFIXES)} {TRADE_WORDS[trade]} {rng.choice(ENTITIES)}"
        if name not in used:
            used.add(name)
            return name


def _slug(name: str) -> str:
    words = [w for w in name.lower().replace("&", "").replace(".", "").split() if w not in ("llc", "inc", "co")]
    return "-".join(words[:2])


def load_class_manual(conn: psycopg.Connection) -> None:
    for code, (desc, hazard, wc, gl) in CLASS_MANUAL.items():
        conn.execute("INSERT INTO pas.class_codes (code, description, hazard) VALUES (%s, %s, %s)", (code, desc, hazard))
        conn.execute("INSERT INTO pas.class_rates (line, class_code, rate) VALUES ('WC', %s, %s), ('GL', %s, %s)",
                     (code, Decimal(str(wc)), code, Decimal(str(gl))))


def generate(conn: psycopg.Connection, start: date, end: date, expired: int = 150, in_force: int = 900,
             new_business: int = 250, seed: int = 2026) -> dict[str, int]:
    rng = random.Random(seed)
    load_class_manual(conn)
    used: set[str] = set()
    counts = {"expired": expired, "in_force": in_force, "new_business": new_business, "backlog_candidates": 0,
              "cancellations": 0}
    groups = ([("expired", start - timedelta(days=730), start - timedelta(days=366))] * expired
              + [("in_force", start - timedelta(days=365), start - timedelta(days=1))] * in_force
              + [("new_business", start, end)] * new_business)
    seq = 0
    for group, lo, hi in groups:
        seq += 1
        eff = lo + timedelta(days=rng.randint(0, (hi - lo).days))
        exp = eff.replace(year=eff.year + 1) if not (eff.month == 2 and eff.day == 29) else eff + timedelta(days=365)
        line = "WC" if rng.random() < 0.6 else "GL"
        trade = rng.choices(TRADES, weights=TRADE_WEIGHTS)[0]
        exposures = [(trade, _payroll(rng, 110_000, 0.75))]
        if rng.random() < 0.6:
            exposures.append(("C-901", _payroll(rng, 45_000, 0.4)))
        if rng.random() < 0.2:
            exposures.append(("C-905", _payroll(rng, 60_000, 0.4)))
        rate_col = 2 if line == "WC" else 3
        rated = [(code, payroll, Decimal(str(CLASS_MANUAL[code][rate_col]))) for code, payroll in exposures]
        premium = sum((p * r / 100 for _, p, r in rated), Decimal(0)).quantize(Decimal("0.01"))
        insured = _insured(rng, trade, used)
        number = f"{line}-{eff.year}-{seq:06d}"
        last_insp = None if rng.random() < 0.3 else eff - timedelta(days=rng.randint(200, 1900))
        conn.execute(
            """INSERT INTO pas.policies (policy_number, line, insured_name, contact_name, insured_email, insured_phone,
                                         city, effective_date, expiration_date, status, cancellation_date,
                                         new_business, last_inspection_date, deposit_premium)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'in_force', NULL, %s, %s, %s)""",
            (number, line, insured, f"{rng.choice(FIRST)} {rng.choice(LAST)}", f"office@{_slug(insured)}.example.com",
             f"555-01{rng.randint(0, 99):02d}", rng.choice(CITIES), eff, exp, group == "new_business", last_insp,
             premium),
        )
        for code, payroll, rate in rated:
            conn.execute("INSERT INTO pas.policy_exposures (policy_number, class_code, estimated_payroll, rate) "
                         "VALUES (%s, %s, %s, %s)", (number, code, payroll, rate))
        if group == "expired":
            if rng.random() < 0.88:
                conn.execute("INSERT INTO pas.audit_history (policy_number, period_end, completed_on) VALUES "
                             "(%s, %s, %s)", (number, exp, exp + timedelta(days=rng.randint(20, 80))))
            else:
                counts["backlog_candidates"] += 1
        elif rng.random() < 0.06:
            first = max(eff + timedelta(days=30), start)
            last = min(exp - timedelta(days=1), end)
            if first < last:
                conn.execute("INSERT INTO sim.pas_changes (policy_number, change_on, kind) VALUES (%s, %s, 'cancel')",
                             (number, first + timedelta(days=rng.randint(0, (last - first).days))))
                counts["cancellations"] += 1
    conn.commit()
    return counts


def apply_pas_changes(conn: psycopg.Connection, today: date) -> int:
    """What the policy system does on its own each day: process the cancellations dated today or earlier."""
    due = conn.execute("SELECT * FROM sim.pas_changes WHERE NOT applied AND change_on <= %s", (today,)).fetchall()
    for c in due:
        conn.execute("UPDATE pas.policies SET status = 'cancelled', cancellation_date = %s WHERE policy_number = %s",
                     (c["change_on"], c["policy_number"]))
        conn.execute("INSERT INTO pas.activity (policy_number, logged_on, author, note) VALUES (%s, %s, %s, %s)",
                     (c["policy_number"], today, "billing", f"Policy cancelled effective {c['change_on']}."))
        conn.execute("UPDATE sim.pas_changes SET applied = true WHERE policy_number = %s AND kind = %s",
                     (c["policy_number"], c["kind"]))
    return len(due)
