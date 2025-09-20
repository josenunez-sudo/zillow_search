# ui/tours_tab.py
# Tours tab: Importer up top + integrated Add/Manage,
# no "Tours for {client}" report panel, side-by-side clients lists remain read-only.

import os
import re
import io
from datetime import date, datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape

import streamlit as st
import requests
from supabase import create_client, Client

# Optional PDF parser
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


# ───────────────────────────── Stops data ─────────────────────────────

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
        # no cache for list-of-stops here; only when viewing stops (not in this layout)
        return True, "ok"
    except Exception as e:
        return False, str(e)


# ───────────────────────────── ShowingTime parsing ─────────────────────────────

_TIME_RE = re.compile(
    r'(\d{1,2}:\d{2}\s?(?:AM|PM|am|pm))\s*(?:–|-|to)\s*(\d{1,2}:\d{2}\s?(?:AM|PM|am|pm))'
)
_DATE_PATTERNS = [
    re.compile(r'([A-Za-z]+day,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})'),  # Saturday, September 21, 2024
    re.compile(r'([A-Za-z]+\s+\d{1,2},\s+\d{4})'),                  # September 21, 2024
    re.compile(r'(\d{1,2}/\d{1,2}/\d{4})'),                         # 09/21/2024
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
    l = l.replace("\xa0", " ")
    l = re.sub(r"([A-Za-z])\s+([A-Za-z])", r"\1\2", l)  # collapse split letters (e.g., W est -> West)
    l = re.sub(r"\s{2,}", " ", l)
    return l.strip()

def parse_showingtime_text(text: str) -> Tuple[Optional[date], List[Dict[str, str]]]:
    """Return (tour_date, stops[{address,start,end,deeplink}])."""
    if not text:
        return None, []
    lines = [ _normalize_line(x) for x in re.split(r"[\r\n]+", text) if x.strip() ]

    # tour date
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

    # stops
    stops: List[Dict[str, str]] = []
    cur_addr: Optional[str] = None
    for line in lines:
        ma = _ADDR_RE.match(line)
        if ma:
            street_city = f"{ma.group(1)}, {ma.group(2)}"
            st_zip = f"{ma.group(3)} {ma.group(4)}"
            cur_addr = f"{street_city}, {st_zip}"
            continue
        mt = _TIME_RE.search(line)
        if mt and cur_addr:
            s1 = mt.group(1).upper().replace("AM", "AM").replace("PM", "PM")
            s2 = mt.group(2).upper().replace("AM", "AM").replace("PM", "PM")
            stops.append({
                "address": cur_addr,
                "start": s1,
                "end": s2,
                "deeplink": _zillow_deeplink_from_addr(cur_addr),
            })
            # keep cur_addr in case multiple lines of times follow

    # dedupe
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
    """Return (tour_date, stops, err) by scraping the Print page as text."""
    if not url:
        return None, [], "Empty URL"
    try:
        r = requests.get(url, timeout=15)
        if not r.ok:
            return None, [], f"HTTP {r.status_code}"
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
    """Return (tour_date, stops, err) by reading the Tour PDF."""
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


# ───────────────────────────── UI: Import + Manage (Top) ─────────────────────────────

def _render_import_and_manage_block():
    st.markdown("### Import / Add Tours")

    if not _sb_ok():
        st.warning("Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE.")
        return

    # Client picker (active first, then inactive)
    all_clients = fetch_clients(include_inactive=True)
    if not all_clients:
        st.info("No clients found. Add a client in the Clients tab.")
        return

    active = [c for c in all_clients if c.get("active")]
    inactive = [c for c in all_clients if not c.get("active")]
    act_names = [c["name"] for c in active]
    inact_names = [c["name"] for c in inactive]
    names = act_names + (["— Inactive —"] if inact_names else []) + inact_names
    name_to_norm = {c["name"]: c.get("name_norm","") for c in all_clients}

    sel_name = st.selectbox("Client", names, index=0, key="__tours_client_sel__")
    if sel_name == "— Inactive —":
        st.warning("Pick a specific inactive client, not the divider.")
        return
    client_norm = name_to_norm.get(sel_name, "")

    # Create empty tour
    col_td, col_btn = st.columns([2, 1])
    with col_td:
        tdate_manual = st.date_input("Tour date", value=date.today(), key="__new_tour_date_top__")
    with col_btn:
        if st.button("➕ Create empty tour", key="__create_empty_tour__", use_container_width=True):
            ok, info = create_tour(client_norm, sel_name, tdate_manual)
            if ok:
                st.success("Tour created.")
                _invalidate_tours_cache()
            else:
                st.error(f"Create failed: {info}")

    st.markdown("---")

    # Importer (Print URL / PDF)
    st.markdown("#### Import from ShowingTime (Print URL or PDF)")
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
        # Preview list: Address hyperlink + time chip
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

        # Choose to create a new tour (on parsed/selected date) or append to an existing tour on that date
        st.markdown("#### Save")
        # Pick final date (defaults to parsed date if present)
        final_date = st.date_input("Tour date to save under", value=(tdate_parsed or date.today()), key="__st_save_date__")

        # Look up existing tours for this client & date
        same_day_tours = [t for t in fetch_tours_for_client(client_norm) if str(t.get("tour_date") or "") == str(final_date)]
        options = ["➕ New tour on this date"]
        if same_day_tours:
            for t in same_day_tours:
                created = t.get("created_at") or ""
                options.append(f"Append to tour #{t['id']} (created {created})")
        choice = st.selectbox("Save into", options, index=0, key="__st_save_into__")

        if st.button("Save all stops", key="__st_save_all__", use_container_width=True):
            # Resolve target tour id
            target_tid: Optional[int] = None
            if choice.startswith("Append to tour #"):
                try:
                    tid_str = choice.split("#", 1)[1].split(" ", 1)[0]
                    target_tid = int(tid_str)
                except Exception:
                    target_tid = None

            if target_tid is None:
                ok, info = create_tour(client_norm, sel_name, final_date)
                if not ok or not info:
                    st.error(f"Create tour failed: {info}")
                    return
                target_tid = int(info)

            errs = 0
            for s in stops_parsed:
                a = s.get("address","")
                start = s.get("start","")
                end = s.get("end","")
                link = s.get("deeplink") or _zillow_deeplink_from_addr(a)
                ok_stop, info_stop = add_stop(target_tid, a, start, end, link)
                if not ok_stop:
                    errs += 1

            if errs == 0:
                st.success("Tour and all stops saved.")
                # clear parsed stash
                st.session_state["__st_parsed__"] = {}
            else:
                st.warning(f"Saved with {errs} error(s).")


# ───────────────────────────── UI: Clients list (side-by-side) ─────────────────────────────

def _client_row(name: str, norm: str, cid: int, active: bool):
    # Simple read-only row: name + status pill
    st.markdown(
        f"<div class='client-row'>"
        f"<div class='client-left'>"
        f"<span class='client-name'>{escape(name)}</span> "
        f"<span class='pill {'active' if active else ''}'>{'active' if active else 'inactive'}</span>"
        f"</div></div>",
        unsafe_allow_html=True
    )
    st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:4px 0 2px 0;'></div>", unsafe_allow_html=True)


# ───────────────────────────── Public entry ─────────────────────────────

def render_tours_tab(state: dict):
    st.subheader("Tours")
    st.caption("Import ShowingTime tours or create empty tours, then view/manage tours in your data tools as needed.")

    # Top: Import / Add integrated
    _render_import_and_manage_block()

    # Side-by-side clients for quick glance
    st.markdown("---")
    st.markdown("### Clients")
    if not _sb_ok():
        st.warning("Supabase is not configured.")
        return

    all_clients = fetch_clients(include_inactive=True)
    active = [c for c in all_clients if c.get("active")]
    inactive = [c for c in all_clients if not c.get("active")]

    colA, colB = st.columns(2)

    with colA:
        st.markdown("#### Active", unsafe_allow_html=True)
        if not active:
            st.write("_No active clients_")
        else:
            for c in active:
                _client_row(c["name"], c.get("name_norm",""), c["id"], active=True)

    with colB:
        st.markdown("#### Inactive", unsafe_allow_html=True)
        if not inactive:
            st.write("_No inactive clients_")
        else:
            for c in inactive:
                _client_row(c["name"], c.get("name_norm",""), c["id"], active=False)
