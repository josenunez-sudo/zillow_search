# app.py

import os, sys
import streamlit as st

# --- Path shim (robust for Streamlit Cloud / different CWDs) ---
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
UI_DIR = os.path.join(HERE, "ui")
if UI_DIR not in sys.path:
    sys.path.insert(0, UI_DIR)

from core.styles import apply_page_base
from ui.run_tab import render_run_tab
from ui.clients_tab import render_clients_tab

# Import tours tab (package import first; fallback to file import)
try:
    from ui.tours_tab import render_tours_tab
except Exception:
    import importlib.util
    spec = importlib.util.spec_from_file_location("tours_tab", os.path.join(UI_DIR, "tours_tab.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    render_tours_tab = getattr(mod, "render_tours_tab")

# ---------- tabs helper: keep last active tab selected after rerun ----------
ALL_TABS = ["Run", "Clients", "Tours"]

def make_tabs():
    active = st.session_state.get("__active_tab__", "Run")
    order = [active] + [t for t in ALL_TABS if t != active] if active in ALL_TABS else ALL_TABS
    t_containers = st.tabs(order)
    mapping = dict(zip(order, t_containers))
    return mapping["Run"], mapping["Clients"], mapping["Tours"]

# ---------- UI ----------
apply_page_base()
st.markdown('<h2 class="app-title">Address Alchemist</h2>', unsafe_allow_html=True)
st.markdown('<p class="app-sub">Paste addresses or <em>any listing links</em> → verified Zillow links</p>', unsafe_allow_html=True)

tab_run, tab_clients, tab_tours = make_tabs()

with tab_run:
    st.session_state["__active_tab__"] = "Run"
    render_run_tab(state=st.session_state)

with tab_clients:
    st.session_state["__active_tab__"] = "Clients"
    render_clients_tab()

with tab_tours:
    st.session_state["__active_tab__"] = "Tours"
    render_tours_tab(state=st.session_state)
