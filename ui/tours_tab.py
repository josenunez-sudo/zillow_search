# ui/tours_tab.py
# Tours tab: ShowingTime Print URL / PDF → parsed stops → optional insert to Supabase (tours, tour_stops) and sent.
import re, io, os, json
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

import requests
import streamlit as st

# ---------- Optional PDF dep ----------
try:
    import PyPDF2  # type: ignore
    _HAVE_PYPDF2 = True
except Exception:
    _HAVE_PYPDF2 = False

# ---------- Supabase ----------
from supabase import create_client, Client

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

SB: Optional[Client] = _get_supabase()

# ---------- Shared helpers ----------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _address_slug(addr: str) -> str:
    return re.sub(r"[^0-9a-z]+", "-", (addr or "").lower()).strip("-")

def _deeplink_from_address(addr: str) -> str:
    # clean & slug → simple Zillow homes deeplink (good enough for SMS/unfurl + later resolution)
    a = re.sub(r"[^\w\s,-]", "", (addr or "").strip())
    a = a.replace(",", "")
    a = re.sub(r"\s+", "-", a.lower())
    return f"https://www.zillow.com/homes/{a}_rb/"

# ---------- Fetch clients (filter out "test test") ----------
@st.cache_data(ttl=60, show_spinner=False)
def _fetch_clients(include_inactive: bool = True) -> List[Dict[str, Any]]:
    if not SB:
        return []
    try:
        rows = SB.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        if include_inactive:
            return rows
        return [r for r in rows if r.get("active")]
    except Exception:
        return []

# ---------- Minimal styles for badges ----------
_badge_css = """
<style>
.tour-list { margin: 0 0 0.5rem 1.1rem; padding: 0; list-style: disc; }
.tour-list li { margin: 0.25rem 0; }
.badge-mini { display:inline-block; font-size: 11px; font-weight:800; padding:1px 8px; border-radius:999px; margin-left:8px; vertical-align: baseline;}
.badge-ok { background:#dcfce7; color:#065f46; border:1px solid rgba(5,150,105,.35); }
.badge-cancel { background:#fee2e2; color:#991b1b; border:1px solid rgba(185,28,28,.35); }
.badge-unk { background:#e2e8f0; color:#334155; border:1px solid rgba(100,116,139,.25); }
</style>
"""

# ---------- ShowingTime parsing ----------
_TIME_RE = r"(\d{1,2}:\d{2}\s*[AP]M)"
_ADDR_RE = r"\d{1,6}\s+[^\n,]+,\s+[A-Za-z][^,]+,\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?"
_CONF_TOKENS = ("CONFIRMED", "Confirmed")
_CANCEL_TOKENS = ("CANCELED", "CANCELLED", "Canceled", "Cancelled")

def _strip_html(html: str) -> str:
    # Remove tags, normalize spaces/newlines
    txt = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html or "")
    txt = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", txt)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = txt.replace("\xa0", " ")
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\s+\n", "\n", txt)
    txt = re.sub(r"\n\s+", "\n", txt)
    return txt.strip()

def _parse_date_from_text(t: str) -> Optional[str]:
    # Examples: "Buyer's Tour - Monday, September 22, 2025"
    m = re.search(r"Buy(?:er|ers)'?s\s+Tour\s*-\s*([A-Za-z]+,\s*[A-Za-z]+\s+\d{1,2},\s*\d{4})", t)
    if not m:
        # Fallback: find Month Day, Year anywhere
        m = re.search(r"([A-Za-z]+,\s*[A-Za-z]+\s+\d{1,2},\s*\d{4})", t)
    if m:
        raw = m.group(1)
        for fmt in ("%A, %B %d, %Y", "%A,%B %d, %Y", "%B %d, %Y"):
            try:
                dt = datetime.strptime(raw.replace("  ", " "), fmt)
                return dt.strftime("%Y-%m-%d")
            except Exception:
                pass
    return None

