#!/usr/bin/env bash
# Runs every command the README documents against a running stack (`docker compose up -d --build --wait`).
set -euo pipefail

API="http://localhost:${API_PORT:-8140}"
DASH="http://localhost:${DASHBOARD_PORT:-8540}"
SIM="http://localhost:${SIM_PORT:-8141}"
PY="docker compose exec -T api python -c"
OUT="$(mktemp -d)"

echo "== health"
curl -fsS "$API/health" && echo
curl -fsS "$SIM/health" && echo
curl -fsS "$DASH/_stcore/health" && echo

echo "== seeded figures"
curl -fsS "$API/metrics/summary" > "$OUT/summary.json"
$PY "import json, sys; s = json.load(sys.stdin); o = s['orders']['all']
assert s['today'] == '2026-06-30', s['today']
assert o['orders'] == 548 and o['finished'] == 477 and o['straight_through'] == 372, o
assert s['audits_within_90_days']['on_time'] == 62 and s['audits_within_90_days']['due'] == 69
assert s['orders']['audit']['backlog_found'] == 20
print('ok', o['orders'], 'orders,', round(o['stp_rate'] * 100), '% straight-through')" < "$OUT/summary.json"

echo "== README: status and trail verification"
docker compose exec -T api opsagents status
docker compose exec -T api opsagents verify-trail

echo "== README: resolve exception 107 through the API"
curl -fsS "$API/exceptions" > "$OUT/exceptions.json"
$PY "import json, sys; rows = {e['id']: e for e in json.load(sys.stdin)}
e = rows[107]; assert (e['reason'], e['order_id'], e['required_role']) == ('variance_review', 509, 'audit_reviewer'), e
print('exception 107 is the open variance review on order 509')" < "$OUT/exceptions.json"
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API/exceptions/107/resolve" \
  -H "Content-Type: application/json" -d '{"action": "approve"}')
[ "$code" = "401" ] || { echo "expected 401 without a token, got $code"; exit 1; }
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API/exceptions/107/resolve" \
  -H "X-Role-Token: demo-underwriter" -H "Content-Type: application/json" -d '{"action": "approve"}')
[ "$code" = "403" ] || { echo "expected 403 for the wrong role, got $code"; exit 1; }
curl -fsS -X POST "$API/exceptions/107/resolve" \
  -H "X-Role-Token: demo-audit-reviewer" -H "Content-Type: application/json" \
  -d '{"action": "approve", "note": "Checked against the self-audit form", "name": "Smoke Test"}'
echo
for _ in $(seq 1 30); do
  curl -fsS "$API/orders/509" > "$OUT/order.json"
  state=$($PY "import json, sys; print(json.load(sys.stdin)['order']['state'])" < "$OUT/order.json")
  [ "$state" = "closed" ] && break
  sleep 2
done
$PY "import json, sys; d = json.load(sys.stdin)
assert d['order']['state'] == 'closed', d['order']['state']
assert d['trail']['ok']
assert d['endorsement']['posted_by'].startswith('ops-agent, approved by human:audit_reviewer'), d['endorsement']
print('order 509 posted by the worker:', d['endorsement']['posted_by'], d['endorsement']['adjustment'])" < "$OUT/order.json"

echo "== README: advance one business day"
docker compose exec -T api opsagents advance --days 1 > "$OUT/advance.json"
cat "$OUT/advance.json"
$PY "import json, sys; s = json.loads(sys.stdin.read().splitlines()[-1]); assert s['errors'] == [] and s['today'] == '2026-07-01'" \
  < "$OUT/advance.json"

echo "== README: extraction accuracy harness"
docker compose exec -T api opsagents eval-extraction --n 20

echo "== README: tests inside the stack"
docker compose exec -T db psql -U ops -d ops -tc "SELECT 1 FROM pg_database WHERE datname = 'ops_test'" | grep -q 1 \
  || docker compose exec -T db psql -U ops -d ops -c "CREATE DATABASE ops_test"
docker compose run --rm -T -e TEST_DATABASE_URL=postgresql://ops:ops@db:5432/ops_test api pytest -q

echo "smoke ok"
