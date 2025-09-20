# ui/tours_tab.py
# Tours tab: parse ShowingTime Print URL or PDF -> stops with date/time + address (as link).
# Cross-check against Supabase 'sent' and previously 'toured' (tours/tour_stops) by address slug.
# Shows chips: SENT and TOURED (with most recent tour date/time). Stores address_slug for future lookups.

import os, re, io, datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape

import requests
import streamlit as st

# ---- Optional PDF dep (install via requirements.txt: PyPDF2==3.0.1)
try:
    import PyPDF2  # type: ignore
except Exception:
    PyPDF2 = None

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

SUPABASE = _get_supabase()

def _sb_ok() -> bool:
    try: return bool(SUPABASE)
    except NameError: return False

def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

# ---------- Clients (cached) ----------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False):
    if not _sb_ok(): return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

# ---------- Styles: high-contrast chips ----------
def _apply_local_styles():
    st.markdown("""
    <style>
      .tour-box { border:1px solid rgba(0,0,0,.08); border-radius:12px; padding:14px; }
      .chip-date {
        display:inline-block; padding:4px 10px; border-radius:999px;
        font-weight:800; font-size:12.5px; letter-spacing:.2px;
        color:#0f172a; background:linear-gradient(180deg,#fde68a 0%, #f59e0b 100%);
        border:1px solid rgba(245,158,11,.45); box-shadow:0 6px 16px rgba(245,158,11,.22),0 1px 3px rgba(0,0,0,.08);
      }
      html[data-theme="dark"] .chip-date, .stApp [data-theme="dark"] .chip-date {
        color:#111827; background:linear-gradient(180deg,#fde047 0%, #f59e0b 100%);
        border:1px solid rgba(245,158,11,.55); box-shadow:0 6px 16px rgba(217,119,6,.45),0 1px 3px rgba(0,0,0,.35);
      }

      .chip-sent {
        display:inline-block; padding:2px 8px; border-radius:999px; font-weight:800; font-size:11px;
        color:#065f46; background:#dcfce7; border:1px solid rgba(5,150,105,.35);
      }
      html[data-theme="dark"] .chip-sent { color:#a7f3d0; background:#064e3b; border-color:rgba(167,243,208,.35);}

      .chip-tour {
        display:inline-block; padding:2px 8px; border-radius:999px; font-weight:800; font-size:11px;
        color:#1e3a8a; background:#dbeafe; border:1px solid rgba(59,130,246,.35);
      }
      html[data-theme="dark"] .chip-tour { color:#bfdbfe; background:#1e3a8a; border-color:rgba(147,197,253,.35);}

      .small { font-size:12.5px; color:#64748b; }
      .stop-card { border-bottom:1px solid rgba(0,0,0,.06); padding:10px 0; }
      .stop-card:last-child { border-bottom:0; }
      .addr { font-weight:700; }
      .time { color:#475569; }
      .actions { margin-top:6px; }
      .list-link { text-decoration:none; font-weight:600; }
      a.addr-link { text-decoration:none; }
    </style>
    """, unsafe_allow_html=True)

# ---------- Helpers ----------
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
}

def _address_slug(addr: str) -> str:
    a = (addr or "").lower()
    a = re.sub(r"[^\w\s,-]", "", a).replace(",", "")
    a = re.sub(r"\s+", "-", a.strip())
    return a

def _zillow_deeplink_for_address(addr: str) -> str:
    slug = _address_slug(addr)
    return f"https://www.zillow.com/homes/{slug}_rb/"

# ---- Normalize glitched text like "W ashington", "V arina", "W est", "W inchester"
def _normalize_glitched_text(text: str) -> str:
    if not text:
        return text
    # Keep newlines; collapse horizontal whitespace only
    text = re.sub(r"[ \t]+", " ", text)
    text = text.replace("\r", "")

    # Merge capital single-letter + lowercase cluster twice (handles "W est" and "N W est" chains)
    text = re.sub(r"\b([A-Z])\s+([a-z]{2,})\b", r"\1\2", text)
    text = re.sub(r"\b([A-Z])\s+([a-z]{2,})\b", r"\1\2", text)

    # Specific common street/place names seen split in PDFs
    common_words = ["Washington", "Winchester", "Watauga", "Varina", "West", "Jackson", "Atlantic", "Longfellow", "Saturn", "Pine"]
    for w in common_words:
        first, rest = w[0], w[1:]
        text = re.sub(rf"\b{first}\s+{rest}\b", w, text, flags=re.I)

    # Tighten excessive newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text

