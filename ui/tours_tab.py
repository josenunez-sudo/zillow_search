# ui/tours_tab.py
# Tours tab: parse ShowingTime Print URL or PDF, preview clean links+times,
# then (optionally) add all stops to a client (tours + tour_stops, and log as "sent").
# No debug logs shown in UI. Status badge (CONFIRMED / CANCELED) visible only here.

import os, io, re, json
from datetime import datetime, date
from typing import List, Dict, Any, Optional, Tuple
from html import escape

import requests
import streamlit as st

# Optional PDF parser
try:
    from PyPDF2 import PdfReader  # ensure PyPDF2 is in requirements.txt
except Exception:
    PdfReader = None  # handled gracefully

# ---------- Supabase ----------
from supabase import create_client, Client

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

# ---------- HTTP ----------
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

REQUEST_TIMEOUT = 15

# ---------- Styling ----------
st.markdown("""
<style>
.tour-wrap { border:1px solid rgba(0,0,0,.08); border-radius:12px; padding:12px; }
ul.tour-list { margin: 6px 0 0 1.1rem; padding:0; list-style:disc; }
ul.tour-list li { margin: 0.18rem 0; line-height:1.35; }
.badge { display:inline-block; font-size:11px; font-weight:800; padding:2px 8px; border-radius:999px; margin-left:8px; vertical-align:1px; }
.badge.confirmed { background: #dcfce7; color:#166534; border:1px solid rgba(16,185,129,.35); }
.badge.canceled { background: #fee2e2; color:#991b1b; border:1px solid rgba(239,68,68,.35); }
.inline-controls { display:flex; gap:8px; align-items:center; }
.smallmuted { font-size:12px; color:#64748b; margin-top:2px; }
</style>
""", unsafe_allow_html=True)

# ---------- Utilities ----------
MONTHS = ("January","February","March","April","May","June","July","August","September","October","November","December")

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _slug(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')

def _addr_slug(addr: str) -> str:
    return _slug(re.sub(r'[,]+', ' ', addr))

def _address_to_zillow_rb(addr: str) -> str:
    a = addr.lower()
    a = re.sub(r"[^\w\s,-]", "", a).replace(",", "")
    a = re.sub(r"\s+", "-", a.strip())
    return f"https://www.zillow.com/homes/{a}_rb/"

def _parse_mmddyyyy_from_str(text: str) -> Optional[date]:
    m = re.search(rf"({'|'.join(MONTHS)})\s+(\d{{1,2}}),\s*(\d{{4}})", text, re.I)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}, {m.group(3)}", "%B %d, %Y").date()
    except Exception:
        return None

def _find_buyer_name(text: str) -> Optional[str]:
    m = re.search(r"Buyer['’]s name\s*:\s*([A-Za-z .,'\-]+)", text, re.I)
    if m:
        return _norm(m.group(1))
    m2 = re.search(r"Buyer['’]s?\s*(?:Tour|-)\s*[^\n]*?\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", text)
    if m2:
        return _norm(m2.group(1))
    m3 = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b[^\n]*Stops", text)
    if m3:
        return _norm(m3.group(1))
    return None

def _clean_text_for_parse(text: str) -> str:
    text = re.sub(r"(?<=\b[A-Za-z])\s(?=[a-z])", "", text)  # fix split letters in PDFs
    text = re.sub(r"[ \t]+", " ", text)
    return text

ADDR_RE = re.compile(
    r"(\d{1,6}\s+[A-Za-z0-9 .'\-]+,\s*[A-Za-z .'\-]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?)", re.I
)
TIME_RE = re.compile(
    r"(\d{1,2}:\d{2}\s?(?:AM|PM))\s*[-–]\s*(\d{1,2}:\d{2}\s?(?:AM|PM))", re.I
)

def _nearby_status(chunk: str) -> Optional[str]:
    if re.search(r"\bCONFIRMED\b", chunk, re.I):
        return "confirmed"
    if re.search(r"\bCANCE?LLED?\b", chunk, re.I):
        return "canceled"
    return None

