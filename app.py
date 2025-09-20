# app.py

import os
import sys
import json
import streamlit as st

# Path shim (robust for different working dirs/Cloud)
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

# ---------- UI ----------
apply_page_base()
st.markdown('<h2 class="app-title">Address Alchemist</h2>', unsafe_allow_html=True)
st.markdown('<p class="app-sub">Paste addresses or <em>any listing links</em> -> verified Zillow links</p>', unsafe_allow_html=True)

# Fixed tab order (Run, Clients, Tours)
tab_run, tab_clients, tab_tours = st.tabs(["Run", "Clients", "Tours"])

# Ensure we have a default remembered tab
if "__active_tab__" not in st.session_state:
    st.session_state["__active_tab__"] = "Run"

with tab_run:
    # Mark active while rendering this tab (so actions from here remember it)
    st.session_state["__active_tab__"] = "Run"
    render_run_tab(state=st.session_state)

with tab_clients:
    # Mark active while rendering this tab (restores Clients' report button behavior)
    st.session_state["__active_tab__"] = "Clients"
    render_clients_tab()

with tab_tours:
    # Tours tab also sets this just before any rerun inside its own actions
    st.session_state["__active_tab__"] = "Tours"
    render_tours_tab(state=st.session_state)

# Re-select the remembered tab after reruns while keeping visible order fixed
wanted = st.session_state.get("__active_tab__", "Run")
st.components.v1.html(
    """
    <script>
    (function() {
      const wanted = %s;
      let tries = 0;
      function pick() {
        const doc = window.parent.document;
        const btns = doc.querySelectorAll('button[role="tab"]');
        if (!btns || !btns.length) {
          if (tries++ < 60) setTimeout(pick, 50);
          return;
        }
        for (const b of btns) {
          const label = (b.innerText || "").trim();
          const selected = b.getAttribute("aria-selected") === "true";
          if (label === wanted && !selected) {
            b.click();
            break;
          }
        }
      }
      setTimeout(pick, 0);
    })();
    </script>
    """ % json.dumps(wanted),
    height=0
)
