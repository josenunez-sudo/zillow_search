# ui/tours_tab.py
import re
from datetime import datetime, date
from html import escape
from typing import List, Dict, Any, Optional

import streamlit as st
import streamlit.components.v1 as components
from supabase import create_client, Client
import os

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

# ---- Small utils ----
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

# ===== Tours model helpers =====
def _create_or_get_tour(client_norm: str, client_display: str, tour_url: str, tour_date: date) -> int:
    # Reuse same-day tour
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
        "status": "saved",
    }).execute()
    if not ins.data:
        raise RuntimeError(f"Create tour failed: {getattr(ins,'dict',lambda:{})()}")
    return ins.data[0]["id"]

def _insert_stops(tour_id: int, stops: List[Dict[str, Any]]) -> int:
    # Avoid duplicates per tour_id + address_slug
    existing = SUPABASE.table("tour_stops").select("address_slug").eq("tour_id", tour_id).execute().data or []
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
            "status": (s.get("status") or None),  # optional (CONFIRMED / CANCELED)
        })
        seen.add(slug)
    if not rows: return 0
    ins = SUPABASE.table("tour_stops").insert(rows).execute()
    return len(ins.data or [])

def _build_repeat_map(client_norm: str) -> Dict[tuple, int]:
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

def _render_client_tours_report(client_display: str, client_norm: str):
    st.markdown('<div id="tours_report_anchor"></div>', unsafe_allow_html=True)

    tq = SUPABASE.table("tours").select("id,tour_date").eq("client", client_norm).order("tour_date", desc=True).limit(2000).execute()
    tours = tq.data or []
    if not tours:
        st.info("No tours logged for this client yet.")
        return

    repeat_map = _build_repeat_map(client_norm)

    # Style for time + repeat tag + status tag
    st.markdown("""
    <style>
      .time{font-weight:700;margin-right:.35rem;}
      .tag{display:inline-block;margin-left:.5rem;padding:2px 6px;border-radius:8px;font-size:11px;font-weight:800;}
      .tag.repeat{background:#fef3c7;color:#92400e;border:1px solid #f59e0b;}
      .tag.ok{background:#dcfce7;color:#166534;border:1px solid #16a34a;}
      .tag.bad{background:#fee2e2;color:#991b1b;border:1px solid #ef4444;}
    </style>
    """, unsafe_allow_html=True)

    for t in tours:
        td = t["tour_date"]
        st.markdown(f"##### {td}")
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
            repeat_html = f' <span class="tag repeat">{_ordinal(visit)} showing</span>' if visit >= 2 else ""
            status = (s.get("status") or "").upper()
            status_html = (f' <span class="tag {"ok" if status=="CONFIRMED" else "bad"}">{escape(status.title())}</span>') if status in ("CONFIRMED","CANCELED","CANCELLED") else ""

            items.append(
                f"<li style='margin:0.25rem 0;'>"
                f"{(f'<span class=\"time\">{escape(when)}</span> ' if when else '')}"
                f"<a href='{escape(href)}' target='_blank' rel='noopener'>{escape(addr)}</a>"
                f"{repeat_html}{status_html}"
                f"</li>"
            )

        st.markdown("<ul class='link-list'>" + "\n".join(items) + "</ul>", unsafe_allow_html=True)

# ===== Parse stub you already have =====
# Expect your existing parser to set st.session_state['__parsed_tour__'] = {
#   'client_guess': 'Keelie Mason',
#   'tour_date': '2025-09-22',  # ISO
#   'stops': [{'address':..., 'start':..., 'end':..., 'deeplink':..., 'status': 'CONFIRMED'|'CANCELED'|None}, ...]
# }
def _get_parsed() -> Dict[str, Any]:
    return st.session_state.get("__parsed_tour__") or {}

def _set_parsed(payload: Dict[str, Any]):
    st.session_state["__parsed_tour__"] = payload or {}

# ===== Main renderer =====
def render_tours_tab(state: dict):
    # --- Top: choose a client to view report (and handle jump from Clients tab) ---
    st.markdown("### Tours report")

    clients = fetch_clients()
    names = [c["name"] for c in clients]
    norms = [c["name_norm"] for c in clients]

    preselect_norm = _qp_get("tours", "")
    default_idx = norms.index(preselect_norm) if preselect_norm in norms else (0 if norms else 0)

    colA, colB = st.columns([1.2, 1])
    with colA:
        idx = st.selectbox("Pick a client", list(range(len(names))), format_func=lambda i: names[i] if names else "", index=default_idx if names else 0, key="__tour_client_pick__")
    with colB:
        st.write("")  # spacing
        if st.button("Show report", use_container_width=True):
            pass  # just re-renders with selected idx

    if names:
        client_display = names[idx]
        client_norm    = norms[idx]
        _render_client_tours_report(client_display, client_norm)
    else:
        st.info("No clients found.")

    st.markdown("---")
    st.markdown("### Import a tour (Print URL or PDF)")
    st.caption("Paste a ShowingTime *Print* page URL or drop the exported Tour PDF.")

    # Your existing parse UI goes here (unchanged):
    # - Text input for URL
    # - File uploader for PDF
    # - Parse button that sets _set_parsed({...})
    # - “Add all stops to client …” flow you already wired
    st.info("Use your existing ‘Parse’ form below this point (kept as-is).")
