# ui/clients_tab.py
import os, re
from datetime import datetime
from html import escape
from typing import List, Dict, Any, Optional

import streamlit as st
from supabase import create_client, Client

# ---------------- Base wiring ----------------
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

def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

def _stay_on_clients():
    st.session_state["__active_tab__"] = "Clients"

def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

# ---------------- Address normalization (dedupe key) ----------------
SUFFIX_MAP = {
    "st": "street", "street": "street",
    "rd": "road", "road": "road",
    "ave": "avenue", "av": "avenue", "avenue": "avenue",
    "blvd": "boulevard", "boulevard": "boulevard",
    "dr": "drive", "drive": "drive",
    "ln": "lane", "lane": "lane",
    "ct": "court", "court": "court",
    "pl": "place", "place": "place",
    "ter": "terrace", "terrace": "terrace",
    "hwy": "highway", "highway": "highway",
    "cir": "circle", "circle": "circle",
    "pkwy": "parkway", "parkway": "parkway",
    "sq": "square", "square": "square",
}
DIR_MAP = {
    "n": "north", "s": "south", "e": "east", "w": "west",
    "north": "north", "south": "south", "east": "east", "west": "west"
}

def _slug_addr(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w\s,/-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"[\s,/]+", "-", s)

def _normalize_address_strong(addr: str) -> str:
    """
    Normalize an address to a single, dedupe-friendly key:
      - lowercase, remove punctuation noise
      - expand directions (E->east) and suffixes (st->street)
      - keep city, state, zip if present
    """
    s = (addr or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"[^\w\s,/-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    toks = s.replace(",", " , ").split()
    out = []
    for t in toks:
        if t == ",":
            out.append(",")
            continue
        if t in DIR_MAP:
            out.append(DIR_MAP[t]); continue
        if t in SUFFIX_MAP:
            out.append(SUFFIX_MAP[t]); continue
        out.append(t)
    # re-join with commas normalized
    s2 = " ".join(out)
    s2 = re.sub(r"\s*,\s*", ", ", s2)
    s2 = re.sub(r"\s+", " ", s2).strip()
    return _slug_addr(s2)

def _address_from_url_fallback(url: str) -> str:
    """
    If DB 'address' is empty, derive an address-like string from a Zillow URL:
      - /homedetails/<slug>/<zpid>/
      - /homes/<slug>_rb/
    """
    u = (url or "")
    m = re.search(r"/homedetails/([^/]+)/\d{6,}_zpid/", u, re.I)
    if m:
        return m.group(1).replace("-", " ")
    m = re.search(r"/homes/([^/_]+)_rb/?", u, re.I)
    if m:
        return m.group(1).replace("-", " ")
    return ""

def _property_key_from_row(row: Dict[str, Any]) -> str:
    """
    NEW: Always dedupe by normalized ADDRESS key (never by raw canonical URL).
    We still parse from URL if the address field is blank.
    This collapses /homes/ variations (st vs street, commas, capitalization, etc).
    """
    addr = (row.get("address") or "").strip()
    if not addr:
        addr = _address_from_url_fallback(row.get("url") or "")
    return _normalize_address_strong(addr)

# ---------------- Data access ----------------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False):
    if not SUPABASE:
        return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

@st.cache_data(ttl=120, show_spinner=False)
def fetch_sent_for_client(client_norm: str, limit: int = 10000) -> List[Dict[str, Any]]:
    if not (SUPABASE and client_norm.strip()):
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
def fetch_all_tour_addresses_for_client(client_norm: str) -> List[str]:
    if not (SUPABASE and client_norm.strip()):
        return []
    try:
        tq = SUPABASE.table("tours").select("id").eq("client", client_norm.strip()).limit(5000).execute()
        tours = tq.data or []
        if not tours:
            return []
        ids = [t["id"] for t in tours]
        if not ids:
            return []
        sq = SUPABASE.table("tour_stops").select("address").in_("tour_id", ids).limit(50000).execute()
        stops = sq.data or []
        return [s.get("address") or "" for s in stops if (s.get("address") or "").strip()]
    except Exception:
        return []

def _build_toured_keyset(client_norm: str) -> set:
    addrs = fetch_all_tour_addresses_for_client(client_norm)
    return { _normalize_address_strong(a) for a in addrs if a.strip() }

# ---------------- Query param helpers (new API only) ----------------
def _qp_get(name: str, default=None):
    try:
        val = st.query_params.get(name, default)
        if isinstance(val, list) and val:
            return val[0]
        return val
    except Exception:
        return st.session_state.get(f"__qp_{name}", default)

def _qp_set(**kwargs):
    try:
        if kwargs:
            st.query_params.update(kwargs)
        else:
            try:
                st.query_params.clear()
            except Exception:
                for k in list(st.session_state.keys()):
                    if k.startswith("__qp_"):
                        del st.session_state[k]
    except Exception:
        for k, v in kwargs.items():
            st.session_state[f"__qp_{k}"] = v

# ---------------- UI bits ----------------
def _toured_badge():
    return "<span style='font-size:11px;font-weight:800;padding:2px 8px;border-radius:999px;background:#0b7f48;color:#ecfdf5;border:1px solid #16a34a;margin-left:6px;'>Toured</span>"

def _campaign_chip(text: str):
    if not text:
        return ""
    return f"<span style='font-size:11px;font-weight:700;padding:2px 6px;border-radius:999px;background:#e2e8f0;margin-left:6px;'>{escape(text)}</span>"

# ---------------- Report builder (hard dedupe by property) ----------------
def _dedupe_and_tag_rows(rows: List[Dict[str, Any]], toured_keys: set,
                         sel_campaign: Optional[str], q_norm: str) -> List[Dict[str, Any]]:
    """
    1) Filter by campaign and search.
    2) Group rows by strong address key ONLY.
    3) Keep the most recent sent_at per property.
    4) Tag 'is_toured' if address key is in toured_keys.
    """
    def _match(row: Dict[str, Any]) -> bool:
        if sel_campaign is not None:
            if (row.get("campaign") or "").strip() != sel_campaign:
                return False
        if q_norm:
            a = (row.get("address") or "").lower()
            m = (row.get("mls_id") or "").lower()
            u = (row.get("url") or "").lower()
            if q_norm not in a and q_norm not in m and q_norm not in u:
                return False
        return True

    filt = [r for r in rows if _match(r)]
    if not filt:
        return []

    groups: Dict[str, Dict[str, Any]] = {}
    for r in filt:
        key = _property_key_from_row(r)
        if not key:
            # If we truly can't make a key, skip row (prevents weird dupes)
            continue

        # parse sent_at
        sa = (r.get("sent_at") or "").strip()
        try:
            when = datetime.fromisoformat(sa.replace("Z", "+00:00"))
        except Exception:
            when = datetime.min

        prev = groups.get(key)
        if prev is None or when > prev["_when"]:
            r["_when"] = when
            r["_key"] = key
            r["is_toured"] = (key in toured_keys)
            groups[key] = r

    out = sorted(groups.values(), key=lambda x: x.get("_when", datetime.min), reverse=True)
    for r in out:
        r.pop("_when", None)
        r.pop("_key", None)
    return out

def _address_text(row: Dict[str, Any]) -> str:
    addr = (row.get("address") or "").strip()
    if addr:
        return addr
    u = (row.get("url") or "").strip()
    a2 = _address_from_url_fallback(u).replace("-", " ").strip().title()
    return a2 or "Listing"

def _format_sent_at(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y • %I:%M %p")
    except Exception:
        return ts or ""

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

    seen_campaigns = []
    for r in rows:
        c = (r.get("campaign") or "").strip()
        if c not in seen_campaigns:
            seen_campaigns.append(c)
    labels = ["All campaigns"] + [("— no campaign —" if c == "" else c) for c in seen_campaigns]
    keys   = [None] + seen_campaigns

    colF1, colF2, colF3 = st.columns([1.2, 1.8, 1])
    with colF1:
        idx = st.selectbox("Filter by campaign", list(range(len(labels))),
                           format_func=lambda i: labels[i], index=0, key=f"__camp_{client_norm}")
        sel_campaign = keys[idx]
    with colF2:
        q = st.text_input("Search address / MLS / URL", value="", placeholder="e.g. 407 Woodall, 2501234, /homedetails/", key=f"__q_{client_norm}")
        q_norm = (q or "").strip().lower()
    with colF3:
        st.caption(f"{total} total logged")

    toured_keys = _build_toured_keyset(client_norm)
    rows_u = _dedupe_and_tag_rows(rows, toured_keys, sel_campaign, q_norm)
    count = len(rows_u)

    st.caption(f"{count} unique listing{'s' if count != 1 else ''} (deduped by property)")

    if not rows_u:
        st.info("No results match the current filters.")
        return

    items_html = []
    for r in rows_u:
        url = (r.get("url") or "").strip()
        addr_txt = _address_text(r)
        sent_at_h = _format_sent_at(r.get("sent_at") or "")
        camp = (r.get("campaign") or "").strip()

        chip = "" if sel_campaign is not None else (_campaign_chip(camp) if camp else "")
        toured = _toured_badge() if r.get("is_toured") else ""

        items_html.append(
            f"""<li style="margin:0.25rem 0;">
                  <a href="{escape(url)}" target="_blank" rel="noopener">{escape(addr_txt)}</a>
                  <span style="color:#64748b; font-size:12px; margin-left:6px;">{escape(sent_at_h)}</span>
                  {chip}{toured}
                </li>"""
        )
    html = "<ul class='link-list'>" + "\n".join(items_html) + "</ul>"
    st.markdown(html, unsafe_allow_html=True)

    # Export filtered unique report
    with st.expander("Export filtered (unique) report"):
        import pandas as pd, io
        df = pd.DataFrame(rows_u)
        csv_buf = io.StringIO()
        df.to_csv(csv_buf, index=False)
        st.download_button(
            "Download CSV",
            data=csv_buf.getvalue(),
            file_name=f"client_report_unique_{client_norm}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=False
        )

# ---------------- Row widget for clients ----------------
def _client_row(name: str, norm: str, cid: int, active: bool):
    col_name, col_rep = st.columns([10, 1])

    with col_name:
        pill = "<span class='pill active'>active</span>" if active else "<span class='pill'>inactive</span>"
        st.markdown(f"<span class='client-name'>{escape(name)}</span> {pill}", unsafe_allow_html=True)

    with col_rep:
        if st.button("▦", key=f"rep_{cid}", help="Open report"):
            _stay_on_clients()
            _qp_set(report=norm, scroll="1")
            _safe_rerun()

    st.markdown("<div style='border-bottom:1px solid rgba(148,163,184,.25); margin:4px 0 2px 0;'></div>", unsafe_allow_html=True)

# ---------------- Main entry ----------------
def render_clients_tab():
    st.markdown("""
    <style>
    .client-name { font-weight:700; color:#111827; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    html[data-theme="dark"] .client-name, .stApp [data-theme="dark"] .client-name { color:#ffffff; }
    .pill { font-size:11px; font-weight:800; padding:2px 10px; border-radius:999px; background:#e2e8f0; color:#0f172a; }
    .pill.active { background:#dcfce7; color:#064e3b; border:1px solid rgba(5,150,105,.35);}
    </style>
    """, unsafe_allow_html=True)

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
                _client_row(c["name"], c.get("name_norm",""), c["id"], active=True)

    with colB:
        st.markdown("### Inactive", unsafe_allow_html=True)
        if not inactive:
            st.write("_No inactive clients_")
        else:
            for c in inactive:
                _client_row(c["name"], c.get("name_norm",""), c["id"], active=False)

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
            # keep report param, drop scroll
            try:
                st.query_params.update({"report": report_norm_qp})
            except Exception:
                st.session_state["__qp_report"] = report_norm_qp
