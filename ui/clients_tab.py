# ui/clients_tab.py
import os, re, io
from datetime import datetime
from html import escape
from typing import List, Dict, Any, Optional, Tuple, OrderedDict
from urllib.parse import unquote

import streamlit as st
from supabase import create_client, Client

# =========================
# Supabase
# =========================
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

# =========================
# Helpers
# =========================
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _slug_simple(s: str) -> str:
    a = (s or "").strip().lower()
    a = re.sub(r"[^\w\s,-]", "", a).replace(",", "")
    return re.sub(r"\s+", "-", a).strip("-")

# Robust address normalizer so "St" == "Street", directions unify, etc.
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
    "way": "way",
    "pkwy": "parkway", "parkway": "parkway",
    "cir": "circle", "circle": "circle",
    "hwy": "highway", "highway": "highway",
}
DIR_MAP = {"n":"north","s":"south","e":"east","w":"west","north":"north","south":"south","east":"east","west":"west"}

def _normalize_addr_for_slug(addr: str) -> str:
    """
    Collapse common differences: "St" vs "Street", "E" vs "East", punctuation/case.
    Also normalizes "NC" vs "Nc" etc. End result is slug-safe.
    """
    s = (addr or "").strip().lower()
    s = unquote(s)
    s = re.sub(r"[^\w\s,-]", " ", s)  # drop punctuation except , and -
    s = re.sub(r"\s+", " ", s).strip()

    # token-by-token normalization
    tokens = s.replace(",", " ").split()
    out = []
    for tok in tokens:
        if tok in DIR_MAP:
            out.append(DIR_MAP[tok])
        elif tok in SUFFIX_MAP:
            out.append(SUFFIX_MAP[tok])
        else:
            out.append(tok)
    norm = " ".join(out)

    # collapse multiple spaces and hyphenate
    norm = re.sub(r"\s+", " ", norm).strip()
    return _slug_simple(norm)

