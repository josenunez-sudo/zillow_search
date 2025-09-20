# ui/tours_tab.py
import io, re, html, json
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

import streamlit as st

# ---------- Inputs ----------
def _is_pdf(name: str) -> bool:
    return name.lower().endswith(".pdf")

# ---------- Simple text helpers ----------
TOUR_DATE_RE = re.compile(
    r"(?:(Mon|Tue|Tues|Wed|Thu|Thur|Fri|Sat|Sun)[a-z]*,?\\s+)?"
    r"([A-Z][a-z]+)\\s+(\\d{1,2}),\\s+(\\d{4})",
    re.I
)

def extract_tour_date(text: str) -> Optional[str]:
    m = TOUR_DATE_RE.search(text or "")
    if m:
        weekday = (m.group(1) or "").title()
        month   = m.group(2)
        day     = m.group(3)
        year    = m.group(4)
        return (f"{weekday}, {month} {day}, {year}" if weekday else f"{month} {day}, {year}")
    return None

TIME_RE = re.compile(r"\\b(\\d{1,2}:\\d{2}\\s*(?:AM|PM))\\b", re.I)
ADDRESS_LINE_RE = re.compile(r"\\d+\\s+[^\\n]+\\b(?:Ave|Av|Avenue|Blvd|Boulevard|Cir|Circle|Ct|Court|Dr|Drive|Hwy|Highway|Ln|Lane|Pkwy|Parkway|Pl|Place|Rd|Road|St|Street|Ter|Terrace)\\b[^\\n]*", re.I)

def extract_text_from_pdf(upload) -> str:
    try:
        import pdfminer.high_level
        return pdfminer.high_level.extract_text(io.BytesIO(upload.getvalue()))
    except Exception:
        # Fallback to PyPDF2 if available
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(upload.getvalue()))
            return "\\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return ""

def parse_showingtime_print_html(html_text: str) -> List[Dict[str, str]]:
    """
    Heuristics for ShowingTime 'Print Tour' page HTML pasted into a text box via requests (if you fetch it elsewhere).
    Here we just parse text blob; you can wire a fetch if you want.
    """
    text = re.sub(r"<[^>]+>", " ", html_text or "", flags=re.I)
    text = re.sub(r"\\s+", " ", text).strip()

    # Times paired as Start/End around the same line
    times = TIME_RE.findall(text)
    # Addresses
    addresses = ADDRESS_LINE_RE.findall(text)

    # Align by order if counts are close; otherwise best-effort
    stops = []
    n = max(len(addresses), len(times)//2)
    for i in range(n):
        addr = addresses[i] if i < len(addresses) else ""
        start = times[2*i] if 2*i < len(times) else ""
        end   = times[2*i+1] if 2*i+1 < len(times) else ""
        if addr and (start or end):
            stops.append({"address_line": addr.strip(), "start": start.strip(), "end": end.strip()})
    return stops

def parse_showingtime_pdf_text(text: str) -> List[Dict[str, str]]:
    """
    Parse the exported ShowingTime 'Tour Details' PDF text.
    Looks for lines with an address and nearby time range.
    """
    if not text:
        return []

    # Normalize spacing
    t = re.sub(r"[\\r\\t]+", " ", text)
    t = re.sub(r"\\s+\\n", "\\n", t)
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]

    stops: List[Dict[str, str]] = []
    pending_addr = None
    pending_start = None

    for ln in lines:
        # time?
        m_t = TIME_RE.search(ln)
        # address-ish?
        m_a = ADDRESS_LINE_RE.search(ln)

        if m_a:
            # if we had a pending address without times, flush it (best effort)
            if pending_addr and (pending_start is None):
                stops.append({"address_line": pending_addr, "start": "", "end": ""})
            pending_addr = m_a.group(0)
            pending_start = None
            continue

        if m_t:
            if pending_start is None:
                pending_start = m_t.group(1)
            else:
                # end time found — emit a stop if we have an address
                if pending_addr:
                    stops.append({"address_line": pending_addr, "start": pending_start, "end": m_t.group(1)})
                    pending_addr = None
                    pending_start = None

    # Flush tail if needed
    if pending_addr:
        stops.append({"address_line": pending_addr, "start": pending_start or "", "end": ""})

    # Basic de-dupe/order
    out = []
    seen = set()
    for s in stops:
        key = (s["address_line"], s["start"], s["end"])
        if key not in seen:
            out.append(s); seen.add(key)
    return out

