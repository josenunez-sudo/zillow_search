# ui/clients_tab.py
import os, re
from datetime import datetime
from html import escape
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import unquote

import streamlit as st
from supabase import create_client, Client

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

# ---------- Small utils ----------
def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

def _stay_on_clients():
    # so a rerun re-selects Clients tab (app.py restores the remembered tab)
    st.session_state["__active_tab__"] = "Clients"

def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _clean_canonical(c: Optional[str]) -> Optional[str]:
    if not c: return None
    c = re.sub(r"[?#].*$", "", str(c).strip())
    return c.lower() or None

def _slug_addr(addr: str) -> str:
    a = (addr or "").strip().lower()
    a = re.sub(r"[^\w\s,-]", "", a).replace(",", " ")
    a = re.sub(r"\s+", " ", a)
    # light expansion to reduce near-dupes
    repl = {
        " st ": " street ", " rd ": " road ", " ave ": " avenue ", " blvd ": " boulevard ",
        " dr ": " drive ", " ln ": " lane ", " ct ": " court ", " cir ": " circle ",
        " hwy ": " highway ", " pkway ": " parkway ", " pkwy ": " parkway ", " ter ": " terrace ",
    }
    a = f" {a} "
    for k,v in repl.items(): a = a.replace(k, v)
    a = re.sub(r"\s+", " ", a).strip()
    return re.sub(r"\s+", "-", a)

def _friendly_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y • %I:%M %p")
    except Exception:
        return iso or ""

def _toured_badge(campaign: str) -> str:
    return "<span class='badge new' title='From a tour'>Toured</span>" if (campaign or "").startswith("toured-") else ""

# Pull a readable address from a Zillow URL if DB 'address' missing
def address_text_from_url(url: str) -> str:
    if not url: return ""
    u = unquote(url)
    m = re.search(r"/homedetails/([^/]+)/\d{6,}_zpid/", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    m = re.search(r"/homes/([^/_]+)_rb/?", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    return ""

# zillow canonicalizer used for dedupe fallback
ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)
def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    if not url: return "", None
    base = re.sub(r'[?#].*$', '', url)
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(url)
    return canon, (m_z.group(1) if m_z else None)

# ---------- Supabase helpers ----------
def _sb_ok() -> bool:
    try: return bool(SUPABASE)
    except NameError: return False

@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False):
    if not _sb_ok(): return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active")\
            .order("name", desc=False).execute().data or []
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

def invalidate_clients_cache():
    try: fetch_clients.clear()  # type: ignore[attr-defined]
    except Exception: pass

def toggle_client_active(client_id: int, new_active: bool):
    if not _sb_ok() or not client_id: return False, "Not configured"
    try:
        SUPABASE.table("clients").update({"active": new_active}).eq("id", client_id).execute()
        invalidate_clients_cache()
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
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_client(client_id: int):
    if not _sb_ok() or not client_id: return False, "Not configured"
    try:
        SUPABASE.table("clients").delete().eq("id", client_id).execute()
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ---------- Query params (stable across reruns) ----------
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

