# services/tour_parsers.py
# Robust ShowingTime Tour extractors for PDF and "Print" HTML
# Works with ShowingTime - Tour Details.pdf and https://scheduling.showingtime.com/.../Tour/Print/<id>

from __future__ import annotations
import re
from typing import List, Dict, Tuple, Optional, Union

# --------- Small helpers ---------

_TIME_RE = re.compile(
    r'^\s*([0-9]{1,2}:[0-9]{2}\s*(?:AM|PM))\s*[-–]\s*([0-9]{1,2}:[0-9]{2}\s*(?:AM|PM))\s*$',
    re.I
)
_ADDR_RE = re.compile(
    r'^\s*(?P<addr>.+?,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?)\s*$'
)
_SEQ_RE = re.compile(r'^\s*(\d+)\s*$')

def _clean_lines(text: str) -> List[str]:
    lines = [ln.strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln]

def _parse_tour_lines(lines: List[str]) -> Tuple[Optional[str], Optional[str], List[Dict]]:
    """
    Very literal parser for ShowingTime 'Print' layout:
      <agent block...>
      Buyer's Tour - <weekday, Month DD, YYYY>
      <Buyer Name>
      ...
      1
      <address line with ", NC 275xx">
      9:00 AM - 9:30 AM
      #ID | status
      2
      <address>
      <time>
      ...
    """
    buyer = None
    tour_date = None
    stops: List[Dict] = []

    # Try to pick buyer name and tour date if present
    for i, ln in enumerate(lines[:40]):
        if "Buyer's Tour - " in ln:
            tour_date = ln.split("Buyer's Tour - ", 1)[-1].strip()
        # “Buyer’s name: Keelie Mason” OR next line after the big title has the name
        if "Buyer's name:" in ln:
            buyer = ln.split("Buyer's name:", 1)[-1].strip()

    i = 0
    n = len(lines)
    while i < n:
        # sequence line is just a number
        m_seq = _SEQ_RE.match(lines[i])
        if not m_seq:
            i += 1
            continue

        seq = int(m_seq.group(1))
        # Expect address on next non-empty line
        j = i + 1
        if j >= n:
            break
        addr_line = lines[j]
        m_addr = _ADDR_RE.match(addr_line)
        if not m_addr:
            # Not an address line => move on
            i += 1
            continue

        address = m_addr.group("addr").strip()

        # Expect time range on the following line
        k = j + 1
        if k < n:
            time_line = lines[k]
            m_time = _TIME_RE.match(time_line)
        else:
            m_time = None

        if not m_time:
            # If time not found, still record address (fallback)
            stops.append({
                "seq": seq,
                "address": address,
                "start_time": None,
                "end_time": None,
                "raw_time": None,
            })
            i = j + 1
            continue

        start_t, end_t = m_time.group(1), m_time.group(2)

        # Optional listing/status row just after time line; skip it if present
        # (We don’t need it to build stops.)
        i = k + 1  # move past time line

        stops.append({
            "seq": seq,
            "address": address,
            "start_time": start_t,
            "end_time": end_t,
            "raw_time": f"{start_t} - {end_t}",
        })

    # De-dup & order by seq
    seen = set()
    uniq = []
    for s in sorted(stops, key=lambda x: x["seq"]):
        key = (s["seq"], s["address"], s.get("raw_time"))
        if key not in seen:
            seen.add(key)
            uniq.append(s)

    return buyer, tour_date, uniq

def _extract_text_from_pdf(data: Union[bytes, str]) -> str:
    """
    data: bytes (uploaded file contents) OR filesystem path (str).
    Tries PyMuPDF (fitz) first, then PyPDF2.
    """
    # Try PyMuPDF
    try:
        import fitz  # PyMuPDF
        if isinstance(data, bytes):
            doc = fitz.open(stream=data, filetype="pdf")
        else:
            doc = fitz.open(data)
        txt = []
        for p in doc:
            txt.append(p.get_text("text"))
        return "".join(txt)
    except Exception:
        pass

    # Fallback: PyPDF2
    try:
        from PyPDF2 import PdfReader
        if isinstance(data, bytes):
            import io
            reader = PdfReader(io.BytesIO(data))
        else:
            reader = PdfReader(data)
        out = []
        for page in reader.pages:
            out.append(page.extract_text() or "")
        return "".join(out)
    except Exception as e:
        raise RuntimeError(f"Could not extract PDF text: {e}")

def parse_showingtime_pdf(pdf_bytes_or_path: Union[bytes, str]) -> Dict:
    """
    Returns: {"buyer": str|None, "date": str|None, "stops": [ {seq,address,start_time,end_time,raw_time}, ... ]}
    Raises if nothing is found to make debugging obvious.
    """
    text = _extract_text_from_pdf(pdf_bytes_or_path)
    lines = _clean_lines(text)
    buyer, date, stops = _parse_tour_lines(lines)
    if not stops:
        raise ValueError("No stops parsed from PDF text.")
    return {"buyer": buyer, "date": date, "stops": stops}

def parse_showingtime_print_html(src: str, *, is_url: bool = True, timeout: int = 12) -> Dict:
    """
    Accepts the 'Print' page URL (default) OR raw HTML (is_url=False).
    We strip tags to plain text and feed into the same line parser.
    """
    if is_url:
        import requests
        r = requests.get(src, timeout=timeout)
        r.raise_for_status()
        html = r.text
    else:
        html = src

    # Strip tags; simple & fast
    html = re.sub(r'(?is)<script.*?</script>', ' ', html)
    html = re.sub(r'(?is)<style.*?</style>', ' ', html)
    text = re.sub(r'(?s)<[^>]+>', ' ', html)
    text = re.sub(r'[ \t]+', ' ', text)
    # Put blocks back into lines so the parser can work
    text = re.sub(r'\s*\n\s*', '\n', text)

    lines = _clean_lines(text)
    buyer, date, stops = _parse_tour_lines(lines)
    if not stops:
        raise ValueError("No stops parsed from Print HTML.")
    return {"buyer": buyer, "date": date, "stops": stops}
