# ui/run_tab.py
import os, re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st

# --- Make supabase import SAFE so the module can import even if package is missing ---
try:
    from supabase import create_client, Client
except Exception:
    create_client = None
    Client = Any  # type: ignore

__all__ = ["render_run_tab"]

# ---------- Styles (parity with Clients/Tours tabs) ----------
def _inject_css_once():
    ver = "run-css-2025-09-21b"
    if st.session_state.get("__run_css__") == ver:
        return
    st.session_state["__run_css__"] = ver
    st.markdown("""
<style>
/* Mac-style blue button */
.blue-btn-zone .stButton > button {
  background: linear-gradient(180deg, #0A84FF 0%, #0060DF 100%) !important;
  color: #FFFFFF !important;
  font-weight: 800 !important;
  border: 0 !important;
  border-radius: 12px !important;
  box-shadow: 0 8px 20px rgba(10,132,255,.35), 0 2px 6px rgba(0,0,0,.18) !important;
  transition: all .1s ease !important;
}
.blue-btn-zone .stButton > button:hover {
  box-shadow: 0 12px 26px rgba(10,132,255,.42), 0 4px 10px rgba(0,0,0,.2) !important;
}

/* Optional neutral chip (not used by default) */
.meta-chip {
  display:inline-block; font-size:11px; font-weight:800;
  padding:2px 6px; border-radius:999px; margin-left:8px;
  background:#eef2ff; color:#1e3a8a; border:1px solid #c7d2fe;
}
html[data-theme="dark"] .meta-chip {
  background:#1f2937; color:#bfdbfe; border-color:#374151;
}

/* Light blue date badge */
.date-badge {
  display:inline-block; font-size:11px; font-weight:800;
  padding:2px 6px; border-radius:999px; margin-left:8px;
  background:#e0f2fe; color:#075985; border:1px solid #7dd3fc;
}
html[data-theme="dark"] .date-badge {
  background:#0b1220; color:#7dd3fc; border-color:#164e63;
}

/* Red toured badge */
.toured-badge {
  display:inline-block; font-size:11px; font-weight:800;
  padding:2px 6px; border-radius:999px; margin-left:8px;
  background:#fee2e2; color:#991b1b; border:1px solid #fecaca;
}
html[data-theme="dark"] .toured-badge {
  background:#7f1d1d; color:#fecaca; border-color:#ef4444;
}

/* Row layout */
.run-row {
  display:flex; justify-content:space-between; align-items:center;
  gap:12px; padding:6px 0; border-bottom:1px solid var(--row-border, #e2e8f0);
}
.run-left  { min-width:0; }
.run-right { flex:0 0 auto; display:flex; align-items:center; gap:8px; }
</style>
""", unsafe_allow_html=True)

# ---------- Supabase ----------
@st.cache_resource(show_spinner=False)
def get_supabase() -> Optional["Client"]:
    if create_client is None:
        return None
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

# ---------- Normalization helpers (match Clients/Tours logic) ----------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

_STTYPE = {
    "street":"st","st":"st","st.":"st",
    "avenue":"ave","ave":"ave","ave.":"ave","av":"ave",
    "road":"rd","rd":"rd","rd.":"rd",
    "drive":"dr","dr":"dr","dr.":"dr",
    "lane":"ln","ln":"ln","ln.":"ln",
    "boulevard":"blvd","blvd":"blvd","blvd.":"blvd",
    "court":"ct","ct":"ct","ct.":"ct",
    "place":"pl","pl":"pl","pl.":"pl",
    "terrace":"ter","ter":"ter","ter.":"ter",
    "highway":"hwy","hwy":"hwy",
    "parkway":"pkwy","pkwy":"pkwy",
    "circle":"cir","cir":"cir",
    "square":"sq","sq":"sq",
}
_DIR = {"north":"n","n":"n","south":"s","s":"s","east":"e","e":"e","west":"w","w":"w","n.":"n","s.":"s","e.":"e","w.":"w"}

def _token_norm(tok: str) -> str:
    t = tok.lower().strip(" .,#")
    if t in _STTYPE: return _STTYPE[t]
    if t in _DIR:    return _DIR[t]
    if t in {"apt","unit","ste","lot"}: return ""
    return re.sub(r"[^a-z0-9-]", "", t)

def _norm_slug_from_text(text: str) -> str:
    s = (text or "").lower().replace("&", " and ")
    toks = re.split(r"[^a-z0-9]+", s)
    norm = [t for t in (_token_norm(t) for t in toks) if t]
    return "-".join(norm)

