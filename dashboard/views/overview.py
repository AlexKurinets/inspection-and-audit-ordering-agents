import altair as alt
import pandas as pd
import streamlit as st
from views.common import KIND_COLORS, REASONS, SINGLE, get, header, pct, thousands

s = header("Inspection and audit agents")
o = s["orders"]
reasons = {r["reason"]: r for r in s["exceptions"]["by_reason"]}

c = st.columns(5)
c[0].metric("Orders identified", f"{o['all']['orders']:,}", help="Every order the rules created, both agents")
c[1].metric("Straight-through", pct(o["all"]["stp_rate"]),
            help="Finished orders that went from identification to posting with no person involved")
c[2].metric("Open exceptions", s["exceptions"]["open"], help="Waiting for a person in the exception queue")
w = s["audits_within_90_days"]
c[3].metric("Audits in 90 days", pct(w["rate"]),
            help=f"{w['on_time']} of {w['due']} audits whose 90-day window after the period end has closed")
c[4].metric("Missed audits found", o["audit"]["backlog_found"],
            help="Expired policies the first sweep found with no audit ever ordered")

left, right = st.columns(2)
for col, kind, title in ((left, "inspection", "Inspection agent"), (right, "audit", "Premium audit agent")):
    m = o[kind]
    with col.container(border=True):
        st.subheader(title)
        k = st.columns(3)
        k[0].metric("Orders", m["orders"])
        k[1].metric("Finished", m["finished"])
        k[2].metric("Straight-through", pct(m["stp_rate"]))
        k = st.columns(3)
        if kind == "inspection":
            k[0].metric("Days to report", "n/a" if m["median_turnaround_days"] is None
                        else f"{m['median_turnaround_days']:.0f}", help="Median, order placed to report received")
            k[1].metric("To underwriter", reasons.get("underwriter_review", {}).get("opened", 0),
                        help="Critical recommendations or a Poor rating")
            k[2].metric("Still open", m["open"])
        else:
            p = s["premium"]
            k[0].metric("Posted alone", p["posted_by_agent_alone"],
                        help=f"Of {p['posted']} audits posted, the ones no person touched")
            k[1].metric("Additional", thousands(p["additional"]), help="Additional premium posted")
            k[2].metric("Return", thousands(p["returned"]), help="Return premium posted")
        st.caption(f"{m['straight_through']} of {m['finished']} finished orders closed with no person involved.")

with st.container(border=True):
    st.subheader("Reading reports")
    e = s["extraction"]
    k = st.columns(4)
    k[0].metric("Reports read", s["documents"]["documents"])
    k[1].metric("Scanned pages OCR'd", s["documents"]["ocr_pages"])
    k[2].metric("Passed every check", pct(e["verified_rate"]), help=f"{e['verified']} of {e['extracted']} extractions")
    k[3].metric("Model calls", s["llm"]["calls"], help=f"{s['llm']['failed']} failed; provider {s['llm']['providers']}")

daily = pd.DataFrame(get("/metrics/daily"))
st.subheader("Weekly volume")
if daily.empty:
    st.info("No activity yet.")
else:
    daily["week"] = pd.to_datetime(daily["day"]).dt.to_period("W-SAT").dt.start_time
    weekly = daily.groupby("week", as_index=False)[["ordered", "closed", "exceptions"]].sum()
    weekly = weekly[weekly["week"] + pd.Timedelta(days=6) <= pd.Timestamp(s["today"])]  # complete weeks only
    long = weekly.melt("week", ["ordered", "closed"], var_name="series", value_name="orders")
    long["series"] = long["series"].map({"ordered": "Orders placed", "closed": "Orders finished"})
    chart = (
        alt.Chart(long)
        .mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=60, filled=True))
        .encode(
            x=alt.X("week:T", title="Week starting", axis=alt.Axis(format="%b %d", grid=False)),
            y=alt.Y("orders:Q", title="Orders per week"),
            color=alt.Color("series:N", title=None,
                            scale=alt.Scale(domain=["Orders placed", "Orders finished"],
                                            range=[KIND_COLORS["audit"], KIND_COLORS["inspection"]]),
                            legend=alt.Legend(orient="top")),
            tooltip=[alt.Tooltip("week:T", title="Week of", format="%b %d"), "series:N", "orders:Q"],
        )
        .properties(height=300)
    )
    st.altair_chart(chart, use_container_width=True)
    st.caption("The first week includes the backlog the first sweep found on day one.")

st.subheader("Why orders went to a person")
by_reason = pd.DataFrame(s["exceptions"]["by_reason"])
if by_reason.empty:
    st.info("No exceptions yet.")
else:
    by_reason["label"] = by_reason["reason"].map(REASONS).fillna(by_reason["reason"])
    bars = (
        alt.Chart(by_reason)
        .mark_bar(color=SINGLE, cornerRadiusEnd=4, height=16)
        .encode(
            x=alt.X("opened:Q", title="Exceptions opened"),
            y=alt.Y("label:N", sort="-x", title=None, axis=alt.Axis(labelLimit=320)),
            tooltip=[alt.Tooltip("label:N", title="Reason"), alt.Tooltip("required_role:N", title="Role"),
                     alt.Tooltip("opened:Q", title="Opened"), alt.Tooltip("open:Q", title="Still open")],
        )
    )
    text = bars.mark_text(align="left", dx=4, color="#52514e").encode(text="opened:Q")
    st.altair_chart((bars + text).properties(height=34 * len(by_reason) + 20), use_container_width=True)
