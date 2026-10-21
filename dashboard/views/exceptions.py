import pandas as pd
import streamlit as st
from views.common import ORDER_TYPES, REASONS, get, header, md, post, reconciliation_table, signed

header("Exception queue")
st.write("Orders the agents stopped on. Each one needs the role shown, and the agent continues only after a "
         "person approves; a rejection closes the order for manual handling.")

if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

rows = get("/exceptions", status="open")
roles = ["all roles"] + sorted({r["required_role"] for r in rows})
role = st.segmented_control("Role", roles, default="all roles")
if role and role != "all roles":
    rows = [r for r in rows if r["required_role"] == role]

if not rows:
    st.info("Nothing waiting.")
    st.stop()

table = pd.DataFrame([{
    "id": r["id"], "opened": r["opened_on"], "needs": r["required_role"],
    "reason": REASONS.get(r["reason"], r["reason"]), "policy": r["policy_number"], "order": r["order_id"],
    "type": ORDER_TYPES.get(r["order_type"], r["order_type"]),
} for r in rows])
st.dataframe(table, hide_index=True, use_container_width=True)

labels = {r["id"]: f"#{r['id']} · {REASONS.get(r['reason'], r['reason'])} · {r['policy_number']}" for r in rows}
ids = list(labels)
wanted = st.query_params.get("id", "")
pick = st.selectbox("Review", ids, format_func=labels.get,
                    index=ids.index(int(wanted)) if wanted.isdigit() and int(wanted) in labels else 0)
exc = next(r for r in rows if r["id"] == pick)

with st.container(border=True):
    st.subheader(REASONS.get(exc["reason"], exc["reason"]))
    st.markdown(md(f"**Order {exc['order_id']}** · {ORDER_TYPES.get(exc['order_type'], exc['order_type'])} · "
                   f"policy {exc['policy_number']} · needs **{exc['required_role']}**"))
    st.markdown(md(exc["detail"]))
    ev = exc["evidence"]
    rec = ev.get("reconciliation")
    if rec:
        st.dataframe(reconciliation_table(rec), hide_index=True, use_container_width=True)
        a, b, c = st.columns(3)
        a.metric("Deposit premium", f"${float(rec['deposit_premium']):,.2f}")
        b.metric("Audited premium", f"${float(rec['audited_premium']):,.2f}")
        c.metric("Adjustment", signed(float(rec["adjustment"])))
    for r in ev.get("recommendations", []):
        st.markdown(md(f"> {r['quote']}"))
    if ev.get("failures"):
        st.dataframe(pd.DataFrame(ev["failures"]), hide_index=True, use_container_width=True)
    if ev.get("body"):
        st.markdown(md(f"> {ev['body']}"))
    st.page_link("views/trail.py", label=f"Open the full trail for order {exc['order_id']}",
                 query_params={"order": exc["order_id"]}, icon=":material/timeline:")

with st.form("resolve"):
    token = st.text_input("Role token", type="password", help="The token for the role this exception needs")
    note = st.text_input("Note for the trail")
    approve, reject = st.columns(2)
    do_approve = approve.form_submit_button("Approve", type="primary", use_container_width=True)
    do_reject = reject.form_submit_button("Reject and handle manually", use_container_width=True)

if do_approve or do_reject:
    action = "approve" if do_approve else "reject"
    r = post(f"/exceptions/{exc['id']}/resolve", {"action": action, "note": note}, token)
    if r.status_code == 200:
        body = r.json()
        st.session_state["flash"] = (f"Exception #{exc['id']} {action}d by {body['resolved_by']}: order "
                                     f"{body['order_id']} is now {body['state']}. The worker picks it up on its "
                                     "next pass, within a few seconds.")
        st.rerun()
    else:
        st.error(f"{r.status_code}: {r.json().get('detail', r.text)}")