_RE_HD = re.compile(r"/homedetails/([^/]+)/\d{6,}_zpid/?", re.I)
_RE_HM = re.compile(r"/homes/([^/_]+)_rb/?", re.I)

def _norm_slug_from_url(url: str) -> str:
    u = (url or "").strip()
    m = _RE_HD.search(u)
    if m: return _norm_slug_from_text(m.group(1))
    m = _RE_HM.search(u)
    if m: return _norm_slug_from_text(m.group(1))
    return ""

def _address_to_deeplink(addr: str) -> str:
    # For plain addresses, make a Zillow search-friendly deeplink
    slug = _norm_slug_from_text(addr)
    return f"https://www.zillow.com/homes/{slug}_rb/"

def _fmt_ts_date_tag(ts: str) -> str:
    """Return YYYYMMDD from ISO or epoch-ish strings; '' if cannot parse."""
    if ts is None or str(ts).strip() == "":
        return ""
    raw = str(ts).strip()
    # epoch seconds/millis
    try:
        if raw.isdigit():
            val = int(raw)
            if val > 10_000_000_000:  # ms
                d = datetime.utcfromtimestamp(val / 1000.0)
            else:
                d = datetime.utcfromtimestamp(val)
            return d.strftime("%Y%m%d")
    except Exception:
        pass
    # ISO
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return d.strftime("%Y%m%d")
    except Exception:
        return ""

# ---------- Main ----------
def render_run_tab(state: dict):
    _inject_css_once()
    st.markdown("### Run")

    # Inputs
    addr_input = st.text_area("Paste addresses or listing links", height=120, placeholder="One per line — address or Zillow URL")
    client_input = st.text_input("Client (optional — used to check if any were toured)")
    st.markdown("<div class='blue-btn-zone'>", unsafe_allow_html=True)
    btn_run = st.button("Parse & Log", use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)

    results: List[Dict[str, Any]] = []
    if btn_run:
        if not addr_input.strip():
            st.warning("Please paste something first.")
        else:
            lines = [x.strip() for x in addr_input.splitlines() if x.strip()]
            now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            for ln in lines:
                # Build a robust property slug from URL or address text
                slug = _norm_slug_from_url(ln) or _norm_slug_from_text(ln)
                # Prefer a deeplink if not already a zillow homedetails/homes URL
                href = ln
                if not (_RE_HD.search(ln) or _RE_HM.search(ln)):
                    href = _address_to_deeplink(ln)
                results.append({
                    "input": ln,
                    "href": href,
                    "slug": slug,
                    "ts": now_iso,
                })

    # If no results (first load or no click), show a tip and return
    if not results:
        st.info("Paste addresses/links above, choose a client (optional), then click **Parse & Log**.")
        return

    # Fetch toured property slugs for this client (normalized)
    toured_slugs: set = set()
    client_norm = _norm_tag(client_input) if client_input else ""
    if client_norm and SUPABASE:
        try:
            tq = SUPABASE.table("tours").select("id").eq("client", client_norm).limit(5000).execute()
            tour_ids = [t["id"] for t in (tq.data or [])]
            if tour_ids:
                sq = SUPABASE.table("tour_stops").select("address,address_slug").in_("tour_id", tour_ids).limit(50000).execute()
                for s in (sq.data or []):
                    if s.get("address_slug"):
                        toured_slugs.add(_norm_slug_from_text(s["address_slug"]))
                    elif s.get("address"):
                        toured_slugs.add(_norm_slug_from_text(s["address"]))
        except Exception:
            pass

    # Render results with consistent badges
    st.markdown("#### Results")
    items: List[str] = []
    for r in results:
        slug = r["slug"]
        href = r["href"]
        label = r["input"]
        date_tag = _fmt_ts_date_tag(r["ts"])
        toured = slug in toured_slugs

        right_badges: List[str] = []
        if date_tag:
            right_badges.append(f"<span class='date-badge'>{date_tag}</span>")
        if toured:
            right_badges.append("<span class='toured-badge'>Toured</span>")

        items.append(
            "<li class='run-row'>"
            + f"<div class='run-left'><a href='{st.html.escape(href)}' target='_blank' rel='noopener'>{st.html.escape(label)}</a></div>"
            + "<div class='run-right'>" + "".join(right_badges) + "</div>"
            + "</li>"
        )

    st.markdown("<ul style='margin:.25rem 0 .5rem 0;padding:0;list-style:none;'>" + "\n".join(items) + "</ul>", unsafe_allow_html=True)
