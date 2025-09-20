# app.py  — drop-in replacement

import os, sys
import streamlit as st

# --- Make sure project root and ./ui are importable (robust on Streamlit Cloud) ---
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
UI_DIR = os.path.join(HERE, "ui")
if UI_DIR not in sys.path:
    sys.path.insert(0, UI_DIR)

# --- Imports after path shim ---
from core.styles import apply_page_base
from ui.run_tab import render_run_tab
from ui.clients_tab import render_clients_tab

# Try package import first, then fall back to direct file import if needed
try:
    from ui.tours_tab import render_tours_tab
except Exception:
    # Last resort (in case the environment treats ./ui as a flat module dir)
    import importlib.util
    tours_path = os.path.join(UI_DIR, "tours_tab.py")
    spec = importlib.util.spec_from_file_location("tours_tab", tours_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    render_tours_tab = getattr(mod, "render_tours_tab")

# --- Page base / header ---
apply_page_base()
st.markdown('<h2 class="app-title">Address Alchemist</h2>', unsafe_allow_html=True)
st.markdown(
    '<p class="app-sub">Paste addresses or <em>any listing links</em> → verified Zillow links</p>',
    unsafe_allow_html=True
)

# --- Tabs ---
tab_run, tab_clients, tab_tours = st.tabs(["Run", "Clients", "Tours"])

with tab_run:
    render_run_tab(state=st.session_state)

with tab_clients:
    render_clients_tab()

with tab_tours:
    render_tours_tab(state=st.session_state)
