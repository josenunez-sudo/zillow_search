# ui/tours_tab.py
# Tours tab with:
# - Active/Inactive clients side-by-side
# - Global "Add / Manage Tours"
# - ShowingTime Importer (Print URL or PDF)
# - Tours list with clickable addresses + time chips, inline edit/delete

import os
import re
import io
from datetime import date, datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape

import streamlit as st
import requests
from supabase import create_client, Client

# Optional PDF parser (we'll degrade gracefully if missing)
try:
    import PyPDF2  # type: ignore
except Exception:
    PyPDF2 = None

# ───────────────────────────── Supabase ─────────────────────────────

@st.cache_resource
def _get_supabase() -> Optional[Client]:
    try:
        url = os.getenv("SUPABASE_URL", st.secrets.get("SUPABASE_URL", ""))
        key = os.getenv("SUPABASE_SERVICE_ROLE", st.secrets.get("SUPABASE_SERVICE_ROLE", ""))
        if not url or not key:
            return None
        return create_client(url, key)
    except Exception:
        return None

SUPABASE = _get_supabase()

def _sb_ok() -> bool:
    try:
        return bool(SUPABASE)
    except NameError:
        return False

# ───────────────────────────── Utils ─────────────────────────────

def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _slug_addr_for_match(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[#,.]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s

def _zillow_deeplink_from_addr(addr: str) -> str:
    s = re.sub(r"\s+", " ", (addr or "").strip())
    if not s:
        return ""
    slug = re.sub(r"[^\w\s,-]", "", s).replace(",", "")
    slug = re.sub(r"\s+", "-", slug.strip())
    return f"https://www.zillow.com/homes/{slug}_rb/"

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

# ───────────────────────────── Styling chips ─────────────────────────────

def _tour_badge(text: str) -> str:
    # Amber high-contrast date badge
    return (
        "<span style='display:inline-block;padding:2px 8px;border-radius:999px;"
        "font-size:12px;font-weight:800;background:#fef3c7;color:#92400e;"
        "border:1px solid rgba(146,64,14,.25);'>"
        f"{escape(text)}</span>"
    )

def _time_chip(t1: str, t2: str) -> str:
    txt = "–".join([x for x in [t1.strip(), t2.strip()] if x]) if (t1 or t2) else ""
    if not txt:
        return ""
    return (
        "<span style='display:inline-block;margin-left:8px;padding:2px 6px;border-radius:999px;"
        "font-size:12px;font-weight:700;background:#dbeafe;color:#1e40af;"
        "border:1px solid rgba(30,64,175,.25);'>"
        f"{escape(txt)}</span>"
    )

# ───────────────────────────── Client registry ─────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = True):
    if not _sb_ok():
        return []
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

# ───────────────────────────── Tours data ─────────────────────────────

@st.cache_data(ttl=120, show_spinner=False)
def fetch_tours_for_client(client_norm: str) -> List[Dict[str, Any]]:
    if not (_sb_ok() and client_norm.strip()):
        return []
    try:
        rows = SUPABASE.table("tours")\
            .select("id, client, client_display, tour_date, created_at")\
            .eq("client", client_norm.strip())\
            .order("tour_date", desc=True)\
            .limit(2000).execute().data or []
        return rows
    except Exception:
        return []

def _invalidate_tours_cache():
    try:
        fetch_tours_for_client.clear()  # type: ignore[attr-defined]
    except Exception:
        pass

@st.cache_data(ttl=120, show_spinner=False)
def fetch_stops_for_tour(tour_id: int) -> List[Dict[str, Any]]:
    if not (_sb_ok() and tour_id):
        return []
    try:
        rows = SUPABASE.table("tour_stops")\
            .select("id, tour_id, address, start, end, deeplink, address_slug")\
            .eq("tour_id", tour_id)\
            .order("start", desc=False)\
            .limit(500).execute().data or []
        return rows
    except Exception:
        return []

def _invalidate_stops_cache():
    try:
        fetch_stops_for_tour.clear()  # type: ignore[attr-defined]
    except Exception:
        pass

# ───────────────────────────── Tours CRUD ─────────────────────────────

def create_tour(client_norm: str, client_display: str, tour_date: date):
    if not _sb_ok():
        return False, "Supabase not configured"
    try:
        payload = {
            "client": client_norm.strip(),
            "client_display": (client_display or "").strip(),
            "tour_date": str(tour_date),
        }
        resp = SUPABASE.table("tours").insert(payload).execute()
        _invalidate_tours_cache()
        return True, (resp.data[0]["id"] if resp.data else None)
    except Exception as e:
        return False, str(e)

def delete_tour(tour_id: int):
    if not _sb_ok():
        return False, "Supabase not configured"
    try:
        SUPABASE.table("tour_stops").delete().eq("tour_id", tour_id).execute()
        SUPABASE.table("tours").delete().eq("id", tour_id).execute()
        _invalidate_tours_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def add_stop(tour_id: int, address: str, start: str, end: str, deeplink: str = ""):
    if not _sb_ok():
        return False, "Supabase not configured"
    try:
        if not deeplink:
            deeplink = _zillow_deeplink_from_addr(address)
        payload = {
            "tour_id": tour_id,
            "address": address.strip(),
            "address_slug": _slug_addr_for_match(address),
            "start": (start or "").strip(),
            "end": (end or "").strip(),
            "deeplink": deeplink.strip(),
        }
        SUPABASE.table("tour_stops").insert(payload).execute()
        _invalidate_stops_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def update_stop(stop_id: int, *, address: Optional[str] = None, start: Optional[str] = None,
                end: Optional[str] = None, deeplink: Optional[str] = None):
    if not _sb_ok():
        return False, "Supabase not configured"
    try:
        patch: Dict[str, Any] = {}
        if address is not None:
            patch["address"] = address.strip()
            patch["address_slug"] = _slug_addr_for_match(address)
        if start is not None:
            patch["start"] = start.strip()
        if end is not None:
            patch["end"] = end.strip()
        if deeplink is not None:
            patch["deeplink"] = deeplink.strip() or _zillow_deeplink_from_addr(address or "")
        if not patch:
            return True, "noop"
        SUPABASE.table("tour_stops").update(patch).eq("id", stop_id).execute()
        _invalidate_stops_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_stop(stop_id: int):
    if not _sb_ok():
        return False, "Supabase not configured"
    try:
        SUPABASE.table("tour_stops").delete().eq("id", stop_id).execute()
        _invalidate_stops_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ───────────────────────────── ShowingTime parsing ─────────────────────────────

_TIME_RE = re.compile(
    r'(\d{1,2}:\d{2}\s?(?:AM|PM|am|pm))\s*(?:–|-|to)\s*(\d{1,2}:\d{2}\s?(?:AM|PM|am|pm))'
)
_DATE_PATTERNS = [
    # Saturday, September 21, 2024
    re.compile(r'([A-Za-z]+day,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})'),
    # September 21, 2024
    re.compile(r'([A-Za-z]+\s+\d{1,2},\s+\d{4})'),
    # 09/21/2024
    re.compile(r'(\d{1,2}/\d{1,2}/\d{4})'),
]
_ADDR_RE = re.compile(
    r'^\s*([0-9A-Za-z#.\- ]+?),\s*([A-Za-z .\-]+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$'
)

def _parse_date_string(s: str) -> Optional[date]:
    s = s.strip()
    for fmt in ("%A, %B %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None

def _normalize_line(l: str) -> str:
    # fix common PDF split artifacts like "W est", "V arina", "W inchester"
    l = l.replace("\xa0", " ")
    l = re.sub(r"([A-Za-z])\s+([A-Za-z])", r"\1\2", l)  # collapse single-letter splits
    l = re.sub(r"\s{2,}", " ", l)
    return l.strip()

def parse_showingtime_text(text: str) -> Tuple[Optional[date], List[Dict[str, str]]]:
    """
    Return (tour_date, stops[]) where each stop = {address, start, end, deeplink}
    Works for text extracted from Print HTML or PDF.
    """
    if not text:
        return None, []

    lines = [ _normalize_line(x) for x in re.split(r"[\r\n]+", text) if x.strip() ]
    tdate: Optional[date] = None
    for line in lines[:25]:
        for pat in _DATE_PATTERNS:
            m = pat.search(line)
            if m:
                tdate = _parse_date_string(m.group(1))
                if tdate:
                    break
        if tdate:
            break

    stops: List[Dict[str, str]] = []
    cur_addr: Optional[str] = None

    for line in lines:
        # Address?
        ma = _ADDR_RE.match(line)
        if ma:
            street_city = f"{ma.group(1)}, {ma.group(2)}"
            st_zip = f"{ma.group(3)} {ma.group(4)}"
            cur_addr = f"{street_city}, {st_zip}"
            continue

        # Time?
        mt = _TIME_RE.search(line)
        if mt and cur_addr:
            s1 = mt.group(1).upper().replace("AM", "AM").replace("PM", "PM")
            s2 = mt.group(2).upper().replace("AM", "AM").replace("PM", "PM")
            stops.append({
                "address": cur_addr,
                "start": s1,
                "end": s2,
                "deeplink": _zillow_deeplink_from_addr(cur_addr)
            })
            # keep cur_addr in case multiple times follow the same address
            continue

    # Dedup identical pairs (address,start,end)
    seen = set()
    uniq: List[Dict[str, str]] = []
    for s in stops:
        key = (s["address"], s["start"], s["end"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(s)
    return tdate, uniq

def parse_showingtime_print_url(url: str) -> Tuple[Optional[date], List[Dict[str, str]], str]:
    """
    Fetch the Print page and parse via text-stripping approach.
    Returns (tour_date, stops, err)
    """
    if not url:
        return None, [], "Empty URL"
    try:
        r = requests.get(url, timeout=15)
        if not r.ok:
            return None, [], f"HTTP {r.status_code}"
        # Convert HTML to text crudely and reuse text parser
        html = r.text
        txt = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
        txt = re.sub(r"</p>", "\n", txt, flags=re.I)
        txt = re.sub(r"<[^>]+>", " ", txt, flags=re.I)
        txt = re.sub(r"\s{2,}", " ", txt)
        tdate, stops = parse_showingtime_text(txt)
        if not stops:
            return tdate, [], "No stops found on Print page"
        return tdate, stops, ""
    except Exception as e:
        return None, [], f"{e}"

def parse_showingtime_pdf(file_bytes: bytes) -> Tuple[Optional[date], List[Dict[str, str]], str]:
    """
    Read a ShowingTime Tour PDF into text, then parse.
    Returns (tour_date, stops, err)
    """
    if not PyPDF2:
        return None, [], "PyPDF2 not installed. Add PyPDF2 to requirements.txt"
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        text_parts = []
        for page in reader.pages:
            try:
                text_parts.append(page.extract_text() or "")
            except Exception:
                continue
        full = "\n".join(text_parts)
        tdate, stops = parse_showingtime_text(full)
        if not stops:
            return tdate, [], "No stops found in PDF"
        return tdate, stops, ""
    except Exception as e:
        return None, [], f"{e}"

# ───────────────────────────── UI blocks ─────────────────────────────

def _client_row(name: str, norm: str, cid: int, active: bool):
    # One-line row with name + status pill + ▦ Open Tours
    col_name, col_tours = st.columns([8, 1])
    with col_name:
        st.markdown(
            f"<span class='client-name'>{escape(name)}</span> "
            f"<span class='pill {'active' if active else ''}'>{'active' if active else 'inactive'}</span>",
            unsafe_allow_html=True
        )
    with col_tours:
        if st.button("▦", key=f"open_tours_{cid}", help="Open Tours report"):
            _qp_set(tclient=norm, tour="")
            _safe_rerun()
    st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:4px 0 2px 0;'></div>", unsafe_allow_html=True)

def _render_client_tours_panel(client_norm: str, client_display: str):
    st.markdown(f"### Tours for {escape(client_display)}", unsafe_allow_html=True)

    # Allow switching the client here (no assumptions)
    all_clients = fetch_clients(include_inactive=True)
    name_to_norm = {c["name"]: c.get("name_norm","") for c in all_clients}
    names_sorted = sorted(name_to_norm.keys(), key=lambda s: s.lower())

    cur_name = next((n for n, nn in name_to_norm.items() if nn == client_norm), client_display)
    sel_name = st.selectbox("Switch client", names_sorted, index=(names_sorted.index(cur_name) if cur_name in names_sorted else 0))
    sel_norm = name_to_norm.get(sel_name, client_norm)
    if sel_norm != client_norm:
        _qp_set(tclient=sel_norm, tour="")
        _safe_rerun()

    # Add a tour for this client
    with st.expander("➕ Add a tour", expanded=False):
        tdate = st.date_input("Tour date", value=date.today(), key="__new_tour_date__")
        if st.button("Add tour", key="__add_tour_btn__", use_container_width=True):
            ok, info = create_tour(sel_norm, sel_name, tdate)
            if ok:
                st.success("Tour created.")
                _safe_rerun()
            else:
                st.error(f"Create failed: {info}")

    tours = fetch_tours_for_client(sel_norm)
    if not tours:
        st.info("No tours for this client yet.")
        return

    # Tour list + actions
    st.markdown("#### All tours")
    for t in tours:
        tid = t["id"]
        tdate = t.get("tour_date") or ""
        stops = fetch_stops_for_tour(tid)
        count = len(stops)

        colA, colB, colC, colD = st.columns([6, 2, 1, 1])
        with colA:
            st.markdown(f"{_tour_badge(str(tdate))} &nbsp; <strong>{count} stop{'s' if count!=1 else ''}</strong>", unsafe_allow_html=True)
        with colB:
            if st.button("Open", key=f"__open_tour_{tid}"):
                _qp_set(tclient=sel_norm, tour=str(tid))
                _safe_rerun()
        with colC:
            if st.button("⟳", key=f"__refresh_tour_{tid}", help="Refresh this tour"):
                _invalidate_stops_cache()
                _safe_rerun()
        with colD:
            if st.button("⌫", key=f"__del_tour_{tid}", help="Delete this tour"):
                ok, info = delete_tour(tid)
                if ok:
                    st.success("Tour deleted.")
                else:
                    st.error(f"Delete failed: {info}")
                _safe_rerun()

        st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:6px 0 10px 0;'></div>", unsafe_allow_html=True)

    # Selected tour details
    tid_qp = _qp_get("tour", "")
    try:
        tid_sel = int(tid_qp) if tid_qp else None
    except Exception:
        tid_sel = None

    if tid_sel:
        st.markdown("---")
        st.markdown("#### Tour details")
        stops = fetch_stops_for_tour(tid_sel)

        if not stops:
            st.info("No stops yet. Add the first stop below.")
        else:
            for s in stops:
                sid = s["id"]
                addr = s.get("address") or ""
                start = s.get("start") or ""
                end = s.get("end") or ""
                deeplink = s.get("deeplink") or _zillow_deeplink_from_addr(addr)

                col1, col2, col3 = st.columns([8, 1, 1])
                with col1:
                    a_html = (
                        f'<a href="{escape(deeplink)}" target="_blank" rel="noopener">{escape(addr)}</a>'
                        f' { _time_chip(start, end) }'
                    )
                    st.markdown(a_html, unsafe_allow_html=True)
                with col2:
                    if st.button("✎", key=f"__edit_stop_{sid}", help="Edit stop"):
                        st.session_state[f"__edit_{sid}"] = True
                with col3:
                    if st.button("⌫", key=f"__del_stop_{sid}", help="Delete stop"):
                        ok, info = delete_stop(sid)
                        if ok:
                            st.success("Stop deleted.")
                        else:
                            st.error(f"Delete failed: {info}")
                        _safe_rerun()

                # Inline editor
                if st.session_state.get(f"__edit_{sid}"):
                    st.markdown("<div class='inline-panel'>", unsafe_allow_html=True)
                    e_addr = st.text_input("Address", value=addr, key=f"__e_addr_{sid}")
                    cc1, cc2 = st.columns(2)
                    with cc1:
                        e_start = st.text_input("Start (e.g. 10:00 AM)", value=start, key=f"__e_start_{sid}")
                    with cc2:
                        e_end = st.text_input("End (e.g. 10:30 AM)", value=end, key=f"__e_end_{sid}")
                    e_link = st.text_input("Link (optional; auto if empty)", value=deeplink, key=f"__e_link_{sid}")
                    ccs, ccc = st.columns([0.2, 0.2])
                    if ccs.button("Save", key=f"__e_save_{sid}"):
                        ok, info = update_stop(sid, address=e_addr, start=e_start, end=e_end, deeplink=e_link)
                        if ok:
                            st.success("Stop updated.")
                        else:
                            st.error(f"Update failed: {info}")
                        st.session_state[f"__edit_{sid}"] = False
                        _safe_rerun()
                    if ccc.button("Cancel", key=f"__e_cancel_{sid}"):
                        st.session_state[f"__edit_{sid}"] = False
                    st.markdown("</div>", unsafe_allow_html=True)

                    st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)

        # Add stop area
        st.markdown("##### Add stop")
        a = st.text_input("Address", key="__new_stop_addr__")
        c1, c2 = st.columns(2)
        with c1:
            s = st.text_input("Start (e.g. 1:00 PM)", key="__new_stop_start__")
        with c2:
            e = st.text_input("End (e.g. 1:30 PM)", key="__new_stop_end__")
        link_default = _zillow_deeplink_from_addr(a) if a else ""
        link = st.text_input("Link (optional; auto if empty)", value=link_default, key="__new_stop_link__")
        if st.button("Add stop", key="__add_stop_btn__", use_container_width=True):
            if not (a or "").strip():
                st.warning("Please enter an address.")
            else:
                ok, info = add_stop(tid_sel, a, s, e, link)
                if ok:
                    st.success("Stop added.")
                    _safe_rerun()
                else:
                    st.error(f"Add failed: {info}")

# ───────────────────────────── ShowingTime Import UI ─────────────────────────────

def _render_showingtime_importer(default_client_norm: Optional[str]):
    st.markdown("### Import from ShowingTime (URL / PDF)")
    st.caption("Paste a **Tour Print URL** or drop the **exported Tour PDF**. We’ll extract the date, addresses, and times.")

    colL, colR = st.columns([2, 1])
    with colL:
        print_url = st.text_input("Print URL", placeholder="https://scheduling.showingtime.com/.../Tour/Print/30235965", key="__st_print_url__")
    with colR:
        uploaded_pdf = st.file_uploader("Or upload Tour PDF", type=["pdf"], key="__st_pdf__")

    parsed = st.session_state.get("__st_parsed__") or {}
    tdate_parsed: Optional[date] = parsed.get("tdate")
    stops_parsed: List[Dict[str,str]] = parsed.get("stops") or []
    parse_err: str = parsed.get("err") or ""

    if st.button("Parse", key="__st_parse_btn__", use_container_width=True):
        tdate_parsed, stops_parsed, parse_err = None, [], ""
        if (print_url or "").strip():
            tdate_parsed, stops_parsed, parse_err = parse_showingtime_print_url(print_url.strip())
        elif uploaded_pdf is not None:
            file_bytes = uploaded_pdf.getvalue()
            tdate_parsed, stops_parsed, parse_err = parse_showingtime_pdf(file_bytes)
        else:
            parse_err = "Provide a Print URL or upload a PDF."

        st.session_state["__st_parsed__"] = {"tdate": tdate_parsed, "stops": stops_parsed, "err": parse_err}
        _safe_rerun()

    if parse_err:
        st.warning(parse_err)

    if stops_parsed:
        # Preview: clickable address + time chip
        badge = _tour_badge(tdate_parsed.isoformat() if tdate_parsed else "No date detected")
        st.markdown(f"#### Preview {badge}", unsafe_allow_html=True)
        for s in stops_parsed:
            addr = s.get("address","")
            start = s.get("start","")
            end = s.get("end","")
            link = s.get("deeplink") or _zillow_deeplink_from_addr(addr)
            st.markdown(
                f'<a href="{escape(link)}" target="_blank" rel="noopener">{escape(addr)}</a> { _time_chip(start, end) }',
                unsafe_allow_html=True
            )

        # Save to client
        all_clients = fetch_clients(include_inactive=True)
        if not all_clients:
            st.info("No clients found. Add a client in the Clients tab to save this tour.")
            return

        # Build selection list: active first, then divider, then inactive
        active = [c for c in all_clients if c.get("active")]
        inactive = [c for c in all_clients if not c.get("active")]
        act_names = [c["name"] for c in active]
        inact_names = [c["name"] for c in inactive]
        names = act_names + (["— Inactive —"] if inact_names else []) + inact_names
        name_to_norm = {c["name"]: c.get("name_norm","") for c in all_clients}

        # Choose client
        # Try to preselect the default client if provided
        def_idx = 0
        if default_client_norm:
            dn = next((c["name"] for c in all_clients if c.get("name_norm")==default_client_norm), None)
            if dn and dn in names:
                def_idx = names.index(dn)

        sel_name = st.selectbox("Save to client", names, index=def_idx)
        if sel_name == "— Inactive —":
            st.warning("Pick a specific inactive client, not the divider.")
            return
        sel_norm = name_to_norm.get(sel_name, "")

        c1, c2 = st.columns([1, 1])
        with c1:
            tdate_final = st.date_input("Tour date", value=(tdate_parsed or date.today()), key="__st_save_date__")
        with c2:
            if st.button("Save all stops", key="__st_save_btn__", use_container_width=True):
                ok, tour_id_or_err = create_tour(sel_norm, sel_name, tdate_final)
                if not ok or not tour_id_or_err:
                    st.error(f"Create tour failed: {tour_id_or_err}")
                else:
                    tid = int(tour_id_or_err)
                    errs = 0
                    for s in stops_parsed:
                        a = s.get("address","")
                        start = s.get("start","")
                        end = s.get("end","")
                        link = s.get("deeplink") or _zillow_deeplink_from_addr(a)
                        ok_stop, info_stop = add_stop(tid, a, start, end, link)
                        if not ok_stop:
                            errs += 1
                    if errs == 0:
                        st.success("Tour and all stops saved.")
                        # Open that client's tours
                        _qp_set(tclient=sel_norm, tour=str(tid))
                        # Clear parsed cache
                        st.session_state["__st_parsed__"] = {}
                        _safe_rerun()
                    else:
                        st.warning(f"Saved with {errs} error(s).")

# ───────────────────────────── Public entry ─────────────────────────────

def render_tours_tab(state: dict):
    st.subheader("Tours")
    st.caption("Active and Inactive clients are side-by-side. Import a ShowingTime tour or add/manage tours for any client.")

    if not _sb_ok():
        st.warning("Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE.")
        return

    # Side-by-side clients lists
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

    # Global Add / Manage section
    st.markdown("---")
    st.markdown("### Add / Manage Tours")

    if not all_clients:
        st.info("No clients yet. Add a client in the Clients tab.")
        # You can still import & preview below, but saving needs a client.
        _render_showingtime_importer(default_client_norm=None)
        return

    # Build list for client picker (active first, divider, then inactive)
    act_names = [c["name"] for c in active]
    inact_names = [c["name"] for c in inactive]
    names = act_names + (["— Inactive —"] if inact_names else []) + inact_names
    name_to_norm = {c["name"]: c.get("name_norm","") for c in all_clients}

    sel_name = st.selectbox("Client", names, index=0 if names else None, key="__tours_global_client__")
    if sel_name == "— Inactive —":
        st.warning("Pick a specific inactive client, not the divider.")
        sel_norm = ""
    else:
        sel_norm = name_to_norm.get(sel_name, "")

    c1, c2 = st.columns([2, 1])
    with c1:
        tdate = st.date_input("Tour date", value=date.today(), key="__global_new_tour_date__")
    with c2:
        if st.button("➕ Add tour", key="__global_add_tour_btn__", use_container_width=True) and sel_norm:
            ok, info = create_tour(sel_norm, sel_name, tdate)
            if ok:
                st.success("Tour created.")
                _qp_set(tclient=sel_norm, tour="")
                _safe_rerun()
            else:
                st.error(f"Create failed: {info}")

    if sel_norm and st.button("▦ Open Tours report", key="__open_report_btn__", use_container_width=True):
        _qp_set(tclient=sel_norm, tour="")
        _safe_rerun()

    # ShowingTime importer is always available below (can use selected client as default)
    st.markdown("---")
    _render_showingtime_importer(default_client_norm=(sel_norm or None))

    # If a report is already open (via ▦ in tables or manage button), show it below
    tclient = _qp_get("tclient", "")
    if tclient:
        display_name = next((c["name"] for c in all_clients if c.get("name_norm")==tclient), tclient)
        st.markdown("---")
        _render_client_tours_panel(tclient, display_name)