def address_text_from_url(url: str) -> str:
    """Human title from Zillow URL when DB 'address' missing."""
    if not url: return ""
    u = unquote(url)
    m = re.search(r"/homedetails/([^/]+)/\d{6,}_zpid/", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    m = re.search(r"/homes/([^/_]+)_rb/?", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    return ""

# Pretty timestamp in America/New_York
def _pretty_sent_at(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        # Python can parse '...+00:00' and '...Z' (replace Z)
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        try:
            # Convert to ET if zoneinfo exists
            from zoneinfo import ZoneInfo  # py>=3.9
            dt_local = dt.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            dt_local = dt  # fallback: leave as-is
        return dt_local.strftime("%b %-d, %Y • %-I:%M %p")
    except Exception:
        # Fallback: show date only
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", iso_str)
        return (m.group(1) if m else iso_str)

# =========================
# Clients CRUD (minimal)
# =========================
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
    try:
        fetch_clients.clear()  # type: ignore[attr-defined]
    except Exception:
        pass

def toggle_client_active(client_id: int, new_active: bool):
    if not _sb_ok() or not client_id: return False, "Not configured"
    try:
        SUPABASE.table("clients").update({"active": new_active}).eq("id", client_id).execute()
        _invalidate_clients_cache()
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

# =========================
# Query params + rerun
# =========================
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

# =========================
# Sent + Tours data
# =========================
@st.cache_data(ttl=120, show_spinner=False)
def fetch_sent_for_client(client_norm: str, limit: int = 5000):
    """
    Returns rows ordered by most recent first.
    Columns: url,address,sent_at,campaign,mls_id,canonical,zpid
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

@st.cache_data(ttl=120, show_spinner=False)
def fetch_tour_address_slugs(client_norm: str) -> Dict[str, int]:
    """
    Returns a dict {address_slug: count_of_occurrences_in_all_tours} for the client.
    Used to tag sent listings that were toured.
    """
    out: Dict[str, int] = {}
    if not (_sb_ok() and client_norm.strip()):
        return out
    try:
        tq = SUPABASE.table("tours").select("id").eq("client", client_norm.strip()).limit(5000).execute()
        tours = tq.data or []
        if not tours: return out
        ids = [t["id"] for t in tours]
        if not ids: return out
        sq = SUPABASE.table("tour_stops").select("address,address_slug").in_("tour_id", ids).limit(50000).execute()
        rows = sq.data or []
        for r in rows:
            addr = (r.get("address") or "").strip()
            slug = (r.get("address_slug") or _normalize_addr_for_slug(addr)).strip()
            if slug:
                out[slug] = out.get(slug, 0) + 1
        return out
    except Exception:
        return out

# =========================
# UI rows
# =========================
def _client_row(name: str, norm: str, cid: int, active: bool):
    col_name, col_rep, col_ren, col_tog, col_del, col_sp = st.columns([8, 1, 1, 1, 1, 2])

    with col_name:
        st.markdown(
            f"<span style='font-weight:700;'>{escape(name)}</span> "
            f"<span style='font-size:11px;font-weight:800;padding:2px 10px;border-radius:999px;"
            f"background:{('#dcfce7' if active else '#e2e8f0')};"
            f"color:{('#166534' if active else '#334155')};"
            f"border:1px solid rgba(0,0,0,.08);'>"
            f"{'active' if active else 'inactive'}</span>",
            unsafe_allow_html=True
        )

    with col_rep:
        if st.button("▦", key=f"rep_{cid}", help="Open report"):
            st.session_state["__active_tab__"] = "Clients"   # stop tab flicker
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
            st.session_state["__active_tab__"] = "Clients"
            _safe_rerun()

    with col_del:
        if st.button("⌫", key=f"del_{cid}", help="Delete"):
            st.session_state[f"__del_{cid}"] = True

    # Inline rename
    if st.session_state.get(f"__edit_{cid}"):
        st.markdown("<div style='margin-top:6px;padding:6px;border:1px dashed #e2e8f0;border-radius:8px;'>", unsafe_allow_html=True)
        new_name = st.text_input("New name", value=name, key=f"rn_val_{cid}")
        c1, c2 = st.columns([0.25, 0.25])
        if c1.button("Save", key=f"rn_save_{cid}"):
            ok, msg = rename_client(cid, new_name)
            if not ok: st.warning(msg)
            st.session_state[f"__edit_{cid}"] = False
            st.session_state["__active_tab__"] = "Clients"
            _safe_rerun()
        if c2.button("Cancel", key=f"rn_cancel_{cid}"):
            st.session_state[f"__edit_{cid}"] = False
        st.markdown("</div>", unsafe_allow_html=True)

    # Inline delete confirm
    if st.session_state.get(f"__del_{cid}"):
        st.markdown("<div style='margin-top:6px;padding:6px;border:1px dashed #e2e8f0;border-radius:8px;'>", unsafe_allow_html=True)
        d1, d2 = st.columns([0.25, 0.25])
        if d1.button("Confirm delete", key=f"del_yes_{cid}"):
            delete_client(cid)
            st.session_state[f"__del_{cid}"] = False
            st.session_state["__active_tab__"] = "Clients"
            _safe_rerun()
        if d2.button("Cancel", key=f"del_no_{cid}"):
            st.session_state[f"__del_{cid}"] = False
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div style='border-bottom:1px solid #e2e8f0; margin:4px 0 2px 0;'></div>", unsafe_allow_html=True)

# ===== Report =====
def _render_client_report_view(client_display_name: str, client_norm: str):
    st.markdown(f"### Report for {escape(client_display_name)}", unsafe_allow_html=True)

    colX, _ = st.columns([1,3])
    with colX:
        if st.button("Close report", key=f"__close_report_{client_norm}"):
            st.session_state["__active_tab__"] = "Clients"
            _qp_set()  # clear query params
            _safe_rerun()

    # Base rows (most recent first)
    rows = fetch_sent_for_client(client_norm)
    total = len(rows)

    # Campaign list
    seen_camps = []
    for r in rows:
        c = (r.get("campaign") or "").strip()
        if c not in seen_camps:
            seen_camps.append(c)
    campaign_labels = ["All campaigns"] + [("— no campaign —" if c == "" else c) for c in seen_camps]
    campaign_keys   = [None] + seen_camps

    colF1, colF2, colF3 = st.columns([1.2, 1.8, 1])
    with colF1:
        sel_idx = st.selectbox("Filter by campaign", list(range(len(campaign_labels))),
                               format_func=lambda i: campaign_labels[i], index=0, key=f"__camp_{client_norm}")
        sel_campaign = campaign_keys[sel_idx]
    with colF2:
        q = st.text_input("Search address / MLS / URL", value="", placeholder="e.g. 407 Woodall, 2501234, /homedetails/", key=f"__q_{client_norm}")
        q_norm = (q or "").strip().lower()
    with colF3:
        st.caption(f"{total} total logged")

    # Cross-check: toured slugs
    toured_slugs = fetch_tour_address_slugs(client_norm)  # dict slug -> count
    toured_set = set(toured_slugs.keys())

    # Filter rows by campaign/search
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

    # ======= HARD DEDUPE (no repeats ever) =======
    # key priority: canonical → zpid → normalized address slug (robust)
    def _dedupe_key(r: Dict[str, Any]) -> str:
        canon = (r.get("canonical") or "").strip().lower()
        if canon:
            return "c:" + canon
        zpid = (r.get("zpid") or "").strip().lower()
        if zpid:
            return "z:" + zpid
        # derive address text then normalized slug
        addr = (r.get("address") or "").strip()
        if not addr:
            addr = address_text_from_url((r.get("url") or "").strip())
        slug = _normalize_addr_for_slug(addr)
        return "a:" + slug

    deduped: Dict[str, Dict[str, Any]] = {}
    # rows_f is already newest-first; keep the FIRST we see for each key
    for r in rows_f:
        k = _dedupe_key(r)
        if k not in deduped:
            deduped[k] = r
    rows_u = list(deduped.values())

    count = len(rows_u)
    st.caption(f"{count} unique listing{'s' if count!=1 else ''}")

    if not rows_u:
        st.info("No results match the current filters.")
        return

    # Render
    items_html = []
    for r in rows_u:
        url = (r.get("url") or "").strip()
        addr = (r.get("address") or "").strip()
        if not addr:
            addr = address_text_from_url(url) or "Listing"

        slug = _normalize_addr_for_slug(addr)
        toured = slug in toured_set
        toured_count = toured_slugs.get(slug, 0)

        sent_at = _pretty_sent_at(r.get("sent_at") or "")
        camp = (r.get("campaign") or "").strip()

        camp_chip = ""
        if sel_campaign is None and camp:
            # Show campaign chip (more subtle) only in "All campaigns"
            camp_chip = (
                f"<span style='font-size:11px;font-weight:700;padding:2px 6px;border-radius:999px;"
                f"background:#e2e8f0;margin-left:6px;'>{escape(camp)}</span>"
            )

        toured_chip = ""
        if toured:
            toured_chip = (
                "<span title='This address appears in the Tours report' "
                "style='font-size:11px;font-weight:800;padding:2px 8px;border-radius:999px;"
                "background:#dcfce7;color:#166534;border:1px solid rgba(5,150,105,.35);"
                "margin-left:6px;'>TOURED"
                + (f" ×{toured_count}" if toured_count > 1 else "") +
                "</span>"
            )

        time_chip = f"<span style='color:#64748b;font-size:12px;margin-left:6px;'>{escape(sent_at)}</span>" if sent_at else ""

        items_html.append(
            f"""<li style="margin:0.25rem 0;">
                  <a href="{escape(url)}" target="_blank" rel="noopener">{escape(addr)}</a>
                  {toured_chip}{time_chip}{camp_chip}
                </li>"""
        )

    st.markdown(
        "<ul style='margin:0 0 .5rem 1.2rem; padding:0; list-style:disc;'>"
        + "\n".join(items_html) +
        "</ul>",
        unsafe_allow_html=True
    )

    # Export filtered+deduped
    with st.expander("Export filtered report"):
        import pandas as pd
        df = pd.DataFrame(rows_u)
        buf = io.StringIO(); df.to_csv(buf, index=False)
        st.download_button(
            "Download CSV (unique only)",
            data=buf.getvalue(),
            file_name=f"client_report_{client_norm}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=False
        )

# =========================
# Public entry
# =========================
def render_clients_tab():
    st.subheader("Clients")
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

    # Report section at bottom
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
                """,
                height=0
            )
            st.session_state["__active_tab__"] = "Clients"
            _qp_set(report=report_norm_qp)
