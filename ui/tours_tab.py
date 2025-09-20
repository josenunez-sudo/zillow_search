# ui/tours_tab.py
import os, re
from datetime import datetime, date
from html import escape
from typing import List, Dict, Any, Optional, Tuple

import requests
import streamlit as st
from supabase import create_client, Client

# ---------- Optional PDF support ----------
try:
    import PyPDF2  # ensure "PyPDF2" is in requirements.txt
except Exception:
    PyPDF2 = None

# ---------- Mac-style blue buttons + light separators ----------
st.markdown("""
<style>
.blue-btn-zone .stButton > button {
  background: linear-gradient(180deg, #0A84FF 0%, #0060DF 100%) !important;
  color: #FFFFFF !important;
  font-weight: 800 !important;
  letter-spacing: .2px !important;
  border: 0 !important;
  border-radius: 12px !important;
  box-shadow: 0 10px 22px rgba(10,132,255,.35), 0 2px 6px rgba(0,0,0,.18) !important;
  transform: translateY(0) !important;
  transition: transform .08s ease, box-shadow .12s ease, filter .08s ease !important;
}
.blue-btn-zone .stButton > button:hover {
  transform: translateY(-1px) !important;
  box-shadow: 0 14px 30px rgba(10,132,255,.42), 0 4px 10px rgba(0,0,0,.20) !important;
  filter: brightness(1.06) !important;
}
.blue-btn-zone .stButton > button:active { transform: translateY(0) scale(.99) !important; }

:root { --row-border:#e2e8f0; }
html[data-theme="dark"], .stApp [data-theme="dark"] { --row-border:#0b1220; }
</style>
""", unsafe_allow_html=True)

# ---------- Supabase ----------
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

def _safe_rerun():
    try: st.rerun()
    except Exception:
        try: st.experimental_rerun()
        except Exception: pass

def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _slug_addr(addr: str) -> str:
    a = (addr or "").strip().lower()
    a = re.sub(r"[^\w\s,-]", "", a).replace(",", "")
    return re.sub(r"\s+", "-", a).strip("-")

def _address_to_deeplink(addr: str) -> str:
    return f"https://www.zillow.com/homes/{_slug_addr(addr)}_rb/"

def _canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    if not url:
        return "", None
    base = re.sub(r"[?#].*$", "", url.strip())
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', base, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = re.search(r'(\d{6,})_zpid', base, re.I)
    return canon, (m_z.group(1) if m_z else None)

