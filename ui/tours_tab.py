# ui/tours_tab.py
import os
import re
from datetime import datetime, date
from html import escape
from typing import List, Dict, Any, Optional, Tuple

import requests
import streamlit as st
from supabase import create_client, Client

# ========= Optional PDF support =========
try:
    import PyPDF2  # add "PyPDF2" to requirements.txt
except Exception:
    PyPDF2 = None


# ========= Supabase =========
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
    try:
        st.rerun()
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
    """Returns (canonical_url, zpid_if_any). Works for /homes/ and /homedetails/."""
    if not url:
        return "", None
    base = re.sub(r"[?#].*$", "", url.strip())
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', base, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = re.search(r'(\d{6,})_zpid', base, re.I)
    return canon, (m_z.group(1) if m_z else None)

# ----- Query params -----
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

# ========= Clients =========
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients():
    if not SUPABASE: return []
    try:
        rows = SUPABASE.table("clients")\
            .select("id,name,name_norm,active")\
            .order("name", desc=False)\
            .execute().data or []
        # filter out test test
        return [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
    except Exception:
        return []

# ========= Parsing (robust, no raw logs displayed) =========
_TIME_RE = re.compile(r'(\d{1,2}:\d{2}\s*(?:AM|PM))\s*[-–]\s*(\d{1,2}:\d{2}\s*(?:AM|PM))', re.I)
_ADDR_RE = re.compile(r'\b\d{1,6}\s+[^\n,]+,\s+[A-Za-z .\'-]+,\s*[A-Z]{2}\s*\d{5}\b')
_DATE_HEADER_RE = re.compile(r'(?:Buyer|Agent)[^\n]{0,80}Tour\s*-\s*[A-Za-z]+,\s*([A-Za-z]+)\s*(\d{1,2}),\s*(\d{4})', re.I)
_GENERIC_DATE_RE = re.compile(r'([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})')

def _month_to_num(m: str) -> int:
    try:
        return datetime.strptime(m[:3], "%b").month
    except Exception:
        return datetime.strptime(m, "%B").month

def _extract_text_from_pdf(file) -> str:
    if not PyPDF2: return ""
    try:
        rd = PyPDF2.PdfReader(file)
        buf = []
        for p in rd.pages:
            buf.append(p.extract_text() or "")
        return "\n".join(buf)
    except Exception:
        return ""

def _html_to_text(html: str) -> str:
    txt = re.sub(r'<script[\s\S]*?</script>', ' ', html, flags=re.I)
    txt = re.sub(r'<style[\s\S]*?</style>', ' ', txt, flags=re.I)
    txt = re.sub(r'<[^>]+>', ' ', txt)
    txt = txt.replace("&nbsp;", " ")
    return re.sub(r'\s+', ' ', txt).strip()

def _fetch_html(url: str) -> str:
    try:
        r = requests.get(url, timeout=12, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
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

    # Gather times near addresses
    addr_iter = list(_ADDR_RE.finditer(txt))
    time_iter = list(_TIME_RE.finditer(txt))
    times_by_pos: List[Tuple[int, Tuple[str,str]]] = []
    for tm in time_iter:
        s = tm.group(1).upper().replace(" ", "")
        e = tm.group(2).upper().replace(" ", "")
        times_by_pos.append((tm.start(), (s, e)))
    times_by_pos.sort(key=lambda x: x[0])

    def nearest_time(addr_pos: int) -> Tuple[str, str]:
        if not times_by_pos: return "", ""
        after = [t for t in times_by_pos if t[0] >= addr_pos]
        return after[0][1] if after else times_by_pos[-1][1]

    def status_around(span_start: int, span_end: int) -> str:
        w = txt[max(0, span_start-300): min(len(txt), span_end+300)].upper()
        if "CANCELLED" in w or "CANCELED" in w:
            return "CANCELED"
        if "CONFIRMED" in w:
            return "CONFIRMED"
        return ""

    stops: List[Dict[str, Any]] = []
    for am in addr_iter:
        addr = am.group(0).strip()
        t1, t2 = nearest_time(am.start())
        stat = status_around(am.start(), am.end())
        starts = t1.replace("AM", " AM").replace("PM", " PM").strip()
        ends   = t2.replace("AM", " AM").replace("PM", " PM").strip()
        stops.append({
            "address": addr,
            "start": starts,
            "end": ends,
            "deeplink": _address_to_deeplink(addr),
            "status": stat  # tag only; we *never* render any raw detailed text
        })

    return {
        "tour_date": (tdate.isoformat() if tdate else None),
        "stops": stops
    }

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

# ========= DB helpers =========
def _create_or_get_tour(client_norm: str, client_display: str, tour_url: Optional[str], tour_date: date) -> int:
    if not SUPABASE:
        raise RuntimeError("Supabase not configured.")
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
        "status": "imported",  # use an allowed status per your constraint
    }).execute()
    if not ins.data:
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
        })
        seen.add(slug)
    if not rows:
        return 0
    ins = SUPABASE.table("tour_stops").insert(rows).execute()
    return len(ins.data or [])

