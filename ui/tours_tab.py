# ui/tours_tab.py
# Tours tab: paste a ShowingTime tour link OR drop a PDF → extract stop addresses + times.

import os, io, re, html
from typing import List, Dict, Any, Tuple, Optional

import requests
import streamlit as st
import streamlit.components.v1 as components

# ---------- Config ----------
REQUEST_TIMEOUT = 15
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------- Helpers ----------
def _clean_spaces_inside_words(s: str) -> str:
    """
    Fix ShowingTime PDF text quirk:
      - "W ashington" -> "Washington"
      - "V arina"     -> "Varina"
      - "W est"       -> "West"
    Strategy: remove a space when a single space is followed by a LOWERCASE letter.
    This keeps normal word boundaries (uppercase next) intact, e.g. "Longfellow Street".
    """
    return re.sub(r'([A-Za-z])\s(?=[a-z])', r'\1', s)

def _normalize_text_block(raw: str) -> str:
    # Collapse multiple spaces/newlines; fix inserted spaces in words.
    txt = raw.replace("\r", "\n")
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n+", "\n", txt)
    txt = _clean_spaces_inside_words(txt)
    return txt.strip()

def _strip_html_to_text(html_src: str) -> str:
    # Keep line breaks from <br> and block tags, then strip tags → text.
    s = html_src
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", s)
    s = re.sub(r"(?i)<(p|div|li|tr|h[1-6])[^>]*>", "", s)
    s = re.sub(r"(?is)<script[^>]*>.*?</script>", "", s)
    s = re.sub(r"(?is)<style[^>]*>.*?</style>", "", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return _normalize_text_block(s)

def _fetch_url(url: str) -> Tuple[str, int]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return (r.text if r.ok else ""), r.status_code
    except Exception:
        return "", 0

def _pdf_to_text(file_bytes: bytes) -> str:
    """
    Use PyPDF2 (pypdf) to extract text from the ShowingTime PDF.
    """
    try:
        import PyPDF2  # pypdf / PyPDF2 works the same here
    except Exception:
        # Graceful fallback message up top instead of crashing.
        st.warning("PyPDF2 not installed. Add `PyPDF2` to requirements.txt for PDF parsing.")
        return ""

    try:
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        txt = "\n\n".join((p.extract_text() or "") for p in reader.pages)
        return _normalize_text_block(txt)
    except Exception:
        return ""

# ---------- Core parser ----------
STOP_RE = re.compile(
    r"""(?mx)
    ^\s*
    (?P<street>[0-9A-Za-z #/.\-']+),\s+
    (?P<city>[A-Za-z .'\-]+),\s+
    (?P<state>[A-Z]{2})\s+
    (?P<zip>\d{5})
    \s+
    (?P<start>\d{1,2}:\d{2}\s?[AP]M)
    \s*-\s*
    (?P<end>\d{1,2}:\d{2}\s?[AP]M)
    """,
    re.IGNORECASE,
)

def parse_tour_text(normalized_text: str) -> List[Dict[str, str]]:
    """
    Parse the normalized text into stops.
    Returns: list of dicts with street, city, state, zip, start, end, address_line.
    """
    stops: List[Dict[str, str]] = []
    for m in STOP_RE.finditer(normalized_text):
        street = m.group("street").strip()
        city   = m.group("city").strip()
        state  = m.group("state").strip().upper()
        zipc   = m.group("zip").strip()
        start  = m.group("start").strip().upper().replace("  ", " ")
        end    = m.group("end").strip().upper().replace("  ", " ")

        # Final pass to fix any remaining split words in street/city
        street = _clean_spaces_inside_words(street)
        city   = _clean_spaces_inside_words(city)

        addr_line = f"{street}, {city}, {state} {zipc}"
        stops.append({
            "street": street,
            "city": city,
            "state": state,
            "zip": zipc,
            "start": start,
            "end": end,
            "address_line": addr_line,
        })
    return stops

def parse_tour_from_pdf(file_bytes: bytes) -> List[Dict[str, str]]:
    txt = _pdf_to_text(file_bytes)
    if not txt:
        return []
    return parse_tour_text(txt)

def parse_tour_from_url(url: str) -> List[Dict[str, str]]:
    html_src, status = _fetch_url(url)
    if not html_src:
        return []

    # Some showingti.me short-links redirect to a Print page. We parse whatever we get by
    # converting HTML → text and then applying the same regex.
    text_from_html = _strip_html_to_text(html_src)
    if not text_from_html:
        return []
    return parse_tour_text(text_from_html)

# ---------- UI rendering ----------
def _render_results(stops: List[Dict[str, str]]):
    st.markdown("#### Tour Stops")

    if not stops:
        st.error("Could not parse this tour. Please ensure it is the ShowingTime Print page or the exported Tour PDF.")
        with st.expander("Troubleshoot (show raw debug)"):
            st.info("Enable the **Debug** switch above and re-parse to view a text preview of what I’m seeing.")
        return

    # TIGHT list + Copy-all button (like Run tab)
    items_html = []
    copy_lines = []
    for s in stops:
        label = f"{s['address_line']} — {s['start']} to {s['end']}"
        items_html.append(f"<li>{html.escape(label)}</li>")
        copy_lines.append(label)
    items_html = "\n".join(items_html)
    copy_text = "\\n".join(copy_lines) + ("\\n" if copy_lines else "")

    html_blob = f"""
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
        <button id="copyAll" class="copyall-btn" title="Copy all stops" aria-label="Copy all stops">Copy</button>
        <ul class="link-list">{items_html}</ul>
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
    est_h = max(80, min(32 * max(1, len(stops)) + 24, 700))
    components.html(html_blob, height=est_h, scrolling=False)

def render_tours_tab():
    st.subheader("Tours")
    st.caption("Paste a ShowingTime tour link (Print page or short link) or upload the exported tour PDF.")

    # Controls similar to Run tab
    st.markdown('<div class="center-box">', unsafe_allow_html=True)
    url = st.text_input("Tour URL (ShowingTime)", value="", placeholder="https://scheduling.showingtime.com/.../Tour/Print/30235965")
    colA, colB = st.columns([1,1])
    with colA:
        debug = st.toggle("Debug", value=False, help="Show a text preview of the parsed content if needed")
    with colB:
        st.write("")  # spacer

    file = st.file_uploader("Or drop a Tour PDF", type=["pdf"], help="Export from ShowingTime → Tour Details PDF", label_visibility="visible")
    st.markdown('</div>', unsafe_allow_html=True)

    # Action
    run = st.button("🗺️ Parse tour", use_container_width=True)

    if not run:
        st.info("Paste a link or upload a PDF, then click **Parse tour**.")
        return

    stops: List[Dict[str, str]] = []

    # Prefer PDF if provided; else use URL
    raw_preview = ""
    if file is not None:
        data = file.getvalue()
        raw_preview = _pdf_to_text(data)
        stops = parse_tour_text(raw_preview or "")
    elif url.strip():
        html_src, status = _fetch_url(url.strip())
        raw_preview = _strip_html_to_text(html_src) if html_src else ""
        stops = parse_tour_text(raw_preview or "")

    if debug:
        with st.expander("Raw text preview (debug)"):
            st.code(raw_preview[:4000] if raw_preview else "(no preview)")

    _render_results(stops)
