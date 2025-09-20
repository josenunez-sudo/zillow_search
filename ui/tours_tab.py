# ui/tours_tab.py
# "Tours" tab: paste ShowingTime URL OR upload PDF → parse stops (address + time).
# Optional: log parsed stops to Supabase "tours" table, and cross-check with "sent".

import os, io, re, csv, json, datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape

import streamlit as st
import requests

# ---------- Optional PDF extractors ----------
try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text
except Exception:
    pdfminer_extract_text = None

def _pdf_text_via_pypdf2(data: bytes) -> str:
    try:
        import PyPDF2
        rdr = PyPDF2.PdfReader(io.BytesIO(data))
        return "\n".join([(p.extract_text() or "") for p in rdr.pages])
    except Exception:
        return ""

def extract_text_from_pdf_bytes(data: bytes) -> str:
    if not data:
        return ""
    if pdfminer_extract_text:
        try:
            return pdfminer_extract_text(io.BytesIO(data)) or ""
        except Exception:
            pass
    # fallback
    return _pdf_text_via_pypdf2(data)

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

SUPABASE: Optional[Client] = get_supabase()

def _sb_ok() -> bool:
    try:
        return bool(SUPABASE)
    except Exception:
        return False

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _norm_addr(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"unit\s+[#\w-]+", "", s)
    s = re.sub(r"apt\s+[#\w-]+", "", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()

@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False):
    if not _sb_ok(): return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

@st.cache_data(ttl=120, show_spinner=False)
def get_sent_addresses_for_client(client_norm: str) -> List[str]:
    """Return a list of normalized address strings from your 'sent' table for cross-check."""
    if not (_sb_ok() and client_norm.strip()):
        return []
    try:
        rows = SUPABASE.table("sent")\
            .select("address")\
            .eq("client", client_norm.strip())\
            .limit(20000)\
            .execute().data or []
        addrs = [r.get("address") or "" for r in rows]
        return [_norm_addr(a) for a in addrs if (a or "").strip()]
    except Exception:
        return []

