# ui/clients_tab.py
import os, re, io
from datetime import datetime
from html import escape
from typing import List, Dict, Any, Optional

import streamlit as st

# --- Make supabase import SAFE so the module can import even if package is missing ---
try:
    from supabase import create_client, Client
except Exception:
    create_client = None
    Client = Any  # type: ignore

DEBUG_REPORT = False  # set True if you want to see per-row debug info


# ================= Basics (pure helpers; safe at import time) =================
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _fmt_ts_date_tag(ts: str) -> str:
    """Return YYYYMMDD from epoch/ISO-ish strings; '' if cannot parse."""
    if ts is None or str(ts).strip() == "":
        return ""
    raw = str(ts).strip()

    # epoch seconds/millis
    try:
        if raw.isdigit():
            val = int(raw)
            if val > 10_000_000_000:  # ms
                d = datetime.utcfromtimestamp(val / 1000.0)
            else:
                d = datetime.utcfromtimestamp(val)
            return d.strftime("%Y%m%d")
    except Exception:
        pass

    # ISO
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return d.strftime("%Y%m%d")
    except Exception:
        return ""

def _pick_timestamp_date_tag(row: Dict[str, Any]) -> str:
    """
    Find first timestamp-like field and convert to YYYYMMDD.
    If none found, try campaign if it looks like YYYYMMDD.
    """
    candidates = [
        "sent_at", "sentAt",
        "created_at", "createdAt",
        "inserted_at", "insertedAt",
        "updated_at", "updatedAt",
        "ts", "timestamp"
    ]
    for k in candidates:
        tag = _fmt_ts_date_tag(row.get(k))
        if tag:
            return tag

    # fallback: campaign that is a pure 8-digit date
    camp = (row.get("campaign") or "").strip()
    if re.fullmatch(r"\d{8}", camp):
        return camp

    return ""

# ------------- Property slug normalization (strong & symmetric) -------------
_STTYPE = {
    "street":"st","st":"st","st.":"st",
    "avenue":"ave","ave":"ave","ave.":"ave","av":"ave",
    "road":"rd","rd":"rd","rd.":"rd",
    "drive":"dr","dr":"dr","dr.":"dr",
    "lane":"ln","ln":"ln","ln.":"ln",
    "boulevard":"blvd","blvd":"blvd","blvd.":"blvd",
    "court":"ct","ct":"ct","ct.":"ct",
    "place":"pl","pl":"pl","pl.":"pl",
    "terrace":"ter","ter":"ter","ter.":"ter",
    "highway":"hwy","hwy":"hwy",
    "parkway":"pkwy","pkwy":"pkwy",
    "circle":"cir","cir":"cir",
    "square":"sq","sq":"sq",
}
_DIR = {"north":"n","n":"n","south":"s","s":"s","east":"e","e":"e","west":"w","w":"w","n.":"n","s.":"s","e.":"e","w.":"w"}

def _token_norm(tok: str) -> str:
    t = tok.lower().strip(" .,#")
    if t in _STTYPE: return _STTYPE[t]
    if t in _DIR:    return _DIR[t]
    if t in {"apt","unit","ste","lot"}: return ""
    return re.sub(r"[^a-z0-9-]", "", t)

def _norm_slug_from_text(text: str) -> str:
    s = (text or "").lower().replace("&", " and ")
    toks = re.split(r"[^a-z0-9]+", s)
    norm = [t for t in (_token_norm(t) for t in toks) if t]
    return "-".join(norm)

_RE_HD = re.compile(r"/homedetails/([^/]+)/\d{6,}_zpid/?", re.I)
_RE_HM = re.compile(r"/homes/([^/_]+)_rb/?", re.I)

def _norm_slug_from_url(url: str) -> str:
    u = (url or "").strip()
    m = _RE_HD.search(u)
    if m: return _norm_slug_from_text(m.group(1))
    m = _RE_HM.search(u)
    if m: return _norm_slug_from_text(m.group(1))
    return ""

def _address_text_from_url(url: str) -> str:
    u = (url or "").strip()
    m = _RE_HD.search(u)
    if m: return re.sub(r"[-+]", " ", m.group(1)).title()
    m = _RE_HM.search(u)
    if m: return re.sub(r"[-+]", " ", m.group(1)).title()
    return ""

