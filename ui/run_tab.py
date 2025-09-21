# ui/run_tab.py
from typing import Optional, Dict, Any
import streamlit as st

__all__ = ["render_run_tab"]

def _inject_css_once():
    """Inject shared badge styles so this tab matches Clients/Tours."""
    ver = "run-tab-css-2025-09-21a"
    if st.session_state.get("__run_tab_css__") == ver:
        return
    st.session_state["__run_tab_css__"] = ver
    st.markdown(
        """
<style>
/* Light blue date badge */
.date-badge {
  display:inline-block; font-size:11px; font-weight:800;
  padding:2px 6px; border-radius:999px; margin-left:8px;
  background:#e0f2fe; color:#075985; border:1px solid #7dd3fc;
}
html[data-theme="dark"] .date-badge {
  background:#0b1220; color:#7dd3fc; border-color:#164e63;
}

/* Red toured badge */
.toured-badge {
  display:inline-block; font-size:11px; font-weight:800;
  padding:2px 6px; border-radius:999px; margin-left:8px;
  background:#fee2e2; color:#991b1b; border:1px solid #fecaca;
}
html[data-theme="dark"] .toured-badge {
  background:#7f1d1d; color:#fecaca; border-color:#ef4444;
}

/* Meta chip (parity with Clients tab) */
.meta-chip {
  display:inline-block; font-size:11px; font-weight:800;
  padding:2px 6px; border-radius:999px; margin-left:8px;
  background:#eef2ff; color:#1e3a8a; border:1px solid #c7d2fe;
}
html[data-theme="dark"] .meta-chip {
  background:#1f2937; color:#bfdbfe; border-color:#374151;
}
</style>
        """,
        unsafe_allow_html=True,
    )

def render_run_tab(state: Optional[Dict[str, Any]] = None):
    """
    Minimal Run tab so `from ui.run_tab import render_run_tab` works.
    Replace with your real Run workflow when ready.
    """
    _inject_css_once()
    st.subheader("Run")
    st.caption("This is a lightweight placeholder so the app can load.")

    with st.expander("Badge preview (style parity)"):
        st.markdown(
            "Example: "
            "<span class='date-badge'>20250921</span> "
            "<span class='toured-badge'>Toured</span> "
            "<span class='meta-chip'>Sample Chip</span>",
            unsafe_allow_html=True,
        )

    st.info(
        "Keep your Clients tab code in `ui/clients_tab.py`. "
        "This file just provides a placeholder for the Run tab."
    )
