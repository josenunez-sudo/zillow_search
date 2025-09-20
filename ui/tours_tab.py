# ui/tours_tab.py
# Tours tab: Parse ShowingTime tour (Print URL or PDF), preview cleanly, then add to Supabase (tours + tour_stops)
# and log each stop to 'sent' so Clients/Run cross-checks can tag as TOURED.
import os, re, io
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

def upsert_client(name: str, active: bool = True, notes: Optional[str] = None) -> Tuple[bool, str]:
    if not _sb_ok() or not (name or "").strip():
        return False, "Not configured or empty name"
    try:
        payload = {"name": name.strip(), "name_norm": _norm_tag(name), "active": active}
        if notes is not None:
            payload["notes"] = notes
        SUPABASE.table("clients").upsert(payload, on_conflict="name_norm").execute()
        try:
            fetch_clients.clear()  # type: ignore[attr-defined]
        except Exception:
            pass
        return True, "ok"
    except Exception as e:
        return False, str(e)

def _address_slug(addr: str) -> str:
    s = (addr or "").lower()
    s = re.sub(r"[^a-z0-9\s,]", " ", s)
    s = s.replace(",", " ")
    s = re.sub(r"\s+", "-", s).strip("-")
    return s

def _zillow_deeplink_from_full_address(addr: str) -> str:
    s = (addr or "").lower()
    s = re.sub(r"[^a-z0-9\s,]", "", s).replace(",", "")
    s = re.sub(r"\s+", "-", s.strip())
    return f"https://www.zillow.com/homes/{s}_rb/"

# ------ Strict parsing regexes ------
TIME_RANGE_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM))\s*-\s*(\d{1,2}:\d{2}\s*(?:AM|PM))\b", re.I)
STREET_SUFFIX = r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Drive|Dr|Lane|Ln|Way|Terrace|Ter|Court|Ct|Place|Pl|Parkway|Pkwy|Square|Sq|Circle|Cir|Highway|Hwy|Route|Rt|Trail|Trl)"
ADDRESS_RE = re.compile(
    rf"\b(\d{{1,6}})\s+([A-Za-z0-9.\-']+(?:\s+[A-Za-z0-9.\-']+){{0,6}})\s+({STREET_SUFFIX})\b[^\n,]*,\s*([A-Za-z .'\-]+),\s*([A-Z]{{2}})\s*(\d{{5}}(?:-\d{{4}})?)\b",
    re.I,
)
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
    window = text[max(0, start - 250): min(len(text), end + 300)]
    if re.search(r"\b(cancel(?:ed|led)?|declined|denied)\b", window, re.I):
        return "CANCELLED"
    if re.search(r"\bconfirmed\b", window, re.I):
        return "CONFIRMED"
    return "UNKNOWN"

def _parse_stops_from_text(text: str) -> List[Dict[str, str]]:
    stops: List[Dict[str, str]] = []
    for m in ADDRESS_RE.finditer(text):
        addr = _recompose_address(m)
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
    uniq, seen = [], set()
    for s in stops:
        key = (s["address"], s["start"], s["end"])
        if key in seen:
            continue
        seen.add(key); uniq.append(s)
    return uniq

def _parse_date_and_client(text: str) -> Tuple[Optional[datetime], str]:
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
    m2 = re.search(r"Buyer[’'`s]{0,2}\s+name:\s*([A-Za-z .'\-]+)", text, re.I)
    if m2:
        client_guess = re.sub(r"\s+", " ", m2.group(1)).strip()
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
    raw = ""
    if pdf_bytes:
        raw = _pdf_text(pdf_bytes)
        if not raw:
            raise ValueError("Could not read text from PDF.")
    elif print_url:
        raw = _fetch_print_page(print_url)
        if not raw:
            raise ValueError("Could not fetch the Print page.")
        raw = re.sub(r"<[^>]+>", " ", raw)   # strip tags
        raw = re.sub(r"\s+", " ", raw)

    if not raw or len(raw) < 40:
        raise ValueError("Empty or unrecognized content.")

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
        payload = {
            "client": client_norm or None,
            "client_display": client_display or None,
            "tour_date": (tour_date.date().isoformat() if isinstance(tour_date, datetime) else None),
            "url": (print_url or None),
            "status": "parsed",
        }
        tour_row = SUPABASE.table("tours").insert(payload).execute()
        data = tour_row.data if isinstance(tour_row.data, list) else [tour_row.data]
        new_id = (data[0] or {}).get("id")
        if not new_id:
            return False, "Could not obtain new tour id.", None

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

