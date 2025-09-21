# ui/clients_tab.py
import os, re, io
from datetime import datetime
from html import escape
from typing import List, Dict, Any, Optional

import streamlit as st
from supabase import create_client, Client

# =====================
# Minimal styles (keep your look; avoid clobbering tags)
# =====================
st.markdown("""
<style>
:root { --row-border:#e2e8f0; --muted:#64748b; }
html[data-theme="dark"], .stApp [data-theme="dark"] { --row-border:#0b1220; --muted:#cbd5e1; }

.client-row { display:flex; align-items:center; justify-content:space-between; padding:10px 8px; border-bottom:1px solid var(--row-border); }
.client-left { display:flex; align-items:center; gap:8px; min-width:0; }
.client-name { font-weight:700; color:#111827; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
html[data-theme="dark"] .client-name { color:#fff; }
.pill { font-size:11px; font-weight:800; padding:2px 10px; border-radius:999px; }
.pill.active {
  background: linear-gradient(180deg, #dcfce7 0%, #bbf7d0 100%);
  color:#166534; border:1px solid rgba(5,150,105,.35);
}
html[data-theme="dark"] .pill.active {
  background: linear-gradient(180deg, #064e3b 0%, #065f46 100%);
  color:#a7f3d0; border-color:rgba(167,243,208,.35);
}

.iconbar { display:flex; align-items:center; gap:8px; }
.iconbar .stButton > button {
  min-width: 28px; height: 28px; padding:0 8px;
  border-radius: 8px; border:1px solid rgba(0,0,0,.08);
  font-weight:700; line-height:1; cursor:pointer;
  background:#f8fafc; color:#64748b;
  transition: transform .08s ease, box-shadow .12s ease, filter .08s ease;
}
html[data-theme="dark"] .iconbar .stButton > button {
  background:#0f172a; color:#cbd5e1; border-color:rgba(255,255,255,.08);
}
.iconbar .stButton > button:hover { transform: translateY(-1px); }
.iconbar .stButton > button:active { transform: translateY(0) scale(.98); }

.section-rule { border-bottom:1px solid var(--row-border); margin:8px 0 6px 0; }
.report-item { margin:0.25rem 0; }
.report-meta { color:var(--muted); font-size:12px; margin-left:8px; }
</style>
""", unsafe_allow_html=True)

# =====================
# Utilities
# =====================
def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _slug_addr(addr: str) -> str:
    a = (addr or "").strip().lower()
    a = re.sub(r"[^\w\s,-]", "", a).replace(",", "")
    return re.sub(r"\s+", "-", a).strip("-")

def _fmt_ts(ts: str) -> str:
    # Expecting ISO (possibly with Z). Fall back to raw.
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return d.strftime("%b %d, %Y • %I:%M %p")
    except Exception:
        return ts

# ==============
# Query params
# ==============
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
    """Use ONLY one API per run to avoid Streamlit's 'single query API' error."""
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

# =====================
# Supabase client
# =====================
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

def _sb_ok() -> bool:
    try:
        return bool(SUPABASE)
    except Exception:
        return False

# =====================
# DB helpers (clients)
# =====================
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False):
    if not _sb_ok(): return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

def _invalidate_clients_cache():
    try: fetch_clients.clear()  # type: ignore[attr-defined]
    except Exception: pass

def toggle_client_active(client_id: int, new_active: bool):
    if not _sb_ok() or not client_id: return False, "Not configured"
    try:
        SUPABASE.table("clients").update({"active": new_active}).eq("id", client_id).execute()
        _invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def rename_client(client_id: int, new_name: str):
    if not _sb_ok() or not client_id or not (new_name or "").strip(): return False, "Bad input"
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

def delete_client(client_id: int):
    if not _sb_ok() or not client_id: return False, "Not configured"
    try:
        SUPABASE.table("clients").delete().eq("id", client_id).execute()
        _invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ==========================