# ---------- Clients ----------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients():
    if not SUPABASE: return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        # hide "test test"
        return [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
    except Exception:
        return []

# ---------- ShowingTime parsing ----------
_BAD_AFTER_NUM = r"(?:Beds?|Baths?|Sqft|Canceled|Cancelled|Confirmed|Reason|Presented|Access|Alarm|Instructions|Agent|Buyer)\b"
_STREET_TYPES = r"(?:St|Street|Ave|Avenue|Dr|Drive|Ln|Lane|Rd|Road|Blvd|Boulevard|Ct|Court|Pl|Place|Ter|Terrace|Way|Cir|Circle|Pkwy|Parkway|Hwy|Highway)"
ADDR_RE = re.compile(
    rf"""\b
    (?P<num>\d{{1,6}})\s+(?!{_BAD_AFTER_NUM})
    (?P<name>(?:[A-Za-z0-9.\-']+\s+){{0,4}}[A-Za-z0-9.\-']+)\s+
    (?P<stype>{_STREET_TYPES})
    (?:\s+(?P<post>[A-Za-z0-9.\-']+))?
    \s*,\s*
    (?P<city>[A-Za-z .'\-]+?)\s*,\s*
    (?P<state>[A-Z]{{2}})\s*
    (?P<zip>\d{{5}}(?:-\d{{4}})?)
    \b""",
    re.I | re.X
)

TIME_RE = re.compile(r'(\d{1,2}:\d{2}\s*(?:AM|PM))\s*[-–]\s*(\d{1,2}:\d{2}\s*(?:AM|PM))', re.I)
DATE_HEADER_RE = re.compile(r'(?:Buyer|Agent)[^\n]{0,80}Tour\s*-\s*[A-Za-z]+,\s*([A-Za-z]+)\s*(\d{1,2}),\s*(\d{4})', re.I)
GENERIC_DATE_RE = re.compile(r'([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})')

def _month_to_num(m: str) -> int:
    try: return datetime.strptime(m[:3], "%b").month
    except Exception: return datetime.strptime(m, "%B").month

def _fix_pdf_spacing(s: str) -> str:
    # Join accidental single-letter splits inside words: "V arina" -> "Varina"
    return re.sub(r'([A-Za-z])\s([a-z])', r'\1\2', s)

def _extract_text_from_pdf(file) -> str:
    if not PyPDF2: return ""
    try:
        reader = PyPDF2.PdfReader(file)
        out = []
        for p in reader.pages:
            t = p.extract_text() or ""
            out.append(_fix_pdf_spacing(t))
        return "\n".join(out)
    except Exception:
        return ""

def _html_to_text(html: str) -> str:
    txt = re.sub(r'<script[\s\S]*?</script>', ' ', html, flags=re.I)
    txt = re.sub(r'<style[\s\S]*?</style>', ' ', txt, flags=re.I)
    txt = re.sub(r'<[^>]+>', ' ', txt)
    txt = txt.replace("&nbsp;", " ")
    txt = re.sub(r'\s+', ' ', txt).strip()
    return _fix_pdf_spacing(txt)

def _fetch_html(url: str) -> str:
    try:
        r = requests.get(url, timeout=12, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
        })
        return r.text if r.ok else ""
    except Exception:
        return ""

# ---------- Status normalization ----------
ALLOWED_STATUSES = {"scheduled", "confirmed", "canceled"}

def _normalize_status(s: str) -> str:
    s = (s or "").strip().lower()
    if s == "cancelled":
        s = "canceled"
    if s not in ALLOWED_STATUSES:
        s = "scheduled"
    return s

def _status_in_window(txt: str) -> str:
    u = (txt or "").upper()
    if "CANCELLED" in u or "CANCELED" in u: return "canceled"
    if "CONFIRMED" in u: return "confirmed"
    return ""  # will be normalized to 'scheduled' later if empty

# ---------- Parse core ----------
def _parse_tour_text(txt: str) -> Dict[str, Any]:
    # Tour date
    tdate = None
    m = DATE_HEADER_RE.search(txt)
    if m:
        mon, day, year = m.group(1), int(m.group(2)), int(m.group(3))
        tdate = date(int(year), _month_to_num(mon), int(day))
    else:
        m2 = GENERIC_DATE_RE.search(txt)
        if m2:
            mon, day, year = m2.group(1), int(m2.group(2)), int(m2.group(3))
            tdate = date(int(year), _month_to_num(mon), int(day))

    stops: List[Dict[str, Any]] = []
    for am in ADDR_RE.finditer(txt):
        # Clean address-only text
        num   = am.group("num").strip()
        name  = re.sub(r'\s+', ' ', am.group("name").strip())
        stype = am.group("stype").strip()
        post  = am.group("post") or ""
        city  = re.sub(r'\s+', ' ', am.group("city").strip())
        state = am.group("state").strip().upper()
        zcode = am.group("zip").strip()

        addr_clean = f"{num} {name} {stype}" + (f" {post}" if post else "") + f", {city}, {state} {zcode}"

        # Time/status window near the address
        win_start = max(0, am.start() - 140)
        win_end   = min(len(txt), am.end() + 80)
        window = txt[win_start:win_end]

        tm = TIME_RE.search(window)
        if tm:
            t1 = tm.group(1).upper().replace(" ", "")
            t2 = tm.group(2).upper().replace(" ", "")
        else:
            t1 = t2 = ""

        stat = _status_in_window(window)

        stops.append({
            "address": addr_clean,
            "start": t1.replace("AM"," AM").replace("PM"," PM").strip(),
            "end":   t2.replace("AM"," AM").replace("PM"," PM").strip(),
            "deeplink": _address_to_deeplink(addr_clean),
            "status": stat  # may be "", normalize later
        })

    return {"tour_date": (tdate.isoformat() if tdate else None), "stops": stops}

