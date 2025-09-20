# ui/tours_tab.py
# Tours tab with robust "Parse → Add all tours" flow, side-by-side clients with ▦ Report,
# per-tour delete, and high-contrast date/time badges. Parser improved for ShowingTime quirks.

import os
import re
import io
from datetime import date, datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape

import streamlit as st
import requests
from supabase import create_client, Client

# Optional PDF parser for ShowingTime PDFs
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

NO_CLIENT_OPT = "➤ No client (show ALL, no logging)"

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


# ───────────────────────────── Badges / chips (high contrast) ─────────────────────────────

def _tour_badge(text: str) -> str:
    return (
        "<span style='display:inline-block;padding:2px 10px;border-radius:999px;"
        "font-size:12px;font-weight:900;background:#f59e0b1a;color:#92400e;"
        "border:1px solid #f59e0b;'>"
        f"{escape(text)}</span>"
    )

def _time_chip(t1: str, t2: str) -> str:
    txt = "–".join([x for x in [t1.strip(), t2.strip()] if x]) if (t1 or t2) else ""
    if not txt:
        return ""
    return (
        "<span style='display:inline-block;margin-left:8px;padding:2px 8px;border-radius:999px;"
        "font-size:12px;font-weight:800;background:#1e40af1a;color:#1e3a8a;"
        "border:1px solid #60a5fa;'>"
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
        return True, "ok"
    except Exception as e:
        return False, str(e)


# ───────────────────────────── ShowingTime parsing (improved) ─────────────────────────────

# Accept "9:00 AM-10:15 AM", "9:00AM - 10:15AM", "9:00 AM – 10:15 AM"
_TIME_RE = re.compile(
    r'(\d{1,2}:\d{2}\s?(?:AM|PM|am|pm))\s*(?:–|-|to)\s*(\d{1,2}:\d{2}\s?(?:AM|PM|am|pm))'
)

# Broader date capture variants
_DATE_PATTERNS = [
    re.compile(r'([A-Za-z]+day,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})'),  # Saturday, September 21, 2024
    re.compile(r'([A-Za-z]+\s+\d{1,2},\s+\d{4})'),                  # September 21, 2024
    re.compile(r'(\d{1,2}/\d{1,2}/\d{4})'),                         # 09/21/2024
]

# Relaxed address pattern; street may be anything up to city, optional comma before city
_ADDR_RELAX = re.compile(
    r'^\s*(?P<street>.+?)\s*,?\s*(?P<city>[A-Za-z .\-]+?)\s*,\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$'
)

def _parse_date_string(s: str) -> Optional[date]:
    s = s.strip()
    for fmt in ("%A, %B %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None

def _clean_unicode(s: str) -> str:
    # Normalize unicode dashes and spaces
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    s = s.replace("\xa0", " ")
    return s

def _fix_common_breaks(s: str) -> str:
    """
    Fix common ShowingTime PDF/HTML text breaks like 'W est' -> 'West' (but keep real word gaps),
    and stray line artifacts.
    """
    s = _clean_unicode(s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    # Turn 'W est', 'E xample' into 'West', 'Example'
    s = re.sub(r"\b([A-Z])\s+([a-z]{2,})\b", r"\1\2", s)
    # Keep words separated otherwise
    return s

def _lines_from_html(html: str) -> str:
    # Convert common HTML separators to newlines, drop tags
    txt = re.sub(r"(?i)<br\s*/?>", "\n", html)
    txt = re.sub(r"(?i)</p>", "\n", txt)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = _clean_unicode(txt)
    txt = re.sub(r"\s{2,}", " ", txt)
    return txt

def _extract_text_from_pdf(file_bytes: bytes) -> str:
    if not PyPDF2:
        return ""
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        parts = []
        for pg in reader.pages:
            try:
                parts.append(pg.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts)
    except Exception:
        return ""

def parse_showingtime_text(text: str) -> Tuple[Optional[date], List[Dict[str, str]], str, str]:
    """
    Return (tour_date, stops[{address,start,end,deeplink}], error, debug_text).
    Robust logic:
      • cleans unicode
      • merges two consecutive lines when needed to find address
      • then finds time on the same or next 2 lines
    """
    if not text:
        return None, [], "Empty input", ""

    raw_text = text  # keep for debug
    text = _clean_unicode(text)
    # Split into lines; keep order
    raw_lines = re.split(r"[\r\n]+", text)
    lines = [_fix_common_breaks(l) for l in raw_lines if l.strip()]

    # Detect tour date near the top
    tdate: Optional[date] = None
    for line in lines[:40]:
        for pat in _DATE_PATTERNS:
            m = pat.search(line)
            if m:
                tdate = _parse_date_string(m.group(1))
                if tdate:
                    break
        if tdate:
            break

    stops: List[Dict[str, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        L0 = lines[i].strip()

        # Try current line as address
        m = _ADDR_RELAX.match(L0)

        used_next = False
        if not m and i + 1 < n:
            # Try merging with next line (addresses often wrap)
            L01 = (L0 + " " + lines[i + 1].strip())
            L01 = _fix_common_breaks(L01)
            m = _ADDR_RELAX.match(L01)
            used_next = bool(m)

        if m:
            street = m.group("street").strip().rstrip(",")
            city   = m.group("city").strip()
            state  = m.group("state").strip()
            zcode  = m.group("zip").strip()
            addr_full = f"{street}, {city}, {state} {zcode}"

            # After an address, search for time on this or next two lines
            time_found = False
            for j in range(i + 1, min(i + 4, n)):
                mt = _TIME_RE.search(lines[j])
                if mt:
                    s1 = mt.group(1).upper().replace("AM", "AM").replace("PM", "PM")
                    s2 = mt.group(2).upper().replace("AM", "AM").replace("PM", "PM")
                    stops.append({
                        "address": addr_full,
                        "start": s1,
                        "end": s2,
                        "deeplink": _zillow_deeplink_from_addr(addr_full),
                    })
                    time_found = True
                    break

            # If no explicit time line is found, still register the stop with empty times
            if not time_found:
                stops.append({
                    "address": addr_full,
                    "start": "",
                    "end": "",
                    "deeplink": _zillow_deeplink_from_addr(addr_full),
                })

            # Advance pointer
            i += 2 if used_next else 1
            continue

        i += 1

    # Deduplicate by (address,start,end)
    seen = set()
    uniq: List[Dict[str, str]] = []
    for s in stops:
        key = (s["address"], s["start"], s["end"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(s)

    if not uniq:
        return tdate, [], "No stops found. Double-check that this is a ShowingTime tour Print page or PDF.", "\n".join(lines)

    return tdate, uniq, "", "\n".join(lines)

def parse_showingtime_print_url(url: str) -> Tuple[Optional[date], List[Dict[str, str]], str, str]:
    if not url:
        return None, [], "Empty URL", ""
    try:
        r = requests.get(url, timeout=20)
        if not r.ok:
            return None, [], f"HTTP {r.status_code}", ""
        html = r.text
        txt = _lines_from_html(html)
        return parse_showingtime_text(txt)
    except Exception as e:
        return None, [], f"{e}", ""

def parse_showingtime_pdf(file_bytes: bytes) -> Tuple[Optional[date], List[Dict[str, str]], str, str]:
    if not PyPDF2:
        return None, [], "PyPDF2 not installed. Add PyPDF2 to requirements.txt", ""
    txt = _extract_text_from_pdf(file_bytes)
    if not txt:
        return None, [], "Could not extract text from PDF", ""
    return parse_showingtime_text(txt)


# ───────────────────────────── UI Helpers ─────────────────────────────

def _client_select(names: List[str], default_to_no_client: bool = True, key: str = "__tours_client_sel__"):
    idx = (len(names) - 1) if default_to_no_client else 0
    return st.selectbox("Client", names, index=idx, key=key)

def _client_row_with_report(name: str, norm: str, cid: int, active: bool):
    col_name, col_btn = st.columns([8, 1])
    with col_name:
        st.markdown(
            f"<span class='client-name'>{escape(name)}</span> "
            f"<span class='pill {'active' if active else ''}'>{'active' if active else 'inactive'}</span>",
            unsafe_allow_html=True
        )
    with col_btn:
        if st.button("▦", key=f"tourrep_{cid}", help="Open tours report"):
            st.session_state["__tours_report_for__"] = {"norm": norm, "name": name}
            _safe_rerun()
    st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:4px 0 2px 0;'></div>", unsafe_allow_html=True)

def _render_client_tours_report(client_norm: str, client_name: str):
    st.markdown(f"### Tours report for {escape(client_name)}", unsafe_allow_html=True)
    if st.button("Close report", key="__close_tours_report__"):
        st.session_state.pop("__tours_report_for__", None)
        _safe_rerun()

    tours = fetch_tours_for_client(client_norm)
    if not tours:
        st.info("No tours logged for this client yet.")
        return

    for t in tours:
        tid = int(t["id"])
        tdate = str(t.get("tour_date") or "")
        badge = _tour_badge(tdate if tdate else "No date")
        st.markdown(f"**Tour** {badge}", unsafe_allow_html=True)

        # Delete/Undo controls
        confirm_key = f"__confirm_del_{tid}"
        if st.session_state.get(confirm_key):
            d1, d2 = st.columns([0.25, 0.25])
            with d1:
                if st.button("Confirm delete", key=f"del_yes_{tid}"):
                    ok, info = delete_tour(tid)
                    st.session_state.pop(confirm_key, None)
                    if ok:
                        st.success("Tour deleted.")
                        _safe_rerun()
                    else:
                        st.error(f"Delete failed: {info}")
            with d2:
                if st.button("Cancel", key=f"del_no_{tid}"):
                    st.session_state.pop(confirm_key, None)
        else:
            if st.button("⌫ Delete tour", key=f"del_{tid}"):
                st.session_state[confirm_key] = True

        stops = fetch_stops_for_tour(tid)
        if not stops:
            st.write("_No stops saved for this tour_")
            continue

        lines = []
        for s in stops:
            a = s.get("address","")
            start = s.get("start","")
            end = s.get("end","")
            link = s.get("deeplink") or _zillow_deeplink_from_addr(a)
            lines.append(f'<li><a href="{escape(link)}" target="_blank" rel="noopener">{escape(a)}</a> {_time_chip(start, end)}</li>')
        st.markdown("<ul class='link-list'>" + "\n".join(lines) + "</ul>", unsafe_allow_html=True)


# ───────────────────────────── Main UI (Parse → Add all) ─────────────────────────────

def render_tours_tab(state: dict):
    st.subheader("Tours")
    st.caption("Parse a ShowingTime Print URL or Tour PDF, verify, then Add all tours to a client. You can delete any tour later.")

    if not _sb_ok():
        st.warning("Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE.")
        return

    # Build client name list with No client last (default selection)
    all_clients = fetch_clients(include_inactive=True)
    active = [c for c in all_clients if c.get("active")]
    inactive = [c for c in all_clients if not c.get("active")]

    act_names = [c["name"] for c in active]
    inact_names = [c["name"] for c in inactive]
    names = act_names + (["— Inactive —"] if inact_names else []) + inact_names + [NO_CLIENT_OPT]
    name_to_norm = {c["name"]: c.get("name_norm","") for c in all_clients}

    # ── Importer (top)
    st.markdown("### Import tours")
    colL, colR = st.columns([2, 1])
    with colL:
        print_url = st.text_input("Print URL", placeholder="https://scheduling.showingtime.com/.../Tour/Print/30235965", key="__st_print_url__")
    with colR:
        uploaded_pdf = st.file_uploader("Or upload Tour PDF", type=["pdf"], key="__st_pdf__")

    st.markdown("### Verify & add")
    sel_name = _client_select(names, default_to_no_client=True, key="__tours_client_sel__")
    if sel_name == "— Inactive —":
        st.warning("Pick a specific inactive client, not the divider.")
        return
    is_no_client = sel_name == NO_CLIENT_OPT
    client_norm = name_to_norm.get(sel_name, "")

    # Keep parsed results in session for review-before-add
    parsed = st.session_state.get("__st_parsed__") or {}
    tdate_parsed: Optional[date] = parsed.get("tdate")
    stops_parsed: List[Dict[str,str]] = parsed.get("stops") or []
    parse_err: str = parsed.get("err") or ""
    dbg_text: str = parsed.get("dbg") or ""

    # Parse action
    if st.button("Parse", key="__st_parse_btn__", use_container_width=True):
        tdate_parsed, stops_parsed, parse_err, dbg_text = None, [], "", ""
        if (print_url or "").strip():
            tdate_parsed, stops_parsed, parse_err, dbg_text = parse_showingtime_print_url(print_url.strip())
        elif uploaded_pdf is not None:
            file_bytes = uploaded_pdf.getvalue()
            tdate_parsed, stops_parsed, parse_err, dbg_text = parse_showingtime_pdf(file_bytes)
        else:
            parse_err = "Provide a Print URL or upload a PDF."
        st.session_state["__st_parsed__"] = {"tdate": tdate_parsed, "stops": stops_parsed, "err": parse_err, "dbg": dbg_text}
        _safe_rerun()

    if parse_err:
        st.warning(parse_err)
        if dbg_text:
            with st.expander("Debug: extracted text"):
                st.text(dbg_text[:6000])

    # Preview + Add all
    if stops_parsed:
        badge = _tour_badge((tdate_parsed or date.today()).isoformat() if tdate_parsed else "No date detected")
        st.markdown(f"#### Preview {badge}", unsafe_allow_html=True)

        for s in stops_parsed:
            addr = s.get("address","")
            start = s.get("start","")
            end = s.get("end","")
            link = s.get("deeplink") or _zillow_deeplink_from_addr(addr)
            st.markdown(
                f'<a href="{escape(link)}" target="_blank" rel="noopener">{escape(addr)}</a> {_time_chip(start, end)}',
                unsafe_allow_html=True
            )

        st.markdown("---")
        st.markdown("#### Add to client")

        colA, colB = st.columns([1.5, 1])
        with colA:
            final_date = st.date_input("Tour date", value=(tdate_parsed or date.today()), key="__st_save_date__")
        with colB:
            append_existing = st.checkbox("Append to existing tour on this date (if any)", value=False)

        add_disabled = is_no_client or not stops_parsed
        btn = st.button("➕ Add all tours to client", use_container_width=True, disabled=add_disabled)
        if is_no_client:
            st.caption("Select a client to enable adding.")
        if btn and not add_disabled:
            target_tid: Optional[int] = None

            if append_existing:
                same_day = [t for t in fetch_tours_for_client(client_norm) if str(t.get("tour_date") or "") == str(final_date)]
                if same_day:
                    same_day_sorted = sorted(same_day, key=lambda x: x.get("created_at") or "", reverse=True)
                    target_tid = int(same_day_sorted[0]["id"])

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
                st.success("Tour added.")
                st.session_state["__st_parsed__"] = {}
            else:
                st.warning(f"Added with {errs} error(s).")

            if st.button("▦ Open tours report for client", use_container_width=True):
                st.session_state["__tours_report_for__"] = {"norm": client_norm, "name": sel_name}
                _safe_rerun()

    # ── Clients side-by-side with ▦ Report buttons
    st.markdown("---")
    st.markdown("### Clients")

    colAct, colIn = st.columns(2)
    with colAct:
        st.markdown("#### Active", unsafe_allow_html=True)
        if not active:
            st.write("_No active clients_")
        else:
            for c in active:
                _client_row_with_report(c["name"], c.get("name_norm",""), c["id"], active=True)

    with colIn:
        st.markdown("#### Inactive", unsafe_allow_html=True)
        if not inactive:
            st.write("_No inactive clients_")
        else:
            for c in inactive:
                _client_row_with_report(c["name"], c.get("name_norm",""), c["id"], active=False)

    rep = st.session_state.get("__tours_report_for__")
    if rep:
        st.markdown("---")
        _render_client_tours_report(rep.get("norm",""), rep.get("name",""))
