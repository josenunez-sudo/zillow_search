# ui/clients_tab.py
import re
from html import escape
import streamlit as st
import streamlit.components.v1 as components
from typing import List, Dict, Any, Optional
from datetime import datetime

# Reuse the SUPABASE handle exactly like in run_tab.py
from supabase import create_client, Client
import os

@st.cache_resource
def get_supabase() -> Optional[Client]:
    try:
        url = os.getenv("SUPABASE_URL", st.secrets.get("SUPABASE_URL", ""))
        key = os.getenv("SUPABASE_SERVICE_ROLE", st.secrets.get("SUPABASE_SERVICE_ROLE", ""))
        if not url or not key:
            return None
        return create_client(url, key)
    except Exception:
        return None

SUPABASE = get_supabase()

def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

# --- Query param helpers (work in both classic & new) ---
def _qp_get(name, default=None):
    try:
        qp = st.query_params
        val = qp.get(name, default)
        if isinstance(val, list) and val:
            return val[0]
        return val
    except Exception:
        qp = st.experimental_get_query_params()
        return (qp.get(name, [default]) or [default])[0]

def _qp_set(**kwargs):
    try:
        if kwargs:
            st.query_params.update(kwargs)
        else:
            st.query_params.clear()
    except Exception:
        if kwargs:
            st.experimental_set_query_params(**kwargs)
        else:
            st.experimental_set_query_params()

# ----- Clients fetch (exclude "test test") -----
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = True):
    if not SUPABASE:
        return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        return [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
    except Exception:
        return []

def toggle_client_active(client_id: int, new_active: bool):
    if not SUPABASE or not client_id: return False, "Not configured"
    try:
        SUPABASE.table("clients").update({"active": new_active}).eq("id", client_id).execute()
        fetch_clients.clear()  # type: ignore
        return True, "ok"
    except Exception as e:
        return False, str(e)

def rename_client(client_id: int, new_name: str):
    if not SUPABASE or not client_id or not (new_name or "").strip():
        return False, "Bad input"
    try:
        new_norm = _norm_tag(new_name)
        existing = SUPABASE.table("clients").select("id").eq("name_norm", new_norm).limit(1).execute().data or []
        if existing and existing[0]["id"] != client_id:
            return False, "Name already exists."
        SUPABASE.table("clients").update({"name": new_name.strip(), "name_norm": new_norm}).eq("id", client_id).execute()
        fetch_clients.clear()  # type: ignore
        return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_client(client_id: int):
    if not SUPABASE or not client_id: return False, "Not configured"
    try:
        SUPABASE.table("clients").delete().eq("id", client_id).execute()
        fetch_clients.clear()  # type: ignore
        return True, "ok"
    except Exception as e:
        return False, str(e)

def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

# --- Inline row with “Report ▦” + “Tours 📅” buttons ---
def _client_row_icons(name: str, norm: str, cid: int, active: bool):
    col_name, col_rep, col_tours, col_tog, col_ren, col_del = st.columns([7,1,1,1,1,1])

    with col_name:
        st.markdown(
            f"<span class='client-name' style='font-weight:700;'>{escape(name)}</span> "
            f"<span class='pill {'active' if active else ''}'>{'active' if active else 'inactive'}</span>",
            unsafe_allow_html=True
        )

    # Existing "sent listings" report button
    with col_rep:
        if st.button("▦", key=f"rep_{cid}", help="Open Sent Listings report"):
            _qp_set(report=norm, scroll="1")
            components.html(
                """
                <script>
                  const tabs = parent.document.querySelectorAll('button[role="tab"]');
                  for (const b of tabs) { if (b.innerText.trim() === 'Clients') { b.click(); break; } }
                  setTimeout(() => {
                    const anchor = parent.document.getElementById("report_anchor");
                    if (anchor) anchor.scrollIntoView({behavior:'smooth', block:'start'});
                  }, 250);
                </script>
                """,
                height=0
            )
            _safe_rerun()

    # NEW: “📅 Tours” button → jumps to Tours tab & opens that client’s Tours report
    with col_tours:
        if st.button("📅", key=f"tour_{cid}", help="Open Tours report"):
            _qp_set(tours=norm)  # read by Tours tab
            components.html(
                """
                <script>
                  const tabs = parent.document.querySelectorAll('button[role="tab"]');
                  for (const b of tabs) { if (b.innerText.trim() === 'Tours') { b.click(); break; } }
                  setTimeout(() => {
                    const anchor = parent.document.getElementById("tours_report_anchor");
                    if (anchor) anchor.scrollIntoView({behavior:'smooth', block:'start'});
                  }, 250);
                </script>
                """,
                height=0
            )

    with col_tog:
        if st.button("⟳", key=f"tg_{cid}", help=("Deactivate" if active else "Activate")):
            toggle_client_active(cid, not active)
            _safe_rerun()

    with col_ren:
        if st.button("✎", key=f"rn_{cid}", help="Rename"):
            st.session_state[f"__edit_{cid}"] = True

    with col_del:
        if st.button("⌫", key=f"del_{cid}", help="Delete"):
            st.session_state[f"__del_{cid}"] = True

    # Inline editors (rename/delete)
    if st.session_state.get(f"__edit_{cid}"):
        st.markdown("<div class='inline-panel'>", unsafe_allow_html=True)
        new_name = st.text_input("New name", value=name, key=f"rn_val_{cid}")
        b1, b2 = st.columns([0.25, 0.25])
        if b1.button("Save", key=f"rn_save_{cid}"):
            ok, msg = rename_client(cid, new_name)
            if not ok: st.warning(msg)
            st.session_state[f"__edit_{cid}"] = False
            _safe_rerun()
        if b2.button("Cancel", key=f"rn_cancel_{cid}"):
            st.session_state[f"__edit_{cid}"] = False
        st.markdown("</div>", unsafe_allow_html=True)

    if st.session_state.get(f"__del_{cid}"):
        st.markdown("<div class='inline-panel'>", unsafe_allow_html=True)
        b1, b2 = st.columns([0.25, 0.25])
        if b1.button("Confirm delete", key=f"del_yes_{cid}"):
            delete_client(cid); st.session_state[f"__del_{cid}"] = False; _safe_rerun()
        if b2.button("Cancel", key=f"del_no_{cid}"):
            st.session_state[f"__del_{cid}"] = False
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:4px 0 2px 0;'></div>", unsafe_allow_html=True)

def render_clients_tab():
    st.subheader("Clients")
    st.caption("")

    all_clients = fetch_clients(include_inactive=True)
    active = [c for c in all_clients if c.get("active")]
    inactive = [c for c in all_clients if not c.get("active")]

    colA, colB = st.columns(2)

    with colA:
        st.markdown("### Active")
        if not active:
            st.write("_No active clients_")
        else:
            for c in active:
                _client_row_icons(c["name"], c.get("name_norm",""), c["id"], active=True)

    with colB:
        st.markdown("### Inactive")
        if not inactive:
            st.write("_No inactive clients_")
        else:
            for c in inactive:
                _client_row_icons(c["name"], c.get("name_norm",""), c["id"], active=False)

    # Existing Sent report (if opened via query param)
    st.markdown('<div id="report_anchor"></div>', unsafe_allow_html=True)
    report_norm = _qp_get("report", "")
    if report_norm:
        st.markdown("---")
        st.markdown(f"#### Sent report for {escape(report_norm)}", unsafe_allow_html=True)
        st.info("Sent report rendering here (unchanged).")
