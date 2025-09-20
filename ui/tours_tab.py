# --- BEGIN robust ShowingTime parser helpers ---

import re, io, requests
from html import unescape

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://scheduling.showingtime.com/",
}

def _fetch_showingtime_print_html(url: str, timeout: int = 15) -> str:
    """Fetch the ShowingTime 'Print' page with good headers and a fallback that
    drops the session segment (S(...)) if first fetch returns a session/login page.
    """
    def _looks_like_session_page(t: str) -> bool:
        t_low = t.lower()
        return ("sign in" in t_low or "session" in t_low) and ("showingtime" in t_low)

    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=timeout, allow_redirects=True)
        if r.ok and len(r.text) > 600 and not _looks_like_session_page(r.text):
            return r.text
    except Exception:
        pass

    # Fallback: drop the (S(...)) path segment if present
    url2 = re.sub(r"/\(S\([^)]+\)\)", "", url)
    if url2 != url:
        try:
            r2 = requests.get(url2, headers=UA_HEADERS, timeout=timeout, allow_redirects=True)
            if r2.ok and len(r2.text) > 600 and not _looks_like_session_page(r2.text):
                return r2.text
        except Exception:
            pass

    return ""


def _html_to_text(html: str) -> str:
    """Very light HTML → text converter; keeps line breaks where <br> / block tags appear."""
    if not html:
        return ""
    h = html

    # normalize block-level separators to newlines to preserve structure
    h = re.sub(r"(?i)</?(?:p|div|tr|li|h\d|section|article|br|hr)[^>]*>", "\n", h)
    # remove other tags
    h = re.sub(r"<[^>]+>", " ", h)
    # unescape entities and compress whitespace
    h = unescape(h)
    h = re.sub(r"[ \t]+\n", "\n", h)
    h = re.sub(r"\n{2,}", "\n", h)
    h = re.sub(r"[ \t]{2,}", " ", h)
    return h.strip()


def _extract_tour_date(text: str) -> str:
    """Try to find a tour date in the page text (various formats)."""
    # Common formats like: Monday, January 6, 2025 or 1/6/2025, or Jan 6, 2025
    pats = [
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}",
        r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
        r"[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}",
    ]
    for pat in pats:
        m = re.search(pat, text)
        if m:
            return m.group(0)
    return ""


def _looks_like_address(line: str) -> bool:
    """Heuristic: addresses often end with 'City, ST ZIP'."""
    return bool(re.search(r",\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?$", line.strip()))


def _normalize_time_line(line: str) -> str:
    """Standardize common time labels and return the value part."""
    # Examples we want to catch:
    #   "Time: 1:15 PM - 1:45 PM"
    #   "Appt Time: 2:00 PM"
    #   "Showing Window: 3:00 PM - 3:30 PM"
    #   "Start: 10:00 AM  End: 10:30 AM"
    l = line.strip()
    l = re.sub(r"(?i)\b(appointment|appt)\b", "Appt", l)
    # Extract times
    m = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM))(?:\s*[-–]\s*(\d{1,2}:\d{2}\s*(?:AM|PM)))?", l, re.I)
    if m:
        start = m.group(1)
        end = m.group(2) or ""
        return f"{start} - {end}" if end else start
    # Fallback: sometimes times appear without label, try just time tokens
    m2 = re.findall(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b", l, re.I)
    if m2:
        return " - ".join(m2) if len(m2) > 1 else m2[0]
    return ""


def parse_showingtime_print_html(html: str):
    """
    Convert the HTML print page to text, then parse tour stops robustly.
    Returns: dict(date: str, stops: List[{address, time}])
    """
    text = _html_to_text(html)
    if not text:
        return {"date": "", "stops": []}

    # Collapse multiple blank lines and split
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    date = _extract_tour_date("\n".join(lines))

    stops = []
    cur = {"address": "", "time": ""}

    # We'll open a new stop when we hit a line that looks like "Stop #", "Tour Stop", or an address line.
    for ln in lines:
        # new stop markers
        if re.search(r"(?i)\b(Tour\s*)?Stop\s*#?\s*\d+\b", ln):
            if cur.get("address") or cur.get("time"):
                stops.append(cur)
            cur = {"address": "", "time": ""}
            continue

        # time lines
        t = _normalize_time_line(ln)
        if t and not cur.get("time"):
            cur["time"] = t
            continue

        # explicit address labels
        if re.search(r"(?i)\b(Address|Property|Location)\s*:", ln):
            addr = re.sub(r"(?i)^(Address|Property|Location)\s*:\s*", "", ln).strip()
            if addr:
                cur["address"] = addr
            continue

        # heuristic address line (City, ST ZIP)
        if _looks_like_address(ln) and not cur.get("address"):
            cur["address"] = ln
            continue

    if cur.get("address") or cur.get("time"):
        stops.append(cur)

    # Clean empty/garbage
    stops = [s for s in stops if s.get("address") or s.get("time")]

    return {"date": date, "stops": stops}


# ------- PDF text extraction (pdfminer preferred, PyPDF2 fallback) ----------
try:
    from pdfminer.high_level import extract_text as _pdfminer_extract_text
except Exception:
    _pdfminer_extract_text = None

try:
    import PyPDF2
except Exception:
    PyPDF2 = None

def _extract_text_from_pdf_bytes(data: bytes) -> str:
    if _pdfminer_extract_text:
        try:
            return _pdfminer_extract_text(io.BytesIO(data)) or ""
        except Exception:
            pass
    if PyPDF2:
        try:
            reader = PyPDF2.PdfReader(io.BytesIO(data))
            pages = []
            for p in reader.pages:
                pages.append(p.extract_text() or "")
            return "\n".join(pages)
        except Exception:
            pass
    return ""


def parse_showingtime_pdf_bytes(data: bytes):
    """Parse the exported Tour PDF bytes."""
    txt = _extract_text_from_pdf_bytes(data)
    if not txt:
        return {"date": "", "stops": []}

    # Normalize
    txt = re.sub(r"[ \t]+\n", "\n", txt)
    txt = re.sub(r"\n{2,}", "\n", txt)
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]

    date = _extract_tour_date("\n".join(lines))
    stops, cur = [], {"address": "", "time": ""}

    for ln in lines:
        # Markers in PDFs vary; use same heuristics
        if re.search(r"(?i)\b(Tour\s*)?Stop\s*#?\s*\d+\b", ln):
            if cur.get("address") or cur.get("time"):
                stops.append(cur)
            cur = {"address": "", "time": ""}
            continue

        t = _normalize_time_line(ln)
        if t and not cur.get("time"):
            cur["time"] = t
            continue

        if re.search(r"(?i)\b(Address|Property|Location)\s*:", ln):
            addr = re.sub(r"(?i)^(Address|Property|Location)\s*:\s*", "", ln).strip()
            if addr:
                cur["address"] = addr
            continue

        if _looks_like_address(ln) and not cur.get("address"):
            cur["address"] = ln
            continue

    if cur.get("address") or cur.get("time"):
        stops.append(cur)

    stops = [s for s in stops if s.get("address") or s.get("time")]
    return {"date": date, "stops": stops}

# --- END robust ShowingTime parser helpers ---
