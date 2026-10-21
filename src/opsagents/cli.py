from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, timedelta

from . import db, metrics, trail
from .agents.runtime import tick
from .integrations.outside import Outside
from .llm import get_llm
from .rules import load_rules
from .synthetic import book, simulate

SEED_START = date(2026, 3, 1)
SEED_END = date(2026, 6, 30)


def _progress(stats: dict) -> None:
    day = date.fromisoformat(stats["today"])
    if day.day == 1 or stats["errors"]:
        print(f"{stats['today']}: identified {stats['identified']}, feed {stats['feed_changes']}, "
              f"mail {stats['mail']}, steps {stats['steps']}, errors {len(stats['errors'])}", flush=True)


def cmd_seed(args) -> int:
    with db.connect() as conn:
        if args.if_empty:
            db.migrate(conn)
            if conn.execute("SELECT count(*) AS n FROM ops.orders").fetchone()["n"]:
                print("already seeded")
                return 0
        db.reset(conn)
        counts = book.generate(conn, args.start, args.end, args.expired, args.in_force, args.new_business)
        print(f"book: {json.dumps(counts)}")
        outside = Outside()
        started = time.perf_counter()
        # Reviewers stop four days before the end, so the queue holds what a few days of exceptions look like.
        errors = simulate.run(conn, outside, load_rules(), get_llm(), args.start, args.end, reviewers=True,
                              progress=_progress, review_until=args.end - timedelta(days=4))
        print(f"simulated {args.start} to {args.end} in {time.perf_counter() - started:.0f}s")
        _print_summary(conn)
        if errors:
            print("\n".join(errors[:20]), file=sys.stderr)
            return 1
    return 0


def cmd_advance(args) -> int:
    with db.connect() as conn:
        outside, rules, llm = Outside(), load_rules(), get_llm()
        for _ in range(args.days):
            day = db.today(conn) + timedelta(days=1)
            stats = simulate.run_day(conn, outside, rules, llm, day, reviewers=False)
            print(json.dumps(stats))
            if stats["errors"]:
                return 1
    return 0


def cmd_tick(args) -> int:
    with db.connect() as conn:
        stats = tick(conn, load_rules(), get_llm(), Outside())
        print(json.dumps(stats))
        return 1 if stats["errors"] else 0


def cmd_worker(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("opsagents.worker")
    rules, llm, outside = load_rules(), get_llm(), Outside()
    while True:
        try:
            with db.connect() as conn:
                stats = tick(conn, rules, llm, outside)
            if stats["identified"] or stats["feed_changes"] or stats["mail"] or stats["steps"] or stats["errors"]:
                log.info("tick %s", json.dumps(stats))
        except Exception:
            log.exception("tick failed")
        if args.once:
            return 0
        time.sleep(args.interval)


def cmd_verify(args) -> int:
    with db.connect() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM ops.orders ORDER BY id")]
        broken = [(i, v) for i in ids if not (v := trail.verify(conn, i))["ok"]]
        events = conn.execute("SELECT count(*) AS n FROM ops.events").fetchone()["n"]
    print(f"{len(ids)} orders, {events} events, {len(broken)} broken chains")
    for i, v in broken[:20]:
        print(f"order {i}: broken at seq {v['broken_at_seq']}")
    return 1 if broken else 0


def _print_summary(conn) -> None:
    s = metrics.summary(conn)
    for kind, m in s["orders"].items():
        print(f"{kind:10s} orders {m['orders']:4d}  finished {m['finished']:4d}  straight-through "
              f"{m['straight_through']:4d} ({(m['stp_rate'] or 0):.0%})  open {m['open']:3d}  "
              f"in review {m['in_review']:3d}  backlog found {m['backlog_found']}")
    w = s["audits_within_90_days"]
    print(f"audits within 90 days of period end: {w['on_time']}/{w['due']} ({(w['rate'] or 0):.0%})")
    e = s["exceptions"]
    print(f"exceptions opened {e['opened']}, open {e['open']}: "
          + ", ".join(f"{r['reason']} {r['opened']}" for r in e["by_reason"]))
    p = s["premium"]
    print(f"audits posted {p['posted']} ({p['audited']} audited, {p['estimated']} estimated), "
          f"additional ${p['additional']:,.2f}, return ${p['returned']:,.2f}, "
          f"posted by the agent alone {p['posted_by_agent_alone']}")
    print(f"documents {s['documents']['documents']} ({s['documents']['ocr_pages']} OCR pages), "
          f"extractions verified {s['extraction']['verified']}/{s['extraction']['extracted']}, "
          f"model calls {s['llm']['calls']} ({s['llm']['failed']} failed, {s['llm']['providers']})")


def cmd_status(args) -> int:
    with db.connect() as conn:
        print(f"business date {db.today(conn)}")
        _print_summary(conn)
    return 0


def cmd_eval(args) -> int:
    from .evaluate import run_eval

    result = run_eval(get_llm(), args.n, args.seed)
    print(json.dumps(result, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="opsagents")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="reset, generate the synthetic book and simulate its history")
    s.add_argument("--if-empty", action="store_true")
    s.add_argument("--start", type=date.fromisoformat, default=SEED_START)
    s.add_argument("--end", type=date.fromisoformat, default=SEED_END)
    s.add_argument("--expired", type=int, default=150)
    s.add_argument("--in-force", type=int, default=900)
    s.add_argument("--new-business", type=int, default=250)
    s.set_defaults(fn=cmd_seed)
    a = sub.add_parser("advance", help="move the business date forward and run the agents for each day")
    a.add_argument("--days", type=int, default=1)
    a.set_defaults(fn=cmd_advance)
    sub.add_parser("tick", help="run both agents once at the current business date").set_defaults(fn=cmd_tick)
    w = sub.add_parser("worker", help="run ticks in a loop")
    w.add_argument("--interval", type=float, default=5.0)
    w.add_argument("--once", action="store_true")
    w.set_defaults(fn=cmd_worker)
    sub.add_parser("verify-trail", help="recompute every order's hash chain").set_defaults(fn=cmd_verify)
    sub.add_parser("status", help="print the summary the dashboard shows").set_defaults(fn=cmd_status)
    e = sub.add_parser("eval-extraction", help="field accuracy of the configured model on generated reports")
    e.add_argument("--n", type=int, default=100)
    e.add_argument("--seed", type=int, default=7)
    e.set_defaults(fn=cmd_eval)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
