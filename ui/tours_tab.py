# ui/tours_tab.py
# Tours tab: import (Print URL or PDF), preview, add to client, and manage tours.
import os, re, io, requests
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape
import html as ihtml  # for unescape

import streamlit as st

# Optional PDF parsing
try:
    import PyPDF2  # type: ignore
except Exception:
    PyPDF2 = None

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

def _sb_ok() -> bool:
    try: return bool(SUPABASE)
    except NameError: return False

REQUEST_TIMEOUT = 12
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

# ---------- Small helpers ----------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def slugify_address(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', (s or '').lower()).strip('-')

def zillow_deeplink_from_address(addr: str) -> str:
    a = (addr or "").strip().lower()
    a = re.sub(r"[^\w\s,-]", "", a).replace(",", "")
    a = re.sub(r"\s+", "-", a.strip())
    return f"https://www.zillow.com/homes/{a}_rb/"

# ---------- Clients ----------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = True):
    if not _sb_ok(): return []
    try:
        cols = "id,name,name_norm,active"
        rows = SUPABASE.table("clients").select(cols).order("name", desc=False).execute().data or []
        # hide the test sentinel (same behavior as Run tab)
        rows = [r for r in rows if (r.get("name_norm") or "").strip().lower() != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

# ---------- Tours storage ----------
def _invalidate_tours_cache():
    try:
        fetch_tours_for_client.clear()  # type: ignore[attr-defined]
        fetch_stops_for_tour.clear()    # type: ignore[attr-defined]
    except Exception:
        pass

def create_tour(client_norm: str, client_display: str, tour_date, source_url: Optional[str] = None):
    """
    Creates a row in public.tours. Handles:
    - older schemas (no client_display)
    - stricter schemas (url NOT NULL) → falls back to empty string if needed.
    """
    if not _sb_ok():
        return False, "Supabase not configured"

    client_norm = (client_norm or "").strip()
    client_display = (client_display or "").strip()
    url_val = (source_url or "").strip()  # empty string if None

    # Try newest schema first
    try:
        payload = {"client": client_norm, "client_display": client_display, "tour_date": str(tour_date), "url": url_val}
        resp = SUPABASE.table("tours").insert(payload).execute()
        _invalidate_tours_cache()
        return True, (resp.data[0]["id"] if resp.data else None)
    except Exception as e1:
        # Try without client_display (older schema) but keep url
        try:
            payload = {"client": client_norm, "tour_date": str(tour_date), "url": url_val}
            resp = SUPABASE.table("tours").insert(payload).execute()
            _invalidate_tours_cache()
            return True, (resp.data[0]["id"] if resp.data else None)
        except Exception as e2:
            # Final fallback: omit url only if DB doesn’t require it
            try:
                payload = {"client": client_norm, "tour_date": str(tour_date)}
                resp = SUPABASE.table("tours").insert(payload).execute()
                _invalidate_tours_cache()
                return True, (resp.data[0]["id"] if resp.data else None)
            except Exception as e3:
                return False, f"{e1} | {e2} | {e3}"

def insert_tour_stops(tour_id: int, stops: List[Dict[str, str]]):
    if not (_sb_ok() and tour_id and stops):
        return False, "Bad input or not configured"
    try:
        payload = []
        for s in stops:
            payload.append({
                "tour_id": tour_id,
                "address": s.get("address",""),
                "address_slug": s.get("address_slug",""),
                "start": s.get("start",""),
                "end": s.get("end",""),
                "deeplink": s.get("deeplink",""),
            })
        SUPABASE.table("tour_stops").insert(payload).execute()
        _invalidate_tours_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

@st.cache_data(ttl=120, show_spinner=False)
def fetch_tours_for_client(client_norm: Optional[str], limit: int = 100):
    if not _sb_ok():
        return []
    try:
        q = SUPABASE.table("tours").select("id,client,client_display,tour_date,url,created_at").order("tour_date", desc=True)
        if client_norm and client_norm.strip().lower() != "__all__":
            q = q.eq("client", client_norm.strip())
        return q.limit(limit).execute().data or []
    except Exception:
        return []

@st.cache_data(ttl=120, show_spinner=False)
def fetch_stops_for_tour(tour_id: int):
    if not (_sb_ok() and tour_id):
        return []
    try:
        return SUPABASE.table("tour_stops").select("id,tour_id,address,address_slug,start,end,deeplink").eq("tour_id", tour_id).order("id", desc=False).execute().data or []
    except Exception:
        return []

def delete_tour(tour_id: int):
    if not (_sb_ok() and tour_id):
        return False, "Not configured or bad id"
    try:
        SUPABASE.table("tour_stops").delete().eq("tour_id", tour_id).execute()
        SUPABASE.table("tours").delete().eq("id", tour_id).execute()
        _invalidate_tours_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_stop(stop_id: int):
    if not (_sb_ok() and stop_id):
        return False, "Not configured or bad id"
    try:
        SUPABASE.table("tour_stops").delete().eq("id", stop_id).execute()
        _invalidate_tours_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ---------- Sent logging (to tag as TOURED later) ----------
def log_sent_for_stops(client_norm: str, stops: List[Dict[str,str]], tour_date: datetime.date):
    """
    Insert each stop as a row in 'sent' so clients/report views can
    detect them via address matching. Uses campaign="tour:YYYYMMDD".
    """
    if not (_sb_ok() and client_norm and stops):
        return False, "Not configured or no stops"
    try:
        rows = []
        now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        campaign = f"tour:{tour_date.strftime('%Y%m%d')}"
        for s in stops:
            rows.append({
                "client": client_norm,
                "campaign": campaign,
                "url": s.get("deeplink","") or None,
                "canonical": None,
                "zpid": None,
                "mls_id": None,
                "address": s.get("address","") or None,
                "sent_at": now_iso,
            })
        SUPABASE.table("sent").insert(rows).execute()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ---------- OLD (simpler) Parsing (Print URL / PDF) ----------
# Keep it close to the version that worked for you:
DASH = r"[-\u2013\u2014\u2212]"  # -, –, —, −

DATE_PAT = re.compile(
    r"Buy(?:er|ers?)'?s?\s+Tour\s*"+DASH+r"\s*[A-Za-z]+,\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})",
    re.I
)
DATE_FALLBACK = re.compile(r"([A-Za-z]+\s+\d{1,2},\s*\d{4})")

# Classic: small gap between address and time, then a slightly larger fallback.
STOP_PAT_A = re.compile(
    rf"(\d{{1,6}}\s+[A-Za-z0-9\.\-'/\s]{{3,120}},\s*[A-Za-z\.\-'\s]{{2,80}},\s*[A-Z]{{2}}\s*\d{{5}}(?:-\d{{4}})?)\s{{0,60}}(\d{{1,2}}:\d{{2}}\s*[AP]M)\s*{DASH}\s*(\d{{1,2}}:\d{{2}}\s*[AP]M)",
    re.I
)
STOP_PAT_B = re.compile(
    rf"(\d{{1,6}}\s+[A-Za-z0-9\.\-'/\s]{{3,120}},\s*[A-Za-z\.\-'\s]{{2,80}},\s*[A-Z]{{2}}\s*\d{{5}}(?:-\d{{4}})?)"
    rf".{{0,220}}?(\d{{1,2}}:\d{{2}}\s*[AP]M)\s*{DASH}\s*(\d{{1,2}}:\d{{2}}\s*[AP]M)",
    re.I | re.S
)

def _coerce_date(s: str):
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except Exception:
            pass
    return None

def _flatten_text(txt: str) -> str:
    if not txt: return ""
    t = txt.replace("\xa0", " ")
    # normalize dashes, then collapse spaces
    t = t.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    t = re.sub(r"\s+", " ", t).strip()
    return t

def _strip_tags(html_text: str) -> str:
    # Remove scripts/styles quickly then drop tags; unescape entities; flatten.
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_text)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = ihtml.unescape(s)
    return _flatten_text(s)