def _insert_sent_for_stops(client_norm: str, stops: List[Dict[str, Any]], tour_date: date) -> int:
    """Also mark as 'toured' in sent (for Client/Run views). Campaign = toured-YYYYMMDD."""
    if not SUPABASE or not client_norm or not stops:
        return 0
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    campaign = f"toured-{tour_date.strftime('%Y%m%d')}"
    rows = []
    for s in stops:
        addr = (s.get("address") or "").strip()
        if not addr:
            continue
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
    if not rows:
        return 0
    try:
        ins = SUPABASE.table("sent").insert(rows).execute()
        return len(ins.data or [])
    except Exception:
        # swallow errors to avoid user-visible log spam
        return 0

def _build_repeat_map(client_norm: str) -> Dict[tuple, int]:
    if not SUPABASE: return {}
    tq = SUPABASE.table("tours").select("id,tour_date").eq("client", client_norm).order("tour_date", desc=False).limit(5000).execute()
    tours = tq.data or []
    if not tours: return {}
    ids = [t["id"] for t in tours]
    if not ids: return {}
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

# ========= Session: parsed payload =========
def _get_parsed() -> Dict[str, Any]:
    return st.session_state.get("__parsed_tour__") or {}

def _set_parsed(payload: Dict[str, Any]):
    st.session_state["__parsed_tour__"] = payload or {}

# ========= Small HTML helpers =========
def _date_badge_html(d: str) -> str:
    # High-contrast inline style so it always shows
    return (
        "<span style='display:inline-block;padding:2px 10px;border-radius:9999px;"
        "background:#1d4ed8;color:#fff;font-weight:800;border:1px solid rgba(0,0,0,.15);'>"
        f"{escape(d)}</span>"
    )

def _status_tag_html(stat_upper: str) -> str:
    if stat_upper == "CONFIRMED":
        return "<span style='margin-left:.4rem;padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:800;background:#166534;color:#ecfdf5;border:1px solid #16a34a;'>Confirmed</span>"
    if stat_upper in ("CANCELED","CANCELLED"):
        return "<span style='margin-left:.4rem;padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:800;background:#7f1d1d;color:#fee2e2;border:1px solid #ef4444;'>Canceled</span>"
    return ""

def _repeat_tag_html(n: int) -> str:
    if n >= 2:
        return "<span style='margin-left:.4rem;padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:800;background:#92400e;color:#fff7ed;border:1px solid #f59e0b;'>2nd+ showing</span>"
    return ""