def _extract_stops_from_text(text: str) -> List[Dict[str, str]]:
    stops: List[Dict[str, str]] = []
    idx = 0
    while True:
        m = ADDR_RE.search(text, idx)
        if not m:
            break
        addr = _norm(m.group(1))
        start_window = m.end()
        window = text[start_window:start_window + 600]
        mt = TIME_RE.search(window)
        start_time, end_time = "", ""
        if mt:
            start_time = _norm(mt.group(1))
            end_time   = _norm(mt.group(2))
        status = _nearby_status(window) or ""
        stops.append({
            "address": addr,
            "start": start_time,
            "end": end_time,
            "status": status,
            "deeplink": _address_to_zillow_rb(addr),
            "address_slug": _addr_slug(addr),
        })
        idx = start_window
    # de-dup consecutive identical (addr, times)
    dedup: List[Dict[str, str]] = []
    seen = set()
    for s in stops:
        key = (s["address"], s["start"], s["end"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(s)
    return dedup

def _parse_print_html(url: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            return None, "Could not fetch Print page."
        html = r.text
        safe = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
        safe = re.sub(r"(?is)<style.*?>.*?</style>", " ", safe)
        text = re.sub(r"(?s)<[^>]+>", " ", safe)
        text = _clean_text_for_parse(text)
        tdate = _parse_mmddyyyy_from_str(text)
        buyer = _find_buyer_name(text)
        stops = _extract_stops_from_text(text)
        if not stops:
            return None, "No stops found. Double-check that this is a ShowingTime tour Print page."
        return {
            "print_url": url,
            "buyer": buyer or "",
            "tour_date": tdate.isoformat() if tdate else "",
            "stops": stops
        }, None
    except Exception:
        return None, "No stops found. Double-check that this is a ShowingTime tour Print page."

def _parse_pdf(file: io.BytesIO) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not PdfReader:
        return None, "PyPDF2 not installed. Add PyPDF2 to requirements.txt for PDF parsing."
    try:
        reader = PdfReader(file)
        raw = ""
        for p in reader.pages:
            raw += "\n" + (p.extract_text() or "")
        text = _clean_text_for_parse(raw)
        tdate = _parse_mmddyyyy_from_str(text)
        buyer = _find_buyer_name(text)
        stops = _extract_stops_from_text(text)
        if not stops:
            return None, "No stops found. Double-check that this is a ShowingTime tour PDF."
        return {
            "print_url": "",
            "buyer": buyer or "",
            "tour_date": tdate.isoformat() if tdate else "",
            "stops": stops
        }, None
    except Exception:
        return None, "No stops found. Double-check that this is a ShowingTime tour PDF."

# ---------- Clients ----------
@st.cache_data(ttl=90, show_spinner=False)
def _fetch_clients(include_inactive: bool = True) -> List[Dict[str, Any]]:
    if not SUPABASE:
        return []
    try:
        resp = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute()
        rows = resp.data or []
        # hide "test test"
        rows = [r for r in rows if (r.get("name_norm") or "") != "test test"]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

def _client_options_for_norm_dropdown() -> Tuple[List[str], List[Optional[str]]]:
    NO_CLIENT = "➤ No client (show ALL, no logging)"
    all_clients = _fetch_clients(include_inactive=True)
    names = [c["name"] for c in all_clients]
    norms = [c.get("name_norm") for c in all_clients]
    labels = [NO_CLIENT] + names
    keys   = [None] + norms
    return labels, keys

def _client_display_dropdown_options() -> List[str]:
    all_clients = _fetch_clients(include_inactive=True)
    return [c["name"] for c in all_clients]

# ---------- DB insertions ----------
def _insert_tour_and_stops(
    client_norm: Optional[str],
    client_display: Optional[str],
    tour_date: Optional[date],
    print_url: Optional[str],
    stops: List[Dict[str, str]],
) -> Tuple[Optional[int], Optional[str]]:
    if not SUPABASE:
        return None, "Supabase not configured."

    # If No client selected (No logging), skip DB writes
    if not client_norm:
        return None, None

    try:
        payload = {
            "client": client_norm or None,
            "client_display": client_display or None,
            "tour_date": (tour_date.isoformat() if isinstance(tour_date, date) else None),
            "url": (print_url or None),
            # IMPORTANT: set a status that passes your check constraint (fix for 'parsed')
            "status": "requested",
        }
        t_res = SUPABASE.table("tours").insert(payload).execute()
        data = (t_res.data or [])
        if not data:
            return None, "Create tour failed (no return)."
        tour_id = data[0].get("id")
        if not tour_id:
            return None, "Create tour failed (no id)."

        stop_rows = []
        for s in stops:
            stop_rows.append({
                "tour_id": tour_id,
                "address": s.get("address") or "",
                "address_slug": s.get("address_slug") or "",
                "start": s.get("start") or "",
                "end": s.get("end") or "",
                "deeplink": s.get("deeplink") or "",
                "status": (s.get("status") or None),
            })
        if stop_rows:
            SUPABASE.table("tour_stops").insert(stop_rows).execute()
        return int(tour_id), None
    except Exception as e:
        try:
            err = getattr(e, "args", [])
            if err and isinstance(err[0], dict):
                return None, f"Create tour failed: {json.dumps(err[0])}"
        except Exception:
            pass
        return None, f"Create tour failed: {e!r}"

def _log_stops_to_sent(client_norm: str, client_display: str, stops: List[Dict[str, str]], tour_date: Optional[date]) -> None:
    if not SUPABASE or not client_norm or not stops:
        return
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    campaign = f"toured-{(tour_date.isoformat() if tour_date else datetime.utcnow().date().isoformat())}"
    rows = []
    for s in stops:
        url = s.get("deeplink") or ""
        addr = s.get("address") or ""
        rows.append({
            "client": client_norm,
            "campaign": campaign,
            "url": url or None,
            "canonical": None,
            "zpid": None,
            "mls_id": None,
            "address": addr or None,
            "sent_at": now_iso,
        })
    if rows:
        try:
            SUPABASE.table("sent").insert(rows).execute()
        except Exception:
            pass

# ---------- Session state ----------
def _get_parsed() -> Dict[str, Any]:
    return st.session_state.get("__tours_parsed__", {})

def _set_parsed(payload: Dict[str, Any]):
    st.session_state["__tours_parsed__"] = payload

def _clear_parsed():
    st.session_state.pop("__tours_parsed__", None)

def _remove_stop(i: int):
    payload = _get_parsed()
    stops = payload.get("stops") or []
    if 0 <= i < len(stops):
        del stops[i]
        payload["stops"] = stops
        _set_parsed(payload)

# ---------- Renderer ----------
def render_tours_tab(state: dict):
    st.subheader("Tours")

    # Import controls
    col_in1, col_in2 = st.columns([1.4, 1])
    with col_in1:
        print_url = st.text_input("Paste ShowingTime Print URL", value="", placeholder="https://scheduling.showingtime.com/.../Tour/Print/30235965")
    with col_in2:
        file = st.file_uploader("Or drop a Tour PDF", type=["pdf"])

    col_btn1, col_btn2 = st.columns([0.15, 0.15])
    with col_btn1:
        do_parse = st.button("Parse", use_container_width=True)
    with col_btn2:
        do_clear = st.button("Clear", use_container_width=True)

    if do_clear:
        _clear_parsed()

    if do_parse:
        payload, err = (None, None)
        if (print_url or "").strip():
            payload, err = _parse_print_html(print_url.strip())
        elif file is not None:
            try:
                payload, err = _parse_pdf(io.BytesIO(file.read()))
            except Exception:
                payload, err = None, "No stops found. Double-check that this is a ShowingTime tour PDF."
        else:
            err = "Provide a Print URL or a Tour PDF."

        if err:
            st.error(err)
        elif payload:
            _set_parsed(payload)

    parsed = _get_parsed()
    stops: List[Dict[str, str]] = parsed.get("stops") or []
    tour_date_iso = parsed.get("tour_date") or ""
    buyer = parsed.get("buyer") or ""
    purl = parsed.get("print_url") or ""

    if stops:
        # Summary (no verbose logs)
        bits = [f"Parsed **{len(stops)}** stop{'s' if len(stops)!=1 else ''}"]
        if buyer: bits.append(escape(buyer))
        if tour_date_iso: bits.append(escape(tour_date_iso))
        st.markdown(" • ".join(bits), unsafe_allow_html=True)

        # Preview list with remove buttons
        st.markdown("<div class='tour-wrap'>", unsafe_allow_html=True)
        for i, s in enumerate(stops):
            addr = s.get("address","")
            href = s.get("deeplink","") or _address_to_zillow_rb(addr)
            begin = s.get("start","")
            end   = s.get("end","")
            status = (s.get("status") or "").lower()
            badge = ""
            if status == "confirmed":
                badge = "<span class='badge confirmed'>CONFIRMED</span>"
            elif status == "canceled":
                badge = "<span class='badge canceled'>CANCELED</span>"

            colL, colR = st.columns([0.9, 0.1])
            with colL:
                st.markdown(
                    f"<li style='list-style: none;'><a href='{escape(href)}' target='_blank' rel='noopener'>{escape(addr)}</a>"
                    f"{(' — ' + escape(begin) + '–' + escape(end)) if (begin and end) else ''}"
                    f"{badge}</li>",
                    unsafe_allow_html=True
                )
            with colR:
                if st.button("✕", key=f"rm_{i}", help="Remove this stop", use_container_width=True):
                    _remove_stop(i)
                    st.experimental_rerun()
        st.markdown("</div>", unsafe_allow_html=True)
        st.caption("Review (and remove) any stops before adding.")

        # ---- Add all stops to client section ----
        st.markdown("---")
        st.markdown("**Add all stops** (optional)")
        labels, keys = _client_options_for_norm_dropdown()
        idx_norm = st.selectbox(
            "Choose client for logging",
            list(range(len(labels))),
            format_func=lambda i: labels[i],
            index=0,  # first is always "No client"
        )
        chosen_norm = keys[idx_norm]  # None == No logging

        # Client display name dropdown (exclude "No client") — only needed if logging
        display_names = _client_display_dropdown_options()
        display_choice = st.selectbox(
            "Client display name (for the tour record)",
            display_names if display_names else ["—"],
            index=0 if display_names else 0,
        ) if chosen_norm else None

        add_col1, add_col2 = st.columns([0.3, 0.7])
        with add_col1:
            if st.button("Add all stops", use_container_width=True, key="add_all"):
                if not chosen_norm:
                    st.success("Checked and ready. Not logged (No client).")
                else:
                    # Parse date once
                    tdate = None
                    try:
                        tdate = datetime.strptime(tour_date_iso, "%Y-%m-%d").date() if tour_date_iso else None
                    except Exception:
                        tdate = None
                    tour_id, err = _insert_tour_and_stops(
                        client_norm=chosen_norm,
                        client_display=display_choice or buyer or "",
                        tour_date=tdate,
                        print_url=(purl or ""),
                        stops=stops,
                    )
                    if err:
                        st.error(err)
                    else:
                        _log_stops_to_sent(chosen_norm, display_choice or buyer or "", stops, tdate)
                        st.success("Tour and stops added.")
        st.caption("Adding will store a tour record and log each stop as 'sent' (campaign = toured-YYYY-MM-DD).")

    else:
        st.info("Paste a ShowingTime Print URL or drop a Tour PDF, then click **Parse**.")