def _normalize_time(tok: str) -> str:
    tok = tok.upper().replace(" ", "")
    tok = re.sub(r"(AM|PM)$", r" \1", tok)
    return tok

def _extract_stops(flat_text: str) -> Tuple[Optional[datetime.date], Optional[str], List[Dict[str,str]]]:
    date_obj = None
    buyer_name = None
    stops: List[Dict[str,str]] = []

    dm = DATE_PAT.search(flat_text)
    if dm:
        date_obj = _coerce_date(dm.group(1))
    if not date_obj:
        dm2 = DATE_FALLBACK.search(flat_text)
        if dm2:
            date_obj = _coerce_date(dm2.group(1))

    # Optional: buyer name
    m_buyer = re.search(r"Buyer'?s?\s+name\s*:\s*([A-Za-z][A-Za-z\-\s']+)", flat_text, re.I)
    if m_buyer:
        buyer_name = m_buyer.group(1).strip()

    matches = list(STOP_PAT_A.finditer(flat_text))
    if not matches:
        matches = list(STOP_PAT_B.finditer(flat_text))

    for m in matches:
        addr = re.sub(r"\s+", " ", m.group(1)).strip()
        start = _normalize_time(m.group(2))
        end   = _normalize_time(m.group(3))
        # sanity check ", City, ST 12345"
        if not re.search(r",\s*[A-Za-z\.\-'\s]+,\s*[A-Z]{2}\s*\d{5}", addr):
            continue
        stops.append({
            "address": addr,
            "start": start,
            "end":   end,
            "address_slug": slugify_address(addr),
            "deeplink": zillow_deeplink_from_address(addr),
        })

    # de-dup keep order
    seen = set(); uniq=[]
    for s in stops:
        k = (s["address_slug"], s["start"], s["end"])
        if k in seen: continue
        seen.add(k); uniq.append(s)
    return date_obj, buyer_name, uniq