def _property_key(row: Dict[str, Any]) -> str:
    url = (row.get("url") or "").strip()
    addr = (row.get("address") or "").strip() or _address_text_from_url(url)
    norm_pslug = _norm_slug_from_url(url) or _norm_slug_from_text(addr)
    if norm_pslug:
        return "normslug::" + norm_pslug
    canon = (row.get("canonical") or "").strip().lower()
    if canon: return "canon::" + canon
    zpid = (row.get("zpid") or "").strip()
    if zpid: return "zpid::" + zpid
    return "url::" + (url.lower())

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
        if kwargs: st.query_params.update(kwargs)
        else:      st.query_params.clear()
    except Exception:
        if kwargs: st.experimental_set_query_params(**kwargs)
        else:      st.experimental_set_query_params()

def _dedupe_by_property(rows: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    def ts(row):
        try: return datetime.fromisoformat((row.get("sent_at") or "").replace("Z","+00:00"))
        except Exception: return datetime.min
    best: Dict[str, Dict[str,Any]] = {}
    for r in rows:
        key = _property_key(r)
        if key not in best or ts(r) > ts(best[key]):
            best[key] = r
    return list(best.values())


# ================= Lazy Streamlit bits (run-time only) =================
def _inject_css_once():
    if st.session_state.get("__clients_css_injected__"):
        return
    st.session_state["__clients_css_injected__"] = True
    st.markdown("""
    <style>
    :root { --row-border:#e2e8f0; --ink:#0f172a; --muted:#475569; }
    html[data-theme="dark"], .stApp [data-theme="dark"] {
      --row-border:#0b1220; --ink:#f8fafc; --muted:#cbd5e1;
    }
    .client-row { display:flex; align-items:center; justify-content:space-between; padding:10px 8px; border-bottom:1px solid var(--row-border); }
    .client-left { display:flex; align-items:center; gap:8px; min-width:0; }
    .client-name { font-weight:700; color:var(--ink); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }

    .pill {
      font-size:11px; font-weight:800; padding:2px 10px; border-radius:999px;
      background:#f1f5f9; color:#0f172a; border:1px solid #cbd5e1; display:inline-block;
    }
    html[data-theme="dark"] .pill { background:#111827; color:#e5e7eb; border-color:#374151; }
    .pill.active {
      background: linear-gradient(180deg, #dcfce7 0%, #bbf7d0 100%);
      color:#166534; border:1px solid rgba(5,150,105,.35);
    }
    html[data-theme="dark"] .pill.active {
      background: linear-gradient(180deg, #064e3b 0%, #065f46 100%);
      color:#a7f3d0; border-color:rgba(167,243,208,.35);
    }
    .pill.inactive { opacity: 0.95; }

    .section-rule { border-bottom:1px solid var(--row-border); margin:8px 0 6px 0; }
    .report-item { margin:0.30rem 0; line-height:1.35; }

    .meta-chip {
      display:inline-block; font-size:11px; font-weight:800;
      padding:2px 6px; border-radius:999px; margin-left:8px;
      background:#eef2ff; color:#1e3a8a; border:1px solid #c7d2fe;
    }
    html[data-theme="dark"] .meta-chip { background:#1f2937; color:#bfdbfe; border-color:#374151; }

    /* Light blue date badge */
    .date-badge {
      display:inline-block; font-size:11px; font-weight:800;
      padding:2px 6px; border-radius:999px; margin-left:8px;
      background:#e0f2fe; color:#075985; border:1px solid #7dd3fc;
    }
    html[data-theme="dark"] .date-badge {
      background:#0b1220; color:#7dd3fc; border-color:#164e63;
    }

    /* Red toured badge */
    .toured-badge {
      display:inline-block; font-size:11px; font-weight:800;
      padding:2px 6px; border-radius:999px; margin-left:8px;
      background:#fee2e2; color:#991b1b; border:1px solid #fecaca;
    }
    html[data-theme="dark"] .toured-badge { background:#7f1d1d; color:#fecaca; border-color:#ef4444; }
    </style>
    """, unsafe_allow_html=True)

def _safe_rerun():
    try: st.rerun()
    except Exception:
        try: st.experimental_rerun()
        except Exception: pass

# ============== Supabase (lazy + safe) ==============
@st.cache_resource(show_spinner=False)
def get_supabase() -> Optional["Client"]:
    if create_client is None:
        return None
    try:
        url = os.getenv("SUPABASE_URL", st.secrets.get("SUPABASE_URL", ""))
        key = os.getenv("SUPABASE_SERVICE_ROLE", st.secrets.get("SUPABASE_SERVICE_ROLE", ""))
        if not url or not key:
            return None
        return create_client(url, key)
    except Exception:
        return None

def _sb_ok(SUPABASE) -> bool:
    try: return bool(SUPABASE)
    except Exception: return False

# ============== DB helpers (lazy supabase handle) ==============
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False) -> List[Dict[str, Any]]:
    SUPABASE = get_supabase()
    if not _sb_ok(SUPABASE): return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        return ([r for r in rows if r.get("active")] if not include_inactive else rows)
    except Exception:
        return []