def _status_from_chunk(chunk: str) -> str:
    if any(tok in chunk for tok in _CANCEL_TOKENS):
        return "canceled"
    if any(tok in chunk for tok in _CONF_TOKENS):
        return "confirmed"
    return "scheduled"

def _extract_stops_from_text(text: str) -> List[Dict[str, str]]:
    # Find each address, then capture the nearest time range right after it
    stops: List[Dict[str, str]] = []
    # Make a list of matches with their spans
    addrs = [(m.group(0), m.start(), m.end()) for m in re.finditer(_ADDR_RE, text)]
    if not addrs:
        return []

    for i, (addr, s, e) in enumerate(addrs):
        nxt = addrs[i+1][1] if i+1 < len(addrs) else len(text)
        window = text[e:nxt]
        m_time = re.search(rf"{_TIME_RE}\s*-\s*{_TIME_RE}", window)
        start_t, end_t = "", ""
        if m_time:
            start_t = re.sub(r"\s+", " ", m_time.group(1)).strip().upper()
            end_t = re.sub(r"\s+", " ", m_time.group(2)).strip().upper()
        status = _status_from_chunk(window)
        stops.append({
            "address": re.sub(r"\s+", " ", addr).strip(),
            "start": start_t,
            "end": end_t,
            "status": status,
        })
    return stops

def parse_showingtime_print_url(url: str) -> Dict[str, Any]:
    try:
        r = requests.get(url, timeout=15, allow_redirects=True)
        if not r.ok:
            return {"ok": False, "error": f"Fetch failed ({r.status_code})"}
        txt = _strip_html(r.text)
        iso = _parse_date_from_text(txt) or datetime.utcnow().strftime("%Y-%m-%d")
        # Attempt buyer name (optional)
        buyer = ""
        m = re.search(r"Buyer['’]s name:\s*([^\n]+)", txt, re.I)
        if m: buyer = m.group(1).strip()
        stops = _extract_stops_from_text(txt)
        return {"ok": True, "tour_date": iso, "buyer": buyer, "stops": stops}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def parse_showingtime_pdf(file_bytes: bytes) -> Dict[str, Any]:
    if not _HAVE_PYPDF2:
        return {"ok": False, "error": "PyPDF2 not installed. Add PyPDF2 to requirements.txt"}
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        buf = []
        for p in reader.pages:
            try:
                buf.append(p.extract_text() or "")
            except Exception:
                pass
        txt = "\n".join(buf)
        # Normalize
        txt = txt.replace("\u200b", " ")
        txt = re.sub(r"[ \t]+", " ", txt)
        txt = re.sub(r"\s+\n", "\n", txt)
        iso = _parse_date_from_text(txt) or datetime.utcnow().strftime("%Y-%m-%d")
        # Buyer (optional)
        buyer = ""
        m = re.search(r"Buyer['’]s name:\s*([^\n]+)", txt, re.I)
        if m: buyer = m.group(1).strip()
        stops = _extract_stops_from_text(txt)
        return {"ok": True, "tour_date": iso, "buyer": buyer, "stops": stops}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------- Supabase inserts ----------
def _create_tour_row(client_norm: Optional[str], client_display: Optional[str],
                     tour_date_iso: str, url: Optional[str]) -> Tuple[bool, Optional[int], str]:
    if not SB:
        return False, None, "Supabase not configured"
    payload = {
        "client": (client_norm or None),
        "client_display": (client_display or None),
        "tour_date": tour_date_iso,
        "status": "requested",           # IMPORTANT: valid per your CHECK constraint
        "url": (url or None),
    }
    try:
        resp = SB.table("tours").insert(payload).execute()
        rows = (resp.data or []) if hasattr(resp, "data") else []
        tid = rows[0].get("id") if rows else None
        return (True if tid else False), tid, ("ok" if tid else "No ID returned")
    except Exception as e:
        return False, None, f"{e}"

