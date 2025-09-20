# ui/tours_tab.py
# Import Tours: parse ShowingTime Print URL or PDF → preview → save to Supabase
# Also shows a Client Tours Report (with Confirmed/Cancelled per stop).
# Notes:
# - Status (confirmed/cancelled) is stored ONLY in tour_stops.status and ONLY displayed in this tab’s report.
# - We still log stops to 'sent' silently (no status there) for your other tabs.

import os, re, io
from datetime import datetime, date
from typing import Dict, Any, List, Optional, Tuple
from html import escape

import streamlit as st
import requests

# Optional PDF parsing
try:
    import PyPDF2  # ensure PyPDF2 is in requirements.txt
except Exception:
    PyPDF2 = None

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
REQUEST_TIMEOUT = 12
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

# ---------- Helpers ----------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w\s-]+", "", s)
    s = re.sub(r"\s+", "-", s)
    return s.strip("-")

def _address_slug(addr: str) -> str:
    a = re.sub(r"[^\w\s,-/#]", "", (addr or "").lower())
    a = re.sub(r"\s+", " ", a).strip()
    return _slug(a)

def _zillow_deeplink_from_full_address(addr: str) -> str:
    core = re.sub(r"[,]+", "", (addr or "")).strip()
    slug = _slug(core)
    return f"https://www.zillow.com/homes/{slug}_rb/"

def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

# ---------- Clients ----------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = True) -> List[Dict[str, Any]]:
    """Load clients and HIDE any whose normalized name is 'test test'."""
    if not SUPABASE:
        return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active")\
            .order("name", desc=False).execute().data or []
        hide_norm = _norm_tag("test test")
        rows = [r for r in rows if _norm_tag(r.get("name_norm","")) != hide_norm]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

# ---------- DB write helpers ----------
def create_tour(*, client_norm: str, client_display: str, tour_date: date,
                source_url: str = "", buyer: Optional[str] = None) -> Tuple[bool, Any]:
    """Insert into 'tours' and return (True, id). Works with older supabase-py (no .select() after insert)."""
    if not SUPABASE:
        return False, "Supabase not configured."
    try:
        now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        payload = {
            "client": client_norm,
            "client_display": client_display,
            "tour_date": tour_date.isoformat(),
            # satisfy NOT NULL if present:
            "url": (source_url or "manual"),
            "buyer": buyer or None,
            "status": "requested",
            "created_at": now_iso,
        }
        res = SUPABASE.table("tours").insert(payload).execute()
        data = getattr(res, "data", None) or []
        if isinstance(data, list) and data and isinstance(data[0], dict) and "id" in data[0]:
            return True, data[0]["id"]
        # Fallback: fetch most recent created for that client/date
        q = SUPABASE.table("tours").select("id")\
            .eq("client", client_norm)\
            .eq("client_display", client_display)\
            .eq("tour_date", tour_date.isoformat())\
            .order("created_at", desc=True).limit(1).execute()
        qd = getattr(q, "data", None) or []
        if qd and "id" in qd[0]:
            return True, qd[0]["id"]
        return False, "Insert ok but could not retrieve tour id."
    except Exception as e:
        return False, str(getattr(e, "args", [e])[0])

def insert_tour_stops(*, tour_id: int, stops: List[Dict[str, Any]]) -> Tuple[bool, str]:
    if not SUPABASE:
        return False, "Supabase not configured."
    if not stops:
        return False, "No stops to insert."
    try:
        rows = []
        for s in stops:
            rows.append({
                "tour_id": tour_id,
                "address": s.get("address") or "",
                "address_slug": s.get("address_slug") or _address_slug(s.get("address","")),
                "start": s.get("start") or "",
                "end":   s.get("end") or "",
                "status": s.get("status") or "unknown",  # <-- persist status here
                "deeplink": s.get("deeplink") or _zillow_deeplink_from_full_address(s.get("address","")),
            })
        SUPABASE.table("tour_stops").insert(rows).execute()
        return True, "ok"
    except Exception as e:
        return False, str(getattr(e, "args", [e])[0])

def log_sent_for_stops_silent(*, client_norm: str, stops: List[Dict[str, Any]], tour_date: date) -> None:
    """Silently log each stop into 'sent' (no status here)."""
    if not SUPABASE:
        return
    try:
        now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        rows = []
        for s in stops:
            rows.append({
                "client": client_norm,
                "campaign": "tour",
                "url": s.get("deeplink") or _zillow_deeplink_from_full_address(s.get("address","")),
                "canonical": None,
                "zpid": None,
                "mls_id": None,
                "address": s.get("address") or "",
                "sent_at": now_iso,
            })
        if rows:
            SUPABASE.table("sent").insert(rows).execute()
    except Exception:
        pass  # silent

