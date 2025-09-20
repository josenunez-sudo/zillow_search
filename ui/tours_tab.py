# ui/tours_tab.py
# Tours tab: Parse ShowingTime tour (Print URL or PDF), preview cleanly, then add to Supabase (tours + tour_stops)
# and log each stop to 'sent' so Clients/Run cross-checks can tag as TOURED.
import os, re, io, json
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape

import requests
import streamlit as st

# ---------- Optional deps ----------
try:
    import PyPDF2  # for PDF text extraction
except Exception:
    PyPDF2 = None  # fallback: only URL parsing will work

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

# ---------- Helpers ----------
REQUEST_TIMEOUT = 15

def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _sb_ok() -> bool:
    try:
        return bool(SUPABASE)
    except NameError:
        return False

@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = True) -> List[Dict[str, Any]]:
    """Load clients; hide 'test test' normalized name."""
    if not _sb_ok():
        return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        return [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
    except Exception:
        return []

def _address_slug(addr: str) -> str:
    s = (addr or "").lower()
    s = re.sub(r"[^a-z0-9\s,]", " ", s)
    s = s.replace(",", " ")
    s = re.sub(r"\s+", "-", s).strip("-")
    return s

def _zillow_deeplink_from_full_address(addr: str) -> str:
    # Build a Zillow /homes/ ... _rb/ deeplink
    s = (addr or "").lower()
    s = re.sub(r"[^a-z0-9\s,]", "", s).replace(",", "")
    s = re.sub(r"\s+", "-", s.strip())
    return f"https://www.zillow.com/homes/{s}_rb/"

# ------ Strict parsing regexes ------
# Time range like "9:00 AM - 9:30 AM"
TIME_RANGE_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM))\s*-\s*(\d{1,2}:\d{2}\s*(?:AM|PM))\b", re.I)

# Recognized street suffixes (required so we don't accidentally grab fluff)
STREET_SUFFIX = r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Drive|Dr|Lane|Ln|Way|Terrace|Ter|Court|Ct|Place|Pl|Parkway|Pkwy|Square|Sq|Circle|Cir|Highway|Hwy|Route|Rt|Trail|Trl)"

# Strict US address pattern:
# <num> <name words> <suffix>, <City>, <ST> <zip>
ADDRESS_RE = re.compile(
    rf"\b(\d{{1,6}})\s+([A-Za-z0-9.\-']+(?:\s+[A-Za-z0-9.\-']+){{0,6}})\s+({STREET_SUFFIX})\b[^\n,]*,\s*([A-Za-z .'\-]+),\s*([A-Z]{{2}})\s*(\d{{5}}(?:-\d{{4}})?)\b",
    re.I,
)

# Tour date in header like "Buyer's Tour - Monday, September 22, 2025"
DATE_FANCY_RE = re.compile(r"\b(?:Tour\s*-\s*)?([A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})\b", re.I)
DATE_NUMERIC_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")

def _recompose_address(m: re.Match) -> str:
    num   = re.sub(r"\s+", " ", m.group(1)).strip()
    name  = re.sub(r"\s+", " ", m.group(2)).strip()
    suf   = re.sub(r"\s+", " ", m.group(3)).strip()
    city  = re.sub(r"\s+", " ", m.group(4)).strip()
    state = m.group(5).upper().strip()
    zipc  = m.group(6).strip()
    return f"{num} {name} {suf}, {city}, {state} {zipc}"

def _nearest_status_around(text: str, start: int, end: int) -> str:
    # Look near the address for 'cancel' variants or 'confirmed'
    window = text[max(0, start - 250): min(len(text), end + 300)]
    if re.search(r"\b(cancel(?:ed|led)?|declined|denied)\b", window, re.I):
        return "CANCELLED"
    if re.search(r"\bconfirmed\b", window, re.I):
        return "CONFIRMED"
    return "UNKNOWN"