def parse_showingtime_input(url: str, uploaded_pdf) -> Dict[str, Any]:
    if uploaded_pdf is not None:
        if not PyPDF2:
            return {"error": "PyPDF2 not installed (add to requirements.txt)."}
        text = _extract_text_from_pdf(uploaded_pdf)
        if not text.strip():
            return {"error": "Could not read PDF text."}
        return _parse_tour_text(text)

    if url and url.strip():
        html = _fetch_html(url.strip())
        if not html:
            return {"error": "Could not fetch the Print page URL."}
        text = _html_to_text(html)
        return _parse_tour_text(text)

    return {"error": "Provide a Print URL or a PDF."}

# ---------- DB helpers ----------
def _create_or_get_tour(client_norm: str, client_display: str, tour_url: Optional[str], tour_date: date) -> int:
    if not SUPABASE: raise RuntimeError("Supabase not configured.")
    q = SUPABASE.table("tours").select("id,url").eq("client", client_norm).eq("tour_date", tour_date.isoformat()).limit(1).execute()
    rows = q.data or []
    if rows:
        tid = rows[0]["id"]
        old_url = (rows[0].get("url") or "").strip()
        if tour_url and not old_url:
            SUPABASE.table("tours").update({"url": tour_url}).eq("id", tid).execute()
        return tid
    ins = SUPABASE.table("tours").insert({
        "client": client_norm,
        "client_display": client_display,
        "url": (tour_url or None),
        "tour_date": tour_date.isoformat(),
        "status": "imported",
    }).execute()
    if not ins.data: raise RuntimeError("Create tour failed.")
    return ins.data[0]["id"]

def _insert_stops(tour_id: int, stops: List[Dict[str, Any]]) -> int:
    """
    Insert stops for a tour.
    NOTE: Do NOT insert 'address_slug' if your table defines it as a GENERATED column.
    We compute a slug in-memory for dedupe only.
    """
    if not SUPABASE: return 0
    # Fetch existing slugs to dedupe
    existing = SUPABASE.table("tour_stops").select("address_slug").eq("tour_id", tour_id).limit(50000).execute().data or []
    seen = {e["address_slug"] for e in existing if e.get("address_slug")}
    rows = []
    for s in stops:
        addr = (s.get("address") or "").strip()
        if not addr: continue
        slug = _slug_addr(addr)
        if slug in seen:
            continue
        # Normalize status to satisfy CHECK constraint
        raw_status = _normalize_status(s.get("status"))
        rows.append({
            "tour_id": tour_id,
            "address": addr,
            # no address_slug (generated by DB)
            "start": (s.get("start") or None),
            "end":   (s.get("end") or None),
            "deeplink": (s.get("deeplink") or _address_to_deeplink(addr)),
            "status": raw_status,
        })
        seen.add(slug)
    if not rows: return 0
    ins = SUPABASE.table("tour_stops").insert(rows).execute()
    return len(ins.data or [])

def _insert_sent_for_stops(client_norm: str, stops: List[Dict[str, Any]], tour_date: date) -> int:
    if not SUPABASE or not client_norm or not stops: return 0
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    campaign = f"toured-{tour_date.strftime('%Y%m%d')}"
    rows = []
    for s in stops:
        addr = (s.get("address") or "").strip()
        if not addr: continue
        url = (s.get("deeplink") or _address_to_deeplink(addr)).strip()
        canon, zpid = _canonicalize_zillow(url)
        rows.append({
            "client":   client_norm,
            "campaign": campaign,
            "url":      url,
            "canonical": canon or None,
            "zpid":     zpid or None,
            "mls_id":   None,
            "address":  addr or None,
            "sent_at":  now_iso,
        })
    if not rows: return 0
    try:
        ins = SUPABASE.table("sent").insert(rows).execute()
        return len(ins.data or [])
    except Exception:
        return 0

