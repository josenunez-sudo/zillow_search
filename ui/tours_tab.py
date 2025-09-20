# ui/tours_tab.py
import os
import re
from datetime import datetime, date
from html import escape
from typing import List, Dict, Any, Optional, Tuple

import requests
import streamlit as st
import streamlit.components.v1 as components
from supabase import create_client, Client

# ====== Optional PDF dependency ======
try:
    import PyPDF2  # for PDF text extraction
except Exception:
    PyPDF2 = None


# ===== Supabase handle =====
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

# ---- Util ----
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _slug_addr(addr: str) -> str:
    a = (addr or "").strip().lower()
    a = re.sub(r"[^\w\s,-]", "", a)
    a = a.replace(",", "")
    a = re.sub(r"\s+", "-", a).strip("-")
    return a

def _address_to_deeplink(addr: str) -> str:
    slug = _slug_addr(addr)
    return f"https://www.zillow.com/homes/{slug}_rb/"

def _ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20: suf = "th"
    else: suf = {1:"st",2:"nd",3:"rd"}.get(n % 10, "th")
    return f"{n}{suf}"

def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

# --- Query-param helpers ---
def _qp_get(name, default=None):
    try:
        qp = st.query_params
        val = qp.get(name, default)
        if isinstance(val, list) and val:
            return val[0]
        return val
    except Exception:
        qp = st.experimental_get_query_params()
        return (qp.get(name, [default]) or [default])[0]

def _qp_set(**kwargs):
    try:
        if kwargs: st.query_params.update(kwargs)
        else: st.query_params.clear()
    except Exception:
        if kwargs: st.experimental_set_query_params(**kwargs)
        else: st.experimental_set_query_params()


# ===== Clients list (exclude "test test") =====
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients():
    if not SUPABASE: return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        return [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
    except Exception:
        return []


# ====== Parsing (robust Print HTML & PDF) ======
_TIME_RE = re.compile(r'(\d{1,2}:\d{2}\s*(?:AM|PM))\s*[-–]\s*(\d{1,2}:\d{2}\s*(?:AM|PM))', re.I)
_ADDR_RE = re.compile(r'\b\d{1,6}\s+[^\n,]+,\s+[A-Za-z .\'-]+,\s*[A-Z]{2}\s*\d{5}\b')  # 114 Atlantic Avenue, Benson, NC 27504
_DATE_HEADER_RE = re.compile(
    r'(?:Buyer|Agent)[^\n]{0,60}Tour\s*-\s*[A-Za-z]+,\s*([A-Za-z]+)\s*(\d{1,2}),\s*(\d{4})', re.I
)
_GENERIC_DATE_RE = re.compile(
    r'([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})'
)

def _month_to_num(m: str) -> int:
    try:
        return datetime.strptime(m[:3], "%b").month
    except Exception:
        return datetime.strptime(m, "%B").month

def _extract_text_from_pdf(file) -> str:
    if not PyPDF2:
        return ""
    try:
        reader = PyPDF2.PdfReader(file)
        buf = []
        for p in reader.pages:
            buf.append(p.extract_text() or "")
        return "\n".join(buf)
    except Exception:
        return ""

def _html_to_text(html: str) -> str:
    if not html: return ""
    txt = re.sub(r'<script[\s\S]*?</script>', ' ', html, flags=re.I)
    txt = re.sub(r'<style[\s\S]*?</style>', ' ', txt, flags=re.I)
    txt = re.sub(r'<[^>]+>', ' ', txt)
    txt = re.sub(r'&nbsp;', ' ', txt)
    return re.sub(r'\s+', ' ', txt).strip()

def _fetch_html(url: str) -> str:
    try:
        r = requests.get(url, timeout=12, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        })
        return r.text if r.ok else ""
    except Exception:
        return ""