def parse_from_print_url(url: str) -> Tuple[Optional[datetime.date], Optional[str], List[Dict[str,str]], str]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            return None, None, [], "Could not fetch Print page."
        flat = _strip_tags(r.text)  # (this is what the old parser effectively did)
        d, b, s = _extract_stops(flat)
        if not s:
            return None, None, [], "No stops found. Double-check that this is a ShowingTime tour Print page."
        return d, b, s, ""
    except Exception as e:
        return None, None, [], f"Fetch/parse error: {e}"

def parse_from_pdf(file) -> Tuple[Optional[datetime.date], Optional[str], List[Dict[str,str]], str]:
    if not PyPDF2:
        return None, None, [], "PyPDF2 not installed. Add PyPDF2 to requirements.txt."
    try:
        content = file.read()
        pdf = PyPDF2.PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in pdf.pages]
        text = _flatten_text(" ".join(pages))
        d, b, s = _extract_stops(text)
        if not s:
            return None, None, [], "No stops found. Double-check that this is a ShowingTime tour PDF."
        return d, b, s, ""
    except Exception as e:
        return None, None, [], f"PDF parse error: {e}"

# ---------- Styles (time pills, contrast) ----------
STYLE = """
<style>
.tour-pill { display:inline-block; font-size:12px; font-weight:800; padding:2px 8px; border-radius:999px; margin-left:8px;
  background: linear-gradient(180deg, #fde68a 0%, #f59e0b 100%); color:#1f2937; border:1px solid rgba(217,119,6,.35);
  box-shadow: 0 2px 8px rgba(217,119,6,.25), 0 1px 2px rgba(0,0,0,.06);
}
html[data-theme="dark"] .tour-pill, .stApp [data-theme="dark"] .tour-pill {
  background: linear-gradient(180deg, #7c2d12 0%, #b45309 100%); color:#fde68a; border-color: rgba(245,158,11,.45);
  box-shadow: 0 2px 8px rgba(180,83,9,.45), 0 1px 2px rgba(0,0,0,.35);
}
.tour-card { border-bottom:1px solid rgba(0,0,0,.08); padding:6px 0 8px 0; }
.tour-actions { margin:4px 0 8px 0; }
</style>
"""