def _parse_stops_from_text(text: str) -> List[Dict[str, str]]:
    """Return structured stops: address (clean), start, end, status, address_slug, deeplink."""
    stops: List[Dict[str, str]] = []
    for m in ADDRESS_RE.finditer(text):
        addr = _recompose_address(m)
        # prefer time after address; fallback before
        aft = text[m.end(): m.end() + 160]
        bef = text[max(0, m.start() - 160): m.start()]
        mt = TIME_RANGE_RE.search(aft) or TIME_RANGE_RE.search(bef)
        start_t, end_t = ("", "")
        if mt:
            start_t, end_t = mt.group(1).strip(), mt.group(2).strip()
        status = _nearest_status_around(text, m.start(), m.end())
        stops.append({
            "address": addr,
            "start": start_t,
            "end": end_t,
            "status": status,
            "address_slug": _address_slug(addr),
            "deeplink": _zillow_deeplink_from_full_address(addr),
        })
    # unique by (address,start,end)
    uniq, seen = [], set()
    for s in stops:
        key = (s["address"], s["start"], s["end"])
        if key in seen:
            continue
        seen.add(key); uniq.append(s)
    return uniq

def _parse_date_and_client(text: str) -> Tuple[Optional[datetime], str]:
    # Client name is often on the same header line as "Buyer’s Tour - <date> <Client Name>"
    # We'll capture date, and heuristically pick the first name-like token after 'Buyer' or from a "Buyer’s name:" field.
    dt = None
    client_guess = ""
    m = DATE_FANCY_RE.search(text) or DATE_NUMERIC_RE.search(text)
    if m:
        raw = m.group(1)
        for fmt in ("%A, %B %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except Exception:
                continue

    # Try explicit Buyer’s name:
    m2 = re.search(r"Buyer[’'`s]{0,2}\s+name:\s*([A-Za-z .'\-]+)", text, re.I)
    if m2:
        client_guess = re.sub(r"\s+", " ", m2.group(1)).strip()

    # Otherwise, pick a proper-noun sequence near 'Buyer' or 'Buyers Tour'
    if not client_guess:
        win = text[:600]
        m3 = re.search(r"Buyer[’'`s]?\s+Tour.*?\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b", win, re.S)
        if m3:
            client_guess = m3.group(1).strip()

    return dt, client_guess

# -------- Fetch + parse sources --------
def _fetch_print_page(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml",
    }
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if not r.ok:
        return ""
    return r.text

def _pdf_text(file_bytes: bytes) -> str:
    if not PyPDF2:
        return ""
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        chunks = []
        for i in range(len(reader.pages)):
            try:
                chunks.append(reader.pages[i].extract_text() or "")
            except Exception:
                continue
        return "\n".join(chunks)
    except Exception:
        return ""

def parse_tour_from_sources(print_url: str = "", pdf_bytes: Optional[bytes] = None) -> Dict[str, Any]:
    """Returns {'date': datetime|None, 'client_guess': str, 'stops': [..]} or raises ValueError."""
    raw = ""
    if pdf_bytes:
        raw = _pdf_text(pdf_bytes)
        if not raw:
            raise ValueError("Could not read text from PDF.")
    elif print_url:
        raw = _fetch_print_page(print_url)
        if not raw:
            raise ValueError("Could not fetch the Print page.")
        # Strip tags for consistency; quick-n-dirty
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = re.sub(r"\s+", " ", raw)

    if not raw or len(raw) < 40:
        raise ValueError("Empty or unrecognized content.")

    # Normalize spaces
    text = re.sub(r"\s+", " ", raw)
    dt, client_guess = _parse_date_and_client(text)
    stops = _parse_stops_from_text(text)
    if not stops:
        raise ValueError("No stops found. Double-check that this is a ShowingTime tour Print page or PDF.")
    return {"date": dt, "client_guess": client_guess, "stops": stops}

# -------- Supabase persistence --------
def _insert_tour_and_stops(client_norm: str, client_display: str, tour_date: Optional[datetime],
                           print_url: str, stops: List[Dict[str, str]]) -> Tuple[bool, str, Optional[int]]:
    if not (_sb_ok() and stops):
        return False, "Supabase not configured or no stops.", None
    try:
        # Create tour
        payload = {
            "client": client_norm or None,
            "client_display": client_display or None,
            "tour_date": (tour_date.date().isoformat() if isinstance(tour_date, datetime) else None),
            "url": (print_url or None),
            "status": "parsed",
        }
        tour_row = SUPABASE.table("tours").insert(payload).execute()
        # supabase-py returns {"data":[{...}]} or {"data":{...}} depending on pgrest,
        # standardize to list:
        data = tour_row.data if isinstance(tour_row.data, list) else [tour_row.data]
        new_id = (data[0] or {}).get("id")
        if not new_id:
            return False, "Could not obtain new tour id.", None

        # Insert stops (bulk)
        stop_rows = []
        for s in stops:
            stop_rows.append({
                "tour_id": new_id,
                "address": s["address"],
                "address_slug": s["address_slug"],
                "start": s.get("start") or None,
                "end": s.get("end") or None,
                "deeplink": s.get("deeplink") or None,
                "status": s.get("status") or None,
            })
        if stop_rows:
            SUPABASE.table("tour_stops").insert(stop_rows).execute()

        return True, "ok", new_id
    except Exception as e:
        return False, f"Create tour failed: {e}", None

def _log_stops_to_sent(client_norm: str, client_display: str, stops: List[Dict[str, str]]):
    """Also log each stop to 'sent' so Run/Clients can mark as TOURED. We only add url, address, sent_at."""
    if not (_sb_ok() and client_norm and stops):
        return False, "skip"
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    rows = []
    for s in stops:
        u = s.get("deeplink") or ""
        if not u:
            continue
        rows.append({
            "client": client_norm,
            "campaign": "tour",
            "url": u,
            "canonical": None,  # can be derived by your existing canonicalizer elsewhere
            "zpid": None,
            "mls_id": None,
            "address": s.get("address") or None,
            "sent_at": now_iso,
        })
    if not rows:
        return False, "empty"
    try:
        SUPABASE.table("sent").insert(rows).execute()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def _remove_stop(stop_id: int) -> Tuple[bool, str]:
    if not _sb_ok():
        return False, "Not configured"
    try:
        SUPABASE.table("tour_stops").delete().eq("id", stop_id).execute()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def _fetch_recent_tour_with_stops(tour_id: int) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    if not _sb_ok():
        return {}, []
    try:
        tour = SUPABASE.table("tours").select("*").eq("id", tour_id).single().execute().data or {}
        stops = SUPABASE.table("tour_stops").select("id,address,start,end,status,deeplink").eq("tour_id", tour_id).order("id").execute().data or []
        return tour, stops
    except Exception:
        return {}, []

# --------- UI renderer ---------
def render_tours_tab(state: dict):
    st.markdown("### Tours")

    # subtle CSS for badges & list
    st.markdown("""
    <style>
      .tour-item { margin: 0.25rem 0; }
      .t-badge { display:inline-block; font-size:11px; font-weight:700; padding:2px 8px; border-radius:999px; background:#e2e8f0; color:#334155; margin-left:6px; }
      .s-badge { display:inline-block; font-size:11px; font-weight:800; padding:2px 8px; border-radius:999px; text-transform:uppercase; margin-left:6px; }
      .s-badge.ok { background:#dcfce7; color:#166534; border:1px solid rgba(5,150,105,.35); }
      .s-badge.no { background:#fee2e2; color:#991b1b; border:1px solid rgba(239,68,68,.35); }
      ul.tour-list { margin:0 0 0.25rem 1.2rem; padding:0; list-style:disc; }
      ul.tour-list li { margin:0.22rem 0; }
    </style>
    """, unsafe_allow_html=True)

    # -------- Import (at top): URL or PDF, then Parse --------
    st.markdown("**Import tour (ShowingTime Print URL or PDF)**")
    colU, colP = st.columns([1.8, 1])
    with colU:
        print_url = st.text_input("Print URL", value="", placeholder="https://scheduling.showingtime.com/.../Tour/Print/30235965")
    with colP:
        pdf_file = st.file_uploader("or drop PDF", type=["pdf"], accept_multiple_files=False)

    parse_clicked = st.button("Parse tour", use_container_width=True)

    if parse_clicked:
        # Clear previous parsed
        st.session_state["__parsed_tour__"] = None

        try:
            if pdf_file is not None:
                payload = parse_tour_from_sources(print_url="", pdf_bytes=pdf_file.getvalue())
            elif print_url.strip():
                payload = parse_tour_from_sources(print_url=print_url.strip(), pdf_bytes=None)
            else:
                st.error("Provide a Print URL or upload a PDF.")
                return
            # Stash
            payload["source_url"] = print_url.strip() or ""
            st.session_state["__parsed_tour__"] = payload
        except ValueError as e:
            st.error(str(e))
        except Exception:
            st.error("Could not parse this tour. Please ensure it is the ShowingTime Print page or the exported Tour PDF.")

    parsed = st.session_state.get("__parsed_tour__")

    if parsed:
        # -------- Preview (no logs, just clean items) --------
        tour_dt = parsed.get("date")
        client_guess = parsed.get("client_guess") or ""
        stops: List[Dict[str, str]] = parsed.get("stops") or []

        # Header line
        dt_label = (tour_dt.strftime("%Y-%m-%d") if isinstance(tour_dt, datetime) else "—")
        st.markdown(f"**Preview:** {len(stops)} stop(s) • Date: <span class='t-badge'>{escape(dt_label)}</span>", unsafe_allow_html=True)

        # list with hyperlink + time badge + status tag
        items = []
        for s in stops:
            u = s.get("deeplink") or "#"
            time_badge = ""
            if s.get("start") and s.get("end"):
                time_badge = f"<span class='t-badge'>{escape(s['start'])} – {escape(s['end'])}</span>"
            status = (s.get("status") or "UNKNOWN").upper()
            sclass = "ok" if status == "CONFIRMED" else ("no" if status == "CANCELLED" else "")
            status_badge = f"<span class='s-badge {sclass}'>{escape(status)}</span>"
            items.append(
                f"<li><a href='{escape(u)}' target='_blank' rel='noopener'>{escape(s['address'])}</a>{time_badge}{status_badge}</li>"
            )
        st.markdown("<ul class='tour-list'>" + "\n".join(items) + "</ul>", unsafe_allow_html=True)

        # -------- Add-all flow --------
        all_clients = fetch_clients(include_inactive=True)
        names = [c["name"] for c in all_clients]
        NO_CLIENT = "➤ No client (show ALL, no logging)"
        options = names + [NO_CLIENT]  # last option is "no client"
        sel = st.selectbox("Add all stops to client (optional)", list(range(len(options))), format_func=lambda i: options[i])

        # Editable client display (default to guess)
        default_display = client_guess or (options[sel] if options[sel] != NO_CLIENT else "")
        client_display = st.text_input("Client display name (for the tour record)", value=default_display)

        can_save = options[sel] != NO_CLIENT and bool(stops)
        btn_label = "Add all stops to selected client"
        add_clicked = st.button(btn_label, use_container_width=True, disabled=not can_save)

        if add_clicked and can_save:
            chosen = options[sel]
            client_norm = _norm_tag(chosen)
            # Insert tour + stops
            ok, msg, tour_id = _insert_tour_and_stops(
                client_norm=client_norm,
                client_display=client_display or chosen,
                tour_date=tour_dt if isinstance(tour_dt, datetime) else None,
                print_url=parsed.get("source_url") or "",
                stops=stops
            )
            if not ok:
                st.error(msg or "Failed to save tour.")
                return

            # Also log to 'sent' so Clients/Run can tag as TOURED
            _log_stops_to_sent(client_norm, client_display or chosen, stops)

            st.success("Tour saved.")

            # ---- Minimal manage UI: show saved tour and allow removing stops ----
            tour, live_stops = _fetch_recent_tour_with_stops(tour_id or 0)
            if tour and live_stops:
                st.markdown("---")
                st.markdown(f"**Saved Tour** • {escape(tour.get('client_display') or chosen)} • <span class='t-badge'>{escape(tour.get('tour_date') or dt_label)}</span>", unsafe_allow_html=True)

                # Render list with Remove buttons
                for s in live_stops:
                    url = s.get("deeplink") or "#"
                    addr = s.get("address") or "Listing"
                    tbadge = ""
                    if s.get("start") and s.get("end"):
                        tbadge = f"<span class='t-badge'>{escape(s['start'])} – {escape(s['end'])}</span>"
                    status = (s.get("status") or "UNKNOWN").upper()
                    sclass = "ok" if status == "CONFIRMED" else ("no" if status == "CANCELLED" else "")
                    sbadge = f"<span class='s-badge {sclass}'>{escape(status)}</span>"

                    colL, colR = st.columns([8, 1])
                    with colL:
                        st.markdown(
                            f"<div class='tour-item'><a href='{escape(url)}' target='_blank' rel='noopener'>{escape(addr)}</a> {tbadge} {sbadge}</div>",
                            unsafe_allow_html=True
                        )
                    with colR:
                        if st.button("Remove", key=f"rm_{s['id']}", use_container_width=True):
                            ok2, _ = _remove_stop(int(s["id"]))
                            if ok2:
                                st.experimental_rerun()
                            else:
                                st.warning("Failed to remove stop.")
