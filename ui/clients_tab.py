# ui/clients_tab.py
# Clients tab: list active/inactive clients + inline "Open sent listings report" below.
# Keeps tab focus on Clients by setting __active_tab__ right before any rerun.
# Adds cross-check: rows in the sent report show a "Toured" badge if their address
# matches any Tour stop for that client (normalized slug match).

import os
import io
import re
from datetime import datetime
from typing import List, Dict, Any, Optional
from html import escape
from urllib.parse import unquote

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from supabase import create_client, Client

# ---------- Rerun helper ----------
def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

# ---------- Supabase ----------
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

# ---------- Helpers ----------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _sb_ok() -> bool:
    try:
        return bool(SUPABASE)
    except NameError:
        return False

def _slug_addr(addr: str) -> str:
    a = (addr or "").strip().lower()
    a = re.sub(r"[^\w\s,-]", "", a).replace(",", "")
    return re.sub(r"\s+", "-", a).strip("-")

def _address_text_from_url(url: str) -> str:
    if not url:
        return ""
    u = unquote(url)
    m = re.search(r"/homedetails/([^/]+)/\d{6,}_zpid/", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    m = re.search(r"/homes/([^/_]+)_rb/?", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    return ""

# ---- Query-params helpers ----
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

# ---------- Clients registry helpers (cached) ----------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False):
    if not _sb_ok():
        return []
    try:
        rows = (
            SUPABASE.table("clients")
            .select("id,name,name_norm,active")
            .order("name", desc=False)
            .execute()
            .data
            or []
        )
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

def invalidate_clients_cache():
    try:
        fetch_clients.clear()  # type: ignore[attr-defined]
    except Exception:
        pass

def toggle_client_active(client_id: int, new_active: bool):
    if not _sb_ok() or not client_id:
        return False, "Not configured"
    try:
        SUPABASE.table("clients").update({"active": new_active}).eq("id", client_id).execute()
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def rename_client(client_id: int, new_name: str):
    if not _sb_ok() or not client_id or not (new_name or "").strip():
        return False, "Bad input"
    try:
        new_norm = _norm_tag(new_name)
        existing = (
            SUPABASE.table("clients")
            .select("id")
            .eq("name_norm", new_norm)
            .limit(1)
            .execute()
            .data
            or []
        )
        if existing and existing[0]["id"] != client_id:
            return False, "A client with that (normalized) name already exists."
        SUPABASE.table("clients").update({"name": new_name.strip(), "name_norm": new_norm}).eq("id", client_id).execute()
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_client(client_id: int):
    if not _sb_ok() or not client_id:
        return False, "Not configured"
    try:
        SUPABASE.table("clients").delete().eq("id", client_id).execute()
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ---------- Sent reports ----------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_sent_for_client(client_norm: str, limit: int = 5000):
    """
    Fetch sent rows for a given normalized client name from Supabase.
    Returns list of dicts: [{url, address, sent_at, campaign, mls_id, canonical, zpid}, ...]
    """
    if not (_sb_ok() and client_norm.strip()):
        return []
    try:
        cols = "url,address,sent_at,campaign,mls_id,canonical,zpid"
        resp = (
            SUPABASE.table("sent")
            .select(cols)
            .eq("client", client_norm.strip())
            .order("sent_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []
    except Exception:
        return []

# ---------- Tours cross-check ----------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_tour_slugs_for_client(client_norm: str) -> set:
    """
    Return a set of address slugs for ALL tour stops for this client.
    """
    if not (_sb_ok() and client_norm.strip()):
        return set()
    try:
        tq = (
            SUPABASE.table("tours")
            .select("id")
            .eq("client", client_norm.strip())
            .limit(5000)
            .execute()
        )
        tours = [t["id"] for t in (tq.data or [])]
        if not tours:
            return set()
        sq = (
            SUPABASE.table("tour_stops")
            .select("address,address_slug")
            .in_("tour_id", tours)
            .limit(50000)
            .execute()
        )
        stops = sq.data or []
        slugs = set()
        for s in stops:
            slug = s.get("address_slug") or _slug_addr(s.get("address") or "")
            if slug:
                slugs.add(slug)
        return slugs
    except Exception:
        return set()

def _slug_from_sent_row(row: Dict[str, Any]) -> str:
    """Try to compute an address slug for a 'sent' record row."""
    addr = (row.get("address") or "").strip()
    if not addr:
        # derive from the Zillow URL if possible
        url = (row.get("url") or "").strip()
        guess = _address_text_from_url(url)
        addr = guess or url
    return _slug_addr(addr)

# ---------- UI pieces ----------
def _client_row_icons(name: str, norm: str, cid: int, active: bool):
    """
    One-line row for a client with inline icon buttons:
    ▦ (report)  ✎ (rename)  ⟳ (toggle)  ⌫ (delete)
    """
    col_name, col_rep, col_ren, col_tog, col_del, col_sp = st.columns([8, 1, 1, 1, 1, 2])

    with col_name:
        st.markdown(
            f"<span class='client-name'>{escape(name)}</span> "
            f"<span class='pill {'active' if active else ''}'>{'active' if active else 'inactive'}</span>",
            unsafe_allow_html=True,
        )

    with col_rep:
        if st.button("▦", key=f"rep_{cid}", help="Open report"):
            # Keep focus on Clients across rerun
            st.session_state["__active_tab__"] = "Clients"
            _qp_set(report=norm, scroll="1")
            _safe_rerun()

    with col_ren:
        if st.button("✎", key=f"rn_btn_{cid}", help="Rename"):
            st.session_state[f"__edit_{cid}"] = True

    with col_tog:
        if st.button("⟳", key=f"tg_{cid}", help=("Deactivate" if active else "Activate")):
            rows = (
                SUPABASE.table("clients")
                .select("active")
                .eq("id", cid)
                .limit(1)
                .execute()
                .data
                or []
            )
            cur = rows[0]["active"] if rows else active
            toggle_client_active(cid, (not cur))
            st.session_state["__active_tab__"] = "Clients"  # keep focus
            _safe_rerun()

    with col_del:
        if st.button("⌫", key=f"del_{cid}", help="Delete"):
            st.session_state[f"__del_{cid}"] = True

    # Inline rename editor
    if st.session_state.get(f"__edit_{cid}"):
        st.markdown("<div class='inline-panel'>", unsafe_allow_html=True)
        new_name = st.text_input("New name", value=name, key=f"rn_val_{cid}")
        cc1, cc2 = st.columns([0.2, 0.2])
        if cc1.button("Save", key=f"rn_save_{cid}"):
            ok, msg = rename_client(cid, new_name)
            if not ok:
                st.warning(msg)
            st.session_state[f"__edit_{cid}"] = False
            st.session_state["__active_tab__"] = "Clients"
            _safe_rerun()
        if cc2.button("Cancel", key=f"rn_cancel_{cid}"):
            st.session_state[f"__edit_{cid}"] = False
        st.markdown("</div>", unsafe_allow_html=True)

    # Inline delete confirm
    if st.session_state.get(f"__del_{cid}"):
        st.markdown("<div class='inline-panel'>", unsafe_allow_html=True)
        dc1, dc2 = st.columns([0.25, 0.25])
        if dc1.button("Confirm delete", key=f"del_yes_{cid}"):
            delete_client(cid)
            st.session_state[f"__del_{cid}"] = False
            st.session_state["__active_tab__"] = "Clients"
            _safe_rerun()
        if dc2.button("Cancel", key=f"del_no_{cid}"):
            st.session_state[f"__del_{cid}"] = False
        st.markdown("</div>", unsafe_allow_html=True)

    # subtle divider under each row
    st.markdown(
        "<div style='border-bottom:1px solid var(--row-border); margin:4px 0 2px 0;'></div>",
        unsafe_allow_html=True,
    )

def _render_client_report_view(client_display_name: str, client_norm: str):
    """Render a report: address as hyperlink → Zillow, with Campaign filter and Search box.
       Adds a "Toured" badge if the address slug matches any tour stop for the client.
    """
    st.markdown(f"### Report for {escape(client_display_name)}", unsafe_allow_html=True)

    colX, colY = st.columns([1, 3])
    with colX:
        if st.button("Close report", key=f"__close_report_{client_norm}"):
            st.session_state["__active_tab__"] = "Clients"  # keep focus on Clients
            _qp_set()  # clear query params
            _safe_rerun()
    with colY:
        # Optional helper: jump to Tours tab quickly
        if st.button("Open Tours tab", key=f"__open_tours_{client_norm}"):
            st.session_state["__active_tab__"] = "Tours"
            _safe_rerun()

    rows = fetch_sent_for_client(client_norm)
    total = len(rows)

    # Cross-check tour slugs once
    tour_slugs = fetch_tour_slugs_for_client(client_norm)

    # Build campaign filter choices
    seen = []
    for r in rows:
        c = (r.get("campaign") or "").strip()
        if c not in seen:
            seen.append(c)
    campaign_labels = ["All campaigns"] + [("— no campaign —" if c == "" else c) for c in seen]
    campaign_keys = [None] + seen

    colF1, colF2, colF3 = st.columns([1.2, 1.8, 1])
    with colF1:
        sel_idx = st.selectbox(
            "Filter by campaign",
            list(range(len(campaign_labels))),
            format_func=lambda i: campaign_labels[i],
            index=0,
            key=f"__camp_{client_norm}",
        )
        sel_campaign = campaign_keys[sel_idx]
    with colF2:
        q = st.text_input(
            "Search address / MLS / URL",
            value="",
            placeholder="e.g. 407 Woodall, 2501234, /homedetails/",
            key=f"__q_{client_norm}",
        )
        q_norm = (q or "").strip().lower()
    with colF3:
        st.caption(f"{total} total logged")

    def _match(row) -> bool:
        if sel_campaign is not None:
            if (row.get("campaign") or "").strip() != sel_campaign:
                return False
        if not q_norm:
            return True
        addr = (row.get("address") or "").lower()
        mls = (row.get("mls_id") or "").lower()
        url = (row.get("url") or "").lower()
        return (q_norm in addr) or (q_norm in mls) or (q_norm in url)

    rows_f = [r for r in rows if _match(r)]
    count = len(rows_f)

    st.caption(f"{count} matching listing{'s' if count != 1 else ''}")

    if not rows_f:
        st.info("No results match the current filters.")
        return

    # Render list with badges
    items_html = []
    for r in rows_f:
        url = (r.get("url") or "").strip()
        addr_display = (r.get("address") or "").strip() or _address_text_from_url(url) or "Listing"
        sent_at = r.get("sent_at") or ""
        camp = (r.get("campaign") or "").strip()
        chip = ""
        if sel_campaign is None and camp:
            chip = f"<span style='font-size:11px; font-weight:700; padding:2px 6px; border-radius:999px; background:#e2e8f0; margin-left:6px;'>{escape(camp)}</span>"

        # Cross-check badge
        slug = _slug_from_sent_row(r)
        toured_badge = ""
        if slug and slug in tour_slugs:
            toured_badge = "<span class='badge new' style='margin-left:6px;'>Toured</span>"

        items_html.append(
            f"""<li>
                  <a href="{escape(url)}" target="_blank" rel="noopener">{escape(addr_display)}</a>
                  <span style="color:#64748b; font-size:12px; margin-left:6px;">{escape(sent_at)}</span>
                  {chip}{toured_badge}
                </li>"""
        )

    html = "<ul class='link-list'>" + "\n".join(items_html) + "</ul>"
    st.markdown(html, unsafe_allow_html=True)

    with st.expander("Export filtered report"):
        df = pd.DataFrame(rows_f)
        csv_buf = io.StringIO()
        df.to_csv(csv_buf, index=False)
        st.download_button(
            "Download CSV",
            data=csv_buf.getvalue(),
            file_name=f"client_report_{client_norm}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=False,
        )

# ---------- Entry point ----------
def render_clients_tab():
    """
    Public entry point called from app.py inside the Clients tab context.
    """
    st.subheader("Clients")
    st.caption("")

    report_norm_qp = _qp_get("report", "")
    want_scroll = _qp_get("scroll", "") in ("1", "true", "yes")

    all_clients = fetch_clients(include_inactive=True)
    active = [c for c in all_clients if c.get("active")]
    inactive = [c for c in all_clients if not c.get("active")]

    colA, colB = st.columns(2)

    with colA:
        st.markdown("### Active", unsafe_allow_html=True)
        if not active:
            st.write("_No active clients_")
        else:
            for c in active:
                _client_row_icons(c["name"], c.get("name_norm", ""), c["id"], active=True)

    with colB:
        st.markdown("### Inactive", unsafe_allow_html=True)
        if not inactive:
            st.write("_No inactive clients_")
        else:
            for c in inactive:
                _client_row_icons(c["name"], c.get("name_norm", ""), c["id"], active=False)

    # ---- REPORT SECTION BELOW THE TABLES ----
    st.markdown('<div id="report_anchor"></div>', unsafe_allow_html=True)
    if report_norm_qp:
        display_name = next((c["name"] for c in all_clients if c.get("name_norm") == report_norm_qp), report_norm_qp)
        st.markdown("---")
        _render_client_report_view(display_name, report_norm_qp)
        if want_scroll:
            components.html(
                """
                <script>
                  const el = parent.document.getElementById("report_anchor");
                  if (el) { el.scrollIntoView({behavior: "smooth", block: "start"}); }
                </script>
                """,
                height=0,
            )
            # clear only the scroll flag so back/refresh won't keep jumping
            _qp_set(report=report_norm_qp)
