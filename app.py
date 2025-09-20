Perfect — we’ll keep **Run, Clients, Tours** in that exact order, and still auto-reselect the last active tab (e.g., Tours after Parse/Clear/Add). You don’t need to touch your Tours tab code again.

Here’s a **full paste** `app.py` that:

* Keeps the tab order fixed: **Run → Clients → Tours**
* Remembers `__active_tab__` in `st.session_state`
* Uses a tiny JS snippet to re-click the saved tab after any rerun (so Streamlit’s “always pick first tab on rerun” behavior is overridden)

### `app.py` (drop-in)

```python
# app.py

import os, sys, json
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

# ---------- UI ----------
apply_page_base()
st.markdown('<h2 class="app-title">Address Alchemist</h2>', unsafe_allow_html=True)
st.markdown('<p class="app-sub">Paste addresses or <em>any listing links</em> → verified Zillow links</p>', unsafe_allow_html=True)

# Fixed tab order: Run, Clients, Tours
tab_run, tab_clients, tab_tours = st.tabs(["Run", "Clients", "Tours"])

# Default the remembered tab if not set
if "__active_tab__" not in st.session_state:
    st.session_state["__active_tab__"] = "Run"

with tab_run:
    # Mark this as the active tab when user interacts here during this render
    st.session_state["__active_tab__"] = "Run"
    render_run_tab(state=st.session_state)

with tab_clients:
    st.session_state["__active_tab__"] = "Clients"
    render_clients_tab()

with tab_tours:
    st.session_state["__active_tab__"] = "Tours"
    render_tours_tab(state=st.session_state)

# --- Re-select the remembered tab after reruns while keeping visible order fixed ---
# This clicks the tab whose label matches st.session_state["__active_tab__"]
wanted = st.session_state.get("__active_tab__", "Run")
st.components.v1.html(
    f"""
    <script>
    (function() {{
      const wanted = {json.dumps(wanted)};
      let tries = 0;
      function pick() {{
        // Streamlit renders tab headers as buttons[role="tab"] in the parent doc
        const btns = window.parent.document.querySelectorAll('button[role="tab"]');
        if (!btns || !btns.length) {{
          if (tries++ < 60) setTimeout(pick, 50);
          return;
        }}
        for (const b of btns) {{
          const label = (b.innerText || "").trim();
          const selected = b.getAttribute("aria-selected") === "true";
          if (label === wanted && !selected) {{
            b.click();
            break;
          }}
        }}
      }}
      // Run ASAP and retry a bit while Streamlit mounts
      setTimeout(pick, 0);
    }})();
    </script>
    """,
    height=0
)
```

**What to keep in your Tours tab (`ui/tours_tab.py`):**

* Leave your `_stay_on_tours()` calls and `st.session_state["__active_tab__"] = "Tours"` logic exactly as you already have it. That’s how we tell the app which tab to reselect.
* No other changes needed.