def _build_repeat_map(client_norm: str) -> Dict[tuple, int]:
    if not SUPABASE: return {}
    tq = SUPABASE.table("tours").select("id,tour_date").eq("client", client_norm).order("tour_date", desc=False).limit(5000).execute()
    tours = tq.data or []
    if not tours: return {}
    ids = [t["id"] for t in tours]
    if not ids: return {}
    sq = SUPABASE.table("tour_stops").select("tour_id,address_slug").in_("tour_id", ids).limit(50000).execute()
    stops = sq.data or []  # <- ensure list
    t2s: Dict[int, List[str]] = {}
    for s in stops:
        t2s.setdefault(s["tour_id"], []).append(s["address_slug"])
    seen_count: Dict[str, int] = {}
    rep: Dict[tuple, int] = {}
    for t in tours:
        td = t["tour_date"]
        for slug in t2s.get(t["id"], []):
            seen_count[slug] = seen_count.get(slug, 0) + 1
            rep[(slug, td)] = seen_count[slug]
    return rep

# ---------- Session: parsed payload ----------
def _get_parsed() -> Dict[str, Any]:
    return st.session_state.get("__parsed_tour__") or {}

def _set_parsed(payload: Dict[str, Any]):
    st.session_state["__parsed_tour__"] = payload or {}

# ---------- HTML helpers ----------
def _date_badge_html(d: str) -> str:
    return ("<span style='display:inline-block;padding:2px 10px;border-radius:9999px;"
            "background:#1d4ed8;color:#fff;font-weight:800;border:1px solid rgba(0,0,0,.15);'>" + escape(d) + "</span>")

def _status_tag_html(stat: str) -> str:
    u = (stat or "").strip().lower()
    if u == "confirmed":
        return "<span style='padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:800;background:#166534;color:#ecfdf5;border:1px solid #16a34a;white-space:nowrap;'>Confirmed</span>"
    if u == "canceled":
        return "<span style='padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:800;background:#7f1d1d;color:#fee2e2;border:1px solid #ef4444;white-space:nowrap;'>Canceled</span>"
    if u == "scheduled":
        return "<span style='padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:800;background:#334155;color:#e2e8f0;border:1px solid #475569;white-space:nowrap;'>Scheduled</span>"
    return ""

def _time_badge_html(when: str) -> str:
    if not when: return ""
    return "<span style='padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:800;background:#0a84ff;color:#ffffff;border:1px solid rgba(0,0,0,.15);white-space:nowrap;'>" + escape(when) + "</span>"

def _repeat_tag_html(n: int) -> str:
    if n >= 2:
        return "<span style='padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:800;background:#92400e;color:#fff7ed;border:1px solid #f59e0b;white-space:nowrap;margin-left:8px;'>2nd+ showing</span>"
    return ""