# ---------- DB read helpers (report) ----------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_tours_for_client(client_norm: str) -> List[Dict[str, Any]]:
    if not SUPABASE or not client_norm:
        return []
    try:
        res = SUPABASE.table("tours").select("id,tour_date,created_at,url,buyer,status")\
            .eq("client", client_norm)\
            .order("tour_date", desc=True).execute()
        return getattr(res, "data", None) or []
    except Exception:
        return []

@st.cache_data(ttl=60, show_spinner=False)
def fetch_stops_for_tour(tour_id: int) -> List[Dict[str, Any]]:
    if not SUPABASE or not tour_id:
        return []
    try:
        res = SUPABASE.table("tour_stops").select("id,address,start,end,deeplink,status")\
            .eq("tour_id", tour_id)\
            .order("id", asc=True).execute()
        return getattr(res, "data", None) or []
    except Exception:
        return []

# ---------- Parsing ----------
DATE_PATTERNS = [
    r"Buyer['’]s\s+Tour\s*-\s*([A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})",
    r"Tour\s+Date\s*[:\-]\s*([A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})",
]
TIME_RANGE_RE = re.compile(r"(\d{1,2}:\d{2}\s?(?:AM|PM))\s*-\s*(\d{1,2}:\d{2}\s?(?:AM|PM))", re.I)
ADDR_LINE_RE = re.compile(r"(\d{1,6}\s+[^\n,]+?,\s*[A-Za-z .'\-]+?,\s*[A-Z]{2}\s*\d{5})", re.I)

def _parse_date_from_text(text: str) -> Optional[date]:
    for pat in DATE_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            raw = m.group(1).strip()
            for fmt in ("%A, %B %d, %Y", "%A, %b %d, %Y"):
                try:
                    return datetime.strptime(raw, fmt).date()
                except Exception:
                    pass
    return None

def _parse_buyer_from_text(text: str) -> Optional[str]:
    m = re.search(r"Buyer['’]s\s+name\s*:\s*([^\n\r]+)", text, re.I)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"Buyer['’]s\s+Tour.*?\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)+)\b.*?\d+\s+Stops", text, re.I | re.S)
    if m2:
        return m2.group(1).strip()
    return None

def _nearest_status_around(text: str, start_idx: int, end_idx: int) -> str:
    """
    Look for CANCELLED or CONFIRMED within a window around the address block.
    Priority: CANCELLED > CONFIRMED > unknown.
    """
    W = 400  # chars window before/after
    seg = text[max(0, start_idx - W): min(len(text), end_idx + W)]
    if re.search(r"\bCANCELLED\b", seg, re.I):
        return "cancelled"
    if re.search(r"\bCONFIRMED\b", seg, re.I):
        return "confirmed"
    return "unknown"

def _parse_stops_from_text(text: str) -> List[Dict[str, str]]:
    stops: List[Dict[str, str]] = []
    for m in ADDR_LINE_RE.finditer(text):
        addr = re.sub(r"\s+", " ", m.group(1)).strip()
        # time near address
        tail = text[m.end(): m.end() + 500]
        mt = TIME_RANGE_RE.search(tail)
        if not mt:
            head = text[max(0, m.start()-250): m.start()]
            mt = TIME_RANGE_RE.search(head)
        if mt:
            start_t, end_t = mt.group(1).strip(), mt.group(2).strip()
        else:
            start_t, end_t = "", ""
        # status near address
        status = _nearest_status_around(text, m.start(), m.end())
        stops.append({
            "address": addr,
            "start": start_t,
            "end": end_t,
            "status": status,  # <-- captured here
            "address_slug": _address_slug(addr),
            "deeplink": _zillow_deeplink_from_full_address(addr),
        })
    # de-dup
    seen = set(); uniq = []
    for s in stops:
        key = (s["address"], s["start"], s["end"])
        if key in seen: 
            continue
        seen.add(key); uniq.append(s)
    return uniq

def _strip_html_to_text(html: str) -> str:
    # Replace tags with spaces and squeeze whitespace
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text

def parse_from_print_url(url: str) -> Tuple[Optional[date], Optional[str], List[Dict[str, str]], Optional[str]]:
    if not url or "Tour/Print" not in url:
        return None, None, [], "Provide a valid ShowingTime Tour Print URL."
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            return None, None, [], f"Fetch failed: {r.status_code}"
        html = r.text
        text = _strip_html_to_text(html)
        d = _parse_date_from_text(text)
        buyer = _parse_buyer_from_text(text)
        stops = _parse_stops_from_text(text)
        if not stops:
            return d, buyer, [], "No stops found. Double-check that this is a ShowingTime tour Print page."
        return d, buyer, stops, None
    except Exception as e:
        return None, None, [], f"Parse error: {e}"