def _log_stops_to_sent(client_norm: str, client_display: str, stops: List[Dict[str, Any]]) -> Tuple[bool, str]:
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
            "canonical": None,
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

    # styles
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

    # -------- Import (top): URL or PDF, then Parse + Clear --------
    st.markdown("**Import tour (ShowingTime Print URL or PDF)**")
    colU, colP = st.columns([1.8, 1])
    with colU:
        print_url = st.text_input(
            "Print URL",
            value=st.session_state.get("__tour_url__", ""),
            placeholder="https://scheduling.showingtime.com/.../Tour/Print/30235965",
            key="__tour_url__",
        )
    with colP:
        pdf_file = st.file_uploader("or drop PDF", type=["pdf"], accept_multiple_files=False, key="__tour_pdf__")

    b1, b2 = st.columns([1, 0.3])
    with b1:
        parse_clicked = st.button("Parse tour", use_container_width=True, key="__parse_btn__")
    with b2:
        clear_clicked = st.button("Clear", use_container_width=True, key="__clear_btn__")

    if clear_clicked:
        st.session_state["__parsed_tour__"] = None
        st.session_state["__tour_url__"] = ""
        try:
            st.rerun()
        except Exception:
            st.experimental_rerun()

    if parse_clicked:
        st.session_state["__parsed_tour__"] = None
        try:
            if pdf_file is not None:
                payload = parse_tour_from_sources(print_url="", pdf_bytes=pdf_file.getvalue())
            elif print_url.strip():
                payload = parse_tour_from_sources(print_url=print_url.strip(), pdf_bytes=None)
            else:
                st.error("Provide a Print URL or upload a PDF.")
                return
            payload["source_url"] = print_url.strip() or ""
            st.session_state["__parsed_tour__"] = payload
        except ValueError as e:
            st.error(str(e))
        except Exception:
            st.error("Could not parse this tour. Please ensure it is the ShowingTime Print page or the exported Tour PDF.")

    parsed = st.session_state.get("__parsed_tour__")

    if parsed:
        tour_dt = parsed.get("date")
        client_guess = parsed.get("client_guess") or ""
        stops: List[Dict[str, str]] = parsed.get("stops") or []

        dt_label = (tour_dt.strftime("%Y-%m-%d") if isinstance(tour_dt, datetime) else "—")
        st.markdown(f"**Preview:** {len(stops)} stop(s) • Date: <span class='t-badge'>{escape(dt_label)}</span>", unsafe_allow_html=True)

        # preview list (hyperlink + time + CONFIRMED/CANCELLED tag)
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
        client_names = [c["name"] for c in all_clients]

        # Inline add-client if empty:
        if not client_names:
            with st.expander("Add a client to save this tour"):
                nn = st.text_input("Client name")
                if st.button("Add client"):
                    okc, msgc = upsert_client(nn, active=True)
                    if okc:
                        st.success("Client added.")
                        try:
                            fetch_clients.clear()  # type: ignore[attr-defined]
                        except Exception:
                            pass
                        try:
                            st.rerun()
                        except Exception:
                            st.experimental_rerun()
                    else:
                        st.error(msgc)

        NO_CLIENT = "➤ No client (show ALL, no logging)"   # first item & ALWAYS the default
        add_options = [NO_CLIENT] + client_names

        # <<< CHANGE: Always default to 'No client' (index 0) >>>
        sel_idx = st.selectbox(
            "Add all stops to client (optional)",
            list(range(len(add_options))),
            format_func=lambda i: add_options[i],
            index=0,                           # <--- always default to No client
            key="__tour_client_sel__"
        )

        # Client display name dropdown (real clients only)
        # Default to guessed client if present; else just first in list.
        display_default = 0
        if client_names:
            guessed_idx = next((i for i, nm in enumerate(client_names) if _norm_tag(nm) == _norm_tag(client_guess)), None)
            if guessed_idx is not None:
                display_default = guessed_idx
        display_choice = st.selectbox(
            "Client display name (for the tour record)",
            client_names if client_names else ["—"],
            index=(display_default if client_names else 0),
            key="__tour_display_sel__"
        )

        add_clicked = st.button("Add all stops to selected client", use_container_width=True, key="__add_all_btn__")

        if add_clicked:
            chosen_label = add_options[sel_idx]
            if chosen_label == NO_CLIENT:
                st.error("Pick a client to save this tour.")
                return
            if not client_names:
                st.error("No clients available. Please add a client first.")
                return

            client_norm = _norm_tag(chosen_label)
            # Save tour + stops
            ok, msg, tour_id = _insert_tour_and_stops(
                client_norm=client_norm,
                client_display=display_choice or chosen_label,
                tour_date=tour_dt if isinstance(tour_dt, datetime) else None,
                print_url=parsed.get("source_url") or "",
                stops=stops
            )
            if not ok:
                st.error(msg or "Failed to save tour.")
                return

            # Also log to 'sent'
            _log_stops_to_sent(client_norm, display_choice or chosen_label, stops)
            st.success("Tour saved.")

            # Manage quickly: show saved tour and allow removing stops
            tour, live_stops = _fetch_recent_tour_with_stops(tour_id or 0)
            if tour and live_stops:
                st.markdown("---")
                st.markdown(
                    f"**Saved Tour** • {escape(tour.get('client_display') or chosen_label)} • "
                    f"<span class='t-badge'>{escape(tour.get('tour_date') or (tour_dt.strftime('%Y-%m-%d') if isinstance(tour_dt, datetime) else '—'))}</span>",
                    unsafe_allow_html=True
                )
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
                                try:
                                    st.rerun()
                                except Exception:
                                    st.experimental_rerun()
                            else:
                                st.warning("Failed to remove stop.")