def _insert_tour_stops(tour_id: int, stops: List[Dict[str, str]]) -> Tuple[bool, str]:
    if not SB or not stops:
        return False, "No stops or Supabase not configured"
    rows = []
    for s in stops:
        addr = s.get("address", "")
        rows.append({
            "tour_id": tour_id,
            "address": addr,
            "address_slug": _address_slug(addr),
            "start": s.get("start") or None,
            "end": s.get("end") or None,
            "deeplink": _deeplink_from_address(addr),
            "status": s.get("status") or "scheduled",
        })
    try:
        SB.table("tour_stops").insert(rows).execute()
        return True, "ok"
    except Exception as e:
        return False, f"{e}"

# Optional: log each stop into `sent` when a client is selected
def _log_stops_to_sent(client_norm: str, client_display: str,
                       stops: List[Dict[str, str]], tour_date_iso: str) -> Tuple[bool, str]:
    if not SB or not client_norm or not stops:
        return False, "No client or no stops"
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    campaign = f"tour-{tour_date_iso}"
    rows = []
    for s in stops:
        addr = s.get("address", "")
        url = _deeplink_from_address(addr)
        rows.append({
            "client": client_norm,
            "campaign": campaign,
            "url": url,
            "canonical": None,
            "zpid": None,
            "mls_id": None,
            "address": addr,
            "sent_at": now_iso,
        })
    try:
        SB.table("sent").insert(rows).execute()
        return True, "ok"
    except Exception as e:
        return False, f"{e}"

# ---------- Session helpers ----------
def _get_parsed() -> Optional[Dict[str, Any]]:
    return st.session_state.get("__tour_parsed__")

def _set_parsed(payload: Dict[str, Any]):
    st.session_state["__tour_parsed__"] = payload

def _clear_parsed():
    st.session_state.pop("__tour_parsed__", None)