def _parse_tour_text(txt: str) -> Dict[str, Any]:
    # Date
    tdate = None
    m = _DATE_HEADER_RE.search(txt)
    if m:
        mon, day, year = m.group(1), int(m.group(2)), int(m.group(3))
        tdate = date(int(year), _month_to_num(mon), int(day))
    else:
        m2 = _GENERIC_DATE_RE.search(txt)
        if m2:
            mon, day, year = m2.group(1), int(m2.group(2)), int(m2.group(3))
            tdate = date(int(year), _month_to_num(mon), int(day))

    # Addresses and times in document order
    addr_iter = list(_ADDR_RE.finditer(txt))
    time_iter = list(_TIME_RE.finditer(txt))

    # Build index of time ranges by start position
    times_by_pos: List[Tuple[int, Tuple[str,str]]] = []
    for tm in time_iter:
        times_by_pos.append((tm.start(), (tm.group(1).upper().replace(" ", ""), tm.group(2).upper().replace(" ", ""))))
    times_by_pos.sort(key=lambda x: x[0])

    stops: List[Dict[str, Any]] = []
    if not addr_iter:
        return {"tour_date": (tdate.isoformat() if tdate else None), "client_guess": "", "stops": []}

    # Helper to find nearest time window around an address occurrence
    def nearest_time(idx: int) -> Tuple[str, str]:
        if not times_by_pos: return "", ""
        apos = addr_iter[idx].start()
        # pick first time whose start is after address, else previous
        after = [t for t in times_by_pos if t[0] >= apos]
        if after:
            return after[0][1]
        return times_by_pos[-1][1]

    # Status detection around each address (look ±300 chars)
    def status_for_span(span_start: int, span_end: int) -> str:
        window_l = max(0, span_start - 300)
        window_r = min(len(txt), span_end + 300)
        w = txt[window_l:window_r].upper()
        if "CANCELLED" in w or "CANCELED" in w:
            return "CANCELED"
        if "CONFIRMED" in w:
            return "CONFIRMED"
        return ""

    for i, am in enumerate(addr_iter):
        addr = am.group(0).strip()
        start, end = nearest_time(i)
        stat = status_for_span(am.start(), am.end())
        stops.append({
            "address": addr,
            "start": start.replace("AM"," AM").replace("PM"," PM").strip(),
            "end":   end.replace("AM"," AM").replace("PM"," PM").strip(),
            "deeplink": _address_to_deeplink(addr),
            "status": stat
        })

    return {
        "tour_date": (tdate.isoformat() if tdate else None),
        "client_guess": "",  # we do not trust name in doc—leave blank
        "stops": stops
    }

def parse_showingtime_input(url: str, uploaded_pdf) -> Dict[str, Any]:
    # Prefer PDF if present (more consistent text)
    if uploaded_pdf is not None:
        if not PyPDF2:
            return {"error": "PyPDF2 not installed (add to requirements.txt)."}
        text = _extract_text_from_pdf(uploaded_pdf)
        if not text.strip():
            return {"error": "Could not read PDF text."}
        return _parse_tour_text(text)

    # Else URL
    if url and url.strip():
        html = _fetch_html(url.strip())
        if not html:
            return {"error": "Could not fetch the Print page URL."}
        text = _html_to_text(html)
        return _parse_tour_text(text)

    return {"error": "Provide a Print URL or a PDF."}


# ====== Tours model helpers ======
def _create_or_get_tour(client_norm: str, client_display: str, tour_url: str, tour_date: date) -> int:
    if not SUPABASE:
        raise RuntimeError("Supabase not configured.")
    # Reuse same-day tour (per client/date)
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
        "status": "imported",  # keep inside allowed check constraint
    }).execute()
    if not ins.data:
        # surface concise message
        raise RuntimeError("Create tour failed.")
    return ins.data[0]["id"]

def _insert_stops(tour_id: int, stops: List[Dict[str, Any]]) -> int:
    if not SUPABASE:
        return 0
    existing = SUPABASE.table("tour_stops").select("address_slug").eq("tour_id", tour_id).limit(50000).execute().data or []
    seen = {e["address_slug"] for e in existing if e.get("address_slug")}
    rows = []
    for s in stops:
        addr = (s.get("address") or "").strip()
        if not addr: continue
        slug = _slug_addr(addr)
        if slug in seen: continue
        rows.append({
            "tour_id": tour_id,
            "address": addr,
            "address_slug": slug,
            "start": (s.get("start") or None),
            "end":   (s.get("end") or None),
            "deeplink": (s.get("deeplink") or _address_to_deeplink(addr)),
            # NOTE: not storing status to avoid schema errors; we will still show tags in preview/report if desired
        })
        seen.add(slug)
    if not rows:
        return 0
    ins = SUPABASE.table("tour_stops").insert(rows).execute()
    return len(ins.data or [])

