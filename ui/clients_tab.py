# ui/clients_tab.py
import re, csv, io
from datetime import datetime, date
from typing import List, Dict, Any
from html import escape

import streamlit as st
import streamlit.components.v1 as components

# Reuse helpers from Run tab to stay consistent
from ui.run_tab import (
    _norm_tag, get_already_sent_maps, is_probable_url, resolve_from_source_url,
    process_single_row, upgrade_to_homedetails_if_needed, make_preview_url, canonicalize_zillow
)

from services.tours import fetch_tours, upsert_tours, update_tour_status, update_tour_meta, delete_tour

# ---------- Small helpers ----------
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

# These two call back into run_tab's cached Supabase-backed functions
from ui.run_tab import fetch_clients, rename_client, toggle_client_active, delete_client

# ---------- Report (unchanged from your original feel) ----------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_sent_for_client(client_norm: str, limit: int = 5000):
    from ui.run_tab import get_supabase
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

# ---------- NEW: Tours manager ----------
def _cross_check_tour_candidates(client_norm: str, raw_items: List[str]) -> List[Dict[str, Any]]:
    """
    Takes pasted links/addresses, resolves to Zillow if needed, then flags:
      - already_sent (in 'sent')
      - on_tour (in 'tours')
    """
    # 1) Resolve to Zillow URL + preview URL, attempt canonical/zpid
    defaults = {"city":"", "state":"", "zip":""}
    resolved: List[Dict[str, Any]] = []
    for item in raw_items:
        item = item.strip()
        if not item:
            continue
        if is_probable_url(item):
            zurl, used_addr = resolve_from_source_url(item, defaults)
            rec = {"input_address": used_addr or "", "zillow_url": upgrade_to_homedetails_if_needed(zurl), "csv_photo": None}
        else:
            rec = process_single_row({"address": item}, delay=0.2, land_mode=True, defaults=defaults,
                                     require_state=True, mls_first=True, default_mls_name="", max_candidates=15)
            rec["zillow_url"] = upgrade_to_homedetails_if_needed(rec.get("zillow_url") or "")
        rec["preview_url"] = make_preview_url(rec.get("zillow_url") or "")
        canon, zpid = canonicalize_zillow(rec.get("zillow_url") or "")
        rec["canonical"] = canon
        rec["zpid"] = zpid
        resolved.append(rec)

    # 2) Already sent map
    canon_set, zpid_set, _, _ = get_already_sent_maps(client_norm)

    # 3) Already in tours
    existing_tours = fetch_tours(client_norm)
    tour_canon = { (r.get("canonical") or "") for r in existing_tours if r.get("canonical") }
    tour_zpid  = { (r.get("zpid") or "") for r in existing_tours if r.get("zpid") }

    # 4) annotate flags
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

    paste = st.text_area(
        "Paste links or addresses from the client (one per line)",
        placeholder="https://www.zillow.com/homedetails/...\n123 US-301 S, Four Oaks, NC 27524",
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

        # list with badges
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

        # Build payload for NEW only
        new_items = []
        for r in checked:
            if not r.get("on_tour"):
                # allow adding even if previously sent — tour list is a separate intent
                new_items.append({
                    "url": r.get("display_url") or r.get("zillow_url") or "",
                    "address": r.get("input_address") or "",
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
        else:
            st.info("All items are already on the tour list.")

    # ---- Existing tours management ----
    st.markdown("##### Current tour list")
    rows = fetch_tours(client_norm)
    if not rows:
        st.caption("_No tour items yet_")
        return

    # Quick table-style controls
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
                if ok1 and ok2:
                    st.success("Saved")
                else:
                    st.error("Save failed")

            del_col = st.columns(1)[0]
            if del_col.button("⌫", key=f"__del_{r['id']}"):
                ok, _ = delete_tour(r["id"])
                if ok:
                    st.success("Deleted")
                    st.rerun()

        st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:4px 0 2px 0;'></div>", unsafe_allow_html=True)

# ---------- Client row + main renderer ----------
def _client_row_icons(name: str, norm: str, cid: int, active: bool):
    # Single row using columns so widgets are inline with the name
    col_name, col_tour, col_rep, col_ren, col_tog, col_del, col_sp = st.columns([8, 1, 1, 1, 1, 1, 2])

    with col_name:
        st.markdown(
            f"<span class='client-name'>{escape(name)}</span> "
            f"<span class='pill {'active' if active else ''}'>{'active' if active else 'inactive'}</span>",
            unsafe_allow_html=True
        )

    with col_tour:
        if st.button("🗓️", key=f"tour_{cid}", help="Open Tours"):
            st.session_state[f"__tour_{cid}"] = not st.session_state.get(f"__tour_{cid}", False)

    with col_rep:
        if st.button("▦", key=f"rep_{cid}", help="Open report"):
            _qp_set(report=norm, scroll="1")
            st.rerun()

    with col_ren:
        if st.button("✎", key=f"rn_btn_{cid}", help="Rename"):
            st.session_state[f"__edit_{cid}"] = True

    with col_tog:
        if st.button("⟳", key=f"tg_{cid}", help=("Deactivate" if active else "Activate")):
            from ui.run_tab import get_supabase
            SUPABASE = get_supabase()
            rows = SUPABASE.table("clients").select("active").eq("id", cid).limit(1).execute().data or []
            cur = rows[0]["active"] if rows else active
            toggle_client_active(cid, (not cur))
            st.rerun()

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
            st.rerun()
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
            st.rerun()
        if dc2.button("Cancel", key=f"del_no_{cid}"):
            st.session_state[f"__del_{cid}"] = False
        st.markdown("</div>", unsafe_allow_html=True)

    # Tours panel (toggle under the row)
    if st.session_state.get(f"__tour_{cid}"):
        st.markdown("<div class='inline-panel'>", unsafe_allow_html=True)
        _render_tours_panel(name, norm)
        st.markdown("</div>", unsafe_allow_html=True)

    # subtle divider under each row
    st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:4px 0 2px 0;'></div>", unsafe_allow_html=True)

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
            _qp_set(report=report_norm_qp)