def parse_from_pdf(uploaded_file) -> Tuple[Optional[date], Optional[str], List[Dict[str, str]], Optional[str]]:
    if not PyPDF2:
        return None, None, [], "PyPDF2 not installed. Add PyPDF2 to requirements.txt."
    try:
        data = uploaded_file.read()
        reader = PyPDF2.PdfReader(io.BytesIO(data))
        raw = []
        for p in reader.pages:
            try:
                raw.append(p.extract_text() or "")
            except Exception:
                pass
        text = "\n".join(raw)
        text = re.sub(r"[ \t]+", " ", text)
        d = _parse_date_from_text(text)
        buyer = _parse_buyer_from_text(text)
        stops = _parse_stops_from_text(text)
        if not stops:
            return d, buyer, [], "No stops found in PDF."
        return d, buyer, stops, None
    except Exception as e:
        return None, None, [], f"PDF parse error: {e}"

# ---------- UI state ----------
STATE_KEY = "__tour_parse__"

def _set_parsed(payload: Dict[str, Any]):
    st.session_state[STATE_KEY] = payload

def _get_parsed() -> Dict[str, Any]:
    return st.session_state.get(STATE_KEY, {})

# ---------- Styles for badges (times + status) ----------
BADGE_CSS = """
<style>
.badge-time {
  font-size: 12px; font-weight: 800; padding: 2px 8px; border-radius: 999px;
  background: linear-gradient(180deg, #0ea5e9 0%, #0284c7 100%); color: #fff; white-space: nowrap;
}
.badge-status-confirmed {
  font-size: 11px; font-weight: 900; padding: 2px 8px; border-radius: 999px;
  background: linear-gradient(180deg, #16a34a 0%, #15803d 100%); color: #fff; text-transform: uppercase;
}
.badge-status-cancelled {
  font-size: 11px; font-weight: 900; padding: 2px 8px; border-radius: 999px;
  background: linear-gradient(180deg, #dc2626 0%, #b91c1c 100%); color: #fff; text-transform: uppercase;
}
.badge-status-unknown {
  font-size: 11px; font-weight: 900; padding: 2px 8px; border-radius: 999px;
  background: #e2e8f0; color: #334155; text-transform: uppercase;
}
.tour-card { 
  padding: 8px 10px; border:1px solid rgba(0,0,0,.08); border-radius:10px; 
  margin: 6px 0; display:flex; align-items:center; justify-content:space-between; gap:10px;
}
</style>
"""

def _status_badge_html(status: str) -> str:
    s = (status or "unknown").lower()
    cls = "badge-status-unknown"
    label = "Unknown"
    if s == "confirmed":
        cls = "badge-status-confirmed"; label = "Confirmed"
    elif s == "cancelled":
        cls = "badge-status-cancelled"; label = "Cancelled"
    return f'<span class="{cls}">{escape(label)}</span>'

