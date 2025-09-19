from core.styles import apply_page_base
import streamlit as st
from ui.run_tab import render_run_tab
from ui.clients_tab import render_clients_tab

apply_page_base()

st.markdown('<h2 class="app-title">Address Alchemist</h2>', unsafe_allow_html=True)
st.markdown('<p class="app-sub">Paste addresses or <em>any listing links</em> → verified Zillow links</p>', unsafe_allow_html=True)

tab_run, tab_clients = st.tabs(["Run", "Clients"])

with tab_run:
    render_run_tab(state=st.session_state)

with tab_clients:
    render_clients_tab()
