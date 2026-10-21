import json

import pandas as pd
import streamlit as st
from views.common import ORDER_TYPES, REASONS, get, header, md, reconciliation_table, signed

header("Order trail")

default = int(st.query_params.get("order", "1")) if st.query_params.get("order", "1").isdigit() else 1
order_id = st.number_input("Order id", min_value=1, value=default, step=1)
st.query_params["order"] = str(order_id)
try:
    d = get(f"/orders/{order_id}")
except Exception:
    st.warning(f"No order {order_id}.")
    st.stop()

o, p, t = d["order"], d["policy"], d["trail"]
st.subheader(f"{o['kind'].title()} order {o['id']} · {p['insured_name']}")
st.markdown(md(
    f"Policy **{o['policy_number']}** ({p['line']}, deposit premium ${float(p['deposit_premium']):,.2f}) · "
    f"{ORDER_TYPES.get(o['order_type'], o['order_type'])}"
    + (f", ref {o['vendor_ref']}" if o["vendor_ref"] else "")
    + f" · state **{o['state']}** · due {o['due_on']}"))
if t["ok"]:
    st.success(f"Hash chain verified: {t['events']} events, head {t['head'][:16]}…", icon=":material/verified:")
else:
    st.error(f"Hash chain broken at event {t['broken_at_seq']}", icon=":material/gpp_bad:")

with st.expander(f"Why this order exists · rules {o['rule_version']}", expanded=True):
    for hit in o["rule_hits"]:
        st.markdown(md(f"- {hit}"))


HIDDEN = {"rule_hits", "subject", "sha256", "seq", "occurred_on", "idempotency_key", "message_id", "exception_id"}


def details(e: dict) -> str:
    parts = []
    for k, v in e["payload"].items():
        if k in HIDDEN or v in (None, "", [], {}):
            continue
        parts.append(f"{k.replace('_', ' ')}: {v if not isinstance(v, (dict, list)) else json.dumps(v, default=str)}")
    text = "; ".join(parts)
    return md(text if len(text) <= 160 else text[:157] + "...")


events = pd.DataFrame([{
    "seq": e["seq"], "date": e["occurred_on"], "actor": e["actor"], "event": e["event_type"],
    "state": f"{e['from_state'] or ''} → {e['to_state'] or ''}" if e["from_state"] != e["to_state"] else e["to_state"],
    "details": details(e), "hash": e["hash"][:8],
} for e in d["events"]])
st.subheader("Events")
st.table(events.set_index("seq"))

if d["exceptions"]:
    st.subheader("Exceptions")
    for x in d["exceptions"]:
        status = (f"{x['resolution']}d by {x['resolved_by']} on {x['resolved_on']}" if x["status"] == "resolved"
                  else f"open since {x['opened_on']}, needs {x['required_role']}")
        st.markdown(md(f"**#{x['id']} {REASONS.get(x['reason'], x['reason'])}**, {status}. {x['detail']}"
                       + (f" *Note: {x['note']}*" if x["note"] else "")))

ex = o["extraction"]
if ex:
    st.subheader("Extraction checks")
    passed = sum(c["ok"] for c in ex["checks"])
    st.caption(f"{passed} of {len(ex['checks'])} checks passed. Every value needs a quote found in the document.")
    st.dataframe(pd.DataFrame(ex["checks"]), hide_index=True, use_container_width=True)

rec = o["reconciliation"]
if rec:
    st.subheader("Premium reconciliation")
    st.dataframe(reconciliation_table(rec), hide_index=True, use_container_width=True)
    a, b, c, e = st.columns(4)
    a.metric("Deposit", f"${float(rec['deposit_premium']):,.2f}")
    b.metric("Audited", f"${float(rec['audited_premium']):,.2f}")
    c.metric("Adjustment", signed(float(rec["adjustment"])))
    e.metric("Within agent authority", "yes" if rec["auto_post"] else "no")

if d["endorsement"]:
    en = d["endorsement"]
    st.info(md(f"Posted to the policy system on {en['posted_on']} by {en['posted_by']}, on an {en['basis']} "
               f"basis, adjustment {signed(float(en['adjustment']))}."), icon=":material/task_alt:")

st.subheader("Policy system activity")
st.dataframe(pd.DataFrame(d["pas_activity"]), hide_index=True, use_container_width=True)

for doc in d["documents"]:
    with st.expander(f"Document {doc['id']}: {doc['source']} · {doc['pages']} page(s), {doc['ocr_pages']} OCR"):
        st.code(doc["text"], language=None)