# ---------- Renderer ----------
def _render_results(stops: List[Dict[str, str]]):
    if not stops:
        st.warning("No stops found. Double-check that this is a ShowingTime tour PDF/print page.")
        return

    # Build clean copy block (addresses + times)
    copy_lines = [f"{s['address_line']} — {s['start']} to {s['end']}".strip() for s in stops]
    copy_text = "\\n".join(copy_lines) + ("\\n" if copy_lines else "")

    # HTML list with TIME CHIPS (option D safe: chips use !important styles from core/styles.py)
    items_html = []
    for s in stops:
        start_chip = f"<span class='chip chip-time'>{html.escape(s['start'])}</span>" if s.get("start") else ""
        end_chip   = f"<span class='chip chip-time'>{html.escape(s['end'])}</span>"   if s.get("end") else ""
        sep = "&nbsp;to&nbsp;" if (start_chip and end_chip) else ""
        label = f"{html.escape(s['address_line'])} — {start_chip}{sep}{end_chip}"
        items_html.append(f"<li>{label}</li>")

    html_block = f"""
    <html><head><meta charset="utf-8" />
      <style>
        /* Keep styles scoped and neutral so they don't fight chips */
        .tour-results-wrap {{ position:relative; box-sizing:border-box; padding:8px 120px 4px 0; }}
        .tour-results-wrap ul.link-list {{ margin:0 0 0.2rem 1.2rem; padding:0; list-style:disc; }}
        .tour-results-wrap ul.link-list li {{ margin:0.25rem 0; }}
        .copyall-btn {{ position:absolute; top:0; right:8px; z-index:5; padding:6px 10px; height:26px; border:0; border-radius:10px; color:#fff; font-weight:700; background:#1d4ed8; cursor:pointer; opacity:.95; }}
        /* DO NOT style span globally here — chips are styled globally in core/styles.py with !important */
      </style>
    </head><body>
      <div class="tour-results-wrap">
        <button id="copyAll" class="copyall-btn" title="Copy all stops" aria-label="Copy all stops">Copy</button>
        <ul class="link-list" id="tourList">{''.join(items_html)}</ul>
      </div>
      <script>
        (function(){{
          const btn=document.getElementById('copyAll');
          const text = {json.dumps(copy_text)};
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

    # Height heuristic
    est_h = max(60, min(34 * max(1, len(stops)) + 20, 700))
    st.components.v1.html(html_block, height=est_h, scrolling=False)

def render_tours_tab():
    st.subheader("Tours")
    st.caption("Paste a ShowingTime Print page URL or upload the exported Tour PDF.")

    colL, colR = st.columns([1.4, 1])
    with colL:
        url = st.text_input("ShowingTime link (Print page URL)", placeholder="https://scheduling.showingtime.com/(S...)/Tour/Print/30235965")
        parse_btn = st.button("Parse link", use_container_width=True)
    with colR:
        up = st.file_uploader("Upload PDF", type=["pdf"])

    # Optional preview date chip (from URL fetch or PDF text)
    tour_date: Optional[str] = None
    stops: List[Dict[str, str]] = []

    if parse_btn and url.strip():
        # If you want true HTML fetching, add your own requests.get(url) and pass the body here.
        # For now we treat it as not directly fetchable (some pages need cookies).
        st.info("Direct fetching of ShowingTime pages often requires authentication. Upload the Tour PDF for best results.")
        # You can still let a user paste the HTML source if you want:
        pasted_html = st.text_area("Paste the HTML source of the Print page (optional)", height=180)
        if pasted_html.strip():
            tour_date = extract_tour_date(pasted_html)
            stops = parse_showingtime_print_html(pasted_html)
            if tour_date:
                st.markdown(f"<div style='margin:6px 0 4px 0;'><span class='chip chip-date'>{html.escape(tour_date)}</span></div>", unsafe_allow_html=True)
            _render_results(stops)
        else:
            st.stop()

    if up and _is_pdf(up.name):
        text = extract_text_from_pdf(up)
        tour_date = extract_tour_date(text)
        stops = parse_showingtime_pdf_text(text)
        if tour_date:
            st.markdown(f"<div style='margin:6px 0 4px 0;'><span class='chip chip-date'>{html.escape(tour_date)}</span></div>", unsafe_allow_html=True)
        _render_results(stops)