# ========= Renderer =========
def render_tours_tab(state: dict):
    # ========== IMPORT (Parse) ==========
    st.markdown("### Import a tour")
    col1, col2 = st.columns([1.3, 1])
    with col1:
        url = st.text_input("Paste ShowingTime **Print** URL", placeholder="https://scheduling.showingtime.com/.../Tour/Print/30235965")
    with col2:
        uploaded = st.file_uploader("Or drop the **Tour PDF**", type=["pdf"])

    cA, cB, _ = st.columns([0.25, 0.25, 0.5])
    with cA:
        do_parse = st.button("Parse", use_container_width=True, key="__parse_btn__")
    with cB:
        do_clear = st.button("Clear", use_container_width=True, key="__clear_btn__")

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

    # ========== Preview ==========
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
                addr = s.get("address","").strip()
                href = (s.get("deeplink") or _address_to_deeplink(addr)).strip()
                start = (s.get("start") or "").strip()
                end   = (s.get("end") or "").strip()
                when  = f"{start}–{end}" if (start and end) else (start or end or "")
                stat  = (s.get("status") or "").upper()
                lis.append(
                    "<li>"
                    + (f"<span style='font-weight:800;margin-right:.35rem;'>{escape(when)}</span> " if when else "")
                    + f"<a href='{escape(href)}' target='_blank' rel='noopener'>{escape(addr)}</a>"
                    + _status_tag_html(stat)
                    + "</li>"
                )
            st.markdown("<ul style='margin:.25rem 0 .5rem 1.2rem;padding:0;'>" + "\n".join(lis) + "</ul>", unsafe_allow_html=True)
        else:
            st.info("No stops to preview.")

        st.markdown("<div style='border-bottom:1px solid var(--row-border); margin:.5rem 0;'></div>", unsafe_allow_html=True)

        # ========== Add all stops flow ==========
        clients = fetch_clients()
        client_names = [c["name"] for c in clients]
        client_norms = [c["name_norm"] for c in clients]

        NO_CLIENT = "➤ No client (show ALL, no logging)"

        # 1) Client display name (dropdown of real clients only)
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

        # 2) Add all stops to client (optional) — first option = No client
        add_names = [NO_CLIENT] + client_names
        add_norms = [""         ] + client_norms
        idx_add = st.selectbox(
            "Add all stops to client (optional)",
            list(range(len(add_names))),
            format_func=lambda i: add_names[i],
            index=0,
            key="__tour_add_to_client__"
        )
        chosen_norm = add_norms[idx_add]

        # 3) Also mark as "toured" in Sent (so client/run views can tag toured)
        also_mark_sent = st.checkbox("Also mark these stops as “toured” in Sent", value=True)

        colAA, colBB = st.columns([0.35, 0.65])
        with colAA:
            can_add = bool(stops) and bool(tdate) and bool(client_display and client_display_norm)
            add_clicked = st.button("Add all stops", use_container_width=True, disabled=not can_add)
            if add_clicked:
                try:
                    # Create/attach to the display client's tour (owner)
                    tdate_obj = datetime.fromisoformat(tdate).date() if tdate else date.today()
                    tour_id = _create_or_get_tour(
                        client_norm=client_display_norm,
                        client_display=client_display,
                        tour_url=(url or None),
                        tour_date=tdate_obj
                    )
                    n = _insert_stops(tour_id, stops)
                    # Optional: also log to sent (for toured indicator)
                    if also_mark_sent and chosen_norm:
                        _insert_sent_for_stops(chosen_norm, stops, tdate_obj)
                    st.success(f"Added {n} stop(s) to {client_display} for {tdate_obj}.")
                except Exception as e:
                    st.error(f"Could not add stops. {e}")

        with colBB:
            st.caption("Tip: choose **No client** if you only want to parse/preview without logging.")

    st.markdown("---")

    # ========== Tours report ==========
    st.markdown("### Tours report")
    clients2 = fetch_clients()
    names2 = [c["name"] for c in clients2]
    norms2 = [c["name_norm"] for c in clients2]

    preselect_norm = _qp_get("tours", "")
    default_idx = norms2.index(preselect_norm) if preselect_norm in norms2 else (0 if norms2 else 0)

    colR1, colR2 = st.columns([1.2, 1])
    with colR1:
        if names2:
            irep = st.selectbox("Pick a client", list(range(len(names2))), format_func=lambda i: names2[i], index=default_idx, key="__tour_client_pick__")
        else:
            irep = 0
    with colR2:
        st.write("")  # spacer
        st.button("Show report", use_container_width=True)

    if names2:
        _render_client_tours_report(names2[irep], norms2[irep])
    else:
        st.info("No clients found.")

    st.markdown('<div id="tours_report_anchor"></div>', unsafe_allow_html=True)


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
            items.append(
                "<li>"
                + (f"<span style='font-weight:800;margin-right:.35rem;'>{escape(when)}</span> " if when else "")
                + f"<a href='{escape(href)}' target='_blank' rel='noopener'>{escape(addr)}</a>"
                + _repeat_tag_html(visit)
                + "</li>"
            )
        st.markdown("<ul style='margin:.25rem 0 .5rem 1.2rem;padding:0;'>" + "\n".join(items) + "</ul>", unsafe_allow_html=True)
