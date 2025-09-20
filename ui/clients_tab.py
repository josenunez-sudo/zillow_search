# ui/clients_tab.py
import os, re, io
from datetime import datetime
from html import escape
from typing import List, Dict, Any, Optional
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

def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

# =========================
# STRICT address slug (shared)
# =========================
_DIR_MAP = {
    "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest"
}
_TYPE_MAP = {
    "st": "street", "street": "street",
    "ave": "avenue", "av": "avenue", "avenue": "avenue",
    "dr": "drive", "drive": "drive",
    "ln": "lane", "lane": "lane",
    "rd": "road", "road": "road",
    "blvd": "boulevard", "boulevard": "boulevard",
    "ct": "court", "court": "court",
    "pl": "place", "place": "place",
    "ter": "terrace", "terrace": "terrace",
    "way": "way",
    "cir": "circle", "circle": "circle",
    "pkwy": "parkway", "parkway": "parkway",
    "hwy": "highway", "highway": "highway",
    "sq": "square", "square": "square",
    "trl": "trail", "trail": "trail",
}

_WORD_RE = re.compile(r"[A-Za-z0-9']+")

def _canonical_words(addr: str) -> List[str]:
    """
    Lowercase, strip punctuation, normalize directionals and street types.
    Keep only alnum words (drop commas/periods). Merge multiple whitespace.
    """
    s = (addr or "").strip().lower()
    # Replace separators with spaces
    s = re.sub(r"[,/]+", " ", s)
    s = re.sub(r"[\-]+", " ", s)  # treat hyphen as separator for words
    s = re.sub(r"\s+", " ", s).strip()

    words: List[str] = []
    for m in _WORD_RE.finditer(s):
        w = m.group(0)

        # expand directionals
        if w in _DIR_MAP:
            w = _DIR_MAP[w]

        # normalize street type (only if it matches exactly a type token)
        if w in _TYPE_MAP:
            w = _TYPE_MAP[w]

        words.append(w)
    return words

def _slug_addr_strict(addr: str) -> str:
    words = _canonical_words(addr)
    return "-".join(words)

# Minimal previous slugger (kept only for fallback use if needed)
def _slug_addr_loose(addr: str) -> str:
    a = (addr or "").strip().lower()
    a = re.sub(r"[^\w\s,-]", "", a).replace(",", "")
    return re.sub(r"\s+", "-", a).strip("-")

# =========================
# URL/ZPID helpers
# =========================
_ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)

def _zpid_from_url(url: str) -> str:
    m = _ZPID_RE.search(url or "")
    return (m.group(1) if m else "").strip()

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

# Pretty timestamp in America/New_York
def _pretty_sent_at(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            pass
        out = dt.strftime("%b %d, %Y • %I:%M %p")
        out = re.sub(r"\b0(\d:)", r"\1", out)  # strip leading zero in hour
        return out
    except Exception:
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
    Returns {strict_slug: count_across_all_tours} using STRICT slugger,
    ignoring DB's stored slug to avoid historical inconsistencies.
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
        # Pull address text; recompute strict slug here
        sq = SUPABASE.table("tour_stops").select("address").in_("tour_id", ids).limit(50000).execute()
        rows = sq.data or []
        for r in rows:
            addr = (r.get("address") or "").strip()
            if not addr: 
                continue
            slug = _slug_addr_strict(addr)
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
            st.session_state["__active_tab__"] = "Clients"   # keep focus on Clients tab
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

    # Campaign picklist
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

    # Cross-check: toured slugs using STRICT slugger
    toured_slugs = fetch_tour_address_slugs(client_norm)  # dict strict_slug -> count
    toured_set = set(toured_slugs.keys())

    # Filter by campaign/search
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
    seen_canon: set = set()
    seen_zpid:  set = set()
    seen_slug:  set = set()
    unique_rows: List[Dict[str, Any]] = []

    for r in rows_f:  # newest-first from DB
        canon = (r.get("canonical") or "").strip().lower()
        zpid  = (r.get("zpid") or "").strip()
        url   = (r.get("url") or "").strip()
        addr  = (r.get("address") or "").strip()

        if not zpid:
            zpid = _zpid_from_url(url)
        if not addr:
            addr = address_text_from_url(url)

        strict_slug = _slug_addr_strict(addr)
        loose_slug  = _slug_addr_loose(addr)  # extra safety for old rows

        # Drop if any identity already seen
        if canon and canon in seen_canon:   continue
        if zpid and zpid in seen_zpid:      continue
        if strict_slug and strict_slug in seen_slug: continue
        if loose_slug and loose_slug in seen_slug:   continue

        unique_rows.append(r)

        # Mark identities
        if canon: seen_canon.add(canon)
        if zpid:  seen_zpid.add(zpid)
        if strict_slug: seen_slug.add(strict_slug)
        if loose_slug:  seen_slug.add(loose_slug)

    count = len(unique_rows)
    st.caption(f"{count} unique listing{'s' if count!=1 else ''}")

    if not unique_rows:
        st.info("No results match the current filters.")
        return

    # Render results with TOURED badge
    items_html = []
    for r in unique_rows:
        url = (r.get("url") or "").strip()
        addr = (r.get("address") or "").strip()
        if not addr:
            addr = address_text_from_url(url) or "Listing"

        slug = _slug_addr_strict(addr)
        toured = slug in toured_set
        toured_count = toured_slugs.get(slug, 0)

        sent_at = _pretty_sent_at(r.get("sent_at") or "")
        camp = (r.get("campaign") or "").strip()

        camp_chip = ""
        if sel_campaign is None and camp:
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
        df = pd.DataFrame(unique_rows)
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

    # Report section
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