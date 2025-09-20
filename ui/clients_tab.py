# ui/clients_tab.py
# Clients tab with inline "Toured" date/time next to each sent listing hyperlink.

import os
import re
import io
from datetime import datetime
from typing import List, Dict, Any, Optional
from html import escape
from urllib.parse import unquote

import streamlit as st

# ---------- Supabase ----------
from supabase import create_client, Client

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

# ---------- Small utils ----------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _slug_addr_for_match(s: str) -> str:
    """
    Slug used to match addresses across 'sent' and 'tour_stops'.
    Make sure the same logic is used wherever you mark "TOURED" in other tabs.
    """
    s = (s or "").lower()
    s = re.sub(r"[#,.]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s

def _qp_get(name, default=None):
    try:
        qp = st.query_params  # Streamlit >= 1.34
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

# ---------- Clients registry helpers ----------
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

def rename_client(client_id: int, new_name: str):
    if not _sb_ok() or not client_id or not (new_name or "").strip():
        return False, "Bad input"
    try:
        new_norm = _norm_tag(new_name)
        existing = SUPABASE.table("clients").select("id").eq("name_norm", new_norm).limit(1).execute().data or []
        if existing and existing[0]["id"] != client_id:
            return False, "A client with that (normalized) name already exists."
        SUPABASE.table("clients").update({"name": new_name.strip(), "name_norm": new_norm}).eq("id", client_id).execute()
        _invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def toggle_client_active(client_id: int, new_active: bool):
    if not _sb_ok() or not client_id:
        return False, "Not configured"
    try:
        SUPABASE.table("clients").update({"active": new_active}).eq("id", client_id).execute()
        _invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_client(client_id: int):
    if not _sb_ok() or not client_id:
        return False, "Not configured"
    try:
        SUPABASE.table("clients").delete().eq("id", client_id).execute()
        _invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ---------- Sent lookups (for the report) ----------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_sent_for_client(client_norm: str, limit: int = 5000):
    """
    Returns: [{url, address, sent_at, campaign, mls_id, canonical, zpid}, ...]
    """
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

def address_text_from_url(url: str) -> str:
    """Best-effort human address text from a Zillow URL when DB 'address' is empty."""
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

# ---------- Tours cross-check (inline pill) ----------
@st.cache_data(ttl=120, show_spinner=False)
def _fetch_tour_map_for_client(client_norm: str):
    """
    Returns a dict: {address_slug: [ {date:'YYYY-MM-DD', start:'10:00 AM', end:'10:30 AM'}, ... ] }
    Most-recent stop is first.
    """
    try:
        if not SUPABASE or not (client_norm or "").strip():
            return {}
        tours = SUPABASE.table("tours")\
            .select("id,tour_date")\
            .eq("client", client_norm.strip())\
            .order("tour_date", desc=True)\
            .limit(5000).execute().data or []
        if not tours:
            return {}
        id_to_date = {t["id"]: t.get("tour_date") for t in tours}
        tour_ids = [t["id"] for t in tours]

        stops = SUPABASE.table("tour_stops")\
            .select("tour_id,address,start,end")\
            .in_("tour_id", tour_ids)\
            .limit(20000).execute().data or []

        by_slug = {}
        for srow in stops:
            slug = _slug_addr_for_match(srow.get("address", ""))
            if not slug:
                continue
            item = {
                "date": id_to_date.get(srow["tour_id"]),
                "start": (srow.get("start") or ""),
                "end": (srow.get("end") or "")
            }
            by_slug.setdefault(slug, []).append(item)

        for slug, arr in by_slug.items():
            arr.sort(key=lambda x: (x.get("date") or ""), reverse=True)
        return by_slug
    except Exception:
        return {}

# ---------- Row UI (name + ▦ ✎ ⟳ ⌫) ----------
def _client_row_icons(name: str, norm: str, cid: int, active: bool):
    # Single line with inline widgets aligned using columns
    col_name, col_rep, col_ren, col_tog, col_del, col_sp = st.columns([8, 1, 1, 1, 1, 2])

    with col_name:
        st.markdown(
            f"<span class='client-name'>{escape(name)}</span> "
            f"<span class='pill {'active' if active else ''}'>{'active' if active else 'inactive'}</span>",
            unsafe_allow_html=True
        )

    with col_rep:
        if st.button("▦", key=f"rep_{cid}", help="Open report"):
            _qp_set(report=norm, scroll="1")
            _safe_rerun()

    with col_ren:
        if st.button("✎", key=f"rn_btn_{cid}", help="Rename"):
            st.session_state[f"__edit_{cid}"] = True

    with col_tog:
        if st.button("⟳", key=f"tg_{cid}", help=("Deactivate" if active else "Activate")):
            try:
                rows = SUPABASE.table("clients").select("active").eq("id", cid).limit(1).execute().data or []
                cur = rows[0]["active"] if rows else active
                toggle_client_active(cid, (not cur))
            except Exception:
                toggle_client_active(cid, (not active))
            _safe_rerun()

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
            _safe_rerun()
        if dc2.button("Cancel", key=f"del_no_{cid}"):
            st.session_state[f"__del_{cid}"] = False
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:4px 0 2px 0;'></div>", unsafe_allow_html=True)

# ---------- Client Report (with inline Toured pill) ----------
def _render_client_report_view(client_display_name: str, client_norm: str):
    """Report: hyperlink to Zillow with Campaign filter, Search, and inline 'Toured' date/time."""
    st.markdown(f"### Report for {escape(client_display_name)}", unsafe_allow_html=True)

    colX, _ = st.columns([1, 3])
    with colX:
        if st.button("Close report", key=f"__close_report_{client_norm}"):
            _qp_set()  # clear query params
            _safe_rerun()

    rows = fetch_sent_for_client(client_norm)
    total = len(rows)

    # Campaign filter
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
            key=f"__camp_{client_norm}"
        )
        sel_campaign = campaign_keys[sel_idx]
    with colF2:
        q = st.text_input(
            "Search address / MLS / URL",
            value="",
            placeholder="e.g. 407 Woodall, 2501234, /homedetails/",
            key=f"__q_{client_norm}"
        )
        q_norm = q.strip().lower()
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

    # Build tour map once
    tour_map = _fetch_tour_map_for_client(client_norm)

    # Higher-contrast pill styling (readable in both themes)
    tour_style = (
        "display:inline-block;margin-left:8px;padding:2px 6px;border-radius:999px;"
        "font-size:12px;font-weight:700;background:#dbeafe;color:#1e40af;"
        "border:1px solid rgba(30,64,175,.25);"
    )

    items_html = []
    for r in rows_f:
        url = (r.get("url") or "").strip()
        addr_text = (r.get("address") or "").strip() or address_text_from_url(url) or "Listing"
        sent_at = r.get("sent_at") or ""
        camp = (r.get("campaign") or "").strip()

        camp_chip = ""
        if sel_campaign is None and camp:
            camp_chip = (
                "<span style='font-size:11px;font-weight:700;padding:2px 6px;"
                "border-radius:999px;background:#e2e8f0;margin-left:6px;'>"
                f"{escape(camp)}</span>"
            )

        # Inline tour pill (most recent stop)
        slug = _slug_addr_for_match(addr_text)
        tlist = tour_map.get(slug) or []
        tour_pill = ""
        if tlist:
            most_recent = tlist[0]
            d = (most_recent.get("date") or "")
            s = (most_recent.get("start") or "")
            e = (most_recent.get("end") or "")
            time_txt = (f"{s}–{e}" if s and e else (s or e or ""))
            if d or time_txt:
                label = "🕑 Toured " + " ".join(x for x in [d, time_txt] if x).strip()
                tour_pill = f"<span style='{tour_style}' title='This property was toured'>{escape(label)}</span>"

        items_html.append(
            f"""<li>
                  <a href="{escape(url)}" target="_blank" rel="noopener">{escape(addr_text)}</a>
                  {tour_pill}
                  <span style="color:#64748b;font-size:12px;margin-left:6px;">{escape(sent_at)}</span>
                  {camp_chip}
                </li>"""
        )

    html = "<ul class='link-list'>" + "\n".join(items_html) + "</ul>"
    st.markdown(html, unsafe_allow_html=True)

    # Export filtered report
    with st.expander("Export filtered report"):
        import pandas as pd
        df = pd.DataFrame(rows_f)
        csv_buf = io.StringIO()
        df.to_csv(csv_buf, index=False)
        st.download_button(
            "Download CSV",
            data=csv_buf.getvalue(),
            file_name=f"client_report_{client_norm}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=False
        )

# ---------- Public entry ----------
def render_clients_tab():
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

    st.markdown('<div id="report_anchor"></div>', unsafe_allow_html=True)
    if report_norm_qp:
        display_name = next((c["name"] for c in all_clients if c.get("name_norm") == report_norm_qp), report_norm_qp)
        st.markdown("---")
        _render_client_report_view(display_name, report_norm_qp)
        if want_scroll:
            st.components.v1.html(
                """
                <script>
                  const el = parent.document.getElementById("report_anchor");
                  if (el) { el.scrollIntoView({behavior: "smooth", block: "start"}); }
                </script>
                """,
                height=0,
            )
            _qp_set(report=report_norm_qp)
