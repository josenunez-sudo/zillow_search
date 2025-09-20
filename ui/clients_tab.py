# ui/clients_tab.py
# Clients tab: side-by-side Active / Inactive with ▦ Report button.
# Report view shows sent listings; listings that were toured are tagged "TOURED (date time)".

import os
import re
from datetime import datetime
from typing import List, Dict, Any, Optional
from html import escape

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

def address_text_from_url(url: str) -> str:
    if not url:
        return ""
    from urllib.parse import unquote
    u = unquote(url)
    m = re.search(r"/homedetails/([^/]+)/\d{6,}_zpid/", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    m = re.search(r"/homes/([^/_]+)_rb/?", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    return ""

def _time_chip(t1: str, t2: str) -> str:
    txt = "–".join([x for x in [t1.strip(), t2.strip()] if x]) if (t1 or t2) else ""
    if not txt:
        return ""
    return (
        "<span style='display:inline-block;margin-left:8px;padding:2px 8px;border-radius:999px;"
        "font-size:12px;font-weight:800;background:#1e40af1a;color:#1e3a8a;"
        "border:1px solid #60a5fa;'>"
        f"{escape(txt)}</span>"
    )

def _toured_chip(dt_iso: str) -> str:
    # high-contrast amber chip for "TOURED"
    label = (dt_iso or "").split("T")[0]
    return (
        "<span style='display:inline-block;margin-left:8px;padding:2px 10px;border-radius:999px;"
        "font-size:12px;font-weight:900;background:#f59e0b1a;color:#92400e;border:1px solid #f59e0b;'>"
        f"TOURED {escape(label)}</span>"
    )


# ───────────────────────────── Clients registry ─────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = True):
    if not _sb_ok():
        return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

def upsert_client(name: str, active: bool = True, notes: str = None):
    if not _sb_ok() or not (name or "").strip():
        return False, "Not configured or empty name"
    try:
        name_norm = _norm_tag(name)
        payload = {"name": name.strip(), "name_norm": name_norm, "active": active}
        if notes is not None:
            payload["notes"] = notes
        SUPABASE.table("clients").upsert(payload, on_conflict="name_norm").execute()
        try:
            fetch_clients.clear()  # type: ignore[attr-defined]
        except Exception:
            pass
        return True, "ok"
    except Exception as e:
        return False, str(e)

def toggle_client_active(client_id: int, new_active: bool):
    if not _sb_ok() or not client_id:
        return False, "Not configured"
    try:
        SUPABASE.table("clients").update({"active": new_active}).eq("id", client_id).execute()
        try:
            fetch_clients.clear()  # type: ignore[attr-defined]
        except Exception:
            pass
        return True, "ok"
    except Exception as e:
        return False, str(e)

def rename_client(client_id: int, new_name: str):
    if not _sb_ok() or not client_id or not (new_name or "").strip():
        return False, "Bad input"
    try:
        new_norm = _norm_tag(new_name)
        existing = SUPABASE.table("clients").select("id").eq("name_norm", new_norm).limit(1).execute().data or []
        if existing and existing[0]["id"] != client_id:
            return False, "A client with that (normalized) name already exists."
        SUPABASE.table("clients").update({"name": new_name.strip(), "name_norm": new_norm}).eq("id", client_id).execute()
        try:
            fetch_clients.clear()  # type: ignore[attr-defined]
        except Exception:
            pass
        return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_client(client_id: int):
    if not _sb_ok() or not client_id:
        return False, "Not configured"
    try:
        SUPABASE.table("clients").delete().eq("id", client_id).execute()
        try:
            fetch_clients.clear()  # type: ignore[attr-defined]
        except Exception:
            pass
        return True, "ok"
    except Exception as e:
        return False, str(e)


# ───────────────────────────── Sent & Tours lookups ─────────────────────────────

@st.cache_data(ttl=120, show_spinner=False)
def fetch_sent_for_client(client_norm: str, limit: int = 5000):
    if not (_sb_ok() and client_norm.strip()):
        return []
    try:
        cols = "url,address,sent_at,campaign,mls_id,canonical,zpid"
        resp = SUPABASE.table("sent")\
            .select(cols)\
            .eq("client", client_norm.strip())\
            .order("sent_at", desc=True)\
            .limit(limit)\
            .execute()
        return resp.data or []
    except Exception:
        return []

@st.cache_data(ttl=120, show_spinner=False)
def fetch_toured_map(client_norm: str) -> Dict[str, Dict[str, str]]:
    """
    Returns { address_slug: { 'date': 'YYYY-MM-DD', 'start': '9:00 AM', 'end': '10:15 AM' } }
    built from tours + tour_stops.
    """
    out: Dict[str, Dict[str, str]] = {}
    if not (_sb_ok() and client_norm.strip()):
        return out
    try:
        tours = SUPABASE.table("tours").select("id,tour_date").eq("client", client_norm.strip()).limit(2000).execute().data or []
        ids = [t["id"] for t in tours if t.get("id")]
        if not ids:
            return out
        stops = SUPABASE.table("tour_stops").select("tour_id,address,address_slug,start,end").in_("tour_id", ids).limit(10000).execute().data or []
        id_to_date = {t["id"]: (t.get("tour_date") or "") for t in tours}
        for s in stops:
            slug = s.get("address_slug") or _slug_addr_for_match(s.get("address",""))
            tdate = id_to_date.get(s.get("tour_id"), "")
            if slug and tdate and slug not in out:
                out[slug] = {"date": tdate, "start": s.get("start",""), "end": s.get("end","")}
        return out
    except Exception:
        return out


# ───────────────────────────── UI pieces ─────────────────────────────

def _client_row_icons(name: str, norm: str, cid: int, active: bool):
    col_name, col_rep, col_ren, col_tog, col_del, _ = st.columns([8, 1, 1, 1, 1, 2])

    with col_name:
        st.markdown(
            f"<span class='client-name'>{escape(name)}</span> "
            f"<span class='pill {'active' if active else ''}'>{'active' if active else 'inactive'}</span>",
            unsafe_allow_html=True
        )

    with col_rep:
        if st.button("▦", key=f"rep_{cid}", help="Open report"):
            st.session_state["__client_report__"] = {"norm": norm, "name": name}

    with col_ren:
        if st.button("✎", key=f"rn_btn_{cid}", help="Rename"):
            st.session_state[f"__edit_{cid}"] = True

    with col_tog:
        if st.button("⟳", key=f"tg_{cid}", help=("Deactivate" if active else "Activate")):
            rows = SUPABASE.table("clients").select("active").eq("id", cid).limit(1).execute().data or []
            cur = rows[0]["active"] if rows else active
            toggle_client_active(cid, (not cur))
            st.experimental_rerun()

    with col_del:
        if st.button("⌫", key=f"del_{cid}", help="Delete"):
            st.session_state[f"__del_{cid}"] = True

    # Inline rename editor
    if st.session_state.get(f"__edit_{cid}"):
        st.markdown("<div class='inline-panel'>", unsafe_allow_html=True)
        new_name = st.text_input("New name", value=name, key=f"rn_val_{cid}")
        cc1, cc2 = st.columns([0.25, 0.25])
        if cc1.button("Save", key=f"rn_save_{cid}"):
            ok, msg = rename_client(cid, new_name)
            if not ok:
                st.warning(msg)
            st.session_state[f"__edit_{cid}"] = False
            st.experimental_rerun()
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
            st.experimental_rerun()
        if dc2.button("Cancel", key=f"del_no_{cid}"):
            st.session_state[f"__del_{cid}"] = False
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:4px 0 2px 0;'></div>", unsafe_allow_html=True)

def _render_client_report_view(client_display_name: str, client_norm: str):
    st.markdown(f"### Report for {escape(client_display_name)}", unsafe_allow_html=True)

    if st.button("Close report", key=f"__close_report_{client_norm}"):
        st.session_state.pop("__client_report__", None)
        st.experimental_rerun()

    rows = fetch_sent_for_client(client_norm)
    total = len(rows)

    # Build 'toured' map for chips
    toured_map = fetch_toured_map(client_norm)

    # Filters
    seen = []
    for r in rows:
        c = (r.get("campaign") or "").strip()
        if c not in seen:
            seen.append(c)
    campaign_labels = ["All campaigns"] + [("— no campaign —" if c == "" else c) for c in seen]
    campaign_keys   = [None] + seen

    colF1, colF2, colF3 = st.columns([1.2, 1.8, 1])
    with colF1:
        sel_idx = st.selectbox("Filter by campaign", list(range(len(campaign_labels))),
                               format_func=lambda i: campaign_labels[i], index=0, key=f"__camp_{client_norm}")
        sel_campaign = campaign_keys[sel_idx]
    with colF2:
        q = st.text_input("Search address / MLS / URL", value="", placeholder="e.g. 407 Woodall, 2501234, /homedetails/", key=f"__q_{client_norm}")
        q_norm = q.strip().lower()
    with colF3:
        st.caption(f"{total} total logged")

    def _match(row) -> bool:
        if sel_campaign is not None and (row.get("campaign") or "").strip() != sel_campaign:
            return False
        if not q_norm:
            return True
        addr = (row.get("address") or "").lower()
        mls  = (row.get("mls_id") or "").lower()
        url  = (row.get("url") or "").lower()
        return (q_norm in addr) or (q_norm in mls) or (q_norm in url)

    rows_f = [r for r in rows if _match(r)]
    count = len(rows_f)
    st.caption(f"{count} matching listing{'s' if count!=1 else ''}")

    if not rows_f:
        st.info("No results match the current filters.")
        return

    items_html = []
    for r in rows_f:
        url = (r.get("url") or "").strip()
        addr = (r.get("address") or "").strip() or address_text_from_url(url) or "Listing"
        sent_at = r.get("sent_at") or ""
        camp = (r.get("campaign") or "").strip()

        # Compute slug from address (or from URL-derived address)
        slug = _slug_addr_for_match(addr if addr else address_text_from_url(url))
        toured_html = ""
        if slug in toured_map:
            tm = toured_map[slug]
            dt = tm.get("date","")
            start = tm.get("start","")
            end   = tm.get("end","")
            toured_html = f" {_toured_chip(dt)}{_time_chip(start, end)}"

        chip = ""
        if sel_campaign is None and camp:
            chip = f"<span style='font-size:11px; font-weight:700; padding:2px 6px; border-radius:999px; background:#e2e8f0; margin-left:6px;'>{escape(camp)}</span>"

        items_html.append(
            f"""<li>
                  <a href="{escape(url)}" target="_blank" rel="noopener">{escape(addr)}</a>
                  {toured_html}
                  <span style="color:#64748b; font-size:12px; margin-left:6px;">{escape(sent_at)}</span>
                  {chip}
                </li>"""
        )

    st.markdown("<ul class='link-list'>" + "\n".join(items_html) + "</ul>", unsafe_allow_html=True)


# ───────────────────────────── Main ─────────────────────────────

def render_clients_tab():
    st.subheader("Clients")
    st.caption("Manage clients and view sent listings. Listings that were toured are tagged TOURED with date/time.")

    if not _sb_ok():
        st.warning("Supabase is not configured.")
        return

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
                _client_row_icons(c["name"], c.get("name_norm",""), c["id"], active=True)

    with colB:
        st.markdown("### Inactive", unsafe_allow_html=True)
        if not inactive:
            st.write("_No inactive clients_")
        else:
            for c in inactive:
                _client_row_icons(c["name"], c.get("name_norm",""), c["id"], active=False)

    rep = st.session_state.get("__client_report__")
    if rep:
        st.markdown("---")
        _render_client_report_view(rep.get("name",""), rep.get("norm",""))