def log_tours_rows(client_norm: str, tour_date_iso: str, source_url: str, stops: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """Insert stops to 'tours' table. Requires table:
       tours(client text, tour_date text, address text, city text, state text, zip text,
             start_time text, end_time text, source_url text, created_at timestamptz)
    """
    if not (_sb_ok() and client_norm.strip() and stops):
        return False, "Not configured or no stops."
    now_iso = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    rows = []
    for s in stops:
        rows.append({
            "client": client_norm.strip(),
            "tour_date": (tour_date_iso or ""),
            "address": s.get("address",""),
            "city": s.get("city",""),
            "state": s.get("state",""),
            "zip": s.get("zip",""),
            "start_time": s.get("start",""),
            "end_time": s.get("end",""),
            "source_url": source_url or "",
            "created_at": now_iso,
        })
    try:
        SUPABASE.table("tours").insert(rows).execute()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ---------- Parsing helpers (works for both HTML Print & PDF text) ----------
WEEKDAYS = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
MONTHS   = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
TIME_RE  = re.compile(r"\b(\d{1,2}:\d{2})\s*(AM|PM)\s*[-–]\s*(\d{1,2}:\d{2})\s*(AM|PM)\b", re.I)
DATE_LONG_RE = re.compile(rf"\b{WEEKDAYS}\w*\s*,\s*{MONTHS}\w*\s+\d{{1,2}},\s*\d{{4}}\b", re.I)
DATE_NUM_RE  = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")

# Address line heuristic: "... City, ST 12345"
ADDR_LINE_RE = re.compile(
    r"(?P<addr>.+?),\s*(?P<city>[A-Za-z .'-]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)",
    re.I
)

def clamp_html_to_text(html: str) -> str:
    # Very light "text" from HTML so regex works; avoid bs4 dependency.
    txt = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    txt = re.sub(r"</(p|div|tr|li|h\d)>", "\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n", txt)
    return txt

def parse_stops_from_text(txt: str) -> Dict[str, Any]:
    """Return dict: { date: ISO or '', stops: [ {address, city, state, zip, start, end}, ... ], debug: {...} }"""
    raw = txt or ""
    text = re.sub(r"[ \t]+", " ", raw)
    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    # Find a tour date anywhere in text
    tour_date_str = ""
    mlong = DATE_LONG_RE.search(text)
    if mlong:
        tour_date_str = mlong.group(0)
    else:
        mnum = DATE_NUM_RE.search(text)
        if mnum:
            tour_date_str = mnum.group(0)

    # Best-effort ISO date
    tour_date_iso = ""
    if tour_date_str:
        try:
            # try multiple formats
            for fmt in ("%A, %B %d, %Y", "%a, %b %d, %Y", "%m/%d/%Y"):
                try:
                    dt = datetime.datetime.strptime(tour_date_str, fmt)
                    tour_date_iso = dt.date().isoformat()
                    break
                except Exception:
                    continue
        except Exception:
            tour_date_iso = ""

    # Scan lines; pair TIMES with ADDRESSES found near them (in-order)
    times: List[Tuple[str,str,int]] = []  # (start, end, line_index)
    addrs: List[Tuple[Dict[str,str],int]] = []  # ({address,city,state,zip}, line_index)

    for i, ln in enumerate(lines):
        # Collect times present on the line
        for m in TIME_RE.finditer(ln):
            start = f"{m.group(1)} {m.group(2).upper()}"
            end   = f"{m.group(3)} {m.group(4).upper()}"
            times.append((start, end, i))
        # Collect addresses
        ma = ADDR_LINE_RE.search(ln)
        if ma:
            d = {
                "address": ma.group("addr").strip(),
                "city": ma.group("city").strip(),
                "state": ma.group("state").upper().strip(),
                "zip": ma.group("zip").strip(),
            }
            addrs.append((d, i))

    # If nothing matched, try a denser pass over the entire text (some PDFs line-break oddly)
    if not times or not addrs:
        for m in TIME_RE.finditer(text):
            start = f"{m.group(1)} {m.group(2).upper()}"
            end   = f"{m.group(3)} {m.group(4).upper()}"
            times.append((start, end, -1))
        for m in ADDR_LINE_RE.finditer(text):
            d = {
                "address": m.group("addr").strip(),
                "city": m.group("city").strip(),
                "state": m.group("state").upper().strip(),
                "zip": m.group("zip").strip(),
            }
            addrs.append((d, -1))

    # Pairing: in order, nearest time to following/preceding address
    stops: List[Dict[str, Any]] = []
    used_addr = set()
    for start, end, ti in times:
        # choose the closest address line index to ti that's not used yet (prefer address after the time)
        best_j = None
        best_dist = 10**9
        for j, (d, ai) in enumerate(addrs):
            if j in used_addr:
                continue
            dist = abs((ai if ai >= 0 else ti) - (ti if ti >= 0 else ai))
            # prefer address that appears within +/- 4 lines, or any if none close
            rank = dist
            if ai >= ti and ti >= 0:
                rank -= 0.25  # nudge for "address after time"
            if rank < best_dist:
                best_dist = rank
                best_j = j
        if best_j is not None:
            used_addr.add(best_j)
            d, _ = addrs[best_j]
            stops.append({
                "address": d["address"],
                "city": d["city"],
                "state": d["state"],
                "zip": d["zip"],
                "start": start,
                "end": end,
            })

    # If we still have unmatched addresses (no time found), add them as time-less stops
    for j, (d, _) in enumerate(addrs):
        if j not in used_addr:
            stops.append({
                "address": d["address"],
                "city": d["city"],
                "state": d["state"],
                "zip": d["zip"],
                "start": "",
                "end": "",
            })

    return {
        "date": tour_date_iso,
        "stops": stops,
        "debug": {
            "lines": lines[:400],
            "found_times": times,
            "found_addrs": [x[0] for x in addrs],
            "date_raw": tour_date_str
        }
    }

def parse_showingtime_url(url: str) -> Dict[str, Any]:
    UA = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }
    try:
        r = requests.get(url, headers=UA, timeout=15)
        if not r.ok:
            return {"date":"", "stops":[], "debug":{"error": f"HTTP {r.status_code}"}}
        html = r.text
        text = clamp_html_to_text(html)
        return parse_stops_from_text(text)
    except Exception as e:
        return {"date":"", "stops":[], "debug":{"error": repr(e)}}

