import io
import os
import re
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

import streamlit as st

# Try preferred PDF text extractor first; fall back if unavailable
try:
    import pdfplumber  # best for extracting text as laid out
except Exception:
    pdfplumber = None

try:
    from PyPDF2 import PdfReader  # fallback
except Exception:
    PdfReader = None

# ---- bring in your resolver primitives so we can turn addresses into Zillow links
# These should already exist in your project (as they do in run_tab).
from services.resolver import (
    process_single_row,
    upgrade_to_homedetails_if_needed,
    make_preview_url,
)
# If you want Bitly tracking on tours too, import your helper:
# from services.resolver import bitly_shorten, make_trackable_url

# ---- Minimal Supabase glue (lightweight copy to avoid circular imports)
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

def _norm_tag(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

@st.cache_data(ttl=60, show_spinner=False)
def _fetch_clients(include_inactive: bool = False) -> List[Dict[str, Any]]:
    if not SUPABASE:
        return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

# Lightweight "sent" logger (re-using your schema)
def _canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    import re
    if not url:
        return "", None
    base = re.sub(r'[#?].*$', '', url)
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = re.search(r'(\d{6,})_zpid', url, re.I)
    return canon, (m_z.group(1) if m_z else None)

def _log_sent_rows(results: List[Dict[str, Any]], client_norm: str, campaign: str) -> Tuple[bool, str]:
    if not SUPABASE or not results:
        return False, "Supabase not configured or no results."
    rows = []
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    for r in results:
        raw_url = (r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "").strip()
        if not raw_url:
            # still store addresses with no URL (rare), so they appear in report
            raw_url = ""
        canon, zpid = _canonicalize_zillow(raw_url)
        rows.append({
            "client":     (client_norm or "").strip(),
            "campaign":   (campaign or "").strip(),
            "url":        raw_url or None,
            "canonical":  canon or None,
            "zpid":       zpid or None,
            "mls_id":     None,  # unknown here
            "address":    (r.get("input_address") or "").strip() or None,
            "sent_at":    now_iso,
        })
    try:
        SUPABASE.table("sent").insert(rows).execute()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# -------------------- ShowingTime parsing --------------------

BUYER_LINE = re.compile(r"Buyer'?s Tour\s*-\s*(?P<dow>[A-Za-z]+),\s*(?P<month>[A-Za-z]+)\s*(?P<day>\d{1,2}),\s*(?P<year>\d{4})", re.I)
# Example stop line in PDF print:
# "1 14 Atlantic Avenue, Benson, NC 27504 9:00 AM - 9:30 AM"
STOP_LINE = re.compile(
    r"^\s*\d+\s+"
    r"(?P<street>[^,]+),\s*(?P<city>[A-Za-z .'\-]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s+"
    r"(?P<start>[0-9: ]+[AP]M)\s*-\s*(?P<end>[0-9: ]+[AP]M)\s*$",
    re.I
)

def _pdf_to_text(pdf_bytes: bytes) -> str:
    # First try pdfplumber for best text layout fidelity
    if pdfplumber:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    # Fall back to PyPDF2 if needed
    if PdfReader:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts = []
        for p in reader.pages:
            try:
                parts.append(p.extract_text() or "")
            except Exception:
                parts.append("")
        return "\n".join(parts)
    # Last resort: nothing available
    return ""

def _html_to_text(html: str) -> str:
    # Strip tags and collapse whitespace — print pages are simple enough
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text

def parse_showingtime_text(raw_text: str) -> Dict[str, Any]:
    """
    Parse text from ShowingTime PDF or Print HTML (after stripping tags).
    Returns: {'buyer': str|None, 'date': 'YYYY-MM-DD'|None, 'stops':[{'address','start','end'}...]}
    """
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    buyer_name = None
    tour_date_iso = None

    # Find the tour header & buyer
    for i, ln in enumerate(lines[:60]):  # header early
        m = BUYER_LINE.search(ln)
        if m:
            # Date
            try:
                d = datetime.strptime(f"{m.group('month')} {m.group('day')} {m.group('year')}", "%B %d %Y")
                tour_date_iso = d.strftime("%Y-%m-%d")
            except Exception:
                tour_date_iso = None
            # Buyer likely on same or next few lines (as in your PDF)
            # We'll scan a few following lines for a name-looking line (e.g., "Keelie Mason")
            for j in range(i+1, min(i+6, len(lines))):
                if re.search(r"Buyer'?s name\s*:\s*", lines[j], re.I):
                    # explicit label
                    buyer_name = re.sub(r"(?i)^.*Buyer'?s name\s*:\s*", "", lines[j]).strip()
                    break
                # Heuristic: a short "Firstname Lastname" line
                if re.match(r"^[A-Za-z][A-Za-z .'\-]+ [A-Za-z .'\-]+$", lines[j]):
                    buyer_name = lines[j].strip()
                    break
            break

    # Collect stops
    stops: List[Dict[str, str]] = []
    for ln in lines:
        m = STOP_LINE.match(ln)
        if m:
            addr = f"{m.group('street').strip()}, {m.group('city').strip()}, {m.group('state').strip()} {m.group('zip').strip()}"
            stops.append({
                "address": addr,
                "start": m.group("start").strip(),
                "end": m.group("end").strip(),
            })

    return {"buyer": buyer_name, "date": tour_date_iso, "stops": stops}

def parse_showingtime_pdf(pdf_bytes: bytes) -> Dict[str, Any]:
    text = _pdf_to_text(pdf_bytes)
    return parse_showingtime_text(text)

def parse_showingtime_print_url(url: str, timeout: int = 12) -> Dict[str, Any]:
    import requests
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    text = _html_to_text(r.text)
    return parse_showingtime_text(text)

# -------------------- UI --------------------

def _results_list_hyperlinks(results: List[Dict[str, Any]], client_selected: bool):
    from html import escape
    import streamlit.components.v1 as components

    li = []
    for r in results:
        href = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        if not href:
            continue
        safe = escape(href)
        txt = href  # URL as anchor text for best SMS unfurls
        badge = ""
        if client_selected and r.get("already_sent"):
            badge = f' <span class="badge dup" title="Duplicate">Duplicate</span>'
        li.append(f'<li style="margin:0.2rem 0;"><a href="{safe}" target="_blank" rel="noopener">{escape(txt)}</a>{badge}</li>')
    items_html = "\n".join(li) if li else "<li>(no results)</li>"
    copy_text = "\\n".join([r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "" for r in results if (r.get("preview_url") or r.get("zillow_url") or r.get("display_url"))])

    html = f"""
    <html><head><meta charset="utf-8" />
      <style>
        html,body {{ margin:0; font-family:-apple-system, Segoe UI, Roboto, Arial, sans-serif; }}
        .results-wrap {{ position:relative; box-sizing:border-box; padding:8px 120px 4px 0; }}
        ul.link-list {{ margin:0 0 0.2rem 1.2rem; padding:0; list-style:disc; }}
        ul.link-list li {{ margin:0.2rem 0; }}
        .copyall-btn {{ position:absolute; top:0; right:8px; z-index:5; padding:6px 10px; height:26px; border:0; border-radius:10px; color:#fff; font-weight:700; background:#1d4ed8; cursor:pointer; opacity:.95; }}
      </style>
    </head><body>
      <div class="results-wrap">
        <button id="copyAll" class="copyall-btn" title="Copy clean URLs" aria-label="Copy clean URLs">Copy</button>
        <ul class="link-list" id="resultsList">{items_html}</ul>
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
    </body></html>"""
    est_h = max(60, min(34 * max(1, len(li)) + 20, 700))
    components.html(html, height=est_h, scrolling=False)

def render_tours_tab():
    st.subheader("Tours (ShowingTime)")
    st.caption("Upload the ShowingTime **PDF** (recommended) or paste a **Print View** URL. We’ll extract addresses + times, optionally resolve to Zillow, and log under a client.")

    # Inputs
    colA, colB = st.columns([1.2, 1])
    with colA:
        pdf_file = st.file_uploader("Upload ShowingTime Tour PDF", type=["pdf"])
    with colB:
        print_url = st.text_input("Or paste ShowingTime Print URL (optional)")

    # Choose client & options
    active_clients = _fetch_clients(include_inactive=False)
    names = [c["name"] for c in active_clients]
    opts = ["➤ No client (no logging)"] + names
    sel_idx = st.selectbox("Client", list(range(len(opts))), format_func=lambda i: opts[i], index=0)
    selected_client = None if sel_idx == 0 else active_clients[sel_idx-1]
    client_norm = _norm_tag(selected_client["name"]) if selected_client else ""

    resolve_to_zillow = st.checkbox("Resolve each address to Zillow URLs", value=True)
    run_btn = st.button("Parse Tour", use_container_width=True)

    tour_data: Dict[str, Any] = {}
    if run_btn:
        try:
            if pdf_file is not None:
                tour_data = parse_showingtime_pdf(pdf_file.read())
            elif print_url.strip():
                tour_data = parse_showingtime_print_url(print_url.strip())
            else:
                st.error("Please upload a PDF or paste a Print URL.")
                st.stop()
        except Exception as e:
            st.error("Unable to parse this tour input.")
            with st.expander("Details"):
                st.exception(e)
            st.stop()

        stops = tour_data.get("stops", [])
        if not stops:
            st.warning("No stops found. Double-check that this is a ShowingTime tour PDF/print page.")
            st.stop()

        buyer = tour_data.get("buyer") or "buyer"
        date_iso = tour_data.get("date")  # 'YYYY-MM-DD' or None
        date_tag = date_iso.replace("-", "") if date_iso else datetime.utcnow().strftime("%Y%m%d")
        campaign = f"tour_{_norm_tag(buyer).replace(' ','-')}_{date_tag}"

        st.markdown(f"**Buyer:** {buyer}  •  **Date:** {date_iso or 'unknown'}  •  **Stops:** {len(stops)}")

        # Show extracted stops
        import pandas as pd
        st.dataframe(pd.DataFrame(stops), use_container_width=True, hide_index=True)

        # Optionally resolve to Zillow + log under client
        results: List[Dict[str, Any]] = []
        if resolve_to_zillow:
            st.write("Resolving addresses to Zillow…")
            prog = st.progress(0.0)
            for i, stop in enumerate(stops, start=1):
                # Feed each address through your existing resolver
                res = process_single_row(
                    {"address": stop["address"]},
                    delay=0.35, land_mode=True, defaults={"city":"", "state":"", "zip":""},
                    require_state=False, mls_first=True, default_mls_name="", max_candidates=20
                )
                # Normalize/upgrade + preview
                for key in ("zillow_url", "display_url"):
                    if res.get(key):
                        res[key] = upgrade_to_homedetails_if_needed(res[key])
                base = res.get("zillow_url")
                res["preview_url"] = make_preview_url(base) if base else ""
                # keep the original address as anchor text fallback
                res["input_address"] = stop["address"]
                results.append(res)
                prog.progress(i/len(stops))
            prog.progress(1.0)

            st.markdown("#### Results")
            _results_list_hyperlinks(results, client_selected=bool(client_norm))

            if client_norm:
                ok, msg = _log_sent_rows(results, client_norm=client_norm, campaign=campaign)
                if ok:
                    st.success(f"Logged {len(results)} tour listing(s) to Supabase as campaign “{campaign}”.")
                else:
                    st.warning(f"Logging skipped/failed: {msg}")
        else:
            st.info("Resolution to Zillow was disabled. Nothing was logged.")