def _build_repeat_map(client_norm: str) -> Dict[tuple, int]:
    if not SUPABASE: return {}
    tq = SUPABASE.table("tours").select("id,tour_date").eq("client", client_norm).order("tour_date", desc=False).limit(5000).execute()
    tours = tq.data or []
    if not tours: return {}
    ids = [t["id"] for t in tours]
    sq = SUPABASE.table("tour_stops").select("tour_id,address_slug").in_("tour_id", ids).limit(50000).execute()
    stops = sq.data or []
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


# ====== Session: parsed payload ======
def _get_parsed() -> Dict[str, Any]:
    return st.session_state.get("__parsed_tour__") or {}

def _set_parsed(payload: Dict[str, Any]):
    st.session_state["__parsed_tour__"] = payload or {}


# ====== Renderer ======
def render_tours_tab(state: dict):
    # ---------- Styles (better tag contrast; minimal) ----------
    st.markdown("""
    <style>
      .tag{display:inline-block;margin-left:.4rem;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:800;}
      .tag.ok{background:#166534; color:#ecfdf5; border:1px solid #16a34a;}
      .tag.bad{background:#7f1d1d; color:#fee2e2; border:1px solid #ef4444;}
      .tag.repeat{background:#92400e; color:#fff7ed; border:1px solid #f59e0b;}
      .time{font-weight:700;margin-right:.35rem;}
      .thinsep{border-bottom:1px solid var(--row-border); margin:.5rem 0;}
    </style>
    """, unsafe_allow_html=True)

    # ---------- IMPORT (Parse) UI (on top) ----------
    st.markdown("### Import a tour")
    col1, col2 = st.columns([1.3, 1])
    with col1:
        url = st.text_input("Paste ShowingTime **Print** URL", placeholder="https://scheduling.showingtime.com/.../Tour/Print/30235965")
    with col2:
        uploaded = st.file_uploader("Or drop the **Tour PDF**", type=["pdf"])

    cA, cB, cC = st.columns([0.25, 0.25, 0.5])
    with cA:
        do_parse = st.button("Parse", use_container_width=True, key="__parse_btn__")
    with cB:
        do_clear = st.button("Clear", use_container_width=True, key="__clear_btn__")
    with cC:
        st.write("")  # spacer

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
            # no logs shown—just store and render below
            _safe_rerun()

    parsed = _get_parsed()

    # ---------- Parsed preview ----------
    if parsed:
        tour_date_iso = parsed.get("tour_date")
        stops = parsed.get("stops", [])
        st.markdown("#### Preview")
        st.caption(f"Parsed {len(stops)} stop(s)" + (f" • {tour_date_iso}" if tour_date_iso else ""))

        if stops:
            items = []
            for s in stops:
                addr = s.get("address","").strip()
                href = (s.get("deeplink") or _address_to_deeplink(addr)).strip()
                start = (s.get("start") or "").strip()
                end   = (s.get("end") or "").strip()
                when  = f"{start}–{end}" if (start and end) else (start or end or "")
                stat  = (s.get("status") or "").upper()
                tag = ""
                if stat in ("CONFIRMED","CANCELED","CANCELLED"):
                    tag = f' <span class="tag {"ok" if stat=="CONFIRMED" else "bad"}">{escape(stat.title())}</span>'
                items.append(
                    f"<li style='margin:0.25rem 0;'>"
                    f"{(f'<span class=\"time\">{escape(when)}</span> ' if when else '')}"
                    f"<a href='{escape(href)}' target='_blank' rel='noopener'>{escape(addr)}</a>{tag}"
                    f"</li>"
                )
            st.markdown("<ul class='link-list'>" + "\n".join(items) + "</ul>", unsafe_allow_html=True)
        else:
            st.info("No stops to show yet.")

        st.markdown('<div class="thinsep"></div>', unsafe_allow_html=True)

        # ---------- Add-all flow ----------
        clients = fetch_clients()
        client_names = [c["name"] for c in clients]
        client_norms = [c["name_norm"] for c in clients]

        NO_CLIENT = "➤ No client (show ALL, no logging)"

        # 1) Client display name for the tour record
        #    (dropdown of actual clients only, excluding "test test" which is already filtered)
        pick_idx = st.selectbox(
            "Client display name (for the tour record)",
            list(range(len(client_names))),
            format_func=lambda i: client_names[i] if client_names else "",
            index=(0 if client_names else 0),
            key="__tour_client_display__"
        )
        client_display = (client_names[pick_idx] if client_names else "").strip()
        client_display_norm = (client_norms[pick_idx] if client_names else "").strip()

        # 2) Add all stops to client (No logging first)
        add_names = [NO_CLIENT] + client_names
        add_norms = [""         ] + client_norms
        add_idx = st.selectbox(
            "Add all stops to client (optional)",
            list(range(len(add_names))),
            format_func=lambda i: add_names[i] if add_names else "",
            index=0,
            key="__tour_add_to_client__"
        )
        cho_client_name = add_names[add_idx]
        cho_client_norm = add_norms[add_idx]

        colAA, colBB = st.columns([0.35, 0.65])
        with colAA:
            can_add = bool(stops) and bool(tour_date_iso) and (client_display and client_display_norm)
            if st.button("Add all stops", use_container_width=True, disabled=not can_add):
                try:
                    # Only create/log when a real client was chosen
                    if cho_client_norm:
                        # Create/reuse tour by (client, date)
                        tdate = datetime.fromisoformat(tour_date_iso).date() if tour_date_iso else date.today()
                        tid = _create_or_get_tour(
                            client_norm=client_display_norm,  # tour owner = display dropdown
                            client_display=client_display,
                            tour_url=(url or None),
                            tour_date=tdate
                        )
                        n = _insert_stops(tid, stops)
                        st.success(f"Added {n} stop(s) to {client_display} for {tdate}.")
                    else:
                        st.info("Preview only — not logged (No client).")

                except Exception as e:
                    st.error(f"Could not add stops. {e}")

        with colBB:
            st.caption("Tip: choose **No client** if you only want to parse/preview without logging.")

    st.markdown("---")

    # ---------- Tours report viewer (pick and jump-able from Clients tab) ----------
    st.markdown("### Tours report")

    clients2 = fetch_clients()
    names2 = [c["name"] for c in clients2]
    norms2 = [c["name_norm"] for c in clients2]

    preselect_norm = _qp_get("tours", "")
    default_idx = norms2.index(preselect_norm) if preselect_norm in norms2 else (0 if norms2 else 0)

    colR1, colR2 = st.columns([1.2, 1])
    with colR1:
        idx = st.selectbox("Pick a client", list(range(len(names2))), format_func=lambda i: names2[i] if names2 else "", index=(default_idx if names2 else 0), key="__tour_client_pick__")
    with colR2:
        st.write("")
        if st.button("Show report", use_container_width=True):
            pass

    if names2:
        client_displayR = names2[idx]
        client_normR    = norms2[idx]
        _render_client_tours_report(client_displayR, client_normR)
    else:
        st.info("No clients found.")

    st.markdown('<div id="tours_report_anchor"></div>', unsafe_allow_html=True)


def _render_client_tours_report(client_display: str, client_norm: str):
    # Show all dates with (time) + hyperlink + repeat tag
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
        st.markdown(f"#### {escape(client_display)} — {td}")
        sq = SUPABASE.table("tour_stops").select("address,address_slug,start,end,deeplink").eq("tour_id", t["id"]).order("start", desc=False).limit(500).execute()
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
            repeat_html = f' <span class="tag repeat">{_ordinal(visit)} showing</span>' if visit >= 2 else ""
            items.append(
                f"<li style='margin:0.25rem 0;'>"
                f"{(f'<span class=\"time\">{escape(when)}</span> ' if when else '')}"
                f"<a href='{escape(href)}' target='_blank' rel='noopener'>{escape(addr)}</a>"
                f"{repeat_html}"
                f"</li>"
            )
        st.markdown("<ul class='link-list'>" + "\n".join(items) + "</ul>", unsafe_allow_html=True)