def _invalidate_clients_cache():
    try: fetch_clients.clear()  # type: ignore[attr-defined]
    except Exception: pass

def toggle_client_active(client_id: int, new_active: bool):
    SUPABASE = get_supabase()
    if not _sb_ok(SUPABASE) or not client_id: return False, "Not configured"
    try:
        SUPABASE.table("clients").update({"active": new_active}).eq("id", client_id).execute()
        _invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def rename_client(client_id: int, new_name: str):
    SUPABASE = get_supabase()
    if not _sb_ok(SUPABASE) or not client_id or not (new_name or "").strip(): return False, "Bad input"
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
    SUPABASE = get_supabase()
    if not _sb_ok(SUPABASE) or not client_id: return False, "Not configured"
    try:
        SUPABASE.table("clients").delete().eq("id", client_id).execute()
        _invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

@st.cache_data(ttl=120, show_spinner=False)
def fetch_sent_for_client(client_norm: str, limit: int = 5000):
    SUPABASE = get_supabase()
    if not (_sb_ok(SUPABASE) and client_norm.strip()): return []
    try:
        cols = "url,address,sent_at,campaign,mls_id,canonical,zpid"
        resp = SUPABASE.table("sent").select(cols)\
            .eq("client", client_norm.strip())\
            .order("sent_at", desc=True)\
            .limit(limit).execute()
        return resp.data or []
    except Exception:
        return []

@st.cache_data(ttl=120, show_spinner=False)
def fetch_tour_norm_slugs_for_client(client_norm: str) -> set:
    SUPABASE = get_supabase()
    if not (_sb_ok(SUPABASE) and client_norm.strip()): return set()
    try:
        tq = SUPABASE.table("tours").select("id").eq("client", client_norm).limit(5000).execute()
        ids = [t["id"] for t in (tq.data or [])]
        if not ids: return set()
        sq = SUPABASE.table("tour_stops").select("address,address_slug").in_("tour_id", ids).limit(50000).execute()
        stops = (sq.data or [])
        out: set = set()
        for s in stops:
            if s.get("address_slug"):
                out.add(_norm_slug_from_text(s["address_slug"]))
            elif s.get("address"):
                out.add(_norm_slug_from_text(s["address"]))
        return out
    except Exception:
        return set()


# ================= UI bits =================
def _client_row_icons(name: str, norm: str, cid: int, active: bool):
    col_name, col_rep, col_ren, col_tog, col_del, col_sp = st.columns([8, 1, 1, 1, 1, 2])

    with col_name:
        status_class = "active" if active else "inactive"
        status_text  = "active" if active else "inactive"
        st.markdown(
            f"<span class='client-name'>{escape(name)}</span> "
            f"<span class='pill {status_class}'>{status_text}</span>",
            unsafe_allow_html=True
        )

    with col_rep:
        if st.button("▦", key=f"rep_{cid}", help="Open report"):
            st.session_state["__active_tab__"] = "Clients"
            _qp_set(report=norm, scroll="1")
            _safe_rerun()

    with col_ren:
        if st.button("✎", key=f"rn_btn_{cid}", help="Rename"):
            st.session_state[f"__edit_{cid}"] = True

    with col_tog:
        if st.button("⟳", key=f"tg_{cid}", help=("Deactivate" if active else "Activate")):
            try:
                SUPABASE = get_supabase()
                rows = [] if not _sb_ok(SUPABASE) else SUPABASE.table("clients").select("active").eq("id", cid).limit(1).execute().data or []
                cur = rows[0]["active"] if rows else active
                toggle_client_active(cid, (not cur))
            except Exception:
                toggle_client_active(cid, (not active))
            st.session_state["__active_tab__"] = "Clients"
            _safe_rerun()

    with col_del:
        if st.button("⌫", key=f"del_{cid}", help="Delete"):
            st.session_state[f"__del_{cid}"] = True

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


