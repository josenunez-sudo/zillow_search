# utils/showingtime.py
import re
from typing import List, Dict, Any, Tuple, Optional
import requests

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
}

def fetch_html(url: str) -> str:
    try:
        r = requests.get(url, headers=UA, timeout=15)
        if r.ok:
            return r.text
    except Exception:
        pass
    return ""

def parse_html_print(html: str) -> List[Dict[str, Any]]:
    """
    Parse ShowingTime 'Print' HTML (Tour) into stops: [{address, time_str}, ...]
    Tries to match common patterns visible in the printable tour page.
    """
    if not html:
        return []
    text = re.sub(r'\s+', ' ', html)
    # Very permissive approach to catch address/time blocks
    # Examples often contain "Stop" or "Showing" headings with times.
    # Capture "HH:MM AM/PM" then a line with address (street + city/state/zip).
    time_pat = r'(\d{1,2}:\d{2}\s?(?:AM|PM))'
    # Address heuristic: at least a number + street name + city + state
    addr_pat = r'(\d{1,5}\s+[A-Za-z0-9\.\- ]+?,\s*[A-Za-z\.\- ]+?,\s*[A-Z]{2}\s*\d{5}(?:-\d{4})?)'
    stops = []
    for m in re.finditer(time_pat + r'.{0,120}?' + addr_pat, text, flags=re.I):
        t = m.group(1).strip()
        a = m.group(2).strip()
        stops.append({"time_str": t, "address": a})
    # Fallback: just grab addresses if times fail
    if not stops:
        for m in re.finditer(addr_pat, text, flags=re.I):
            a = m.group(1).strip()
            stops.append({"time_str": "", "address": a})
    return stops

def parse_pdf_bytes(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    """
    Very lightweight PDF text scrape using PyPDF2 if available.
    """
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(io_bytes := (pdf_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return parse_text_block(text)
    except Exception:
        # If PyPDF2 missing/unreliable, just return empty; UI will show a hint.
        return []

def parse_text_block(text: str) -> List[Dict[str, Any]]:
    text = re.sub(r'\s+', ' ', text or '')
    time_pat = r'(\d{1,2}:\d{2}\s?(?:AM|PM))'
    addr_pat = r'(\d{1,5}\s+[A-Za-z0-9\.\- ]+?,\s*[A-Za-z\.\- ]+?,\s*[A-Z]{2}\s*\d{5}(?:-\d{4})?)'
    stops = []
    for m in re.finditer(time_pat + r'.{0,120}?' + addr_pat, text, flags=re.I):
        t = m.group(1).strip()
        a = m.group(2).strip()
        stops.append({"time_str": t, "address": a})
    if not stops:
        for m in re.finditer(addr_pat, text, flags=re.I):
            a = m.group(1).strip()
            stops.append({"time_str": "", "address": a})
    return stops