# ---------- UI ----------
def render_tours_tab():
    st.subheader("Tours")
    st.caption("Paste a ShowingTime link **or** upload a tour PDF, then click **Parse tour**.")

    # Client picker (optional logging + cross-check)
    clients = fetch_clients(include_inactive=False)
    name_options = ["➤ No client (no logging)"] + [c["name"] for c in clients]
    sel_idx = st.selectbox("Client", list(range(len(name_options))), format_func=lambda i: name_options[i], index=0)
    selected_client = None if sel_idx == 0 else clients[sel_idx-1]
    client_norm = (selected_client["name_norm"] if selected_client else "").strip()

    # Inputs like Run tab
    col1, col2 = st.columns([1.6, 1])
    with col1:
        url_in = st.text_input("ShowingTime link (Print or short link)", placeholder="https://scheduling.showingtime.com/... /Tour/Print/... or https://showingti.me/...")
    with col2:
        st.caption("or drop a PDF below")

    uploaded = st.file_uploader("Upload ShowingTime Tour PDF", type=["pdf"], label_visibility="collapsed")
    col3, col4, col5 = st.columns([1,1,1.4])
    with col3:
        show_table = st.checkbox("Show table", value=True)
    with col4:
        show_debug = st.checkbox("Show debug", value=False)
    with col5:
        parse_clicked = st.button("🧭 Parse tour", use_container_width=True)

    parsed: Dict[str, Any] = {}
    stops: List[Dict[str, Any]] = []
    text_preview = ""

    if parse_clicked:
        if uploaded is not None:
            data = uploaded.read()
            text_preview = extract_text_from_pdf_bytes(data)
            parsed = parse_stops_from_text(text_preview)
        elif (url_in or "").strip():
            parsed = parse_showingtime_url(url_in.strip())
            # For debug view
            try:
                text_preview = "\n".join(parsed.get("debug",{}).get("lines", []))
            except Exception:
                text_preview = ""
        else:
            st.warning("Provide a ShowingTime link or upload a PDF.")
            st.stop()

        stops = parsed.get("stops", []) or []
        if not stops:
            st.error("Could not parse this tour. Please ensure it is the ShowingTime Print page or the exported Tour PDF.")
        else:
            st.success(f"Parsed {len(stops)} stop(s)" + (f" — Date: {parsed.get('date')}" if parsed.get("date") else ""))

    # Cross-check (addresses sent already)
    sent_addr_norms = set(get_sent_addresses_for_client(client_norm)) if client_norm else set()
    for s in stops:
        s["_addr_norm"] = _norm_addr(", ".join([s.get("address",""), s.get("city",""), s.get("state",""), s.get("zip","")]))

    if stops:
        # Results list like Run tab
        st.markdown("#### Tour Stops")
        items = []
        for s in stops:
            addr_full = ", ".join([p for p in [s.get("address",""), s.get("city",""), s.get("state",""), s.get("zip","")] if p])
            t = ""
            if s.get("start") or s.get("end"):
                t = f"<span class='hl'>{escape(s.get('start',''))} – {escape(s.get('end',''))}</span> "
            badge = ""
            if client_norm and s.get("_addr_norm") in sent_addr_norms:
                badge = " <span class='badge dup' title='This address appears in Sent for this client.'>Sent</span>"
            gmap = "https://www.google.com/maps/search/" + requests.utils.quote(addr_full)
            items.append(f"<li style='margin:0.2rem 0;'>{t}<a href='{escape(gmap)}' target='_blank' rel='noopener'>{escape(addr_full)}</a>{badge}</li>")
        html = "<ul class='link-list'>" + "\n".join(items) + "</ul>"
        st.markdown(html, unsafe_allow_html=True)

        # Optional table
        if show_table:
            import pandas as pd
            df = pd.DataFrame([{
                "start": s.get("start",""),
                "end": s.get("end",""),
                "address": s.get("address",""),
                "city": s.get("city",""),
                "state": s.get("state",""),
                "zip": s.get("zip",""),
                "sent_already": (client_norm and s.get("_addr_norm") in sent_addr_norms)
            } for s in stops])
            st.dataframe(df, use_container_width=True, hide_index=True)

        # Export CSV
        csv_buf = io.StringIO()
        w = csv.DictWriter(csv_buf, fieldnames=["start","end","address","city","state","zip"])
        w.writeheader()
        for s in stops:
            w.writerow({k: s.get(k,"") for k in ["start","end","address","city","state","zip"]})
        st.download_button("Export CSV", data=csv_buf.getvalue(),
                           file_name=f"tour_{parsed.get('date') or 'stops'}.csv",
                           mime="text/csv", use_container_width=True)

        # Log all to Supabase
        if selected_client:
            if st.button(f"➕ Add all stops to {selected_client['name']}", use_container_width=True):
                ok, msg = log_tours_rows(
                    client_norm=selected_client["name_norm"],
                    tour_date_iso=parsed.get("date",""),
                    source_url=(url_in or ""),
                    stops=stops
                )
                st.success("Logged to Supabase.") if ok else st.warning(f"Supabase insert failed: {msg}")

    # Debug view
    if parse_clicked and show_debug:
        with st.expander("Debug view (first ~400 lines)"):
            st.code(text_preview or "(no preview)", language="text")
            st.json(parsed.get("debug", {}), expanded=False)