# ---------- Main renderer ----------
def render_tours_tab(state: dict):
    st.subheader("Import Tours")
    st.caption("Paste a ShowingTime Print URL or upload its Tour PDF, preview, then add all included stops.")

    # Import UI
    col_u, col_p = st.columns([1.3, 1])
    with col_u:
        print_url = st.text_input(
            "ShowingTime Print URL",
            placeholder="https://scheduling.showingtime.com/(S(...))/Tour/Print/30235965"
        )
    with col_p:
        pdf_file = st.file_uploader("or upload Tour PDF", type=["pdf"])

    c1, c2 = st.columns([1,1])
    parse_clicked = c1.button("Parse tour", use_container_width=True)
    clear_clicked = c2.button("Clear parsed tour", use_container_width=True)

    if clear_clicked:
        st.session_state.pop(STATE_KEY, None)
        _safe_rerun()

    parsed = _get_parsed()

    if parse_clicked:
        if print_url:
            d, buyer, stops, err = parse_from_print_url(print_url.strip())
            source = print_url.strip()
        elif pdf_file is not None:
            d, buyer, stops, err = parse_from_pdf(pdf_file)
            source = f"pdf:{pdf_file.name}"
        else:
            d, buyer, stops, err = None, None, [], "Provide a Print URL or a Tour PDF."
            source = ""

        if err:
            st.error(err)
        else:
            flags = [True] * len(stops)
            _set_parsed({
                "date": d.isoformat() if isinstance(d, date) else None,
                "buyer": buyer,
                "stops": stops,     # [{address, start, end, status, address_slug, deeplink}]
                "source": source,
                "flags": flags,
            })
            parsed = _get_parsed()
            subtitle = []
            if parsed.get("buyer"): subtitle.append(parsed["buyer"])
            if parsed.get("date"):  subtitle.append(parsed["date"])
            st.success(f"Parsed {len(stops)} stop(s)" + ((" • " + " • ".join(subtitle)) if subtitle else ""))

    # Preview section (NOTE: we do NOT show status here, per your request)
    if parsed.get("stops"):
        st.markdown(BADGE_CSS, unsafe_allow_html=True)
        st.markdown("#### Preview")
        new_flags = []
        for i, s in enumerate(parsed["stops"]):
            default_val = bool(parsed["flags"][i]) if i < len(parsed["flags"]) else True
            cols = st.columns([0.08, 0.92])
            with cols[0]:
                chk = st.checkbox("", value=default_val, key=f"__tour_inc_{i}", help="Uncheck to exclude this stop.")
                new_flags.append(chk)
            with cols[1]:
                time_str = f'{s.get("start","")} – {s.get("end","")}'.strip(" –")
                st.markdown(
                    f"""
                    <div class="tour-card">
                      <a href="{escape(s["deeplink"])}" target="_blank" rel="noopener">{escape(s["address"])}</a>
                      <span class="badge-time">{escape(time_str) if time_str else "—"}</span>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
        parsed["flags"] = new_flags
        _set_parsed(parsed)

        # Client picker (No client sentinel LAST)
        clients = fetch_clients(include_inactive=True)
        names = [c["name"] for c in clients]
        name_to_norm = {c["name"]: c.get("name_norm","") for c in clients}
        options = ["— Choose client —"] + names + ["➤ No client (show ALL, no logging)"]
        sel = st.selectbox("Add all included stops to client", options, index=0)

        add_clicked = st.button("Add all included stops", use_container_width=True)
        if add_clicked:
            if sel == "— Choose client —":
                st.warning("Pick a client to save these stops.")
                st.stop()
            if sel == "➤ No client (show ALL, no logging)":
                st.info("Preview only: no client selected, nothing will be saved.")
                st.stop()

            client_display = sel
            client_norm = name_to_norm.get(sel, _norm_tag(sel))
            # Tour date
            tdate: date
            if parsed.get("date"):
                try:
                    tdate = datetime.fromisoformat(parsed["date"]).date()
                except Exception:
                    tdate = datetime.utcnow().date()
            else:
                tdate = datetime.utcnow().date()

            final_stops = [s for s, inc in zip(parsed["stops"], parsed["flags"]) if inc]
            if not final_stops:
                st.warning("No stops selected.")
                st.stop()

            ok_t, tour_id_or_err = create_tour(
                client_norm=client_norm,
                client_display=client_display,
                tour_date=tdate,
                source_url=parsed.get("source",""),
                buyer=parsed.get("buyer")
            )
            if not ok_t:
                st.error(f"Create tour failed: {tour_id_or_err}")
                st.stop()
            tour_id = tour_id_or_err

            ok_s, msg_s = insert_tour_stops(tour_id=int(tour_id), stops=final_stops)
            if not ok_s:
                st.error(f"Insert stops failed: {msg_s}")
                st.stop()

            # Silent logging to 'sent' (no status included)
            log_sent_for_stops_silent(client_norm=client_norm, stops=final_stops, tour_date=tdate)

            st.success(f"Saved {len(final_stops)} stop(s) to {client_display} for {tdate}.")

    # -------- Client Tours Report (status only visible here) --------
    st.markdown("### Client Tours Report")
    clients_all = fetch_clients(include_inactive=True)
    if not clients_all:
        st.info("No clients found.")
        return

    names_all = [c["name"] for c in clients_all]
    name_to_norm = {c["name"]: c.get("name_norm","") for c in clients_all}
    sel_rpt = st.selectbox("Select client", ["— Choose client —"] + names_all, index=0)
    if sel_rpt and sel_rpt != "— Choose client —":
        client_norm = name_to_norm.get(sel_rpt, _norm_tag(sel_rpt))
        tours = fetch_tours_for_client(client_norm)
        if not tours:
            st.info("No tours for this client yet.")
            return

        st.markdown(BADGE_CSS, unsafe_allow_html=True)
        for t in tours:
            tdate = (t.get("tour_date") or "")[:10]
            buyer = t.get("buyer") or ""
            subtitle_bits = [tdate]
            if buyer: subtitle_bits.append(buyer)
            st.markdown(f"**Tour — {' • '.join([escape(x) for x in subtitle_bits if x])}**")

            stops = fetch_stops_for_tour(int(t["id"]))
            if not stops:
                st.caption("_No stops saved for this tour._")
                continue

            # Render stops with TIME + STATUS
            for s in stops:
                time_str = f'{s.get("start","")} – {s.get("end","")}'.strip(" –")
                status_html = _status_badge_html(s.get("status","unknown"))
                st.markdown(
                    f"""
                    <div class="tour-card">
                      <a href="{escape(s.get('deeplink',''))}" target="_blank" rel="noopener">{escape(s.get('address',''))}</a>
                      <div style="display:flex; gap:8px; align-items:center;">
                        <span class="badge-time">{escape(time_str) if time_str else "—"}</span>
                        {status_html}
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            st.markdown("---")