# ---------- Main renderer ----------
def render_tours_tab(state: dict):
    # ===== Import (Parse) =====
    st.markdown("### Import a tour")
    col1, col2 = st.columns([1.3, 1])
    with col1:
        url = st.text_input("Paste ShowingTime **Print** URL", placeholder="https://scheduling.showingtime.com/.../Tour/Print/30235965")
    with col2:
        uploaded = st.file_uploader("Or drop the **Tour PDF**", type=["pdf"])

    cA, cB = st.columns([0.22, 0.22])
    with cA:
        st.markdown("<div class='blue-btn-zone'>", unsafe_allow_html=True)
        do_parse = st.button("Parse", use_container_width=True, key="__parse_btn__")
        st.markdown("</div>", unsafe_allow_html=True)
    with cB:
        st.markdown("<div class='blue-btn-zone'>", unsafe_allow_html=True)
        do_clear = st.button("Clear", use_container_width=True, key="__clear_btn__")
        st.markdown("</div>", unsafe_allow_html=True)

    if do_clear:
        _set_parsed({})
        st.success("Cleared.")
        _safe_rerun()

    if do_parse:
        parsed = parse_showingtime_input(url, uploaded)
        if parsed.get("error"):
            st.error(parsed["error"])
        else:
            if not parsed.get("stops"):
                st.warning("No stops found. Double-check that this is a ShowingTime tour Print page or the exported Tour PDF.")
            _set_parsed(parsed)
            _safe_rerun()

    parsed = _get_parsed()

    # ===== Preview =====
    if parsed:
        tdate = parsed.get("tour_date")
        stops = parsed.get("stops", [])
        st.markdown("#### Preview")
        left, right = st.columns([1,1])
        with left:
            st.caption(f"Parsed {len(stops)} stop(s)")
        with right:
            if tdate:
                st.markdown(f"<div style='text-align:right;'>{_date_badge_html(tdate)}</div>", unsafe_allow_html=True)

        if stops:
            lis = []
            for s in stops:
                addr = (s.get("address","").strip())
                href = (s.get("deeplink") or _address_to_deeplink(addr)).strip()
                start = (s.get("start") or "").strip()
                end   = (s.get("end") or "").strip()
                when  = f"{start}–{end}" if (start and end) else (start or end or "")
                stat  = _normalize_status(s.get("status"))

                right_badges = []
                if stat:
                    right_badges.append(_status_tag_html(stat))
                if when:
                    right_badges.append(_time_badge_html(when))

                lis.append(
                    "<li style='display:flex;justify-content:space-between;align-items:center;gap:12px;padding:6px 0;border-bottom:1px solid var(--row-border);'>"
                    + "<div style='min-width:0;'>"
                    + f"<a href='{escape(href)}' target='_blank' rel='noopener'>{escape(addr)}</a>"
                    + "</div>"
                    + "<div style='flex:0 0 auto;display:flex;align-items:center;gap:8px;'>"
                    + "".join(right_badges)
                    + "</div>"
                    + "</li>"
                )
            st.markdown("<ul style='margin:.25rem 0 .5rem 0;padding:0;list-style:none;'>" + "\n".join(lis) + "</ul>", unsafe_allow_html=True)
        else:
            st.info("No stops to preview.")

        st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:.5rem 0;'></div>", unsafe_allow_html=True)

        # ===== Add all stops flow =====
        clients = fetch_clients()
        client_names = [c["name"] for c in clients]
        client_norms = [c["name_norm"] for c in clients]

        # Display client (real client list only)
        if client_names:
            idx_disp = st.selectbox(
                "Client display name (for the tour record)",
                list(range(len(client_names))),
                format_func=lambda i: client_names[i],
                index=0,
                key="__tour_client_display__"
            )
            client_display = client_names[idx_disp]
            client_display_norm = client_norms[idx_disp]
        else:
            client_display = ""
            client_display_norm = ""

        # Add all stops to client (first option is No logging)
        NO_CLIENT = "➤ No client (show ALL, no logging)"
        add_names = [NO_CLIENT] + client_names
        add_norms = [""         ] + client_norms
        idx_add = st.selectbox(
            "Add all stops to client (optional)",
            list(range(len(add_names))),
            format_func=lambda i: add_names[i],
            index=0,  # default "No logging"
            key="__tour_add_to_client__"
        )
        chosen_norm = add_norms[idx_add]
        also_mark_sent = st.checkbox("Also mark these stops as “toured” in Sent", value=True)

        colAA, colCC = st.columns([0.4, 0.60])
        with colAA:
            can_add = bool(stops) and bool(tdate) and bool(client_display and client_display_norm)
            st.markdown("<div class='blue-btn-zone'>", unsafe_allow_html=True)
            add_clicked = st.button("Add all stops", use_container_width=True, disabled=not can_add)
            st.markdown("</div>", unsafe_allow_html=True)
            if add_clicked:
                try:
                    tdate_obj = datetime.fromisoformat(tdate).date() if tdate else date.today()
                    tour_id = _create_or_get_tour(
                        client_norm=client_display_norm,
                        client_display=client_display,
                        tour_url=(url or None),
                        tour_date=tdate_obj
                    )
                    n = _insert_stops(tour_id, stops)
                    if also_mark_sent and chosen_norm:
                        _insert_sent_for_stops(chosen_norm, stops, tdate_obj)
                    st.success(f"Added {n} stop(s) to {client_display} for {tdate_obj}.")
                except Exception as e:
                    st.error(f"Could not add stops. {e}")

        with colCC:
            st.caption("Tip: choose **No client** if you only want to parse/preview without logging.")

    st.markdown("---")
    st.markdown('<div id="report_anchor"></div>', unsafe_allow_html=True)

    # ===== Tours report (inline) =====
    st.markdown("### Tours report")
    clients2 = fetch_clients()
    names2 = [c["name"] for c in clients2]
    norms2 = [c["name_norm"] for c in clients2]

    if names2:
        colPick, colBtn = st.columns([0.68, 0.32])
        with colPick:
            irep = st.selectbox("Pick a client", list(range(len(names2))),
                                format_func=lambda i: names2[i], index=0, key="__tour_client_pick__")
        with colBtn:
            st.markdown("<div class='blue-btn-zone'>", unsafe_allow_html=True)
            # The ONLY View report button in the entire tab:
            st.button("View report", use_container_width=True, key="__view_report_here__")
            st.markdown("</div>", unsafe_allow_html=True)
            # (Button is primarily visual here; the section already shows the report for the selected client.)

        _render_client_tours_report(names2[irep], norms2[irep])
    else:
        st.info("No clients found.")