# Sent + Tours for reports
# ==========================
@st.cache_data(ttl=120, show_spinner=False)
def fetch_sent_for_client(client_norm: str, limit: int = 5000):
    """Return list of dicts: {url,address,sent_at,campaign,mls_id,canonical,zpid}"""
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
    if not url: return ""
    from urllib.parse import unquote
    u = unquote(url)
    m = re.search(r"/homedetails/([^/]+)/\d{6,}_zpid/", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    m = re.search(r"/homes/([^/_]+)_rb/?", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    return ""

@st.cache_data(ttl=120, show_spinner=False)
def fetch_tour_slugs_for_client(client_norm: str):
    """Return a set of address slugs that appear in any tour for this client."""
    if not (_sb_ok() and client_norm.strip()): return set()
    try:
        tq = SUPABASE.table("tours").select("id").eq("client", client_norm).limit(5000).execute()
        ids = [t["id"] for t in (tq.data or [])]
        if not ids: return set()
        sq = SUPABASE.table("tour_stops").select("address,address_slug").in_("tour_id", ids).limit(50000).execute()
        stops = sq.data or []
        slugs = set()
        for s in stops:
            slug = s.get("address_slug") or _slug_addr(s.get("address",""))
            if slug: slugs.add(slug)
        return slugs
    except Exception:
        return set()

def _dedupe_by_property(rows: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    """
    Keep ONLY the most-recent entry per property for the report.
    Key = lower(canonical) if present, else slug(address/url).
    """
    def parse_ts(ts: str):
        try:
            return datetime.fromisoformat(ts.replace("Z","+00:00"))
        except Exception:
            return datetime.min

    buckets: Dict[str, Dict[str,Any]] = {}
    for r in rows:
        key = (r.get("canonical") or "").strip().lower()
        if not key:
            addr = (r.get("address") or "").strip() or address_text_from_url(r.get("url",""))
            key = _slug_addr(addr)
        cur = buckets.get(key)
        if not cur or parse_ts(r.get("sent_at") or "") > parse_ts(cur.get("sent_at") or ""):
            buckets[key] = r
    return list(buckets.values())

# =================
# Client row (icons)
# =================
def _client_row_icons(name: str, norm: str, cid: int, active: bool):
    col_name, col_rep, col_ren, col_tog, col_del, col_sp = st.columns([8, 1, 1, 1, 1, 2])

    with col_name:
        st.markdown(
            f"<span class='client-name'>{escape(name)}</span> "
            f"<span class='pill {'active' if active else ''}'>{'active' if active else 'inactive'}</span>",
            unsafe_allow_html=True
        )

    with col_rep:
        # ▦ Open report
        if st.button("▦", key=f"rep_{cid}", help="Open report"):
            st.session_state["__active_tab__"] = "Clients"  # stay on tab after rerun
            _qp_set(report=norm, scroll="1")
            _safe_rerun()

    with col_ren:
        # ✎ Rename
        if st.button("✎", key=f"rn_btn_{cid}", help="Rename"):
            st.session_state[f"__edit_{cid}"] = True

    with col_tog:
        # ⟳ Toggle active
        if st.button("⟳", key=f"tg_{cid}", help=("Deactivate" if active else "Activate")):
            try:
                rows = SUPABASE.table("clients").select("active").eq("id", cid).limit(1).execute().data or []
                cur = rows[0]["active"] if rows else active
                toggle_client_active(cid, (not cur))
            except Exception:
                toggle_client_active(cid, (not active))
            st.session_state["__active_tab__"] = "Clients"
            _safe_rerun()

    with col_del:
        # ⌫ Delete
        if st.button("⌫", key=f"del_{cid}", help="Delete"):
            st.session_state[f"__del_{cid}"] = True

    # Inline rename editor
    if st.session_state.get(f"__edit_{cid}"):
        st.markdown("<div class='section-rule'></div>", unsafe_allow_html=True)
        new_name = st.text_input("New name", value=name, key=f"rn_val_{cid}")
        cc1, cc2 = st.columns([0.2, 0.2])
        if cc1.button("Save", key=f"rn_save_{cid}"):
            ok, msg = rename_client(cid, new_name)
            if not ok: st.warning(msg)
            st.session_state[f"__edit_{cid}"] = False
            st.session_state["__active_tab__"] = "Clients"
            _safe_rerun()
        if cc2.button("Cancel", key=f"rn_cancel_{cid}"):
            st.session_state[f"__edit_{cid}"] = False

    # Inline delete confirm
    if st.session_state.get(f"__del_{cid}"):
        st.markdown("<div class='section-rule'></div>", unsafe_allow_html=True)
        dc1, dc2 = st.columns([0.25, 0.25])
        if dc1.button("Confirm delete", key=f"del_yes_{cid}"):
            delete_client(cid)
            st.session_state[f"__del_{cid}"] = False
            st.session_state["__active_tab__"] = "Clients"
            _safe_rerun()
        if dc2.button("Cancel", key=f"del_no_{cid}"):
            st.session_state[f"__del_{cid}"] = False

    st.markdown("<div class='section-rule'></div>", unsafe_allow_html=True)

# ======================
# Inline report renderer
# ======================
def _render_client_report_view(client_display_name: str, client_norm: str):
    st.markdown(f"### Report for {escape(client_display_name)}", unsafe_allow_html=True)

    # Close button
    colX, _ = st.columns([1,3])
    with colX:
        if st.button("Close report", key=f"__close_report_{client_norm}"):
            st.session_state["__active_tab__"] = "Clients"
            _qp_set()  # clear query params
            _safe_rerun()

    sent_rows = fetch_sent_for_client(client_norm)
    tour_slugs = fetch_tour_slugs_for_client(client_norm)

    if not sent_rows:
        st.info("No listings have been sent to this client yet.")
        return

    # Filters
    seen_campaigns = []
    for r in sent_rows:
        c = (r.get("campaign") or "").strip()
        if c not in seen_campaigns: seen_campaigns.append(c)
    campaign_labels = ["All campaigns"] + [("— no campaign —" if c == "" else c) for c in seen_campaigns]
    campaign_keys   = [None] + seen_campaigns

    colF1, colF2, colF3 = st.columns([1.2, 1.8, 1])
    with colF1:
        sel_idx = st.selectbox("Filter by campaign",
                               list(range(len(campaign_labels))),
                               format_func=lambda i: campaign_labels[i], index=0,
                               key=f"__camp_{client_norm}")
        sel_campaign = campaign_keys[sel_idx]
    with colF2:
        q = st.text_input("Search address / MLS / URL", value="", placeholder="e.g. 407 Woodall, 2501234, /homedetails/",
                          key=f"__q_{client_norm}")
        q_norm = (q or "").strip().lower()
    with colF3:
        st.caption(f"{len(sent_rows)} total logged")

    # Filter rows
    def _match(row) -> bool:
        if sel_campaign is not None:
            if (row.get("campaign") or "").strip() != sel_campaign:
                return False
        if not q_norm:
            return True
        addr = (row.get("address") or "").lower()
        mls  = (row.get("mls_id") or "").lower()
        url  = (row.get("url") or "").lower()
        return (q_norm in addr) or (q_norm in mls) or (q_norm in url)

    filtered = [r for r in sent_rows if _match(r)]

    # STRICT de-dupe for display (by property)
    deduped = _dedupe_by_property(filtered)
    st.caption(f"{len(deduped)} unique listing{'s' if len(deduped)!=1 else ''} (deduped by property)")

    # Render list with INLINE styles for tags to avoid CSS conflicts
    def _chip(text: str) -> str:
        # neutral chip for campaign
        return f"<span style='font-size:11px;font-weight:700;padding:2px 6px;border-radius:999px;background:#e2e8f0;margin-left:8px;'>{escape(text)}</span>"

    def _badge_toured() -> str:
        # blue-ish "Toured" badge
        return "<span style='font-size:11px;font-weight:800;padding:2px 6px;border-radius:999px;background:#e0f2fe;color:#075985;border:1px solid #7dd3fc;margin-left:8px;'>Toured</span>"

    items_html = []
    for r in deduped:
        url = (r.get("url") or "").strip()
        addr = (r.get("address") or "").strip() or address_text_from_url(url) or "Listing"
        sent_at = _fmt_ts(r.get("sent_at") or "")
        camp = (r.get("campaign") or "").strip()

        # Toured badge via slug intersection
        slug = _slug_addr(addr or address_text_from_url(url))
        toured = slug in tour_slugs

        # Meta line: date • time  |  campaign  |  Toured
        meta_bits = []
        if sent_at:
            meta_bits.append(f"<span class='report-meta'>{escape(sent_at)}</span>")
        if camp:
            meta_bits.append(_chip(camp))
        if toured:
            meta_bits.append(_badge_toured())

        items_html.append(
            f"""<li class="report-item">
                   <a href="{escape(url)}" target="_blank" rel="noopener">{escape(addr)}</a>
                   {' '.join(meta_bits)}
                </li>"""
        )

    if not items_html:
        st.warning("No results returned.")
        return

    st.markdown("<ul class='link-list'>" + "\n".join(items_html) + "</ul>", unsafe_allow_html=True)

    # Export
    with st.expander("Export filtered (deduped)"):
        import pandas as pd
        buf = io.StringIO()
        pd.DataFrame(deduped).to_csv(buf, index=False)
        st.download_button(
            "Download CSV",
            data=buf.getvalue(),
            file_name=f"client_report_{client_norm}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=False
        )

# ======================
# Public entry point
# ======================
def render_clients_tab():
    st.subheader("Clients")
    st.caption("Use ▦ to open an inline report; ✎ rename; ⟳ toggle active; ⌫ delete.")

    report_norm_qp = _qp_get("report", "")
    want_scroll = _qp_get("scroll", "") in ("1","true","yes")

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

    # Inline report section
    st.markdown('<div id="report_anchor"></div>', unsafe_allow_html=True)
    if report_norm_qp:
        display_name = next((c["name"] for c in all_clients if c.get("name_norm")==report_norm_qp), report_norm_qp)
        st.markdown("---")
        _render_client_report_view(display_name, report_norm_qp)

        # Smooth scroll without switching tabs
        if want_scroll:
            st.components.v1.html(
                """
                <script>
                  const el = parent.document.getElementById("report_anchor");
                  if (el) { el.scrollIntoView({behavior: "smooth", block: "start"}); }
                </script>
                """, height=0
            )
            # keep `report` but clear `scroll` so it doesn't keep jumping on every rerun
            _qp_set(report=report_norm_qp)