def _render_client_report_view(client_display_name: str, client_norm: str):
    st.markdown(f"### Report for {escape(client_display_name)}", unsafe_allow_html=True)

    colX, _ = st.columns([1,3])
    with colX:
        if st.button("Close report", key=f"__close_report_{client_norm}"):
            st.session_state["__active_tab__"] = "Clients"
            _qp_set()
            _safe_rerun()

    sent_rows = fetch_sent_for_client(client_norm)
    if not sent_rows:
        st.info("No listings have been sent to this client yet.")
        return

    tour_norm_slugs = fetch_tour_norm_slugs_for_client(client_norm)

    # Filters
    seen_camps: List[str] = []
    for r in sent_rows:
        c = (r.get("campaign") or "").strip()
        if c not in seen_camps: seen_camps.append(c)
    labels = ["All campaigns"] + [("— no campaign —" if c=="" else c) for c in seen_camps]
    keys   = [None] + seen_camps

    colF1, colF2, colF3 = st.columns([1.2, 1.8, 1])
    with colF1:
        i = st.selectbox("Filter by campaign", list(range(len(labels))),
                         format_func=lambda j: labels[j], index=0, key=f"__camp_{client_norm}")
        sel_camp = keys[i]
    with colF2:
        q = st.text_input("Search address / MLS / URL", value="",
                          placeholder="e.g. 407 Woodall, 2501234, /homedetails/",
                          key=f"__q_{client_norm}")
        qn = (q or "").strip().lower()
    with colF3:
        st.caption(f"{len(sent_rows)} total logged")

    def _match(row) -> bool:
        if sel_camp is not None and (row.get("campaign") or "").strip() != sel_camp:
            return False
        if not qn: return True
        return (qn in (row.get("address","").lower())
                or qn in (row.get("mls_id","").lower())
                or qn in (row.get("url","").lower()))

    filtered = [r for r in sent_rows if _match(r)]
    deduped  = _dedupe_by_property(filtered)
    st.caption(f"{len(deduped)} unique listing{'s' if len(deduped)!=1 else ''} (deduped by property)")

    def chip(t: Any) -> str:
        if t is None: return ""
        s = str(t).strip()
        if not s: return ""
        return f"<span class='meta-chip'>{escape(s)}</span>"

    # Build Markdown bullet list with inline HTML chips/badges (avoids raw </li> showing)
    md_lines: List[str] = []
    for r in deduped:
        url  = (r.get("url") or "").strip()
        addr = (r.get("address") or "").strip() or _address_text_from_url(url) or "Listing"

        # Single DATE tag (YYYYMMDD)
        date_tag = _pick_timestamp_date_tag(r)

        # Toured badge (once)
        norm_pslug = _norm_slug_from_url(url) or _norm_slug_from_text(addr)
        toured = norm_pslug in tour_norm_slugs

        meta: List[str] = []
        if date_tag:
            meta.append(f"<span class='date-badge'>{escape(date_tag)}</span>")
        if toured:
            meta.append("<span class='toured-badge'>Toured</span>")

        debug_html = ""
        if DEBUG_REPORT:
            debug_html = (
                " <span style='font-size:10px;opacity:.7'>(dbg "
                f"date_tag={escape(date_tag or '-')}, camp={(r.get('campaign') or '')}, "
                f"slug={escape(norm_pslug)}, toured={'yes' if toured else 'no'})</span>"
            )

        # Use Markdown bullet + inline HTML so no literal </li> appears
        line = f"- <a href=\"{escape(url)}\" target=\"_blank\" rel=\"noopener\">{escape(addr)}</a> {' '.join(meta)}{debug_html}"
        md_lines.append(line)

    if not md_lines:
        st.warning("No results returned.")
        return

    st.markdown("\n".join(md_lines), unsafe_allow_html=True)

    # Export (deduped)
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


# ============== Public entry (call this from app.py) ==============
def render_clients_tab():
    _inject_css_once()

    st.subheader("Clients")
    st.caption("Use ▦ to open an inline report; ✎ rename; ⟳ toggle active; ⌫ delete.")

    report_norm_qp = _qp_get("report", "")
    want_scroll    = _qp_get("scroll", "") in ("1","true","yes")

    all_clients = fetch_clients(include_inactive=True)
    active   = [c for c in all_clients if c.get("active")]
    inactive = [c for c in all_clients if not c.get("active")]

    colA, colB = st.columns(2)
    with colA:
        st.markdown("### Active", unsafe_allow_html=True)
        if not active: st.write("_No active clients_")
        for c in active:
            _client_row_icons(c["name"], c.get("name_norm",""), c["id"], active=True)
    with colB:
        st.markdown("### Inactive", unsafe_allow_html=True)
        if not inactive: st.write("_No inactive clients_")
        for c in inactive:
            _client_row_icons(c["name"], c.get("name_norm",""), c["id"], active=False)

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
            _qp_set(report=report_norm_qp)
