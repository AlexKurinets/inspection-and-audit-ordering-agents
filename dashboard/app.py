import streamlit as st

st.set_page_config(page_title="Inspection and audit agents", page_icon=":material/fact_check:", layout="wide")

pages = [
    st.Page("views/overview.py", title="Overview", icon=":material/monitoring:", default=True),
    st.Page("views/exceptions.py", title="Exception queue", icon=":material/rule:", url_path="exceptions"),
    st.Page("views/orders.py", title="Orders and vendors", icon=":material/local_shipping:", url_path="orders"),
    st.Page("views/trail.py", title="Order trail", icon=":material/timeline:", url_path="trail"),
]
st.navigation(pages).run()
