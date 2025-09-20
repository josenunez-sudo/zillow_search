# ui/tours_tab.py
# Tours tab: list clients (Active/Inactive), open a client's Tours report,
# view/edit tour stops (showings), add tours & stops, with clickable links + times.

import os
import re
import io
from datetime import date, datetime
from typing import List, Dict, Any, Optional
from html import escape
from urllib.parse import quote_plus

import streamlit as st
from supabase import create_client, Client

# ───────────────────────────── Supabase ─────────────────────────────

@st.cache_resource
def _get_supabase() -> Optional[Client]:
    try:
        url = os.getenv("SUPABASE_URL", st.secrets.get("SUPABASE_URL", ""))
        key = os.getenv("SUPABASE_SERVICE_ROLE", st.secrets.get("SUPABASE_SERVICE_ROLE", ""))
        if not url or not key:
            return None
        return create_client(url, key)
    except Exception:
        return None

SUPABASE = _get_supabase()

def _sb_ok() -> bool:
    try:
        return bool(SUPABASE)
    except NameError:
        return False

# ───────────────────────────── Utils ─────────────────────────────

def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _slug_addr_for_match(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[#,.]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s

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

def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

def _zillow_deeplink_from_addr(addr: str) -> str:
    # Clean, stable search deeplink (works well for SMS unfurls)
    s = re.sub(r"\s+", " ", (addr or "").strip())
    if not s:
        return ""
    slug = re.sub(r"[^\w\s,-]", "", s).replace(",", "")
    slug = re.sub(r"\s+", "-", slug.strip())
    return f"https://www.zillow.com/homes/{slug}_rb/"

# ────────────────────────── Client registry ──────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False):
    if not _sb_ok():
        return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

def _invalidate_clients_cache():
    try:
        fetch_clients.clear()  # type: ignore[attr-defined]
    except Exception:
        pass

# ───────────────────────────── Tours data ─────────────────────────────

@st.cache_data(ttl=120, show_spinner=False)
def fetch_tours_for_client(client_norm: str) -> List[Dict[str, Any]]:
    if not (_sb_ok() and client_norm.strip()):
        return []
    try:
        rows = SUPABASE.table("tours")\
            .select("id, client, client_display, tour_date, created_at")\
            .eq("client", client_norm.strip())\
            .order("tour_date", desc=True)\
            .limit(2000).execute().data or []
        return rows
    except Exception:
        return []

def _invalidate_tours_cache():
    try:
        fetch_tours_for_client.clear()  # type: ignore[attr-defined]
    except Exception:
        pass

@st.cache_data(ttl=120, show_spinner=False)
def fetch_stops_for_tour(tour_id: int) -> List[Dict[str, Any]]:
    if not (_sb_ok() and tour_id):
        return []
    try:
        rows = SUPABASE.table("tour_stops")\
            .select("id, tour_id, address, start, end, deeplink, address_slug")\
            .eq("tour_id", tour_id)\
            .order("start", desc=False)\
            .limit(500).execute().data or []
        return rows
    except Exception:
        return []

def _invalidate_stops_cache():
    try:
        fetch_stops_for_tour.clear()  # type: ignore[attr-defined]
    except Exception:
        pass

# ───────────────────────────── Tours CRUD ─────────────────────────────

def create_tour(client_norm: str, client_display: str, tour_date: date):
    if not _sb_ok():
        return False, "Supabase not configured"
    try:
        payload = {
            "client": client_norm.strip(),
            "client_display": (client_display or "").strip(),
            "tour_date": str(tour_date),
        }
        resp = SUPABASE.table("tours").insert(payload).execute()
        _invalidate_tours_cache()
        return True, resp.data[0]["id"] if resp.data else True
    except Exception as e:
        return False, str(e)

def delete_tour(tour_id: int):
    if not _sb_ok():
        return False, "Supabase not configured"
    try:
        SUPABASE.table("tour_stops").delete().eq("tour_id", tour_id).execute()
        SUPABASE.table("tours").delete().eq("id", tour_id).execute()
        _invalidate_tours_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def add_stop(tour_id: int, address: str, start: str, end: str, deeplink: str = ""):
    if not _sb_ok():
        return False, "Supabase not configured"
    try:
        if not deeplink:
            deeplink = _zillow_deeplink_from_addr(address)
        payload = {
            "tour_id": tour_id,
            "address": address.strip(),
            "address_slug": _slug_addr_for_match(address),
            "start": (start or "").strip(),
            "end": (end or "").strip(),
            "deeplink": deeplink.strip(),
        }
        SUPABASE.table("tour_stops").insert(payload).execute()
        _invalidate_stops_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def update_stop(stop_id: int, *, address: Optional[str] = None, start: Optional[str] = None,
                end: Optional[str] = None, deeplink: Optional[str] = None):
    if not _sb_ok():
        return False, "Supabase not configured"
    try:
        patch: Dict[str, Any] = {}
        if address is not None:
            patch["address"] = address.strip()
            patch["address_slug"] = _slug_addr_for_match(address)
        if start is not None:
            patch["start"] = start.strip()
        if end is not None:
            patch["end"] = end.strip()
        if deeplink is not None:
            patch["deeplink"] = deeplink.strip() or _zillow_deeplink_from_addr(address or "")
        if not patch:
            return True, "noop"
        SUPABASE.table("tour_stops").update(patch).eq("id", stop_id).execute()
        _invalidate_stops_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_stop(stop_id: int):
    if not _sb_ok():
        return False, "Supabase not configured"
    try:
        SUPABASE.table("tour_stops").delete().eq("id", stop_id).execute()
        _invalidate_stops_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ───────────────────────────── UI helpers ─────────────────────────────

def _client_row(name: str, norm: str, cid: int, active: bool):
    # Layout matches Clients tab: name + ▦ ✎ ⟳ ⌫ (we only need ▦ here)
    col_name, col_tours, col_sp = st.columns([9, 1, 2])
    with col_name:
        st.markdown(
            f"<span class='client-name'>{escape(name)}</span> "
            f"<span class='pill {'active' if active else ''}'>{'active' if active else 'inactive'}</span>",
            unsafe_allow_html=True
        )
    with col_tours:
        if st.button("▦", key=f"open_tours_{cid}", help="Open Tours report"):
            _qp_set(tclient=norm, scroll="1")
            _safe_rerun()
    st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:4px 0 2px 0;'></div>", unsafe_allow_html=True)

def _tour_badge(text: str) -> str:
    # Higher-contrast badge for dates (easy to see in both themes)
    return (
        "<span style='display:inline-block;padding:2px 8px;border-radius:999px;"
        "font-size:12px;font-weight:800;background:#fef3c7;color:#92400e;"
        "border:1px solid rgba(146,64,14,.25);'>"
        f"{escape(text)}</span>"
    )

def _time_chip(t1: str, t2: str) -> str:
    txt = "–".join([x for x in [t1.strip(), t2.strip()] if x]) if (t1 or t2) else ""
    if not txt:
        return ""
    return (
        "<span style='display:inline-block;margin-left:8px;padding:2px 6px;border-radius:999px;"
        "font-size:12px;font-weight:700;background:#dbeafe;color:#1e40af;"
        "border:1px solid rgba(30,64,175,.25);'>"
        f"{escape(txt)}</span>"
    )

# ───────────────────────────── Tours report UI ─────────────────────────────

def _render_client_tours_panel(client_norm: str, client_display: str):
    st.markdown(f"### Tours for {escape(client_display)}", unsafe_allow_html=True)

    # Add tour
    with st.expander("➕ Add a tour", expanded=False):
        tdate = st.date_input("Tour date", value=date.today(), key="__new_tour_date__")
        if st.button("Add tour", key="__add_tour_btn__", use_container_width=True):
            ok, info = create_tour(client_norm, client_display, tdate)
            if ok:
                st.success("Tour created.")
                _safe_rerun()
            else:
                st.error(f"Create failed: {info}")

    tours = fetch_tours_for_client(client_norm)
    if not tours:
        st.info("No tours for this client yet.")
        return

    # Quick list of tours
    st.markdown("#### All tours")
    items = []
    for t in tours:
        tid = t["id"]
        tdate = t.get("tour_date") or ""
        # Count stops (cheap extra fetch for counts)
        stops = fetch_stops_for_tour(tid)
        count = len(stops)
        items.append((tid, tdate, count))

    for tid, tdate, count in items:
        colA, colB, colC, colD = st.columns([6, 2, 1, 1])
        with colA:
            st.markdown(f"{_tour_badge(str(tdate))} &nbsp; <strong>{count} stop{'s' if count!=1 else ''}</strong>", unsafe_allow_html=True)
        with colB:
            if st.button("Open", key=f"__open_tour_{tid}"):
                _qp_set(tclient=client_norm, tour=str(tid))
                _safe_rerun()
        with colC:
            if st.button("⟳", key=f"__refresh_tour_{tid}", help="Refresh this tour"):
                _invalidate_stops_cache()
                _safe_rerun()
        with colD:
            if st.button("⌫", key=f"__del_tour_{tid}", help="Delete this tour"):
                ok, info = delete_tour(tid)
                if ok:
                    st.success("Tour deleted.")
                else:
                    st.error(f"Delete failed: {info}")
                _safe_rerun()
        st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:6px 0 10px 0;'></div>", unsafe_allow_html=True)

    # Selected tour details
    tid_qp = _qp_get("tour", "")
    try:
        tid_sel = int(tid_qp) if tid_qp else None
    except Exception:
        tid_sel = None

    if tid_sel:
        st.markdown("---")
        st.markdown("#### Tour details")
        stops = fetch_stops_for_tour(tid_sel)

        if not stops:
            st.info("No stops yet. Add the first stop below.")
        else:
            for s in stops:
                sid = s["id"]
                addr = s.get("address") or ""
                start = s.get("start") or ""
                end = s.get("end") or ""
                deeplink = s.get("deeplink") or _zillow_deeplink_from_addr(addr)

                # Inline card: hyperlink + time chip + quick Edit/Delete
                col1, col2, col3 = st.columns([8, 1, 1])
                with col1:
                    a_html = (
                        f'<a href="{escape(deeplink)}" target="_blank" rel="noopener">{escape(addr)}</a>'
                        f' { _time_chip(start, end) }'
                    )
                    st.markdown(a_html, unsafe_allow_html=True)
                with col2:
                    if st.button("✎", key=f"__edit_stop_{sid}", help="Edit stop"):
                        st.session_state[f"__edit_{sid}"] = True
                with col3:
                    if st.button("⌫", key=f"__del_stop_{sid}", help="Delete stop"):
                        ok, info = delete_stop(sid)
                        if ok:
                            st.success("Stop deleted.")
                        else:
                            st.error(f"Delete failed: {info}")
                        _safe_rerun()

                # Inline editor
                if st.session_state.get(f"__edit_{sid}"):
                    with st.container():
                        st.markdown("<div class='inline-panel'>", unsafe_allow_html=True)
                        e_addr = st.text_input("Address", value=addr, key=f"__e_addr_{sid}")
                        cc1, cc2 = st.columns(2)
                        with cc1:
                            e_start = st.text_input("Start (e.g. 10:00 AM)", value=start, key=f"__e_start_{sid}")
                        with cc2:
                            e_end = st.text_input("End (e.g. 10:30 AM)", value=end, key=f"__e_end_{sid}")
                        e_link = st.text_input("Link (optional; auto if empty)", value=deeplink, key=f"__e_link_{sid}")
                        ccs, ccc = st.columns([0.2, 0.2])
                        if ccs.button("Save", key=f"__e_save_{sid}"):
                            ok, info = update_stop(sid, address=e_addr, start=e_start, end=e_end, deeplink=e_link)
                            if ok:
                                st.success("Stop updated.")
                            else:
                                st.error(f"Update failed: {info}")
                            st.session_state[f"__edit_{sid}"] = False
                            _safe_rerun()
                        if ccc.button("Cancel", key=f"__e_cancel_{sid}"):
                            st.session_state[f"__edit_{sid}"] = False
                        st.markdown("</div>", unsafe_allow_html=True)

                    st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)

        # Add stop to this tour
        st.markdown("##### Add stop")
        with st.container():
            a = st.text_input("Address", key="__new_stop_addr__")
            c1, c2 = st.columns(2)
            with c1:
                s = st.text_input("Start (e.g. 1:00 PM)", key="__new_stop_start__")
            with c2:
                e = st.text_input("End (e.g. 1:30 PM)", key="__new_stop_end__")
            link_default = _zillow_deeplink_from_addr(a) if a else ""
            link = st.text_input("Link (optional; auto if empty)", value=link_default, key="__new_stop_link__")
            if st.button("Add stop", key="__add_stop_btn__", use_container_width=True):
                if not (a or "").strip():
                    st.warning("Please enter an address.")
                else:
                    ok, info = add_stop(tid_sel, a, s, e, link)
                    if ok:
                        st.success("Stop added.")
                        _safe_rerun()
                    else:
                        st.error(f"Add failed: {info}")

# ───────────────────────────── Public entry ─────────────────────────────

def render_tours_tab(state: dict):
    st.subheader("Tours")
    st.caption("View and edit showings (tour stops). Click a client to see their tours and all properties toured.")

    if not _sb_ok():
        st.warning("Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE.")
        return

    tclient = _qp_get("tclient", "")
    want_scroll = _qp_get("scroll", "") in ("1", "true", "yes")

    all_clients = fetch_clients(include_inactive=True)
    active = [c for c in all_clients if c.get("active")]
    inactive = [c for c in all_clients if not c.get("active")]

    colA, colB = st.columns([2, 3])

    # Left: clients list (like Clients tab)
    with colA:
        st.markdown("### Clients", unsafe_allow_html=True)
        st.markdown("#### Active", unsafe_allow_html=True)
        if not active:
            st.write("_No active clients_")
        else:
            for c in active:
                _client_row(c["name"], c.get("name_norm", ""), c["id"], active=True)

        st.markdown("#### Inactive", unsafe_allow_html=True)
        if not inactive:
            st.write("_No inactive clients_")
        else:
            for c in inactive:
                _client_row(c["name"], c.get("name_norm", ""), c["id"], active=False)

    # Right: selected client's Tours report
    with colB:
        st.markdown('<div id="tours_report_anchor"></div>', unsafe_allow_html=True)
        if tclient:
            display_name = next((c["name"] for c in all_clients if c.get("name_norm") == tclient), tclient)
            _render_client_tours_panel(tclient, display_name)

            # Smooth scroll when opening from the left list
            if want_scroll:
                st.components.v1.html(
                    """
                    <script>
                      const el = parent.document.getElementById("tours_report_anchor");
                      if (el) { el.scrollIntoView({behavior: "smooth", block: "start"}); }
                    </script>
                    """,
                    height=0,
                )
                _qp_set(tclient=tclient)
        else:
            st.info("Select a client on the left to view and edit their tours.")
