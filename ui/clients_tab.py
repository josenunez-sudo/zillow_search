# ui/clients_tab.py
import os, re, json
from datetime import datetime
from html import escape
from typing import List, Dict, Any, Optional, Tuple

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

def _slug_addr(addr: str) -> str:
    a = (addr or "").strip().lower()
    a = re.sub(r"[^\w\s,-]", "", a).replace(",", " ")
    a = re.sub(r"\s+", " ", a).strip()
    return re.sub(r"\s+", "-", a)

# ---------------- Canonicalizers ----------------
ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)

def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    """Return (canonical_url, zpid) if present."""
    if not url:
        return "", None
    base = re.sub(r'[?#].*$', '', url.strip())
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', base, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(base)
    return canon, (m_z.group(1) if m_z else None)

# Strong address normalizer → consistent key across "St/Street", "E/East", etc.
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

def _normalize_address_strong(addr: str) -> str:
    """
    Normalize an address string to a consistent, dedupe-friendly key.
    Handles: casing, punctuation, Street/St, Rd/Road, directions E/East, collapsing whitespace.
    """
    s = (addr or "").strip().lower()
    if not s:
        return ""
    # strip punctuation except spaces/commas
    s = re.sub(r"[^\w\s,]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    # split into tokens, normalize common patterns:
    tokens = s.split()
    out = []
    for t in tokens:
        base = t.strip(", ")
        # Expand directional tokens
        if base in DIR_MAP:
            out.append(DIR_MAP[base]); continue
        # Expand suffix tokens
        if base in SUFFIX_MAP:
            out.append(SUFFIX_MAP[base]); continue
        out.append(base)

    # example unify "e" before street names like "e woodall" already handled by DIR_MAP above
    s2 = " ".join(out)

    # collapse multiple commas/spaces
    s2 = re.sub(r"\s*,\s*", ", ", s2)
    s2 = re.sub(r"\s+", " ", s2).strip()

    # create a stable slug (keeps numbers and words)
    return _slug_addr(s2)

def _key_for_sent_row(row: Dict[str, Any]) -> str:
    """
    Choose a single key per property:
    1) zpid if present
    2) canonical Zillow homedetails URL if present
    3) strong-normalized address slug
    """
    url = (row.get("url") or "").strip()
    address = (row.get("address") or "").strip()
    canonical = (row.get("canonical") or "").strip()
    zpid = (row.get("zpid") or "").strip()

    if zpid:
        return f"zpid:{zpid}"

    # If we have a proper homedetails canonical, prefer that
    can2, z2 = canonicalize_zillow(canonical or url)
    if z2:
        return f"zpid:{z2}"
    if "/homedetails/" in (can2 or ""):
        return f"canon:{can2.lower()}"

    # else normalized address slug
    norm = _normalize_address_strong(address) or _normalize_address_strong(_address_from_url_fallback(url))
    return f"addr:{norm}"

def _address_from_url_fallback(url: str) -> str:
    if not url:
        return ""
    u = re.sub(r"%2C", ",", url, flags=re.I)
    m = re.search(r"/homedetails/([^/]+)/\d{6,}_zpid/", u, re.I)
    if m:
        return m.group(1).replace("-", " ")
    m = re.search(r"/homes/([^/_]+)_rb/?", u, re.I)
    if m:
        return m.group(1).replace("-", " ")
    return ""

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
    """
    Pull all tour stop addresses for this client (across all dates)
    and return raw address strings.
    """
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
    """Return a set of property keys (same scheme as _key_for_sent_row) that were toured."""
    addrs = fetch_all_tour_addresses_for_client(client_norm)
    ks = set()
    for a in addrs:
        k = f"addr:{_normalize_address_strong(a)}"
        if k and k not in ks:
            ks.add(k)
    return ks

# ---------------- UI helpers ----------------
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

# Badge
def _toured_badge():
    return "<span style='font-size:11px;font-weight:800;padding:2px 8px;border-radius:999px;background:#0b7f48;color:#ecfdf5;border:1px solid #16a34a;margin-left:6px;'>Toured</span>"

def _campaign_chip(text: str):
    if not text:
        return ""
    return f"<span style='font-size:11px;font-weight:700;padding:2px 6px;border-radius:999px;background:#e2e8f0;margin-left:6px;'>{escape(text)}</span>"

# ---------------- Report builder (deduped) ----------------
def _dedupe_and_tag_rows(rows: List[Dict[str, Any]], toured_keys: set,
                         sel_campaign: Optional[str], q_norm: str) -> List[Dict[str, Any]]:
    """
    Filter (campaign/search), then group by property key and keep the MOST RECENT sent_at.
    Tag row['is_toured'] if key is in toured_keys (address-normalized).
    """
    # Filter by campaign + search first
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

    # Group by property key; keep the most recent sent_at
    groups: Dict[str, Dict[str, Any]] = {}
    for r in filt:
        key = _key_for_sent_row(r)
        if not key:
            # fallback to row index-based unique key to avoid crash (won't dedupe)
            key = f"row:{id(r)}"
        # compute "is_toured" using addr-key form
        addr_key = key
        if key.startswith("zpid:") or key.startswith("canon:"):
            # Build addr-based key too if we can
            addr_key = f"addr:{_normalize_address_strong(r.get('address') or _address_from_url_fallback(r.get('url') or ''))}"
        r["_key"] = key
        r["is_toured"] = addr_key in toured_keys

        # parse sent_at for recency compare (default to very old if parsing fails)
        sa = r.get("sent_at") or ""
        try:
            # Supabase returns ISO with timezone; datetime.fromisoformat handles offset in Py3.11
            when = datetime.fromisoformat(sa.replace("Z", "+00:00"))
        except Exception:
            when = datetime.min

        prev = groups.get(key)
        if (prev is None) or (when > prev["_when"]):
            r["_when"] = when
            groups[key] = r

    # Emit one row per property (sorted newest first)
    out = sorted(groups.values(), key=lambda x: x.get("_when", datetime.min), reverse=True)
    for r in out:
        r.pop("_when", None)
        r.pop("_key", None)
    return out

def _address_text(row: Dict[str, Any]) -> str:
    addr = (row.get("address") or "").strip()
    if addr:
        return addr
    # fallback from url slug
    u = (row.get("url") or "").strip()
    a2 = _address_from_url_fallback(u).replace("-", " ").strip().title()
    return a2 or "Listing"

def _format_sent_at(ts: str) -> str:
    # show like "Sep 20, 2025 • 06:48 PM"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y • %I:%M %p")
    except Exception:
        return ts or ""

def _render_client_report_view(client_display_name: str, client_norm: str):
    st.markdown(f"### Report for {escape(client_display_name)}", unsafe_allow_html=True)

    # Close button
    colX, _ = st.columns([1,3])
    with colX:
        if st.button("Close report", key=f"__close_report_{client_norm}"):
            _stay_on_clients()
            _qp_set()  # clear query params
            _safe_rerun()

    rows = fetch_sent_for_client(client_norm)
    total = len(rows)

    # Campaign select options
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

    # Build toured keyset (addr-based)
    toured_keys = _build_toured_keyset(client_norm)

    # Dedupe & tag
    rows_u = _dedupe_and_tag_rows(rows, toured_keys, sel_campaign, q_norm)
    count = len(rows_u)

    st.caption(f"{count} unique listing{'s' if count != 1 else ''} (deduped by property)")

    if not rows_u:
        st.info("No results match the current filters.")
        return

    # Render list
    items_html = []
    for r in rows_u:
        url = (r.get("url") or "").strip()
        addr_txt = _address_text(r)
        sent_at_h = _format_sent_at(r.get("sent_at") or "")
        camp = (r.get("campaign") or "").strip()

        chip = "" if sel_campaign is not None else _campaign_chip(camp)
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
    # Inline row with "report" icon; ensure we stay on Clients tab before rerun
    col_name, col_rep = st.columns([10, 1])

    with col_name:
        status = "active" if active else "inactive"
        pill = f"<span class='pill active'>active</span>" if active else "<span class='pill'>inactive</span>"
        st.markdown(f"<span class='client-name'>{escape(name)}</span> {pill}", unsafe_allow_html=True)

    with col_rep:
        if st.button("▦", key=f"rep_{cid}", help="Open report"):
            _stay_on_clients()
            _qp_set(report=norm, scroll="1")
            _safe_rerun()

    # subtle divider
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
            # Keep report param but drop scroll so we don't auto-scroll every rerun
            _stay_on_clients()
            st.experimental_set_query_params(**{"report": report_norm_qp})