def _extract_text_from_pdf(file_bytes: bytes) -> str:
    if not PyPDF2:
        raise RuntimeError("PyPDF2 not installed")
    with io.BytesIO(file_bytes) as bio:
        reader = PyPDF2.PdfReader(bio)
        parts = []
        for p in reader.pages:
            try:
                parts.append(p.extract_text() or "")
            except Exception:
                continue
        raw = "\n".join(parts)
        return _normalize_glitched_text(raw)

def _fetch_print_html(url: str) -> str:
    r = requests.get(url, headers=UA_HEADERS, timeout=15)
    r.raise_for_status()
    return r.text

def _parse_date(text: str) -> Optional[str]:
    # "Tour Date: 9/19/2025", "Date: Friday, September 19, 2025", or naked "9/19/2025"
    pats = [
        r"Tour Date\s*:\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
        r"Date\s*:\s*([A-Za-z]+,\s+[A-Za-z]+\s+[0-9]{1,2},\s+[0-9]{4})",
        r"\b([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})\b",
    ]
    for p in pats:
        m = re.search(p, text, re.I)
        if m:
            raw = m.group(1).strip()
            try:
                if re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", raw):
                    mm, dd, yy = [int(x) for x in raw.split("/")]
                    yy = yy if yy > 99 else (2000 + yy)
                    dt = datetime.date(yy, mm, dd)
                else:
                    dt = datetime.datetime.strptime(raw, "%A, %B %d, %Y").date()
                return dt.isoformat()
            except Exception:
                return raw
    return None