# ---------- Main renderer ----------
def render_tours_tab(state: dict):
    st.markdown(_badge_css, unsafe_allow_html=True)

    # --- Import block (top) ---
    st.subheader("Import Tour (ShowingTime)")
    colL, colR = st.columns([1.4, 1])
    with colL:
        url_in = st.text_input("ShowingTime Print URL (or sharing link)", value="", placeholder="https://scheduling.showingtime.com/(S(...))/Tour/Print/30235965")
    with colR:
        pdf_file = st.file_uploader("…or drop a Tour PDF", type=["pdf"])

    # Controls row: Parse + Clear on the right
    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        parse_clicked = st.button("Parse Tour", type="primary", use_container_width=True)
    with c2:
        clear_clicked = st.button("Clear", use_container_width=True)
    with c3:
        st.write("")  # spacer

    if clear_clicked:
        _clear_parsed()
        st.success("Cleared.")
        st.stop()

    # --- Parse action ---
    if parse_clicked:
        parsed = None
        if pdf_file is not None:
            if not _HAVE_PYPDF2:
                st.error("PDF parsing requires `PyPDF2`. Add it to your requirements.txt.")
                st.stop()
            content = pdf_file.getvalue()
            parsed = parse_showingtime_pdf(content)
        elif url_in.strip():
            parsed = parse_showingtime_print_url(url_in.strip())
        else:
            st.error("Provide a Print URL or upload a Tour PDF.")
            st.stop()

        if not parsed or not parsed.get("ok"):
            st.error("Could not parse this tour. Please ensure it is the ShowingTime Print page or the exported Tour PDF.")
            st.stop()

        stops = parsed.get("stops") or []
        if not stops:
            st.error("No stops found. Double-check that this is a ShowingTime tour Print page or PDF.")
            st.stop()

        # Attach deeplinks + slugs
        for s in stops:
            s["deeplink"] = _deeplink_from_address(s.get("address", ""))
            s["address_slug"] = _address_slug(s.get("address", ""))

        parsed["source_url"] = (url_in.strip() or None)
        _set_parsed(parsed)

    # --- Show parsed list (if any) ---
    data = _get_parsed()
    if data:
        tour_date = data.get("tour_date") or ""
        stops: List[Dict[str, str]] = data.get("stops") or []

        # Clean display list: time + link + badge
        items = []
        for s in stops:
            start = s.get("start") or ""
            end = s.get("end") or ""
            addr = s.get("address") or ""
            link = s.get("deeplink") or _deeplink_from_address(addr)
            status = (s.get("status") or "scheduled").lower()
            if status == "confirmed":
                badge = "<span class='badge-mini badge-ok'>CONFIRMED</span>"
            elif status == "canceled":
                badge = "<span class='badge-mini badge-cancel'>CANCELED</span>"
            else:
                badge = "<span class='badge-mini badge-unk'>SCHEDULED</span>"
            timetxt = f"{start}–{end}" if (start and end) else (start or end or "")
            head = (timetxt + " • ") if timetxt else ""
            items.append(
                f"<li>{head}<a href='{link}' target='_blank' rel='noopener'>{addr}</a> {badge}</li>"
            )
        st.markdown("<ul class='tour-list'>" + "\n".join(items) + "</ul>", unsafe_allow_html=True)

        st.divider()

        # --- Add All Stops to client + Client display ---
        # clients list (active + inactive), but no "test test"
        clients = _fetch_clients(include_inactive=True)
        client_names = [c["name"] for c in clients]
        # Build options with 'No client' as the LAST option (and default)
        NO_CLIENT = "➤ No client (show ALL, no logging)"
        client_options = client_names + [NO_CLIENT]
        default_idx = len(client_options) - 1  # default: No client
        sel_idx = st.selectbox(
            "Add all stops to client (optional)",
            list(range(len(client_options))),
            format_func=lambda i: client_options[i],
            index=default_idx,
            key="__tour_add_client_idx__",
        )
        selected_name = client_options[sel_idx]
        client_norm = _norm_tag(selected_name) if selected_name != NO_CLIENT else ""

        # Client display dropdown: only real clients (exclude 'test test' and NO_CLIENT)
        display_names = client_names[:]  # already filtered
        # Try to pre-select if a real client was chosen above
        pre_idx = display_names.index(selected_name) if (selected_name in display_names) else 0 if display_names else 0
        client_display = st.selectbox(
            "Client display name (for the tour record)",
            display_names if display_names else ["(none)"],
            index=(pre_idx if display_names else 0),
            key="__tour_display_name__",
        )
        if not display_names:
            client_display = None  # nothing to choose

        # --- Add All + Clear (always clickable) ---
        a1, a2 = st.columns([1, 1])
        with a1:
            add_clicked = st.button("➕ Add All Stops", type="primary", use_container_width=True)
        with a2:
            clear2 = st.button("Clear", use_container_width=True, key="__clear2__")

        if clear2:
            _clear_parsed()
            st.success("Cleared.")
            st.stop()

        if add_clicked:
            # Insert tour (status 'requested' always)
            ok, tour_id, info = _create_tour_row(
                client_norm=(client_norm or None),
                client_display=(client_display if client_display and client_display != "(none)" else None),
                tour_date_iso=tour_date or datetime.utcnow().strftime("%Y-%m-%d"),
                url=data.get("source_url"),
            )
            if not ok or not tour_id:
                st.error(f"Create tour failed: {info}")
                st.stop()

            # Insert stops
            ok2, info2 = _insert_tour_stops(tour_id, stops)
            if not ok2:
                st.error(f"Insert stops failed: {info2}")
                st.stop()

            # If a client is selected (not No client), also log to sent
            if client_norm:
                ok3, info3 = _log_stops_to_sent(client_norm, (client_display or ""), stops, tour_date)
                if not ok3:
                    # Don’t spam logs; just surface minimal error
                    st.warning(f"Added tour, but logging to 'sent' had an issue: {info3}")

            st.success("Tour and stops added.")
            # keep the parsed list visible so you can verify or add again if needed