# ---------- SENT lookups ----------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_sent_for_client(client_norm: str, limit: int = 10000):
    """
    Fetch sent rows for one client (most recent first).
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

# ---------- UI bits ----------
CLIENT_ROW_CSS = """
<style>
.badge { display:inline-block; font-size:12px; font-weight:800; padding:2px 8px; border-radius:999px; margin-left:8px; }
.badge.new {
  background: linear-gradient(180deg, #dcfce7 0%, #bbf7d0 100%);
  color:#065f46;
  border:1px solid rgba(5,150,105,.35);
  box-shadow: 0 6px 16px rgba(16,185,129,.25), 0 1px 3px rgba(0,0,0,.08);
  text-transform: uppercase;
}
html[data-theme="dark"] .badge.new,
.stApp [data-theme="dark"] .badge.new {
  background: linear-gradient(180deg, #064e3b 0%, #065f46 100%);
  color:#a7f3d0;
  border-color: rgba(167,243,208,.35);
  box-shadow: 0 6px 16px rgba(6,95,70,.45), 0 1px 3px rgba(0,0,0,.35);
}
.client-row { display:flex; align-items:center; justify-content:space-between; padding:10px 8px; border-bottom:1px solid var(--row-border, #e2e8f0); }
.client-left { display:flex; align-items:center; gap:8px; min-width:0; }
.client-name { font-weight:700; color:#ffffff !important; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
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
.inline-panel { margin-top:6px; padding:6px; border:1px dashed var(--row-border, #e2e8f0); border-radius:8px; background:rgba(148,163,184,.08); }
.pill { font-size:11px; font-weight:800; padding:2px 10px; border-radius:999px; }
.pill.active {
  background: linear-gradient(180deg, #dcfce7 0%, #bbf7d0 100%);
  color:#166534; border:1px solid rgba(5,150,105,.35);
}
</style>
"""

def _client_row_icons(name: str, norm: str, cid: int, active: bool):
    st.markdown(CLIENT_ROW_CSS, unsafe_allow_html=True)
    col_name, col_rep, col_ren, col_tog, col_del, col_sp = st.columns([8, 1, 1, 1, 1, 2])

    with col_name:
        st.markdown(
            f"<span class='client-name'>{escape(name)}</span> "
            f"<span class='pill {'active' if active else ''}'>{'active' if active else 'inactive'}</span>",
            unsafe_allow_html=True
        )

    with col_rep:
        if st.button("▦", key=f"rep_{cid}", help="Open report"):
            # Remember Clients tab, set report client, request scroll
            _stay_on_clients()
            _qp_set(report=norm, scroll="1")
            _safe_rerun()

    with col_ren:
        if st.button("✎", key=f"rn_btn_{cid}", help="Rename"):
            st.session_state[f"__edit_{cid}"] = True

    with col_tog:
        if st.button("⟳", key=f"tg_{cid}", help=("Deactivate" if active else "Activate")):
            rows = SUPABASE.table("clients").select("active").eq("id", cid).limit(1).execute().data or []
            cur = rows[0]["active"] if rows else active
            toggle_client_active(cid, (not cur))
            _stay_on_clients()
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
            if not ok: st.warning(msg)
            st.session_state[f"__edit_{cid}"] = False
            _stay_on_clients(); _safe_rerun()
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
            _stay_on_clients(); _safe_rerun()
        if dc2.button("Cancel", key=f"del_no_{cid}"):
            st.session_state[f"__del_{cid}"] = False
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div style='border-bottom:1px solid var(--row-border, #e2e8f0); margin:4px 0 2px 0;'></div>", unsafe_allow_html=True)

# ---------- Dedupe strictly for the Listings Sent report ----------
def _dedupe_rows(rows_f: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Enforce uniqueness: prefer zpid → canonical → url → address slug.
    """
    seen = set()
    out = []
    for r in rows_f:
        zpid = (r.get("zpid") or "").strip()
        canon = _clean_canonical(r.get("canonical"))
        url = (r.get("url") or "").strip()
        addr = (r.get("address") or "").strip()

        # derive canon/zpid from url if missing
        if (not zpid or not canon) and url:
            c2, z2 = canonicalize_zillow(url)
            if not canon: canon = _clean_canonical(c2)
            if not zpid and z2: zpid = z2

        if zpid:
            key = ("zpid", zpid)
        elif canon:
            key = ("canon", canon)
        elif url:
            key = ("url", _clean_canonical(url))
        else:
            key = ("addr", _slug_addr(addr))

        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out

# ---------- REPORT VIEW ----------
def _render_client_report_view(client_display_name: str, client_norm: str):
    st.markdown(f"### Report for {escape(client_display_name)}", unsafe_allow_html=True)

    colX, _ = st.columns([1,3])
    with colX:
        if st.button("Close report", key=f"__close_report_{client_norm}"):
            _stay_on_clients()
            _qp_set()  # clear query params
            _safe_rerun()

    rows = fetch_sent_for_client(client_norm)
    total = len(rows)

    # Build campaign filters
    seen_camps = []
    for r in rows:
        c = (r.get("campaign") or "").strip()
        if c not in seen_camps:
            seen_camps.append(c)
    campaign_labels = ["All campaigns"] + [("— no campaign —" if c == "" else c) for c in seen_camps]
    campaign_keys   = [None] + seen_camps

    colF1, colF2, colF3 = st.columns([1.2, 1.8, 1])
    with colF1:
        sel_idx = st.selectbox("Filter by campaign",
                               list(range(len(campaign_labels))),
                               format_func=lambda i: campaign_labels[i],
                               index=0, key=f"__camp_{client_norm}")
        sel_campaign = campaign_keys[sel_idx]
    with colF2:
        q = st.text_input("Search address / MLS / URL", value="", placeholder="e.g. 407 Woodall, 2501234, /homedetails/", key=f"__q_{client_norm}")
        q_norm = q.strip().lower()
    with colF3:
        st.caption(f"{total} total logged")

    # Filter
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

    rows_f = [r for r in rows if _match(r)]
    rows_f = _dedupe_rows(rows_f)  # <<< hard dedupe before rendering
    count = len(rows_f)

    st.caption(f"{count} unique listing{'s' if count!=1 else ''}")

    if not rows_f:
        st.info("No results match the current filters.")
        return

    # Render list
    items_html = []
    for r in rows_f:
        url = (r.get("url") or "").strip()
        addr = (r.get("address") or "").strip() or address_text_from_url(url) or "Listing"
        sent_at = _friendly_ts(r.get("sent_at") or "")
        camp = (r.get("campaign") or "").strip()
        camp_chip = ""
        if sel_campaign is None and camp:
            camp_chip = f"<span style='font-size:11px; font-weight:700; padding:2px 6px; border-radius:999px; background:#e2e8f0; margin-left:6px;'>{escape(camp)}</span>"
        toured = _toured_badge(camp)

        items_html.append(
            f"""<li>
                  <a href="{escape(url)}" target="_blank" rel="noopener">{escape(addr)}</a>
                  <span style="color:#64748b; font-size:12px; margin-left:6px;">{escape(sent_at)}</span>
                  {camp_chip} {toured}
                </li>"""
        )
    st.markdown("<ul class='link-list'>" + "\n".join(items_html) + "</ul>", unsafe_allow_html=True)

    # Simple export (filtered & deduped)
    import pandas as pd, io
    with st.expander("Export filtered (unique) report"):
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

# ---------- PUBLIC ENTRY ----------
def render_clients_tab():
    st.subheader("Clients")
    st.caption("")

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

    # ---- REPORT SECTION BELOW THE TABLES ----
    st.markdown('<div id="report_anchor"></div>', unsafe_allow_html=True)
    if report_norm_qp:
        display_name = next((c["name"] for c in all_clients if c.get("name_norm")==report_norm_qp), report_norm_qp)
        st.markdown("---")
        _render_client_report_view(display_name, report_norm_qp)
        if want_scroll:
            st.components.v1.html(
                """
                <script>
                  const el = parent.document.getElementById("report_anchor");
                  if (el) { el.scrollIntoView({behavior: "smooth", block: "start"}); }
                </script>
                """, height=0
            )
            # keep the 'report' param but clear scroll so it doesn't jump every rerun
            _qp_set(report=report_norm_qp)