# ---------- Main renderer ----------
def render_tours_tab(state: dict):
    st.markdown(STYLE, unsafe_allow_html=True)

    # --- Import (at top) ---
    st.subheader("Import Tour")
    col_u, col_p = st.columns([1.2, 1])
    with col_u:
        print_url = st.text_input("ShowingTime Print URL", placeholder="https://scheduling.showingtime.com/.../Tour/Print/30235965")
    with col_p:
        pdf_file = st.file_uploader("or upload Tour PDF", type=["pdf"])

    parse_clicked = st.button("Parse tour", use_container_width=True)

    parsed_date: Optional[datetime.date] = None
    parsed_buyer: Optional[str] = None
    parsed_stops: List[Dict[str,str]] = []

    if parse_clicked:
        if print_url:
            d, b, s, err = parse_from_print_url(print_url.strip())
        elif pdf_file is not None:
            d, b, s, err = parse_from_pdf(pdf_file)
        else:
            d, b, s, err = None, None, [], "Provide a Print URL or a Tour PDF."

        if err:
            st.error(err)
        else:
            parsed_date, parsed_buyer, parsed_stops = d, b, s
            st.success(f"Parsed {len(parsed_stops)} stop(s)" + (f" • {parsed_buyer}" if parsed_buyer else "") + (f" • {parsed_date}" if parsed_date else ""))

            # Preview with include checkboxes
            st.markdown("#### Preview")
            include_flags = []
            for i, s in enumerate(parsed_stops):
                chk = st.checkbox("", value=True, key=f"__inc_{i}", help="Uncheck to exclude this stop before saving.")
                include_flags.append(chk)
                addr = s["address"]
                link = s["deeplink"]
                start_end = f'{s["start"]} – {s["end"]}'
                st.markdown(
                    f"""
                    <div class="tour-card">
                      <a href="{escape(link)}" target="_blank" rel="noopener">{escape(addr)}</a>
                      <span class="tour-pill">{escape(start_end)}</span>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

            # Choose client & add all (sentinel LAST in this tab)
            clients = fetch_clients(include_inactive=True)
            names = [c["name"] for c in clients]
            name_to_norm = {c["name"]: c.get("name_norm","") for c in clients}
            options = ["— Choose client —"] + names + ["➤ No client (show ALL, no logging)"]
            sel = st.selectbox("Add all included stops to client", options, index=0)

            add_clicked = st.button("Add all included stops", use_container_width=True)
            if add_clicked:
                if sel == "— Choose client —":
                    st.warning("Pick a client to save these stops.")
                    st.stop()
                if sel == "➤ No client (show ALL, no logging)":
                    st.info("Preview only: no client selected, nothing will be saved.")
                    st.stop()

                client_display = sel
                client_norm = name_to_norm.get(sel, _norm_tag(sel))
                tour_date = parsed_date or datetime.utcnow().date()

                final_stops = [s for s, inc in zip(parsed_stops, include_flags) if inc]
                if not final_stops:
                    st.warning("No stops selected.")
                    st.stop()

                # Record source (url or pdf:<name>)
                src_url = print_url.strip() if print_url else (f"pdf:{pdf_file.name}" if pdf_file else "")
                ok_t, tour_id_or_err = create_tour(client_norm=client_norm, client_display=client_display, tour_date=tour_date, source_url=src_url)
                if not ok_t:
                    st.error(f"Create tour failed: {tour_id_or_err}")
                    st.stop()
                tour_id = tour_id_or_err

                ok_s, msg_s = insert_tour_stops(tour_id=int(tour_id), stops=final_stops)
                if not ok_s:
                    st.error(f"Insert stops failed: {msg_s}")
                    st.stop()

                # Also log into 'sent' so Clients tab can tag as TOURED
                ok_l, msg_l = log_sent_for_stops(client_norm=client_norm, stops=final_stops, tour_date=tour_date)
                if not ok_l:
                    st.warning(f"Logged to 'sent' skipped/failed: {msg_l}")

                st.success(f"Saved {len(final_stops)} stop(s) to {client_display} for {tour_date}.")
                st.toast("Tour created.", icon="✅")

    st.markdown("---")

    # --- Manage Tours ---
    st.subheader("Manage Tours")

    clients_all = fetch_clients(include_inactive=True)
    names_all = [c["name"] for c in clients_all]
    name_to_norm_all = {c["name"]: c.get("name_norm","") for c in clients_all}

    mgmt_options = ["All clients"] + names_all + ["➤ No client (show ALL, no logging)"]
    sel_mgmt = st.selectbox("View tours for", mgmt_options, index=0)
    if sel_mgmt == "All clients" or sel_mgmt == "➤ No client (show ALL, no logging)":
        sel_norm = "__all__"
    else:
        sel_norm = name_to_norm_all.get(sel_mgmt, _norm_tag(sel_mgmt))

    tours = fetch_tours_for_client(sel_norm)
    if not tours:
        st.info("No tours found.")
        return

    for t in tours:
        t_id = t["id"]
        t_date = t.get("tour_date") or ""
        t_client = t.get("client_display") or t.get("client") or ""
        t_url = t.get("url") or ""
        stops = fetch_stops_for_tour(t_id)
        st.markdown(f"**{escape(t_client)}** — {escape(str(t_date))}  •  {len(stops)} stop(s)")
        if t_url:
            st.caption(f"Source: {t_url}")

        for s in stops:
            addr = s.get("address","")
            link = s.get("deeplink","")
            start_end = f'{s.get("start","")} – {s.get("end","")}'
            c1, c2 = st.columns([8, 1])
            with c1:
                st.markdown(
                    f"""<div class="tour-card">
                           <a href="{escape(link)}" target="_blank" rel="noopener">{escape(addr)}</a>
                           <span class="tour-pill">{escape(start_end)}</span>
                        </div>""",
                    unsafe_allow_html=True
                )
            with c2:
                if st.button("🗑️", key=f"del_stop_{s['id']}", help="Delete this stop"):
                    okd, msgd = delete_stop(s["id"])
                    if not okd:
                        st.warning(msgd)
                    else:
                        st.experimental_rerun()

        st.write("")
        if st.button("Delete tour", key=f"del_tour_{t_id}", help="Delete this tour and all its stops"):
            ok, msg = delete_tour(t_id)
            if not ok:
                st.error(msg)
            else:
                st.success("Tour deleted.")
                st.experimental_rerun()

        st.markdown("---")