def _parse_stops_from_text(text: str) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Extract tour_date + stop items of {address, start, end, raw_time}.
    Works for ShowingTime print page & their PDF text.
    """
    # Normalize (keep newlines for proximity matching)
    txt = _normalize_glitched_text(text)

    tour_date = _parse_date(txt)

    # Address like "114 Atlantic Avenue, Benson, NC 27504"
    ADDR_RE = re.compile(r"(\d{1,6}\s+[^\n,]+,\s*[A-Za-z .\-']+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?)")
    # Time windows like "10:00 AM - 10:15 AM" or "1:15PM-1:30PM"
    TIME_RE = re.compile(r"(\d{1,2}:\d{2}\s*[AP]M)\s*[-–]\s*(\d{1,2}:\d{2}\s*[AP]M)", re.I)
    # Structured "Address: ... Time: ..." blocks
    BLOCK_RE = re.compile(r"(Address|Property|Location)[:\s]+(.+?)\s+(?:Appt\s*Time|Time)[:\s]+(.{1,80}?[AP]M\s*[-–]\s*.{1,80}?[AP]M)", re.I | re.DOTALL)

    stops: List[Dict[str, Any]] = []

    # Try structured blocks first
    for m in BLOCK_RE.finditer(txt):
        addr = re.sub(r"\s+", " ", m.group(2).strip())
        when = " ".join(m.group(3).split())
        tm = TIME_RE.search(when)
        start, end = (tm.group(1).strip(), tm.group(2).strip()) if tm else ("", "")
        stops.append({"address": addr, "start": start, "end": end, "raw_time": when})

    # Fallback: match time lines + nearest address line within a few lines
    if not stops:
        lines = [(ln or "").strip() for ln in txt.splitlines()]
        for i, ln in enumerate(lines):
            t = TIME_RE.search(ln)
            if t:
                addr = ""
                for j in range(i+1, min(i+6, len(lines))):
                    a = ADDR_RE.search(lines[j])
                    if a:
                        addr = re.sub(r"\s+", " ", a.group(1).strip())
                        break
                if addr:
                    stops.append({"address": addr, "start": t.group(1).strip(), "end": t.group(2).strip(), "raw_time": ln})

    # Last resort: collect addresses only
    if not stops:
        for a in ADDR_RE.finditer(txt):
            addr = re.sub(r"\s+", " ", a.group(1).strip())
            stops.append({"address": addr, "start": "", "end": "", "raw_time": ""})

    # De-dup by address + time
    uniq = {}
    for s in stops:
        key = (s.get("address",""), s.get("start",""), s.get("end",""))
        if key not in uniq:
            uniq[key] = s
    stops = list(uniq.values())

    return tour_date, stops

def parse_tour_from_print_url(url: str) -> Tuple[Optional[str], List[Dict[str, Any]], Optional[str]]:
    """Return (tour_date_iso, stops, err)."""
    try:
        html = _fetch_print_html(url)
    except Exception as e:
        return None, [], f"Fetch failed: {e}"

    # Preserve <br> as newlines, strip tags, but DO NOT collapse newlines away
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?s)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?s)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    # Collapse horizontal spaces, keep newlines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Fix broken words before parsing
    text = _normalize_glitched_text(text)

    td, stops = _parse_stops_from_text(text)
    if not stops:
        return td, [], "No stops found on the print page."
    return td, stops, None

def parse_tour_from_pdf(file_bytes: bytes) -> Tuple[Optional[str], List[Dict[str, Any]], Optional[str]]:
    if not PyPDF2:
        return None, [], "PyPDF2 not installed"
    try:
        txt = _extract_text_from_pdf(file_bytes)
        td, stops = _parse_stops_from_text(txt)
        if not stops:
            return td, [], "No stops found in the PDF text."
        return td, stops, None
    except Exception as e:
        return None, [], f"PDF parse error: {e}"

# ---------- Sent cross-check ----------
@st.cache_data(ttl=120, show_spinner=False)
def _fetch_sent_for_client(client_norm: str, limit: int = 5000):
    if not (_sb_ok() and client_norm.strip()):
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

def _address_matches_sent(addr: str, sent_rows: List[Dict[str, Any]]) -> bool:
    slug = _address_slug(addr)
    if not slug:
        return False
    for r in sent_rows:
        u = (r.get("url") or "").lower()
        if not u:
            continue
        if "/homedetails/" in u or "/homes/" in u:
            if slug in u:
                return True
    return False

# ---------- Previously toured cross-check ----------
@st.cache_data(ttl=90, show_spinner=False)
def _prev_tour_index_for_client(client_norm: str) -> Dict[str, List[Dict[str, str]]]:
    """
    Returns slug -> list of {date,start,end,address} for that client.
    Most recent first.
    """
    out: Dict[str, List[Dict[str, str]]] = {}
    if not (_sb_ok() and (client_norm or "").strip()):
        return out
    try:
        tours = SUPABASE.table("tours").select("id,tour_date").eq("client", client_norm.strip())\
                .order("tour_date", desc=True).limit(2000).execute().data or []
        if not tours:
            return out
        id_to_date = {t["id"]: (t.get("tour_date") or None) for t in tours if t.get("id")}
        tour_ids = list(id_to_date.keys())
        if not tour_ids:
            return out

        # fetch all stops for these tours
        # Note: supabase-py supports .in_(col, list)
        stops = SUPABASE.table("tour_stops")\
            .select("tour_id,address,address_slug,start,end")\
            .in_("tour_id", tour_ids)\
            .limit(20000)\
            .execute().data or []

        for s in stops:
            slug = s.get("address_slug") or _address_slug(s.get("address",""))
            if not slug:
                continue
            out.setdefault(slug, [])
            out[slug].append({
                "date": id_to_date.get(s.get("tour_id")),
                "start": s.get("start") or "",
                "end": s.get("end") or "",
                "address": s.get("address") or "",
            })

        # sort most recent first
        for slug, items in out.items():
            items.sort(key=lambda x: (x.get("date") or ""), reverse=True)
        return out
    except Exception:
        return out

# ---------- Store to tours tables (optional) ----------
def _store_tour(client_norm: str, client_display: str, tour_date_iso: Optional[str], stops: List[Dict[str, Any]]) -> Tuple[bool,str]:
    if not (_sb_ok() and client_norm and stops):
        return False, "Not configured or empty."
    try:
        payload = {
            "client": client_norm,
            "client_display": client_display,
            "tour_date": tour_date_iso or None,
            "created_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        }
        res = SUPABASE.table("tours").insert(payload).execute()
        tour_id = (res.data or [{}])[0].get("id")
        if not tour_id:
            return False, "Insert tours row failed."

        rows = []
        for s in stops:
            addr = s.get("address","")
            start = s.get("start","")
            end   = s.get("end","")
            rows.append({
                "tour_id": tour_id,
                "address": addr,
                "address_slug": _address_slug(addr) if addr else None,
                "start": start,
                "end": end,
                "deeplink": _zillow_deeplink_for_address(addr) if addr else None
            })
        SUPABASE.table("tour_stops").insert(rows).execute()
        return True, "Saved to tours/tour_stops."
    except Exception as e:
        return False, f"Save skipped/failed: {e}"

# ---------- UI ----------
def render_tours_tab(state: dict):
    _apply_local_styles()

    st.subheader("Tours")

    colL, colR = st.columns([1.2, 1])
    with colL:
        tour_url = st.text_input("Paste ShowingTime Print URL", placeholder="https://scheduling.showingtime.com/.../Tour/Print/30235965")
    with colR:
        pdf_file = st.file_uploader("…or drop a Tour PDF", type=["pdf"])

    go = st.button("Parse tour", use_container_width=True)
    tour_date_iso: Optional[str] = None
    stops: List[Dict[str, Any]] = []
    err: Optional[str] = None

    if go:
        if tour_url.strip():
            tour_date_iso, stops, err = parse_tour_from_print_url(tour_url.strip())
        elif pdf_file is not None:
            try:
                buf = pdf_file.getvalue()
            except Exception:
                buf = None
            if buf:
                tour_date_iso, stops, err = parse_tour_from_pdf(buf)
            else:
                err = "Could not read PDF bytes."
        else:
            err = "Provide a ShowingTime Print URL or upload a PDF."

        if err:
            st.error("Could not parse this tour. Please ensure it is the ShowingTime Print page or the exported Tour PDF.")
            with st.expander("Parser details"):
                st.write(err)
            return
        if not stops:
            st.warning("No stops found. Double-check that this is a ShowingTime tour PDF/print page.")
            return

        # Date chip
        human_date = tour_date_iso
        try:
            if tour_date_iso and re.match(r"^\d{4}-\d{2}-\d{2}$", tour_date_iso):
                dt = datetime.date.fromisoformat(tour_date_iso)
                human_date = dt.strftime("%a, %b %d, %Y")
        except Exception:
            pass

        st.markdown(
            f"<div class='tour-box'><span class='chip-date' title='{escape(tour_date_iso or '')}'>"
            f"{escape(human_date or 'Tour')}</span> &nbsp; "
            f"<span class='small'>{len(stops)} stop{'s' if len(stops)!=1 else ''}</span></div>",
            unsafe_allow_html=True
        )
        st.write("")

        # Optional client for cross-check + storing
        active_clients = fetch_clients(include_inactive=False)
        names = [c["name"] for c in active_clients]

        sentinel = -1
        options = [sentinel] + list(range(len(names)))
        idx = st.selectbox(
            "Add all stops to client (optional)",
            options,
            format_func=lambda i: ("— Select —" if i == sentinel else names[i]),
            index=0  # valid index (sentinel)
        )
        chosen_client = active_clients[idx] if (idx is not None and idx >= 0 and idx < len(active_clients)) else None
        client_norm = _norm_tag(chosen_client["name"]) if chosen_client else ""

        # Cross-check with `sent` and previously toured
        sent_rows = _fetch_sent_for_client(client_norm) if chosen_client else []
        prev_index = _prev_tour_index_for_client(client_norm) if chosen_client else {}

        # Show stops
        for s in stops:
            addr = s.get("address","").strip()
            start = s.get("start","")
            end   = s.get("end","")
            deeplink = _zillow_deeplink_for_address(addr) if addr else ""
            slug = _address_slug(addr)

            was_sent = _address_matches_sent(addr, sent_rows) if sent_rows else False

            # Previously toured?
            prev_items = prev_index.get(slug) if prev_index else None
            most_recent = prev_items[0] if (prev_items and len(prev_items)>0) else None
            toured_chip = ""
            if most_recent and most_recent.get("date"):
                # include time if available on that previous stop
                t_start = most_recent.get("start") or ""
                t_end   = most_recent.get("end") or ""
                time_span = f" {t_start}-{t_end}" if (t_start and t_end) else (f" {t_start}" if t_start else "")
                toured_chip = f"<span class='chip-tour' title='Previously toured'>{escape(most_recent['date'] + time_span)}</span>"

            st.markdown("<div class='stop-card'>", unsafe_allow_html=True)

            # Address as a LINK
            addr_html = (
                f"<a class='addr-link' href='{escape(deeplink)}' target='_blank' rel='noopener'>"
                f"{escape(addr) if addr else '(address missing)'}"
                f"</a>"
            )
            st.markdown(
                f"<div class='addr'>{addr_html}</div>"
                f"<div class='time'>{escape(start)}"
                f"{(' – ' + escape(end)) if (start and end) else ''}</div>",
                unsafe_allow_html=True
            )

            # Chips / actions
            chips = []
            if toured_chip:
                chips.append(toured_chip)
            if was_sent:
                chips.append("<span class='chip-sent' title='Matched by address slug in sent links'>SENT</span>")
            if chips:
                st.markdown("<div class='actions'>" + " &nbsp; ".join(chips) + "</div>", unsafe_allow_html=True)

            st.markdown("</div>", unsafe_allow_html=True)

        # Store all (optional)
        if chosen_client:
            if st.button(f"Add all stops to {chosen_client['name']}", use_container_width=True):
                ok, msg = _store_tour(client_norm, chosen_client["name"], tour_date_iso, stops)
                if ok:
                    st.success("Tour saved.")
                else:
                    st.warning(msg)

    # Helpful note about dependencies
    st.caption("Tip: If PDF parsing fails, make sure `PyPDF2` is in your requirements.txt.")
