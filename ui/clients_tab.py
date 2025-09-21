# ui/clients_tab.py
# Clients tab: list Active/Inactive clients + inline actions and inline "Listings sent" report
# - Stays on Clients tab when opening reports (no jumping)
# - Dedupes listings (strict): canonical → zpid → zpid_from_url → url-addrslug → address-slug → url_base
# - Adds TOURED badge by cross-checking tour stops for the same client

import os, re
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape
from urllib.parse import unquote

import streamlit as st
import streamlit.components.v1 as components
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
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _slug_addr(addr: str) -> str:
    a = (addr or "").strip().lower()
    a = re.sub(r"[^\w\s,-]", "", a).replace(",", "")
    return re.sub(r"\s+", "-", a).strip("-")

def _url_base(u: str) -> str:
    if not u: return ""
    u = re.sub(r'#.*$', '', u.strip())
    u = u.split('?', 1)[0]
    return u.lower()

def _extract_zpid(u: str) -> Optional[str]:
    if not u: return None
    m = re.search(r'([0-9]{6,})_zpid', u, re.I)
    return m.group(1) if m else None

def _url_addr_slug(u: str) -> Optional[str]:
    if not u: return None
    # Try /homedetails/{slug}/{zpid}_zpid/
    m = re.search(r'/homedetails/([^/]+)/[0-9]{6,}_zpid/', u, re.I)
    if m: return m.group(1).lower()
    # Try /homes/{slug}_rb
    m = re.search(r'/homes/([^/_]+)_rb', u, re.I)
    if m: return m.group(1).lower()
    return None

def _stay_on_clients():
    st.session_state["__active_tab__"] = "Clients"

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

# ---------- Clients registry ----------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False):
    if not SUPABASE: return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

def toggle_client_active(client_id: int, new_active: bool):
    if not SUPABASE or not client_id: return False, "Not configured"
    try:
        SUPABASE.table("clients").update({"active": new_active}).eq("id", client_id).execute()
        fetch_clients.clear()  # type: ignore[attr-defined]
        return True, "ok"
    except Exception as e:
        return False, str(e)

def rename_client(client_id: int, new_name: str):
    if not SUPABASE or not client_id or not (new_name or "").strip():
        return False, "Bad input"
    try:
        new_norm = _norm_tag(new_name)
        existing = SUPABASE.table("clients").select("id").eq("name_norm", new_norm).limit(1).execute().data or []
        if existing and existing[0]["id"] != client_id:
            return False, "A client with that (normalized) name already exists."
        SUPABASE.table("clients").update({"name": new_name.strip(), "name_norm": new_norm}).eq("id", client_id).execute()
        fetch_clients.clear()  # type: ignore[attr-defined]
        return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_client(client_id: int):
    if not SUPABASE or not client_id: return False, "Not configured"
    try:
        SUPABASE.table("clients").delete().eq("id", client_id).execute()
        fetch_clients.clear()  # type: ignore[attr-defined]
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ---------- SENT lookups (raw) ----------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_sent_for_client_raw(client_norm: str, limit: int = 8000) -> List[Dict[str, Any]]:
    """
    Raw rows from sent, newest first. We dedupe in Python with a strong identity key.
    """
    if not (SUPABASE and (client_norm or "").strip()):
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

# ---------- TOURS lookups for cross-check ----------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_tour_stop_slugs_by_date(client_norm: str) -> Dict[str, List[str]]:
    """
    Returns map: slug -> [tour_date_iso, ...] for the given client.
    """
    out: Dict[str, List[str]] = {}
    if not (SUPABASE and (client_norm or "").strip()):
        return out
    try:
        tq = SUPABASE.table("tours").select("id,tour_date").eq("client", client_norm).order("tour_date", desc=False).limit(5000).execute()
        tours = tq.data or []
        if not tours:
            return out
        tour_id_to_date = {t["id"]: t["tour_date"] for t in tours}
        ids = list(tour_id_to_date.keys())
        if not ids:
            return out
        sq = SUPABASE.table("tour_stops").select("tour_id,address_slug,address").in_("tour_id", ids).limit(50000).execute()
        stops = sq.data or []
        for s in stops:
            slug = (s.get("address_slug") or _slug_addr(s.get("address") or "")).strip()
            td = tour_id_to_date.get(s.get("tour_id"))
            if not (slug and td):
                continue
            out.setdefault(slug, [])
            if td not in out[slug]:
                out[slug].append(td)
        return out
    except Exception:
        return out

