# app.py

import os
import sys
import json
import traceback
import importlib
import importlib.util  # keep this at module level to avoid scoping issues
from typing import Optional, Tuple, Any, Callable

import streamlit as st

# Path shim (robust for different working dirs/Cloud)
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
UI_DIR = os.path.join(HERE, "ui")
if UI_DIR not in sys.path:
    sys.path.insert(0, UI_DIR)

# --- Utilities to surface real errors in the UI ---
def _show_exc(heading: str, ex: BaseException):
    st.error(f"{heading}: {type(ex).__name__}")
    st.code("".join(traceback.format_exception(type(ex), ex, ex.__traceback__)), language="python")

def _safe_import_attr(module_name: str, attr: str, fallback_path: Optional[str] = None) -> Tuple[Optional[Callable[..., Any]], Optional[BaseException]]:
    """
    Try `importlib.import_module(module_name)` and get `attr`.
    If that fails and fallback_path is given, try loading that file directly.
    Returns (callable_or_None, error_or_None).
    """
    try:
        mod = importlib.import_module(module_name)
        try:
            fn = getattr(mod, attr)
            return fn, None
        except Exception as e_attr:
            return None, e_attr
    except Exception as e_mod:
        if fallback_path:
            try:
                spec = importlib.util.spec_from_file_location(module_name.rsplit(".", 1)[-1], fallback_path)
                if spec is None or spec.loader is None:
                    return None, e_mod
                mod2 = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod2)  # type: ignore
                try:
                    fn = getattr(mod2, attr)
                    return fn, None
                except Exception as e_attr2:
                    return None, e_attr2
            except Exception:
                # Return the original import error so traceback points to the root cause
                return None, e_mod
        else:
            return None, e_mod

# ----- Import base styles early (assumed stable) -----
try:
    from core.styles import apply_page_base
except Exception as e:
    st.set_page_config(page_title="Address Alchemist", page_icon="🏠", layout="wide")
    _show_exc("import core.styles.apply_page_base", e)
    def apply_page_base():  # no-op fallback
        pass

# ---------- UI ----------
apply_page_base()
st.markdown('<h2 class="app-title">Address Alchemist</h2>', unsafe_allow_html=True)
st.markdown('<p class="app-sub">Paste addresses or <em>any listing links</em> -> verified Zillow links</p>', unsafe_allow_html=True)

# Fixed tab order (Run, Clients, Tours)
tab_run, tab_clients, tab_tours = st.tabs(["Run", "Clients", "Tours"])

# Ensure we have a default remembered tab
if "__active_tab__" not in st.session_state:
    st.session_state["__active_tab__"] = "Run"

# ---- Lazy, safe import per tab (so errors render in the UI instead of being redacted) ----
with tab_run:
    fn, err = _safe_import_attr("ui.run_tab", "render_run_tab")
    if err:
        _show_exc("import ui.run_tab.render_run_tab", err)
    else:
        try:
            fn(state=st.session_state)
        except Exception as e:
            _show_exc("render_run_tab()", e)

with tab_clients:
    # IMPORTANT: do NOT import ui.clients_tab at the top of the file
    fn, err = _safe_import_attr("ui.clients_tab", "render_clients_tab")
    if err:
        _show_exc("import ui.clients_tab.render_clients_tab", err)
    else:
        try:
            fn()
        except Exception as e:
            _show_exc("render_clients_tab()", e)

with tab_tours:
    # Keep historical file-fallback behavior for tours
    fallback = os.path.join(UI_DIR, "tours_tab.py")
    fn, err = _safe_import_attr("ui.tours_tab", "render_tours_tab", fallback_path=fallback)
    if err:
        _show_exc("import ui.tours_tab.render_tours_tab", err)
    else:
        try:
            fn(state=st.session_state)
        except Exception as e:
            _show_exc("render_tours_tab()", e)

# Re-select the remembered tab after reruns while keeping visible order fixed.
# Hide the tab bar until the correct tab is selected to prevent visible flicker.
wanted = st.session_state.get("__active_tab__", "Run")
st.components.v1.html(
    """
    <style>
      /* hide tab bar until selection applied */
      :root #tabs-ready-flag { display:none }
      ._aa_tabbar_mask { visibility:hidden }
    </style>
    <script>
    (function() {
      const wanted = %s;
      let tries = 0;
      function pick() {
        const doc = window.parent.document;
        // First time: wrap the tablist in a mask so it's hidden until we click the right tab.
        const tablists = doc.querySelectorAll('[role="tablist"]');
        if (tablists && tablists.length) {
          for (const tl of tablists) { tl.classList.add('_aa_tabbar_mask'); }
        }
        const btns = doc.querySelectorAll('button[role="tab"]');
        if (!btns || !btns.length) {
          if (tries++ < 80) setTimeout(pick, 25);
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
        // Unhide the tab bar after selection applied (small delay to allow click to take effect)
        setTimeout(function(){
          const tablists2 = doc.querySelectorAll('[role="tablist"]');
          for (const tl of tablists2) { tl.classList.remove('_aa_tabbar_mask'); }
        }, 0);
      }
      // run asap
      setTimeout(pick, 0);
    })();
    </script>
    """ % json.dumps(wanted),
    height=0
)
