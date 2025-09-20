# ui/clients_tab.py
import re
from datetime import datetime, date
from typing import List, Dict, Any
from html import escape

import streamlit as st
import streamlit.components.v1 as components

# Break the circular import by using service modules (NOT ui.run_tab)
from services.clients import (
    fetch_clients, rename_client, toggle_client_active, delete_client, get_already_sent_maps
)
from services.resolver_light import (
    is_probable_url,
    canonicalize_zillow,
    make_preview_url,
    upgrade_to_homedetails_if_needed,
    resolve_from_source_url,
)

# Optional tours manager (if you added it earlier). If not using tours, you can remove these imports/usage.
try:
    from services.tours import fetch_tours, upsert_tours, update_tour_status, update_tour_meta, delete_tour
    TOURS_ENABLED = True
except Exception:
    TOURS_ENABLED = False

# ---------- Query param helpers ----------
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

# ---------- Sent report ----------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_sent_for_client(client_norm: str, limit: int = 5000):
    from services.clients import get_supabase
    SUPABASE = get_supabase()
    if not SUPABASE or not client_norm.strip():
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
    from urllib.parse import unquote
    if not url: return ""
    u = unquote(url)
    m = re.search(r"/homedetails/([^/]+)/\d{6,}_zpid/", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    m = re.search(r"/homes/([^/_]+)_rb/?", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    return ""

def _render_client_report_view(client_display_name: str, client_norm: str):
    st.markdown(f"### Report for {escape(client_display_name)}", unsafe_allow_html=True)

    colX, _ = st.columns([1,3])
    with colX:
        if st.button("Close report", key=f"__close_report_{client_norm}"):
            _qp_set()  # clear qp
            st.rerun()

    rows = fetch_sent_for_client(client_norm)
    total = len(rows)

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
        chip = ""
        if sel_campaign is None and camp:
            chip = f"<span style='font-size:11px; font-weight:700; padding:2px 6px; border-radius:999px; background:#e2e8f0; margin-left:6px;'>{escape(camp)}</span>"
        items_html.append(
            f"""<li>
                  <a href="{escape(url)}" target="_blank" rel="noopener">{escape(addr)}</a>
                  <span style="color:#64748b; font-size:12px; margin-left:6px;">{escape(sent_at)}</span>
                  {chip}
                </li>"""
        )
    html = "<ul class='link-list'>" + "\n".join(items_html) + "</ul>"
    st.markdown(html, unsafe_allow_html=True)

# ---------- Optional: tours (kept compact). If not using tours, you can delete this block ----------
def _cross_check_tour_candidates(client_norm: str, raw_items: List[str]) -> List[Dict[str, Any]]:
    """
    Lightweight cross-check: resolve URLs (follows redirects → try homedetails),
    compute canonical/zpid, and flag against 'sent' + 'tours'.
    Non-URLs are ignored (use Run tab to resolve plain addresses first).
    """
    resolved: List[Dict[str, Any]] = []
    for item in raw_items:
        item = item.strip()
        if not item:
            continue
        if not is_probable_url(item):
            # skip non-URL here to avoid importing the full resolver; you can paste Zillow links directly
            continue
        zurl, _ = resolve_from_source_url(item)
        zurl = upgrade_to_homedetails_if_needed(zurl)
        prev = make_preview_url(zurl) if zurl else ""
        canon, zpid = canonicalize_zillow(zurl or "")
        resolved.append({
            "input_address": "",
            "zillow_url": zurl,
            "preview_url": prev,
            "canonical": canon,
            "zpid": zpid,
        })

    # 'already sent'
    canon_set, zpid_set, _, _ = get_already_sent_maps(client_norm)

    # 'already on tours'
    tour_canon = set(); tour_zpid = set()
    if TOURS_ENABLED:
        for r in fetch_tours(client_norm):
            c = (r.get("canonical") or "").strip()
            z = (r.get("zpid") or "").strip()
            if c: tour_canon.add(c)
            if z: tour_zpid.add(z)

    out = []
    for r in resolved:
        canon = (r.get("canonical") or "").strip()
        zpid  = (r.get("zpid") or "").strip()
        already_sent = (canon and canon in canon_set) or (zpid and zpid in zpid_set)
        on_tour      = (canon and canon in tour_canon) or (zpid and zpid in tour_zpid)
        out.append({
            **r,
            "already_sent": bool(already_sent),
            "on_tour": bool(on_tour),
            "display_url": r.get("preview_url") or r.get("zillow_url") or ""
        })
    return out

def _render_tours_panel(client_display_name: str, client_norm: str):
    st.markdown(f"#### 🗓️ Tours for {escape(client_display_name)}", unsafe_allow_html=True)
    st.caption("Paste Zillow (or other) listing links below. For plain addresses, resolve them via the Run tab first, then paste the Zillow links here.")

    paste = st.text_area(
        "Paste links (one per line)",
        placeholder="https://www.zillow.com/homedetails/...\nhttps://l.hms.pt/short/... (will resolve)",
        height=120
    )
    c1, c2 = st.columns([1,1])
    with c1:
        tour_date = st.date_input("Tour date (optional)", value=None, format="YYYY-MM-DD")
    with c2:
        notes_default = st.text_input("Notes (optional)", value="")

    if st.button("Cross-check", use_container_width=True, key=f"__cross_{client_norm}"):
        raw_lines = [ln.strip() for ln in (paste or "").splitlines() if ln.strip()]
        checked = _cross_check_tour_candidates(client_norm, raw_lines)

        if not checked:
            st.info("Nothing to check.")
            return

        lis = []
        for r in checked:
            url = r.get("display_url") or r.get("zillow_url") or ""
            if not url:
                continue
            badge = ""
            if r.get("on_tour"):
                badge = ' <span class="badge dup" title="Already on this client’s tour list">ON TOUR</span>'
            elif r.get("already_sent"):
                badge = ' <span class="badge dup" title="Already sent to this client">Duplicate</span>'
            else:
                badge = ' <span class="badge new" title="New for this client">NEW</span>'
            lis.append(f'<li style="margin:0.25rem 0;"><a href="{escape(url)}" target="_blank" rel="noopener">{escape(url)}</a>{badge}</li>')

        html = "<ul class='link-list'>" + "\n".join(lis) + "</ul>"
        st.markdown(html, unsafe_allow_html=True)

        if TOURS_ENABLED:
            new_items = []
            for r in checked:
                if not r.get("on_tour"):
                    new_items.append({
                        "url": r.get("display_url") or r.get("zillow_url") or "",
                        "address": "",
                        "notes": (notes_default or "").strip(),
                        "status": "requested",
                        "tour_date": tour_date if isinstance(tour_date, date) else None
                    })
            if new_items:
                if st.button(f"Add {len(new_items)} to Tours", type="primary", use_container_width=True, key=f"__add_tours_{client_norm}"):
                    ok, msg = upsert_tours(client_norm, new_items)
                    if ok:
                        st.success("Added to Tours.")
                        st.rerun()
                    else:
                        st.error(f"Add failed: {msg}")

    if TOURS_ENABLED:
        st.markdown("##### Current tour list")
        rows = fetch_tours(client_norm)
        if not rows:
            st.caption("_No tour items yet_")
        else:
            for r in rows:
                col1, col2, col3, col4, col5 = st.columns([4, 1.6, 1.6, 2.2, 0.8])
                with col1:
                    url = r.get("url") or ""
                    addr = (r.get("address") or "") or address_text_from_url(url) or "Listing"
                    st.markdown(f'<a href="{escape(url)}" target="_blank" rel="noopener">{escape(addr)}</a>', unsafe_allow_html=True)
                with col2:
                    st.caption(r.get("status") or "requested")
                    new_status = st.selectbox(
                        "Status", ["requested","scheduled","seen","offered","lost","canceled"],
                        index=["requested","scheduled","seen","offered","lost","canceled"].index(r.get("status") or "requested"),
                        key=f"__stat_{r['id']}"
                    )
                with col3:
                    cur_date = r.get("tour_date")
                    new_date = st.date_input("Tour date", value=(date.fromisoformat(cur_date) if cur_date else None),
                                             key=f"__date_{r['id']}", format="YYYY-MM-DD")
                with col4:
                    new_notes = st.text_input("Notes", value=r.get("notes") or "", key=f"__notes_{r['id']}")
                with col5:
                    if st.button("Save", key=f"__sav_{r['id']}"):
                        ok1, _ = update_tour_status(r["id"], new_status)
                        ok2, _ = update_tour_meta(r["id"], notes=new_notes, tour_date=new_date)
                        st.success("Saved" if (ok1 and ok2) else "Save failed")
                    if st.button("⌫", key=f"__del_{r['id']}"):
                        ok, _ = delete_tour(r["id"])
                        if ok:
                            st.success("Deleted")
                            st.rerun()
                st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:4px 0 2px 0;'></div>", unsafe_allow_html=True)

# ---------- Client row + main renderer ----------
def _client_row_icons(name: str, norm: str, cid: int, active:_