def _render_client_tours_report(client_display: str, client_norm: str):
    if not SUPABASE:
        st.info("Supabase not configured.")
        return
    tq = SUPABASE.table("tours").select("id,tour_date").eq("client", client_norm).order("tour_date", desc=True).limit(2000).execute()
    tours = tq.data or []
    if not tours:
        st.info("No tours logged for this client yet.")
        return

    repeat_map = _build_repeat_map(client_norm)

    for t in tours:
        td = t["tour_date"]
        st.markdown(f"#### {escape(client_display)} {_date_badge_html(td)}", unsafe_allow_html=True)
        sq = SUPABASE.table("tour_stops").select("address,address_slug,start,end,deeplink,status").eq("tour_id", t["id"]).order("start", desc=False).limit(500).execute()
        stops = sq.data or []
        if not stops:
            st.caption("_(No stops logged for this date.)_")
            continue

        items = []
        for s in stops:
            addr = s.get("address") or ""
            slug = s.get("address_slug") or _slug_addr(addr)
            href = (s.get("deeplink") or _address_to_deeplink(addr)).strip()
            start = (s.get("start") or "").strip()
            end   = (s.get("end") or "").strip()
            when  = f"{start}–{end}" if (start and end) else (start or end or "")
            visit = repeat_map.get((slug, td), 1)
            stat  = _normalize_status(s.get("status"))

            right_badges = []
            if stat:
                right_badges.append(_status_tag_html(stat))
            if when:
                right_badges.append(_time_badge_html(when))
            rep_tag = _repeat_tag_html(visit)

            items.append(
                "<li style='display:flex;justify-content:space-between;align-items:center;gap:12px;padding:6px 0;border-bottom:1px solid var(--row-border);'>"
                + "<div style='min-width:0;'>"
                + f"<a href='{escape(href)}' target='_blank' rel='noopener'>{escape(addr)}</a>"
                + (rep_tag if rep_tag else "")
                + "</div>"
                + "<div style='flex:0 0 auto;display:flex;align-items:center;gap:8px;'>"
                + "".join(right_badges)
                + "</div>"
                + "</li>"
            )
        st.markdown("<ul style='margin:.25rem 0 .5rem 0;padding:0;list-style:none;'>" + "\n".join(items) + "</ul>", unsafe_allow_html=True)
