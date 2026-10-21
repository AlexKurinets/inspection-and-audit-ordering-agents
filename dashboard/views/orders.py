import altair as alt
import pandas as pd
import streamlit as st
from views.common import KIND_COLORS, get, header

header("Orders and vendors")

STATES = ["identified", "ordered", "acknowledged", "scheduled", "waiting_on_insured", "report_received",
          "in_review", "approved", "closed", "closed_manual"]

orders = pd.DataFrame(get("/orders", limit=2000)["orders"])
if orders.empty:
    st.info("No orders yet.")
    st.stop()

st.subheader("Where every order is now")
counts = orders.groupby(["state", "kind"], as_index=False).size().rename(columns={"size": "orders"})
chart = (
    alt.Chart(counts)
    .mark_bar(cornerRadiusEnd=4, height=12)
    .encode(
        y=alt.Y("state:N", sort=STATES, title=None, axis=alt.Axis(labelLimit=220)),
        yOffset=alt.YOffset("kind:N"),
        x=alt.X("orders:Q", title="Orders"),
        color=alt.Color("kind:N", title=None, scale=alt.Scale(domain=list(KIND_COLORS), range=list(KIND_COLORS.values())),
                        legend=alt.Legend(orient="top")),
        tooltip=["state:N", "kind:N", "orders:Q"],
    )
    .properties(height=420)
)
st.altair_chart(chart, use_container_width=True)

st.subheader("Vendors")
vendors = pd.DataFrame(get("/metrics/vendors"))
names = {"LFS": "Larkspur Field Surveys (API)", "QRS": "Quillmate Risk Services (email)",
         "HAP": "Harrowgate Audit Partners (API)", "SELF": "Insured self-audit (email)"}
vendors["vendor"] = vendors["vendor"].map(names).fillna(vendors["vendor"])
st.dataframe(vendors.rename(columns={"median_days": "median days to report", "chased": "chased by agent"}),
             hide_index=True, use_container_width=True)

st.subheader("All orders")
f1, f2 = st.columns(2)
kind = f1.selectbox("Agent", ["both", "inspection", "audit"])
state = f2.selectbox("State", ["any"] + STATES)
view = orders
if kind != "both":
    view = view[view["kind"] == kind]
if state != "any":
    view = view[view["state"] == state]
st.caption(f"{len(view)} orders. Open one on the Order trail page by its id.")
st.dataframe(view.drop(columns=["order_key"]), hide_index=True, use_container_width=True)
