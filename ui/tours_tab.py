# ui/tours_tab.py
# Tours tab: parse ShowingTime Print page or Tour PDF → list of Zillow links with readable date chips.
# Self-contained to avoid circular imports.

import os, re, io, csv, requests
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from html import unescape as html_unescape, escape

import streamlit as st
import streamlit.components.v1 as components

# ---------- Optional PDF deps ----------
try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text  # preferred
except Exception:
    pdfminer_extract_text = None

try:
    import PyPDF2  # fallback
except Exception:
    PyPDF2 = None

# ---------- Supabase (optional) ----------
try:
    from supabase import create_client, Client  # type: ignore
except Exception:
    Client = None  # noqa: N816

@st.cache_resource
def _get_supabase() -> Optional["Client"]:
    try:
        url = os.getenv("SUPABASE_URL", st.secrets.get("SUPABASE_URL", ""))
        key = os.getenv("SUPABASE_SERVICE_ROLE", st.secrets.get("SUPABASE_SERVICE_ROLE", ""))
        if not url or not key:
            return None
        return create_client(url, key)  # type: ignore
    except Exception:
        return None

SUPABASE = _get_supabase()

# ---------- High-contrast chip styles (Option D) ----------
# Note: we only inject once per session
if "__tours_css__" not in st.session_state:
    st.markdown(
        """
        <style>
        /* Date/Time chip – high-contrast, subtle gradient, readable in both themes */
        .date-chip {
          display:inline-flex; align-items:center; gap:6px;
          padding:2px 10px; border-radius:999px; font-weight:800; letter-spacing:.2px;
          font-size:12.5px; line-height:1; border:1px solid rgba(0,0,0,.18);
          color:#0b1220; background:linear-gradient(180deg,#f9fafb 0%, #e5e7eb 100%);
          box-shadow:0 2px 8px rgba(0,0,0,.10), inset 0 1px 0 rgba(255,255,255,.66);
        }
        html[data-theme="dark"] .date-chip, .stApp [data-theme="dark"] .date-chip {
          color:#f8fafc; border-color:rgba(255,255,255,.18);
          background:linear-gradient(180deg,#1f2937 0%, #111827 100%);
          box-shadow:0 2px 10px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.06);
        }
        .date-chip .dot {
          width:8px; height:8px; border-radius:999px; background:#2563eb;
          box-shadow:0 0 0 2px rgba(37,99,235,.15);
        }
        html[data-theme="dark"] .date-chip .dot { background:#60a5fa; box-shadow:0 0 0 2px rgba(96,165,250,.20); }

        /* Tight list like Run tab */
        ul.tour-list { margin:6px 0 0.3rem 1.2rem; padding:0; list-style:disc; }
        ul.tour-list li { margin:0.25rem 0; }
        .results-wrap { position:relative; box-sizing:border-box; padding:8px 120px 4px 0; }
        .copyall-btn { position:absolute; top:0; right:8px; z-index:5; padding:6px 10px; height:26px;
                       border:0; border-radius:10px; color:#fff; font-weight:700; background:#1d4ed8;
                       cursor:pointer; opacity:.95; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.session_state["__tours_css__"] = True


# ---------- Small helpers ----------
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")

def _zillow_deeplink_from_address(addr: str) -> str:
    # Good SMS unfurls. Works even without exact homedetails.
    slug = _slug(re.sub(r"[,\s]+", " ", (addr or "").strip()))
    return f"https://www.zillow.com/homes/{slug}_rb/"

def _extract_text_from_pdf_bytes(data: bytes) -> str:
    # Try pdfminer first (keeps order), fallback to PyPDF2, else "".
    if pdfminer_extract_text:
        try:
            return pdfminer_extract_text(io.BytesIO(data)) or ""
        except Exception:
            pass
    if PyPDF2:
        try:
            r = PyPDF2.PdfReader(io.BytesIO(data))
            parts = []
            for p in r.pages:
                try:
                    parts.append(p.extract_text() or "")
                except Exception:
                    continue
            return "\n".join(parts)
        except Exception:
            pass
    return ""

def _get_url_text(url: str) -> str:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=15)
        if not r.ok:
            return ""
        html = r.text
        # Strip tags into text; keep spacing
        text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
        text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html_unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\s*\n+\s*", "\n", text)
        return text
    except Exception:
        return ""

def _clean_lines(text: str) -> List[str]:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    lines = [ln.strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln]

# Address patterns (resilient to commas/newlines)
ADDR_PATTERNS = [
    # 123 Main St, City, ST 12345
    re.compile(r"\b\d{1,6}\s+[A-Za-z0-9.#/\- ]+?,\s*[A-Za-z .'-]+,\s*[A-Z]{2}\s*\d{5}(?:-\d{4})?\b"),
    # 123 Main St City, ST 12345  (missing comma after street)
    re.compile(r"\b\d{1,6}\s+[A-Za-z0-9.#/\- ]+?\s+[A-Za-z .'-]+,\s*[A-Z]{2}\s*\d{5}(?:-\d{4})?\b"),
]

TIME_RANGE_RE = re.compile(
    r"\b(\d{1,2}:\d{2}\s?(?:AM|PM))\s*(?:–|-|to|–|—)\s*(\d{1,2}:\d{2}\s?(?:AM|PM))\b",
    re.I,
)

DATE_CANDIDATE_RE = re.compile(
    r"\b(?:Tour\s*Date|Date)\s*[:\-]?\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})\b|"
    r"\b([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})\b"
)

def _find_first_date(text: str) -> Optional[str]:
    m = DATE_CANDIDATE_RE.search(text)
    if not m:
        return None
    ds = m.group(1) or m.group(2)
    try:
        dt = datetime.strptime(ds, "%B %d, %Y")
        return dt.strftime("%a, %b %d, %Y")
    except Exception:
        return ds.strip()

def _nearest_address(lines: List[str], i: int) -> Optional[str]:
    # Check current, next, prev lines; then merge with neighbor and test again
    candidates: List[str] = []
    for j in (i, i + 1, i - 1):
        if 0 <= j < len(lines):
            candidates.append(lines[j])
    for text in candidates:
        for rx in ADDR_PATTERNS:
            m = rx.search(text)
            if m:
                return m.group(0).strip()
    # merge heuristics
    merges = []
    if 0 <= i < len(lines) - 1:
        merges.append(lines[i] + " " + lines[i + 1])
    if i > 0:
        merges.append(lines[i - 1] + " " + lines[i])
    for text in merges:
        for rx in ADDR_PATTERNS:
            m = rx.search(text)
            if m:
                return m.group(0).strip()
    return None

def _parse_showingtime_text(text: str) -> Tuple[Optional[str], List[Dict[str, str]]]:
    """
    Return (tour_date_str, stops[])
    stops: {address, start, end, time_text, zillow_url}
    """
    if not text:
        return None, []
    date_str = _find_first_date(text)
    lines = _clean_lines(text)
    stops: List[Dict[str, str]] = []

    # Strategy: for each line with a time-range, associate the nearest address line.
    for i, ln in enumerate(lines):
        tm = TIME_RANGE_RE.search(ln)
        if not tm:
            continue
        start, end = tm.group(1).upper().replace(" ", ""), tm.group(2).upper().replace(" ", "")
        addr = _nearest_address(lines, i)
        if not addr:
            # try a little wider search window
            for j in range(max(0, i - 3), min(len(lines), i + 4)):
                addr = _nearest_address(lines, j)
                if addr:
                    break
        if not addr:
            # if still no address, skip this time range
            continue

        z = _zillow_deeplink_from_address(addr)
        stops.append({
            "address": addr,
            "start": start,
            "end": end,
            "time_text": f"{start}–{end}",
            "zillow_url": z,
        })

    # Deduplicate by (address, time_text)
    uniq = {}
    for s in stops:
        key = (s["address"], s["time_text"])
        if key not in uniq:
            uniq[key] = s
    return date_str, list(uniq.values())

# ---------- Supabase helpers (optional) ----------
@st.cache_data(ttl=60, show_spinner=False)
def _fetch_clients_for_tours(include_inactive: bool = False) -> List[Dict[str, Any]]:
    if not SUPABASE:
        return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _log_tour_stops(client_tag: str, tour_date: Optional[str], stops: List[Dict[str, str]]) -> Tuple[bool, str]:
    """
    Inserts tour stops into 'tours' table:
    columns: client (text), tour_date (text), address (text), zillow_url (text), time_start (text), time_end (text), created_at (timestamp default)
    If the table doesn't exist, this will fail gracefully.
    """
    if not SUPABASE or not client_tag.strip() or not stops:
        return False, "Supabase not configured or no data."
    rows = []
    for s in stops:
        rows.append({
            "client": client_tag.strip(),
            "tour_date": tour_date or "",
            "address": s.get("address", ""),
            "zillow_url": s.get("zillow_url", ""),
            "time_start": s.get("start", ""),
            "time_end": s.get("end", ""),
        })
    try:
        SUPABASE.table("tours").insert(rows).execute()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ---------- Results list with copy (like Run tab) ----------
def _results_list_with_copy(stops: List[Dict[str, str]], tour_date: Optional[str]):
    li_html = []
    for s in stops:
        href = s.get("zillow_url") or ""
        if not href:
            continue
        addr = s.get("address") or href
        time_txt = s.get("time_text") or ""
        chip = (
            f"<span class='date-chip'><span class='dot'></span>"
            f"{escape(tour_date) + ' • ' if tour_date else ''}{escape(time_txt)}</span>"
        )
        li_html.append(
            f"<li><a href='{escape(href)}' target='_blank' rel='noopener'>{escape(addr)}</a> {chip}</li>"
        )

    items_html = "\n".join(li_html) if li_html else "<li>(no stops)</li>"
    copy_lines = [s.get("zillow_url","").strip() for s in stops if s.get("zillow_url")]
    copy_text = "\\n".join(copy_lines) + ("\\n" if copy_lines else "")

    html = f"""
    <html><head><meta charset="utf-8" /></head>
    <body>
      <div class="results-wrap">
        <button id="copyAll" class="copyall-btn" title="Copy clean URLs" aria-label="Copy clean URLs">Copy</button>
        <ul class="tour-list" id="tourList">{items_html}</ul>
      </div>
      <script>
        (function(){{
          const btn=document.getElementById('copyAll');
          const text = "{copy_text}".replaceAll("\\n", "\\n");
          btn.addEventListener('click', async () => {{
            try {{
              await navigator.clipboard.writeText(text);
              const prev=btn.textContent; btn.textContent='✓'; setTimeout(()=>{{ btn.textContent=prev; }}, 900);
            }} catch(e) {{
              const prev=btn.textContent; btn.textContent='×'; setTimeout(()=>{{ btn.textContent=prev; }}, 900);
            }}
          }});
        }})();
      </script>
    </body></html>
    """
    est_h = max(60, min(34 * max(1, len(li_html)) + 20, 700))
    components.html(html, height=est_h, scrolling=False)


# ---------- Public entry point ----------
def render_tours_tab(state: Optional[dict] = None):
    st.subheader("Tours")

    col1, col2 = st.columns([1.4, 1])
    with col1:
        url = st.text_input(
            "ShowingTime URL (Print or Public link)",
            placeholder="https://scheduling.showingtime.com/.../Tour/Print/30235965",
            key="__tour_url__",
        )
    with col2:
        pdf = st.file_uploader("Or drop a Tour PDF", type=["pdf"], key="__tour_pdf__")

    cA, cB = st.columns([1, 1])
    with cA:
        parse_btn = st.button("Parse tour", use_container_width=True)
    with cB:
        reset_btn = st.button("Reset", use_container_width=True)

    if reset_btn:
        st.session_state.pop("__tour_results__", None)
        st.experimental_rerun()

    # On parse
    if parse_btn:
        text = ""
        src = ""
        if url.strip():
            text = _get_url_text(url.strip())
            src = "url"
        elif pdf is not None:
            text = _extract_text_from_pdf_bytes(pdf.getvalue())
            src = "pdf"

        if not text:
            st.error("Could not parse this tour. Please ensure it is the ShowingTime Print page or the exported Tour PDF.")
            return

        tour_date, stops = _parse_showingtime_text(text)
        if not stops:
            st.error("No stops found. Double-check that this is a ShowingTime tour PDF/print page.")
            with st.expander("Debug (first 1200 chars)"):
                st.code((text[:1200] + "…") if len(text) > 1200 else text)
            return

        st.session_state["__tour_results__"] = {
            "date": tour_date,
            "stops": stops,
            "source": src,
        }

    data = st.session_state.get("__tour_results__")
    if not data:
        st.info("Paste a ShowingTime **Print** URL or **upload a PDF**, then click **Parse tour**.")
        return

    tour_date = data.get("date")
    stops = data.get("stops") or []

    # Results header
    st.markdown("#### Stops")
    _results_list_with_copy(stops, tour_date)

    # Save to client (optional)
    clients = _fetch_clients_for_tours(include_inactive=False)
    names = [c["name"] for c in clients]
    idx = st.selectbox("Add all stops to client (optional)", [-1] + list(range(len(names))), format_func=lambda i: ("— Select —" if i == -1 else names[i]), index=-1)
    if idx != -1:
        cli = clients[idx]
        client_tag = _norm_tag(cli.get("name", ""))
        if st.button(f"Save {len(stops)} stop(s) to client “{cli.get('name','')}”", use_container_width=True):
            ok, msg = _log_tour_stops(client_tag, tour_date, stops)
            if ok:
                st.success("Saved tour stops.")
            else:
                st.warning(f"Could not save: {msg}")

    # Export CSV of stops
    with st.expander("Export stops (CSV)"):
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["address", "zillow_url", "time_start", "time_end", "date"])
        w.writeheader()
        for r in stops:
            w.writerow({
                "address": r.get("address",""),
                "zillow_url": r.get("zillow_url",""),
                "time_start": r.get("start",""),
                "time_end": r.get("end",""),
                "date": tour_date or "",
            })
        st.download_button("Download CSV", s.getvalue(), file_name=f"tour_{(tour_date or 'unknown').replace(',','').replace(' ','_')}.csv", mime="text/csv", use_container_width=True)