# ---------- Identity + Dedup ----------
def _identity_key_for_row(row: Dict[str, Any]) -> Tuple[str, str]:
    """
    Returns (namespace, key) for dedupe priority:
      1) canonical
      2) zpid
      3) zpid_from_url
      4) url_addrslug
      5) address_slug (from row.address)
      6) url_base
    """
    url = (row.get("url") or "").strip()
    addr = (row.get("address") or "").strip()
    canonical = (row.get("canonical") or "").strip()
    zpid = (row.get("zpid") or "").strip()

    if canonical:
        return ("canonical", canonical.lower())
    if zpid:
        return ("zpid", zpid)

    zpid_u = _extract_zpid(url)
    if zpid_u:
        return ("zpid_url", zpid_u)

    slug_u = _url_addr_slug(url)
    if slug_u:
        return ("urlslug", slug_u)

    slug_a = _slug_addr(addr) if addr else ""
    if slug_a:
        return ("addrslug", slug_a)

    base = _url_base(url)
    if base:
        return ("urlbase", base)

    # Last resort: whole URL
    return ("url", url.lower())

def dedupe_sent_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Keep the newest row per identity key (rows already sorted desc by sent_at).
    """
    seen = set()
    out = []
    for r in rows:
        k = _identity_key_for_row(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out

# ---------- Address pretty from URL (fallback) ----------
def address_text_from_url(u: str) -> str:
    if not u: return ""
    uu = unquote(u)
    m = re.search(r"/homedetails/([^/]+)/\d{6,}_zpid/", uu, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    m = re.search(r"/homes/([^/_]+)_rb/?", uu, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    return ""

# ---------- UI bits ----------
def _client_row_icons(name: str, norm: str, cid: int, active: bool):
    # Columns: name | ▦ | ✎ | ⟳ | ⌫
    col_name, col_rep, col_ren, col_tog, col_del, _ = st.columns([8, 1, 1, 1, 1, 2])

    with col_name:
        st.markdown(
            f"<span class='client-name' style='font-weight:700;color:#ffffff'>{escape(name)}</span> "
            f"<span class='pill {'active' if active else ''}'>{'active' if active else 'inactive'}</span>",
            unsafe_allow_html=True
        )

    with col_rep:
        if st.button("▦", key=f"rep_{cid}", help="Open sent report"):
            _stay_on_clients()
            _qp_set(report=norm, scroll="1")
            _safe_rerun()

    with col_ren:
        if st.button("✎", key=f"rn_btn_{cid}", help="Rename"):
            st.session_state[f"__edit_{cid}"] = True

    with col_tog:
        if st.button("⟳", key=f"tg_{cid}", help=("Deactivate" if active else "Activate")):
            # read current server value (avoids drift)
            rows = SUPABASE.table("clients").select("active").eq("id", cid).limit(1).execute().data or []
            cur = rows[0]["active"] if rows else active
            toggle_client_active(cid, (not cur))
            _stay_on_clients()
            _qp_set()  # clear any report param if present to avoid odd scrolls
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
            _stay_on_clients()
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
            _stay_on_clients()
            _safe_rerun()
        if dc2.button("Cancel", key=f"del_no_{cid}"):
            st.session_state[f"__del_{cid}"] = False
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:4px 0 2px 0;'></div>", unsafe_allow_html=True)

# ---------- Report renderer ----------
def _render_client_report_view(client_display_name: str, client_norm: str):
    st.markdown(f"### Report for {escape(client_display_name)}", unsafe_allow_html=True)
    colX, _ = st.columns([1,3])
    with colX:
        if st.button("Close report", key=f"__close_report_{client_norm}"):
            _stay_on_clients()
            _qp_set()  # clear query params
            _safe_rerun()

    # Load raw sent, then dedupe
    rows_raw = fetch_sent_for_client_raw(client_norm)
    # newest-first uniqueness
    rows_u = dedupe_sent_rows(rows_raw)

    # Tours cross-check
    tour_slug_to_dates = fetch_tour_stop_slugs_by_date(client_norm)
    toured_slugs = set(tour_slug_to_dates.keys())

    # Filters
    seen_campaigns = []
    for r in rows_u:
        c = (r.get("campaign") or "").strip()
        if c not in seen_campaigns:
            seen_campaigns.append(c)

    campaign_labels = ["All campaigns"] + [("— no campaign —" if c == "" else c) for c in seen_campaigns]
    campaign_keys   = [None] + seen_campaigns

    colF1, colF2, colF3 = st.columns([1.2, 1.8, 1])
    with colF1:
        sel_idx = st.selectbox("Filter by campaign", list(range(len(campaign_labels))),
                               format_func=lambda i: campaign_labels[i], index=0, key=f"__camp_{client_norm}")
        sel_campaign = campaign_keys[sel_idx]
    with colF2:
        q = st.text_input("Search address / MLS / URL", value="", placeholder="e.g. 407 Woodall, 2501234, /homedetails/", key=f"__q_{client_norm}")
        q_norm = q.strip().lower()
    with colF3:
        st.caption(f"{len(rows_u)} unique logged")

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

    rows_f = [r for r in rows_u if _match(r)]
    count = len(rows_f)
    st.caption(f"{count} matching listing{'s' if count!=1 else ''}")

    if not rows_f:
        st.info("No results match the current filters.")
        return

    # Render list with badges (TOURED)
    items_html = []
    for r in rows_f:
        url = (r.get("url") or "").strip()
        sent_at = r.get("sent_at") or ""
        camp = (r.get("campaign") or "").strip()
        adisplay = (r.get("address") or "").strip() or address_text_from_url(url) or "Listing"

        # Build slug identity to test "toured"
        slug = _url_addr_slug(url) or _slug_addr(r.get("address") or "")
        toured_dates = tour_slug_to_dates.get(slug, [])
        toured_badge = ""
        tour_dates_html = ""
        if toured_dates:
            toured_badge = "<span style='font-size:11px;font-weight:800;padding:2px 6px;border-radius:9999px;background:#0a84ff;color:#ffffff;margin-left:6px;'>TOURED</span>"
            # Small muted dates (optional)
            tiny_date_chips = []
            for td in toured_dates[:3]:
                tiny_date_chips.append(f"<span style='font-size:10px;font-weight:700;padding:1px 5px;border-radius:9999px;background:#e2e8f0;color:#475569;margin-left:4px;'>{escape(td)}</span>")
            tour_dates_html = "".join(tiny_date_chips)

        # human time
        try:
            # Accept both "2025-09-20T18:48:01+00:00" and "...secondsZ"
            ts = str(sent_at)
            ts2 = ts.replace("Z","+00:00") if ts.endswith("Z") else ts
            dt = datetime.fromisoformat(ts2)
            stamp = dt.strftime("%b %-d, %Y • %-I:%M %p")
        except Exception:
            stamp = sent_at

        camp_chip = ""
        if sel_campaign is None and camp:
            camp_chip = f"<span style='font-size:11px;font-weight:700;padding:2px 6px;border-radius:9999px;background:#e2e8f0;margin-left:6px;'>{escape(camp)}</span>"

        items_html.append(
            f"""<li style="margin:.25rem 0;">
                  <a href="{escape(url)}" target="_blank" rel="noopener">{escape(adisplay)}</a>
                  <span style="color:#64748b; font-size:12px; margin-left:6px;">{escape(stamp)}</span>
                  {camp_chip}
                  {toured_badge}{tour_dates_html}
                </li>"""
        )

    html = "<ul class='link-list'>" + "\n".join(items_html) + "</ul>"
    st.markdown(html, unsafe_allow_html=True)

    with st.expander("Export filtered report"):
        import pandas as pd, io
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
            components.html(
                """
                <script>
                  const el = parent.document.getElementById("report_anchor");
                  if (el) { el.scrollIntoView({behavior: "smooth", block: "start"}); }
                </script>
                """, height=0
            )
            _stay_on_clients()
            _qp_set(report=report_norm_qp)
